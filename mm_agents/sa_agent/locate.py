"""Deterministic screen *locate*: match a TRUSTED needle against the AT-SPI tree.

This is **not** an endorser and deliberately does not live in ``endorse.py``.
Nothing untrusted is cleared here, because nothing untrusted crosses: the string
being searched for is authored by a trusted source (the user's task instruction,
a chrome/OS-zone label, an already-cleared variable), and what flows forward is
that same string plus a *reference to a node*. The screen's own rendering of the
needle is never adopted.

Contrast with the extraction direction (``attest``, below), where the Q-VLM
authors the needle by reading untrusted content: there the host can only confirm
the model reported faithfully, which says nothing about whether the content is
honest. See the endorsement design.

The planner-facing wrappers are ``tools.locate_on_screen`` and
``tools.attest_screen_value``; ``endorsement/`` imports this module directly
so the benchmark and the agent can never test different code.

The contract is **uniqueness, not presence**. "Is the first row max0815?" answers
yes, deterministically and correctly, even when rows one and three both render
``max0815`` -- and acting on that yes is wrong. A boolean cannot express the
failure; ``UNIQUE | AMBIGUOUS | NOT_FOUND`` can, so ambiguity is a refusal and
never a tie-break.

Deterministic, no model, no network.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Optional, Sequence

from mm_agents.sa_agent.a11y import (
    A11yNode,
    ENTRY_TAGS,
    INTERACTIVE_TAGS,
    parse_a11y_nodes,
)

MIN_NEEDLE_LEN = 3

STOP_NEEDLES = frozenset({
    "ok", "yes", "no", "add", "new", "all", "the", "and", "for", "you",
    "user", "name", "search", "submit", "cancel", "close", "open", "back",
    "next", "menu", "file", "edit", "view", "help", "home", "settings",
})

_BOUND = r"[\w._\-]"

_CONFUSABLE_FOLD = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "ѕ": "s", "і": "i", "ј": "j",
    "һ": "h", "ԛ": "q", "ԁ": "d",
    "ο": "o", "α": "a", "β": "b", "ε": "e", "ι": "i",
    "κ": "k", "ν": "v", "ρ": "p", "τ": "t", "χ": "x",
    "ı": "i", "ɡ": "g",
    "0": "o", "1": "l", "5": "s", "8": "b",
}


class LocateStatus(str, Enum):
    UNIQUE = "UNIQUE"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    NEEDLE_REJECTED = "NEEDLE_REJECTED"


@dataclass(frozen=True)
class LocateResult:
    status: LocateStatus
    needle: str
    normalized: str
    node: Optional[A11yNode]
    matches: tuple[A11yNode, ...]
    match_kind: str
    confusables: tuple[A11yNode, ...]
    reason: str

    @property
    def ok(self) -> bool:
        return self.status is LocateStatus.UNIQUE

    @property
    def center(self) -> Optional[tuple[int, int]]:
        n = self.node
        return (n.x + n.w // 2, n.y + n.h // 2) if n is not None else None

    @property
    def bbox(self) -> Optional[tuple[int, int, int, int]]:
        n = self.node
        return (n.x, n.y, n.w, n.h) if n is not None else None

    def to_json(self) -> dict[str, Any]:
        def _n(n: A11yNode) -> dict[str, Any]:
            return {"tag": n.tag, "label": n.label, "bbox": [n.x, n.y, n.w, n.h]}
        return {
            "status": self.status.value,
            "needle": self.needle,
            "normalized": self.normalized,
            "match_kind": self.match_kind,
            "node": _n(self.node) if self.node else None,
            "matches": [_n(n) for n in self.matches],
            "confusables": [_n(n) for n in self.confusables],
            "reason": self.reason,
        }



def normalize(s: str, *, case_sensitive: bool = False) -> str:
    """NFKC, strip Unicode control/format characters, collapse whitespace, fold case.

    Format characters (zero-width joiners, bidi overrides) are removed rather
    than preserved: they are invisible, so leaving them in would let two labels
    that render identically compare unequal -- which defeats the ambiguity check
    that carries this module's security load.
    """
    s = unicodedata.normalize("NFKC", s or "")
    s = "".join(ch for ch in s if ch.isspace() or not unicodedata.category(ch).startswith("C"))
    s = " ".join(s.split())
    return s if case_sensitive else s.casefold()


def skeleton(s: str) -> str:
    """Confusable skeleton, for REPORTING near neighbours only -- never matching."""
    return "".join(_CONFUSABLE_FOLD.get(ch, ch) for ch in normalize(s))



def _in_region(n: A11yNode, region: Sequence[int]) -> bool:
    rx, ry, rw, rh = (int(v) for v in region)
    cx, cy = n.x + n.w // 2, n.y + n.h // 2
    return rx <= cx < rx + rw and ry <= cy < ry + rh


def _box_contains(outer: A11yNode, inner: A11yNode, tol: int = 2) -> bool:
    return (outer.x - tol <= inner.x
            and outer.y - tol <= inner.y
            and outer.x + outer.w + tol >= inner.x + inner.w
            and outer.y + outer.h + tol >= inner.y + inner.h)


def _dedupe_nested(matches: Sequence[A11yNode]) -> list[A11yNode]:
    """Collapse a matched node and its matched ancestors into one hit.

    An AT-SPI row commonly carries its name on both the ``list-item`` and a
    ``label`` child; counting those as two matches would make every honest
    lookup AMBIGUOUS. Keep the smallest (most specific) box of each nest.
    """
    kept: list[A11yNode] = []
    for n in sorted(matches, key=lambda m: (m.area, m.y, m.x)):
        if any(_box_contains(n, k) or _box_contains(k, n) for k in kept):
            continue
        kept.append(n)
    return kept



def locate(
    needle: str,
    tree: str | Iterable[A11yNode] | None,
    *,
    region: Optional[Sequence[int]] = None,
    case_sensitive: bool = False,
    interactive_only: bool = False,
    exclude_tags: frozenset[str] = ENTRY_TAGS,
    min_needle_len: int = MIN_NEEDLE_LEN,
    allow_token_match: bool = True,
    search_text: bool = False,
) -> LocateResult:
    """Locate *needle* -- authored by a TRUSTED source -- in the accessibility tree.

    ``exclude_tags`` defaults to ``ENTRY_TAGS`` because the agent's own typed
    text otherwise collides with the completion it is trying to select: having
    typed "max0815" into the search box, the box itself matches. That readback
    is self-authored, not a candidate for selection. Pass ``exclude_tags=frozenset()``
    for tasks that genuinely need to read back what was typed (plan §8.11).

    Exact whole-label matches win outright; whole-token matches inside a longer
    label are the fallback, so a row rendering "Max Mustermann max0815" is found
    without "termcolor" ever matching "termcolour".
    """
    nodes: list[A11yNode]
    if tree is None:
        nodes = []
    elif isinstance(tree, str):
        nodes = parse_a11y_nodes(tree)
    else:
        nodes = list(tree)

    n_needle = normalize(needle, case_sensitive=case_sensitive)
    if not n_needle:
        return LocateResult(LocateStatus.NEEDLE_REJECTED, needle, n_needle, None, (), "", (),
                            "needle is empty after normalization")
    if len(n_needle) < min_needle_len:
        return LocateResult(LocateStatus.NEEDLE_REJECTED, needle, n_needle, None, (), "", (),
                            f"needle shorter than {min_needle_len} characters")
    if n_needle in STOP_NEEDLES:
        return LocateResult(LocateStatus.NEEDLE_REJECTED, needle, n_needle, None, (), "", (),
                            "needle is a generic UI word; locating it means nothing")

    pool = [n for n in nodes if n.label]
    if exclude_tags:
        pool = [n for n in pool if n.tag not in exclude_tags]
    if interactive_only:
        pool = [n for n in pool if n.tag in INTERACTIVE_TAGS]
    if region is not None:
        try:
            pool = [n for n in pool if _in_region(n, region)]
        except (TypeError, ValueError):
            return LocateResult(LocateStatus.NEEDLE_REJECTED, needle, n_needle, None, (), "", (),
                                "region is not a well-formed (x, y, w, h) box")

    def _searchable(n) -> str:
        txt = getattr(n, "text", "")
        if search_text and txt and txt != n.label:
            return normalize(f"{n.label} {txt}", case_sensitive=case_sensitive)
        return normalize(n.label, case_sensitive=case_sensitive)

    labels = {id(n): _searchable(n) for n in pool}

    exact = [n for n in pool if labels[id(n)] == n_needle]
    kind = "exact"
    hits = exact
    if not hits and allow_token_match:
        pat = re.compile(rf"(?<!{_BOUND}){re.escape(n_needle)}(?!{_BOUND})")
        hits = [n for n in pool if pat.search(labels[id(n)])]
        kind = "token"

    hits = _dedupe_nested(hits)

    skel = skeleton(needle)
    confusables = tuple(_dedupe_nested([
        n for n in pool
        if n not in hits and skeleton(n.label) == skel and labels[id(n)] != n_needle
    ]))

    if not hits:
        return LocateResult(LocateStatus.NOT_FOUND, needle, n_needle, None, (), "", confusables,
                            "no element on screen renders this value")
    if len(hits) > 1:
        return LocateResult(
            LocateStatus.AMBIGUOUS, needle, n_needle, None, tuple(hits), kind, confusables,
            f"{len(hits)} distinct elements render this value; refusing rather than guessing")
    return LocateResult(LocateStatus.UNIQUE, needle, n_needle, hits[0], tuple(hits), kind,
                        confusables, "")


def locate_in_obs(needle: str, obs_raw: Optional[dict], **kwargs: Any) -> LocateResult:
    """``locate`` against ``obs_raw['accessibility_tree']``."""
    tree = obs_raw.get("accessibility_tree") if isinstance(obs_raw, dict) else None
    return locate(needle, tree, **kwargs)



MAX_CLAIM_LEN = 120


@dataclass(frozen=True)
class AttestResult:
    attested: bool
    claim: str
    normalized: str
    nodes: tuple[A11yNode, ...]
    match_kind: str
    bbox: Optional[tuple[int, int, int, int]]
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "attested": self.attested,
            "claim": self.claim,
            "match_kind": self.match_kind,
            "n_nodes": len(self.nodes),
            "bbox": list(self.bbox) if self.bbox else None,
            "reason": self.reason,
        }


def attest(
    claim: str,
    tree: str | Iterable[A11yNode] | None,
    *,
    case_sensitive: bool = False,
    exclude_tags: frozenset[str] = frozenset(),
    max_claim_len: int = MAX_CLAIM_LEN,
) -> AttestResult:
    """Confirm that a Q-VLM-authored *claim* is literally rendered on screen.

    This is the extraction direction, and its guarantee is far weaker than
    ``locate``'s: the model chose the string, and the attacker wrote what is on
    the screen, so a successful attestation says only that **the extractor
    reported faithfully** -- not that the content is honest. It catches a
    hallucinating or compromised Q-VLM. It confers **no integrity level**.

    It confers no level and it must not be used to *narrow* one either. ``bbox``
    is the union of the nodes that render the claim, and the obvious next step --
    relabel the value with ``region_metadata(bbox)`` instead of
    ``extraction_metadata``'s whole-screen join -- is unsound: the model's string
    is a string an attacker can choose by injection, so they would also be
    choosing which node's label the value inherits. Narrowing needs a *trusted*
    principal to have chosen **where to look** before anything was read, which
    is what ``query_masked_region``'s planner-named region already is (and it
    already labels by region). See the endorsement design.

    Unlike ``locate``, multiplicity is not a refusal -- two copies of a value the
    model correctly read are still a correct read. And unlike ``locate``, the
    entry field is *not* excluded by default: readback of the agent's own typing
    is exactly the kind of claim worth attesting.
    """
    nodes: list[A11yNode]
    if tree is None:
        nodes = []
    elif isinstance(tree, str):
        nodes = parse_a11y_nodes(tree)
    else:
        nodes = list(tree)

    n_claim = normalize(claim, case_sensitive=case_sensitive)
    if not n_claim:
        return AttestResult(False, claim, n_claim, (), "", None,
                            "claim is empty after normalization")
    if len(n_claim) > max_claim_len:
        return AttestResult(False, claim, n_claim, (), "", None,
                            f"claim is longer than {max_claim_len} characters: prose, not a "
                            "value read off the screen -- not attestable against the tree")

    pool = [n for n in nodes if n.label]
    if exclude_tags:
        pool = [n for n in pool if n.tag not in exclude_tags]
    labels = {id(n): normalize(n.label, case_sensitive=case_sensitive) for n in pool}

    hits = [n for n in pool if labels[id(n)] == n_claim]
    kind = "exact"
    if not hits:
        pat = re.compile(rf"(?<!{_BOUND}){re.escape(n_claim)}(?!{_BOUND})")
        hits = [n for n in pool if pat.search(labels[id(n)])]
        kind = "token"

    if not hits:
        return AttestResult(False, claim, n_claim, (), "", None,
                            "no element on screen renders this value; the extractor did not "
                            "read it off this screen")

    x0 = min(n.x for n in hits)
    y0 = min(n.y for n in hits)
    x1 = max(n.x + n.w for n in hits)
    y1 = max(n.y + n.h for n in hits)
    return AttestResult(True, claim, n_claim, tuple(hits), kind, (x0, y0, x1 - x0, y1 - y0), "")


def attest_in_obs(claim: str, obs_raw: Optional[dict], **kwargs: Any) -> AttestResult:
    """``attest`` against ``obs_raw['accessibility_tree']``."""
    tree = obs_raw.get("accessibility_tree") if isinstance(obs_raw, dict) else None
    return attest(claim, tree, **kwargs)
