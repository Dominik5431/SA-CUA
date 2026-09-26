"""
policies.py — deterministic pre-execution gating of consequential tool calls
=============================================================================

The IFC enforcement point of sa_agent. ``SAPolicy.check_tool_call`` runs
BEFORE every P-LLM tool call executes and raises ``PolicyViolation`` to block
it (the loop reports the reason back to the planner; nothing runs in the VM).

What counts as "consequential"
------------------------------
Every tool is assigned a side-effect ``Tier``:

* READ              — pure inspection (screen queries, scrolling, reporting…);
                      never gated.
* REVERSIBLE_WRITE  — state changes that are cheap to undo (typing, key
                      presses, in-page navigation, host-side web fetch).
* DESTRUCTIVE       — sends/submits/executes (terminal commands, clicks on
                      SEND/EXECUTE/UNKNOWN-capability targets).

Screen actions (click & friends) have no static tier: their effect is whatever
the clicked element does, so their tier comes from the target affordance's
``Capability`` (from accessibility_tree_wrap) via ``CAP_TIER`` — a click on a
DOCUMENT "submit" button is DESTRUCTIVE, a click on a link is
REVERSIBLE_WRITE (NAVIGATE).

Blocking rules (evaluated on ``effective = args_label ⊔ target_label``):

* integrity, DESTRUCTIVE tier      — blocked if *effective* doesn't reach
  ``tier_min_level[DESTRUCTIVE]`` (either the arguments derive from
  under-trusted screen/web/terminal data, or the target element itself lives
  in attacker-controlled DOCUMENT content).
* integrity, REVERSIBLE_WRITE tier — blocked only if the *arguments* don't
  reach ``tier_min_level[REVERSIBLE_WRITE]``. Deliberate relaxation: an
  under-trusted *target* alone does not block, otherwise clicking any web
  link would be impossible.
* confidentiality (both tiers)     — blocked if the payload's reader set does
  not cover the sink's audience (exfiltration: {"user"}-confidential data into
  a public page / off-allowlist web request / terminal).

See ``lattice.IntegrityLabel.Level`` for the full integrity ladder (TRUSTED <
SELF_AUTHORED < ENDORSED < COMPUTED_PLATFORM_VALUE < FREE_TEXT_THIRD_PARTY
< UNTRUSTED) and ``screen_labels.is_untrusted()`` / ``meets_level()`` for how
the module-wide ``_MIN_TRUSTED_LEVEL`` cutoff (default COMPUTED_PLATFORM_VALUE,
set via ``--sa_min_trusted_level``) governs what the planner gets to *see*
in the clear (redaction, action-list masking, ...).

``tier_min_level`` (below) is **derived from** ``_MIN_TRUSTED_LEVEL`` via
``derive_tier_min_level()``, not set independently: a tier can never require
*more* trust than the display cutoff already allows through as a bare
literal, because gating a specific tool-call argument against a value the
planner can already read in its own context is unenforceable -- the planner
can always re-derive/retype/paraphrase it into a fresh literal argument with
no ``var_ref`` to gate on at all (see sa-agent's
``sa-agent-trust-gate-literal-laundering`` note: this is exactly how a
a URL endorsed only to the ENDORSED level got hand-typed straight past a
DESTRUCTIVE-tier ``run_terminal_command`` block in practice, not just in
theory). Letting the two drift independently — as an earlier version of this
module did via a separate ``--sa_allow_corroborated_destructive`` flag —
produces exactly that false sense of security: one gate visibly permits what
the other still (unenforceably) claims to block.

A block message always ends with the endorsement hint: taint is cleared
only by ``endorse_by_crosscheck`` / ``endorse_by_checksum`` (see
endorse.py) after independent verification — never on trust alone.

``mode="audit"`` logs would-block decisions to ``trace_path`` instead of
raising (for measuring over-blocking on benign tasks).
"""

from __future__ import annotations

import datetime
import json
import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, Optional, Sequence
from urllib.parse import urlparse

from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

from mm_agents.accessibility_tree_wrap.coord_to_semantic import ActionType, Capability, Zone
from mm_agents.sa_agent.lattice import FidesLattice, IntegrityLabel, Lattice, ProductLabel
from mm_agents.sa_agent.screen_labels import (
    PRINCIPALS,
    SAScreenView,
    conf_permits,
    is_untrusted,
    meets_level,
    trusted_public,
    untrusted_public,
)


@dataclass
class Action:
    pass

@dataclass
class Query(Action):
    messages : list[ChatCompletionMessageParam]
    tools: list[ChatCompletionToolParam]

@dataclass
class ToolCall(Action):
    id: str
    name: str
    arguments: str
    var_refs: list[str] = field(default_factory=list)

@dataclass
class Response(Action):
    response: str

class PolicyViolation(Exception):
    def __init__(self, reason: str):
        super().__init__(f"Policy violation: {reason}")
        self.reason = reason

Policy = Callable[[list[tuple[Action, Lattice]]], None]



class Tier(Enum):
    READ = "READ"
    REVERSIBLE_WRITE = "REVERSIBLE_WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"


CAP_TIER: dict[Capability, Tier] = {
    Capability.READ:     Tier.READ,
    Capability.NAVIGATE: Tier.REVERSIBLE_WRITE,
    Capability.WRITE:    Tier.REVERSIBLE_WRITE,
    Capability.SEND:     Tier.DESTRUCTIVE,
    Capability.EXECUTE:  Tier.DESTRUCTIVE,
    Capability.UNKNOWN:  Tier.DESTRUCTIVE,
}

DEFAULT_TIER_MIN_LEVEL: dict[Tier, IntegrityLabel.Level] = {
    Tier.READ:              IntegrityLabel.Level.UNTRUSTED,
    Tier.REVERSIBLE_WRITE:  IntegrityLabel.Level.ENDORSED,
    Tier.DESTRUCTIVE:       IntegrityLabel.Level.TRUSTED,
}


def derive_tier_min_level(
    display_cutoff: IntegrityLabel.Level,
    base: Optional[dict[Tier, IntegrityLabel.Level]] = None,
) -> dict[Tier, IntegrityLabel.Level]:
    """Reconcile each tier's floor (*base*, default ``DEFAULT_TIER_MIN_LEVEL``) with
    *display_cutoff* (``screen_labels._MIN_TRUSTED_LEVEL``, i.e. ``--sa_min_trusted_level``):
    a tier's effective floor is never allowed to demand *more* trust than the display cutoff
    already lets through as a bare literal in the planner's own context.

    ``IntegrityLabel.Level`` values increase as trust decreases (TRUSTED=0 ... UNTRUSTED=5), so
    "no more demanding than the display cutoff" means "value no lower than the cutoff's" --
    ``max(floor.value, display_cutoff.value)`` picks whichever of the two requires *less* trust.
    A tier's own base floor still wins whenever it's already looser than the cutoff (e.g. READ's
    UNTRUSTED floor, i.e. "never gated", is untouched by any cutoff choice).

    This is the one and only place tier floors are computed from here on -- see the module
    docstring for why letting ``tier_min_level`` and the display cutoff drift independently is
    unsound, not just inconsistent."""
    base = base if base is not None else DEFAULT_TIER_MIN_LEVEL
    return {
        tier: IntegrityLabel.Level(max(floor.value, display_cutoff.value))
        for tier, floor in base.items()
    }


TARGET = "TARGET"
ENDORSE = "ENDORSE"
COMMAND = "COMMAND"

TOOL_TIER: dict[str, Tier | str] = {
    "query_screen": Tier.READ,
    "report": Tier.READ,
    "locate_on_screen": Tier.READ,
    "find_by_text": Tier.READ,
    "attest_screen_value": Tier.READ,
    "endorse_element": Tier.READ,
    "wait": Tier.READ,
    "done": Tier.READ,
    "fail": Tier.READ,
    "move_to": Tier.READ,
    "hscroll": Tier.READ,
    "vscroll": Tier.READ,
    "click": TARGET,
    "right_click": TARGET,
    "mouse_down": TARGET,
    "mouse_up": TARGET,
    "drag_to": TARGET,
    "type_text": Tier.REVERSIBLE_WRITE,
    "press": Tier.REVERSIBLE_WRITE,
    "key_down": Tier.REVERSIBLE_WRITE,
    "key_up": Tier.REVERSIBLE_WRITE,
    "hotkey": Tier.REVERSIBLE_WRITE,
    "fetch_web_content": Tier.REVERSIBLE_WRITE,
    "run_terminal_command": COMMAND,
    "endorse": ENDORSE,
    "get_contacts": Tier.READ,
    "endorse_by_registry": ENDORSE,
    "endorse_by_listing": ENDORSE,
    "endorse_by_procedure": ENDORSE,
    "endorse_by_crosscheck": ENDORSE,
    "endorse_by_task_match": ENDORSE,
    "endorse_by_directory": ENDORSE,
    "endorse_by_domain_pin": ENDORSE,
    "endorse_by_candidate_set": ENDORSE,
    "endorse_by_checksum": ENDORSE,
    "endorse_by_recall": ENDORSE,
}

TARGET_TIER_TOOLS = frozenset(n for n, t in TOOL_TIER.items() if t == TARGET)

_STRUCTURAL_COMMAND_VERBS = {
    ("git", "clone"),
    ("git", "status"),
}
_STRUCTURAL_COMMAND_SINGLE_VERBS = {"mkdir"}

_SHELL_METACHAR_RE = re.compile(r"[;&|`$<>\n]")


_SEND_VERBS = frozenset({
    "curl", "wget", "wget2", "xh", "http", "https", "aria2c", "lwp-request",
    "nc", "ncat", "netcat", "socat",
    "ssh", "scp", "sftp", "rsync", "ftp", "lftp", "telnet", "tftp",
    "mail", "mailx", "sendmail", "mutt", "ssmtp", "msmtp",
})
_UNBOUNDED_SHELL_RE = re.compile(r"\$\(|`|\$\{|<\(|>\(|\||;|&&|\|\||&")


def _command_can_send(command: str) -> bool:
    """True if *command* could transmit data off this machine, so its confidentiality audience is
    the public set (see _sink_audience). Conservative: an unparseable command, or one using a
    substitution/pipe/chain we cannot bound, counts as capable of sending. Only a command that
    parses cleanly and whose every verb is demonstrably local returns False."""
    if not command or not command.strip():
        return False
    if _UNBOUNDED_SHELL_RE.search(command):
        return True
    try:
        toks = shlex.split(command)
    except ValueError:
        return True
    return any(t.rsplit("/", 1)[-1] in _SEND_VERBS for t in toks)


def _is_structural_command(command: str) -> bool:
    """True iff *command* is a single, unchained invocation of a small allowlisted verb known to
    write/fetch/print only bounded, tool-generated status/progress text -- never execute anything
    from an untrusted source, and never surface arbitrary file or network content. Two uses:
    (1) hybrid_tools.py grades such a result COMPUTED_PLATFORM_VALUE instead of unconditionally
    UNTRUSTED; (2) this module's COMMAND tier resolves such a call to REVERSIBLE_WRITE instead of
    the DESTRUCTIVE default, since "clone/fetch a file" and "execute a file" are different risk
    classes and only the latter needs DESTRUCTIVE's stricter floor. Default-deny: any parse
    failure, any shell metacharacter, or any verb not on the allowlist means False (DESTRUCTIVE
    tier / unconditionally UNTRUSTED output, today's blanket behavior)."""
    if _SHELL_METACHAR_RE.search(command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    if tuple(tokens[:2]) in _STRUCTURAL_COMMAND_VERBS:
        return True
    return tokens[0] in _STRUCTURAL_COMMAND_SINGLE_VERBS

_TOOL_ACTION_TYPE: dict[str, ActionType] = {
    "click": ActionType.CLICK,
    "right_click": ActionType.RIGHT_CLICK,
    "drag_to": ActionType.DRAG,
    "mouse_down": ActionType.CLICK,
    "mouse_up": ActionType.CLICK,
}

_POINTER_ACTION_TYPES = frozenset({ActionType.CLICK, ActionType.RIGHT_CLICK, ActionType.DRAG})

_KEYBOARD_TOOLS = frozenset({"type_text", "press", "hotkey", "key_down", "key_up"})

_COMPOSITOR_HOTKEYS: frozenset = frozenset({
    ("ctrl", "alt", "t"),
    ("alt", "tab"), ("alt", "shift", "tab"), ("super",), ("super", "d"),
    ("ctrl", "alt", "left"), ("ctrl", "alt", "right"),
    ("ctrl", "alt", "up"), ("ctrl", "alt", "down"),
})


def _is_compositor_hotkey(name: str, args: dict) -> bool:
    """True iff this call is one of the window-manager combos the focused app never receives."""
    if name != "hotkey":
        return False
    keys = args.get("keys")
    if not isinstance(keys, (list, tuple)):
        return False
    return tuple(str(k).strip().lower() for k in keys) in _COMPOSITOR_HOTKEYS

_POINTER_MOVES = frozenset({"move_to", "click", "right_click", "drag_to"})

_CURSOR_TOOLS = frozenset({"mouse_down", "mouse_up"})


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _split_source_rule(rule: str) -> tuple[str, str]:
    """Split an allowlist entry into (host suffix, path prefix).

    An entry is ``host`` or ``host/path/prefix`` -- e.g. ``ubuntu.com`` (any path on that host) or
    ``docs.python.org/3`` (only under /3). Scheme and a trailing slash are tolerated so an operator
    can paste a URL straight out of a browser.
    """
    rule = (rule or "").strip().lower()
    rule = re.sub(r"^[a-z][a-z0-9+.-]*://", "", rule)
    host, _, path = rule.partition("/")
    host = host.lstrip(".")
    path = path.rstrip("/")
    return host, (f"/{path}" if path else "")


def _path_allows(url_path: str, prefix: str) -> bool:
    """Does *url_path* sit at or under *prefix*?

    Compared on a segment boundary, so a ``/3`` prefix admits ``/3`` and ``/3/library/token.html``
    but NOT ``/30`` -- a plain ``startswith`` would let ``docs.python.org/3`` vouch for
    ``docs.python.org/30-reasons-attacker-controlled``.
    """
    if not prefix:
        return True
    p = (url_path or "/").lower().rstrip("/") or "/"
    return p == prefix or p.startswith(prefix + "/")


def source_allowed(url: str, rules: Sequence[str]) -> bool:
    """Is *url* covered by one of the allowlist *rules* (host suffix, and path prefix if pinned)?

    Host matching is unchanged: exact, or a dot-boundary suffix, so ``evilubuntu.com`` and
    ``ubuntu.com.attacker.example`` both correctly fail. The path half is the addition -- an
    allowlist of bare hosts vouches for *every* path on them, which is how a fully-allowlisted
    ``api.github.com/search/repositories?q=repo:attacker/X`` could stand in as verification
    evidence (the benchmark's `query_laundering` case). Pinning ``github.com/repos`` refuses that
    without refusing the API -- though the shipped default no longer pins github (it is host-level,
    so ``/search`` is admitted); ``--sa_task_derived_evidence``, not the path pin, is what now
    keeps the querier from choosing the answer. Path pinning remains available for hosts where only
    one path is an oracle.

    A rule with no path keeps exactly the old behaviour, so an operator who pins nothing gets the
    old semantics rather than a silent tightening.
    """
    host = _hostname(url)
    if not host:
        return False
    try:
        url_path = urlparse(url).path or "/"
    except ValueError:
        return False
    for rule in rules:
        rule_host, rule_path = _split_source_rule(rule)
        if not rule_host:
            continue
        if host != rule_host and not host.endswith("." + rule_host):
            continue
        if _path_allows(url_path, rule_path):
            return True
    return False


def source_pinned_by_path(url: str, rules: Sequence[str]) -> bool:
    """Is *url* admitted by a rule that PINS A PATH, rather than by a bare-host rule?

    The distinction matters wherever a caller needs to know whether the OPERATOR already named the
    resource. ``nodejs.org/dist`` admits exactly the vendor's release index and nothing else on the
    host, so a URL under it is one a trusted principal chose; ``sqlite.org`` admits the whole site,
    so a URL under it is one the planner chose. ``endorse.listing`` uses this to decide whether
    it still has to require a task term in the path (see the guard's own comment).
    """
    host = _hostname(url)
    if not host:
        return False
    try:
        url_path = urlparse(url).path or "/"
    except ValueError:
        return False
    for rule in rules:
        rule_host, rule_path = _split_source_rule(rule)
        if not rule_host or not rule_path or rule_path == "/":
            continue
        if host != rule_host and not host.endswith("." + rule_host):
            continue
        if _path_allows(url_path, rule_path):
            return True
    return False


host_in_domains = source_allowed


def host_served_by_vm(url: str, hosts: Sequence[str]) -> bool:
    """Is *url*'s host EXACTLY one of the names the guest serves itself (a task's
    ``vm_hosted_domains``)?

    Deliberately not :func:`source_allowed`. That is a trust allowlist, where a dot-boundary
    suffix is the right reading (``docs.python.org`` vouches for its subpaths and a rule for
    ``ubuntu.com`` covers ``help.ubuntu.com``). Routing is a different question -- WHERE the page
    comes from -- and the guest serves only the names its /etc/hosts maps to 127.0.0.1. With a
    suffix match ``www.uber.com`` counted as VM-hosted because ``uber.com`` is, was curled inside
    the guest, resolved there to the REAL internet (no hosts entry), and came back as the real
    site's page under the label of the mock's. Exact, case-insensitive, trailing dot ignored.
    """
    host = _hostname(url)
    if not host:
        return False
    host = host.rstrip(".").lower()
    return any(host == str(h).strip().rstrip(".").lower() for h in hosts if str(h).strip())


class SAPolicy:
    """Deterministic pre-execution policy. See module docstring for the rules."""

    def __init__(
        self,
        mode: Literal["enforce", "audit"] = "enforce",
        strict_targets: bool = False,
        trusted_domains: Sequence[str] = (),
        trace_path: Optional[str] = None,
        tier_min_level: Optional[dict[Tier, IntegrityLabel.Level]] = None,
        trusted_domains_corroborating: Sequence[str] = (),
        mask_click_policy: Literal["block", "off"] = "block",
    ):
        self.mode = mode
        self.strict_targets = strict_targets
        self.mask_click_policy = mask_click_policy
        self._mask = None
        self._pointer: Optional[tuple[float, float]] = None
        self.trusted_domains = list(trusted_domains)
        self.trusted_domains_corroborating = list(trusted_domains_corroborating)
        self.trace_path = trace_path
        self.tier_min_level = dict(DEFAULT_TIER_MIN_LEVEL) if tier_min_level is None else dict(tier_min_level)


    def set_mask(self, mask) -> None:
        """Install the mask for the current turn (None disables the location rule for this turn).

        Called once per observation refresh by the loop, from the SAME Mask object whose pixels
        were sent to the planner. That identity is the whole point: what is refused and what is
        invisible are then the same bitmap rather than two computations that happen to agree.
        """
        self._mask = mask

    def _focused_element_masked(self, screen_view) -> Optional[str]:
        """Description of the focused element if it sits under a redaction, else None.

        Uses the element's CENTRE rather than any overlap test: a large focused container whose
        edge clips a redaction is not "a redacted input", and blocking on overlap would refuse
        typing into the address bar the moment a masked page rendered anywhere beneath it.
        """
        if self._mask is None or self.mask_click_policy == "off" or screen_view is None:
            return None
        for action, _label in getattr(screen_view, "affordances", ()) or ():
            if not getattr(action, "focused", False):
                continue
            if self._mask.hit(action.x_center, action.y_center):
                if (getattr(action, "role", "") or "").lower() == "terminal":
                    return None
                region = self._mask.region_at(action.x_center, action.y_center)
                where = f" in region {region.get('id')}" if region else ""
                return f"the focused {action.role or 'element'}{where}"
            return None
        return None

    @staticmethod
    def _point_of(args: dict[str, Any]) -> Optional[tuple[float, float]]:
        """The (x, y) a call names in its own arguments, or None if it names none."""
        x, y = args.get("x"), args.get("y")
        if x is None or y is None:
            return None
        try:
            return float(x), float(y)
        except (TypeError, ValueError):
            return None

    def _record_pointer(self, name: str, args: dict[str, Any]) -> None:
        """Remember where a pointer tool leaves the cursor.

        Called ONLY on the paths where the call is about to execute. A blocked call never moved the
        cursor, and recording its coordinates anyway would leave the policy testing a point the
        pointer is not at -- which reopens this very hole from the other side: aim at a redacted
        pixel, get a click on a visible one refused for some unrelated trust reason, and the
        remembered point now says "visible" while the cursor still sits on the redaction.
        """
        if name not in _POINTER_MOVES:
            return
        point = self._point_of(args)
        if point is not None:
            self._pointer = point

    def _masked_point(self, point: Optional[tuple[float, float]]) -> Optional[str]:
        """A short description of the redacted region *point* falls in, or None.

        Location, not provenance. This is what makes the rule immune to literal laundering: a
        planner that reads a coordinate off the screen and retypes it as a bare number with no
        var_ref still lands on the same pixel, and the same bitmap still refuses it.
        """
        if self._mask is None or self.mask_click_policy == "off" or point is None:
            return None
        fx, fy = point
        blocked = (self._mask.click_blocked(fx, fy) if hasattr(self._mask, "click_blocked")
                   else self._mask.hit(fx, fy))
        if not blocked:
            return None
        region = self._mask.region_at(fx, fy)
        if region is None:
            return f"({int(fx)},{int(fy)}) is inside a redacted area"
        return (f"({int(fx)},{int(fy)}) is inside redacted region {region.get('id')} "
                f"({region.get('label')})")

    def _masked_hit(self, args: dict[str, Any]) -> Optional[str]:
        """The redacted region the point named in *args* lands in, or None. Retained as the entry
        point for calls that carry their own coordinates; the coordinate-less pointer tools go
        through _masked_point with the remembered cursor instead."""
        return self._masked_point(self._point_of(args))


    def _resolve_target(
        self,
        name: str,
        point: Optional[tuple[float, float]],
        screen_view: Optional[SAScreenView],
    ) -> tuple[Tier, FidesLattice, str]:
        """Tier + label + description of the screen element a pointer action hits.

        Takes the already-resolved *point* rather than the raw arguments, because for
        mouse_down/mouse_up that point is the remembered cursor and is not in the arguments at all.
        """
        hit = None
        if screen_view is not None and point is not None:
            try:
                hit = screen_view.affordance_at(point[0], point[1], action_types=_POINTER_ACTION_TYPES)
            except (TypeError, ValueError):
                hit = None
        if hit is None:
            if self.strict_targets:
                return Tier.DESTRUCTIVE, untrusted_public(), "unresolved target (strict)"
            if self._masked_point(point) is not None:
                return Tier.DESTRUCTIVE, untrusted_public(), "unresolved target inside a redacted region"
            return Tier.REVERSIBLE_WRITE, trusted_public(), "unresolved target (lenient)"
        action, label = hit
        wanted = _TOOL_ACTION_TYPE.get(name)
        capability = action.capability
        if wanted is not None and action.action_capabilities:
            capability = action.action_capabilities.get(wanted, action.capability)
        tier = CAP_TIER.get(capability, Tier.DESTRUCTIVE)
        if self._mask is not None and point is not None:
            visible = not self._mask.hit(point[0], point[1])
            integ = IntegrityLabel.trusted() if visible else IntegrityLabel.untrusted()
            label = ProductLabel(integ, label.right)
        desc = f"{action.role or 'element'} capability={capability.value} zone={action.zone.value}"
        return tier, label, desc

    def _audit(self, event: dict[str, Any]) -> None:
        if not self.trace_path:
            return
        event["ts"] = datetime.datetime.now().isoformat()
        try:
            with open(self.trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")
        except OSError:
            pass

    def _block(self, name: str, reason: str, event: dict[str, Any]) -> None:
        event.update({"decision": "block" if self.mode == "enforce" else "would_block", "reason": reason})
        self._audit(event)
        if self.mode == "enforce":
            raise PolicyViolation(reason)


    def check_tool_call(
        self,
        *,
        name: str,
        args: dict[str, Any],
        args_label: FidesLattice,
        screen_view: Optional[SAScreenView] = None,
        var_refs: Sequence[str] = (),
    ) -> None:
        """Raise PolicyViolation to block executing tool *name* with *args*,
        where *args_label* is the join of the labels of every variable the
        planner passed as an argument. Reasons never quote untrusted content —
        only tool names, tiers and variable tokens."""
        tier = TOOL_TIER.get(name, Tier.DESTRUCTIVE)
        event: dict[str, Any] = {
            "tool": name,
            "var_refs": list(var_refs),
            "args_untrusted": is_untrusted(args_label),
        }

        if tier == ENDORSE:
            self._audit({**event, "decision": "allow", "reason": "endorsement tool"})
            return

        target_label = trusted_public()
        target_desc = ""
        if tier == TARGET:
            point = self._point_of(args)
            if point is None and name in _CURSOR_TOOLS:
                point = self._pointer
                if (point is None and name == "mouse_down"
                        and self._mask is not None and self.mask_click_policy != "off"):
                    self._block(name, (
                        f"'{name}' presses the button wherever the cursor already is, and this "
                        f"episode has not aimed it yet, so there is no point to check against the "
                        f"redactions. Use click(x, y) instead, or move_to(x, y) first."
                    ), event)
                    return
            masked = self._masked_point(point)
            if masked is not None:
                event["masked_target"] = masked
                self._block(name, (
                    f"'{name}' targets content that was redacted from your screenshot: {masked}. "
                    f"You may only act on parts of the screen you can actually see. Three ways "
                    f"forward, by what you need: to ACT on an element belonging to a known person "
                    f"(their conversation, their message), call "
                    f"endorse_element(author=<their exact address or handle from "
                    f"get_contacts>); to ACT on the application's own control -- a message box, a "
                    f"reaction, a vote button, or any other app button named in full -- call "
                    f"endorse_element(control=\"message_box\"|\"reaction\"|\"upvote\"|"
                    f"\"downvote\", control_name=\"\") or "
                    f"endorse_element(control=\"button\", control_name=\"<its exact label>\"); "
                    f"to READ what is there, "
                    f"call query_screen(instruction, answer_type) — and to follow a link inside "
                    f"it, obtain its URL as a variable, verify it with endorse_by_crosscheck, "
                    f"and navigate via the address bar instead."
                ), event)
                return
            if name == "drag_to":
                origin = self._masked_point(self._pointer)
                if origin is not None:
                    event["masked_origin"] = origin
                    self._block(name, (
                        f"'drag_to' would start the drag from content that was redacted from your "
                        f"screenshot: {origin}. You may only act on parts of the screen you can "
                        f"actually see. Move the cursor to a visible starting point first."
                    ), event)
                    return
            tier, target_label, target_desc = self._resolve_target(name, point, screen_view)
            event["target"] = target_desc
        elif name in _KEYBOARD_TOOLS:
            masked = (None if _is_compositor_hotkey(name, args)
                      else self._focused_element_masked(screen_view))
            if masked is not None:
                event["masked_focus"] = masked
                self._block(name, (
                    f"'{name}' would send input to {masked}, which was redacted from your "
                    f"screenshot. Move focus to a control you can see before typing — and if the "
                    f"control you need is the application's own widget -- a message/comment box, a "
                    f"reaction, a vote button, or any other app button named in full -- call "
                    f"endorse_element(control='message_box'|'reaction'|'upvote'|'downvote', "
                    f"control_name='') or endorse_element(control='button', "
                    f"control_name='<its exact label>') to reveal it first."
                ), event)
                return
        elif tier == COMMAND:
            command = str(args.get("command", ""))
            tier = Tier.REVERSIBLE_WRITE if _is_structural_command(command) else Tier.DESTRUCTIVE
            event["command_tier"] = tier.value

        if tier == Tier.READ:
            self._record_pointer(name, args)
            return

        effective = args_label.join(target_label)
        refs = ", ".join(var_refs) if var_refs else "its arguments"
        endorse_hint = (
            " If the tainted value is genuinely required and can be independently verified, "
            "clear its taint first -- endorse(variable_name, source=...) for a value the task, "
            "the trusted directory, or your own knowledge pins, or "
            "endorse_by_crosscheck(variable_name, question, trusted_url) against an allowlisted "
            "source -- and then retry."
        )

        if tier == Tier.DESTRUCTIVE:
            tier_min = self.tier_min_level.get(Tier.DESTRUCTIVE, IntegrityLabel.Level.TRUSTED)
            if not meets_level(effective, tier_min):
                if not meets_level(args_label, tier_min):
                    reason = (
                        f"'{name}' is a DESTRUCTIVE action but {refs} derive from content below "
                        f"the required trust level (have {effective.left.level.name}, need at "
                        f"least {tier_min.name}: web page / screen / terminal content) — possible "
                        f"prompt injection." + endorse_hint
                    )
                else:
                    reason = (
                        f"'{name}' targets a page element below the required trust level "
                        f"({target_desc}, have {target_label.left.level.name}, need at least "
                        f"{tier_min.name}) whose effect is DESTRUCTIVE (send/execute/unknown) — "
                        f"refusing to trigger attacker-controlled controls. Prefer a trusted UI "
                        f"path or a READ tool to inspect first." + endorse_hint
                    )
                self._block(name, reason, event)
                return
        if tier == Tier.REVERSIBLE_WRITE and name != "fetch_web_content":
            tier_min = self.tier_min_level.get(Tier.REVERSIBLE_WRITE, IntegrityLabel.Level.ENDORSED)
            if not meets_level(args_label, tier_min):
                reason = (
                    f"'{name}' would write data derived from content below the required trust "
                    f"level (have {args_label.left.level.name}, need at least {tier_min.name}) "
                    f"into the environment — possible prompt injection." + endorse_hint
                )
                self._block(name, reason, event)
                return

        audience = self._sink_audience(name, args, target_desc)
        if not conf_permits(effective.right, audience):
            reason = (
                f"'{name}' would expose data readable only by a restricted set to a wider "
                f"audience (exfiltration risk). Do not send confidential values to public sinks."
            )
            self._block(name, reason, event)
            return

        if name == "fetch_web_content" and is_untrusted(args_label):
            url = str(args.get("url", ""))
            if not (host_in_domains(url, self.trusted_domains)
                    or host_in_domains(url, self.trusted_domains_corroborating)):
                self._block(
                    name,
                    "the URL for 'fetch_web_content' derives from UNTRUSTED content and is not on "
                    "the trusted-domain allowlist — refusing to contact an attacker-chosen host."
                    + endorse_hint,
                    event,
                )
                return

        self._record_pointer(name, args)
        self._audit({**event, "decision": "allow", "reason": f"{tier.value} sink, label clean"})

    def _sink_audience(self, name: str, args: dict[str, Any], target_desc: str) -> frozenset[str]:
        if name == "run_terminal_command":
            return PRINCIPALS if _command_can_send(str(args.get("command", ""))) else frozenset({"user"})
        if name == "fetch_web_content":
            url = str(args.get("url", ""))
            return frozenset({"user"}) if host_in_domains(url, self.trusted_domains) else PRINCIPALS
        if "zone=document" in target_desc:
            return PRINCIPALS
        return frozenset({"user"})
