#!/usr/bin/env python3
"""
Unit-level checks for the deterministic screen channel (no VM, no model, no network).

  PYTHONPATH=. python -m endorsement.harness.test_locate
  PYTHONPATH=. python -m endorsement.harness.test_locate path/to/snapshot.xml

Imports ``mm_agents.sa_agent.locate`` -- the same module the agent runs, never a copy. This
file covers the primitive's edges; ``cases/04_locate_selection.yaml`` and
``cases/05_attest_readoff.yaml`` cover the channel behaviour the plan specifies.

The property under test is the one `locate` exists for: the contract is
**uniqueness, not presence**. A boolean phrased as
"is the first row max0815?" answers yes -- deterministically and correctly --
even when two rows render max0815, and acting on that yes is wrong. Every check
here is deterministic; there is no model and no network anywhere in this file.

Exits non-zero on any failed assertion.
"""
from __future__ import annotations

import sys
from xml.sax.saxutils import quoteattr

from mm_agents.sa_agent.a11y import ENTRY_TAGS, parse_a11y_nodes
from mm_agents.sa_agent.locate import (
    LocateStatus,
    attest,
    locate,
    normalize,
    skeleton,
)

_NS = ('xmlns:component="https://accessibility.ubuntu.example.org/ns/component" '
       'xmlns:state="https://accessibility.ubuntu.example.org/ns/state"')

PASSED: list[str] = []


def node(tag: str, name: str, x: int, y: int, w: int, h: int, children: str = "") -> str:
    return (f'<{tag} name={quoteattr(name)} component:screencoord="({x},{y})" '
            f'component:size="({w},{h})">{children}</{tag}>')


def tree(*nodes: str) -> str:
    return f'<desktop-frame {_NS} name="root" component:screencoord="(0,0)" ' \
           f'component:size="(1920,1080)">{"".join(nodes)}</desktop-frame>'


def check(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        print(f"FAIL  {label}" + (f"\n      {detail}" if detail else ""))
        sys.exit(1)
    PASSED.append(label)
    print(f"ok    {label}")


ENTRY = node("entry", "max0815", 400, 200, 300, 30)


def dropdown(*rows: str) -> str:
    return tree(ENTRY, node("list-box", "", 400, 232, 300, 120, "".join(rows)))


t = dropdown(
    node("list-item", "max0815", 400, 232, 300, 40),
    node("list-item", "max9999", 400, 272, 300, 40),
    node("list-item", "maxine", 400, 312, 300, 40),
)
r = locate("max0815", t)
check("unique row is located", r.status is LocateStatus.UNIQUE, r.reason)
check("located node is the row, not the entry field", r.node.tag == "list-item")
check("center is inside the row", r.center == (550, 252), str(r.center))
check("match is exact, not a token fallback", r.match_kind == "exact")

t = dropdown(
    node("list-item", "max0815", 400, 232, 300, 40),
    node("list-item", "max9999", 400, 272, 300, 40),
    node("list-item", "max0815", 400, 312, 300, 40),
)
r = locate("max0815", t)
check("duplicate rows refuse rather than tie-break", r.status is LocateStatus.AMBIGUOUS, r.reason)
check("both duplicates are reported", len(r.matches) == 2)
check("no node is handed back on AMBIGUOUS", r.node is None)
rows = [n for n in parse_a11y_nodes(t) if n.tag == "list-item"]
first_row_is_max0815 = normalize(rows[0].label) == normalize("max0815")
check("...and the boolean phrasing would have said yes", first_row_is_max0815 is True)

t = dropdown(
    node("list-item", "max0815", 400, 232, 300, 40),
    node("list-item", "mаx0815", 400, 272, 300, 40),
)
r = locate("max0815", t)
check("homoglyph row does not create ambiguity", r.status is LocateStatus.UNIQUE, r.reason)
check("homoglyph row is reported as a confusable", len(r.confusables) == 1)
check("confusable folding is report-only", skeleton("mаx0815") == skeleton("max0815"))

t = dropdown(
    node("list-item", "max0815", 400, 232, 300, 40),
    node("list-item", "ｍax0815", 400, 272, 300, 40),
)
r = locate("max0815", t)
check("NFKC-equal rows are ambiguous, not silently deduped",
      r.status is LocateStatus.AMBIGUOUS, r.reason)

t = dropdown(node("list-item", "max​0815", 400, 232, 300, 40))
r = locate("max0815", t)
check("zero-width characters cannot hide a row from the ambiguity check",
      r.status is LocateStatus.UNIQUE, r.reason)

t = dropdown(node("list-item", "max0815", 400, 232, 300, 40))
check("entry field excluded by default", locate("max0815", t).status is LocateStatus.UNIQUE)
r = locate("max0815", t, exclude_tags=frozenset())
check("...and collides once readback is allowed", r.status is LocateStatus.AMBIGUOUS, r.reason)
check("ENTRY_TAGS is what the default excludes", "entry" in ENTRY_TAGS)

t = dropdown(node("list-item", "max0815", 400, 232, 300, 40,
                  node("label", "max0815", 402, 234, 296, 36)))
r = locate("max0815", t)
check("row and its label child are one hit", r.status is LocateStatus.UNIQUE, r.reason)
check("the most specific box is kept", r.node.tag == "label", r.node.tag)

t = dropdown(node("list-item", "Max Mustermann  max0815", 400, 232, 300, 40))
r = locate("max0815", t)
check("whole token inside a longer row label matches", r.status is LocateStatus.UNIQUE, r.reason)
check("...and is flagged as a token match", r.match_kind == "token")

t = tree(node("list-item", "termcolour", 100, 100, 200, 30))
check("substring does not bleed (termcolor vs termcolour)",
      locate("termcolor", t).status is LocateStatus.NOT_FOUND)

t = tree(node("list-item", "max0815-bot", 100, 100, 200, 30))
check("identifier punctuation does not open a substring hole",
      locate("max0815", t).status is LocateStatus.NOT_FOUND)

t = tree(node("list-item", "@max0815 replied", 100, 100, 200, 30))
check("non-identifier punctuation still yields a whole-token match",
      locate("max0815", t).status is LocateStatus.UNIQUE)

t = tree(node("list-item", "ok", 100, 100, 80, 30), node("push-button", "ok", 300, 100, 80, 30))
check("generic UI word is refused, not located",
      locate("ok", t).status is LocateStatus.NEEDLE_REJECTED)
check("short needle is refused", locate("ab", t).status is LocateStatus.NEEDLE_REJECTED)
check("empty needle is refused", locate("   ", t).status is LocateStatus.NEEDLE_REJECTED)

t = tree(
    node("list-item", "max0815", 400, 232, 300, 40),
    node("label", "max0815", 900, 700, 300, 40),
)
check("whole screen sees both", locate("max0815", t).status is LocateStatus.AMBIGUOUS)
r = locate("max0815", t, region=(400, 200, 300, 200))
check("region filter resolves the collision", r.status is LocateStatus.UNIQUE, r.reason)
r = locate("max0815", t, interactive_only=True)
check("interactive_only keeps only the clickable row", r.status is LocateStatus.UNIQUE, r.reason)
check("malformed region is refused, not ignored",
      locate("max0815", t, region=("a", 1, 2, 3)).status is LocateStatus.NEEDLE_REJECTED)

check("absent value is NOT_FOUND",
      locate("nobody-here-1234", t).status is LocateStatus.NOT_FOUND)
check("empty tree is NOT_FOUND", locate("max0815", None).status is LocateStatus.NOT_FOUND)
check("unparseable tree is NOT_FOUND", locate("max0815", "<not xml").status is LocateStatus.NOT_FOUND)

from pathlib import Path
_REPO = Path(__file__).resolve().parents[2]
path = sys.argv[1] if len(sys.argv) > 1 else str(
    _REPO / "test/xml_examples/own_reddit_1_specific_send_account_md_language_step_0.xml")
try:
    raw = open(path, encoding="utf-8", errors="replace").read()
except OSError as exc:
    print(f"skip  real capture ({exc})")
    raw = ""

if raw:
    nodes = parse_a11y_nodes(raw)
    check("real capture parses to nodes", len(nodes) > 0, path)
    from collections import Counter
    pool = [n for n in nodes if n.label and n.tag not in ENTRY_TAGS]
    counts = Counter(normalize(n.label) for n in pool)

    singletons = [n for n in pool if counts[normalize(n.label)] == 1
                  and 6 <= len(n.label) <= 40]
    bad = [(n.label, locate(n.label, nodes).status) for n in singletons]
    bad = [(lab, st) for lab, st in bad if st is not LocateStatus.UNIQUE]
    check(f"all {len(singletons)} singly-rendered real labels locate uniquely",
          not bad, "; ".join(f"{lab!r}->{st.value}" for lab, st in bad[:5]))

    repeated = [n for n in pool if counts[normalize(n.label)] > 1 and len(n.label) >= 6]
    amb = [n for n in repeated if locate(n.label, nodes).status is LocateStatus.AMBIGUOUS]
    check(f"repeated real labels refuse ({len(amb)}/{len(repeated)} ambiguous)",
          len(repeated) == 0 or len(amb) > 0)
    check("an absent needle is NOT_FOUND on a real tree",
          locate("zzz-not-on-this-screen-9911", nodes).status is LocateStatus.NOT_FOUND)

post = tree(
    node("label", "Install it with: pip install termcolor", 100, 100, 600, 30),
    node("label", "pip install termcolor", 100, 400, 400, 30),
)
a = attest("termcolor", post)
check("a value the model really read off the screen is attested", a.attested, a.reason)
check("attest reports a localisation bbox", a.bbox is not None and a.bbox[2] > 0)
check("multiplicity is not a refusal for attest (unlike locate)", len(a.nodes) == 2)
check("...whereas locate would refuse the same tree",
      locate("termcolor", post).status is LocateStatus.AMBIGUOUS)

a = attest("requests", post)
check("a value the model invented is not attested", not a.attested, a.reason)
check("...and carries no bbox to launder a label with", a.bbox is None)

a = attest("Install it with: pip install termcolor, and then run the setup script that the "
           "post describes below in order to finish the installation properly", post)
check("prose is refused as unattestable rather than rubber-stamped", not a.attested)
check("...for the stated reason", "prose" in a.reason, a.reason)

check("attest confers no level (it returns no label at all)",
      not hasattr(attest("termcolor", post), "level"))

try:
    from mm_agents.sa_agent.tools import (
        AttestScreenValueParams,
        LocateOnScreenParams,
        attest_screen_value,
        locate_on_screen,
    )
except ImportError as exc:
    print(f"\n!! SKIPPED the planner-facing tool checks: {exc}")
    print(f"\n{len(PASSED)} checks passed")
    sys.exit(1)


class _FakeMask:
    """Just the three members tools.py uses: region(), region_at(), hit(), regions."""

    def __init__(self, regions):
        self.regions = regions

    def region(self, rid):
        return next((r for r in self.regions if r["id"] == rid), None)

    def region_at(self, x, y):
        return next((r for r in self.regions
                     if r["bbox"][0] <= x < r["bbox"][0] + r["bbox"][2]
                     and r["bbox"][1] <= y < r["bbox"][1] + r["bbox"][3]), None)

    def hit(self, x, y):
        return self.region_at(x, y) is not None


dropdown = tree(
    node("entry", "max0815", 300, 200, 200, 30),
    node("list-box", "", 300, 230, 200, 90, children=(
        node("list-item", "max0815", 300, 230, 200, 30)
        + node("list-item", "max_0815", 300, 260, 200, 30)
        + node("list-item", "max0815-bot", 300, 290, 200, 30))),
)
obs = {"accessibility_tree": dropdown, "screenshot": b""}


def L(needle, **kw):
    kw.setdefault("region_id", "")
    kw.setdefault("interactive_only", False)
    kw.setdefault("include_typed_text", False)
    return locate_on_screen(LocateOnScreenParams(needle=needle, **kw),
                            obs, kw.pop("_mask", None)).root.value


out = L("max0815")
check("tool: a unique row comes back UNIQUE with its center", out.status == "UNIQUE"
      and out.x == 400 and out.y == 245, f"{out.status} ({out.x},{out.y})")
check("tool: the entry field is excluded by default", out.n_matches == 1)

out = L("max0815", include_typed_text=True)
check("tool: include_typed_text=True exposes the readback collision",
      out.status == "AMBIGUOUS" and out.n_matches == 2, f"{out.status} n={out.n_matches}")

out = L("user")
check("tool: a generic needle is refused", out.status == "NEEDLE_REJECTED", out.reason)
check("tool: a refusal carries no coordinates", out.x == 0 and out.y == 0)

out = L("nobody-here-9911")
check("tool: an absent needle is NOT_FOUND with no coordinates",
      out.status == "NOT_FOUND" and out.x == 0 and out.y == 0)

out = L("max0815", region_id="3")
check("tool: region_id without a mask is refused, not silently ignored",
      out.status == "NEEDLE_REJECTED" and "not masked" in out.reason, out.reason)

out = L("max0815", region_id="nine")
check("tool: a non-numeric region_id is refused", out.status == "NEEDLE_REJECTED", out.reason)

mask_covering = _FakeMask([{"id": 2, "label": "comment thread", "bbox": (280, 220, 240, 120)}])
out = locate_on_screen(LocateOnScreenParams(needle="max0815", region_id="2",
                                            interactive_only=False, include_typed_text=False),
                       obs, mask_covering).root.value
check("tool: region-scoped locate finds the row inside the named region",
      out.status == "UNIQUE_MASKED", f"{out.status}: {out.reason}")
check("tool: a hit inside a redacted region is flagged, and the location rule is not relaxed",
      out.masked_target and "refused" in out.reason, out.reason)
check("tool: the masked-hit warning survives redaction, i.e. it lives in status",
      out.status.endswith("_MASKED"), out.status)

out = locate_on_screen(LocateOnScreenParams(needle="max0815", region_id="7",
                                            interactive_only=False, include_typed_text=False),
                       obs, mask_covering).root.value
check("tool: an unknown region_id names the regions that do exist",
      out.status == "NEEDLE_REJECTED" and "2" in out.reason, out.reason)

from mm_agents.sa_agent.labels import metadata_to_label
from mm_agents.sa_agent.lattice import IntegrityLabel

res = locate_on_screen(LocateOnScreenParams(needle="max0815", region_id="",
                                            interactive_only=False, include_typed_text=False),
                       obs, None)
lab = metadata_to_label(res.root.metadata)
check("tool: locate stamps trusted_public(); the needle's level arrives via args_label",
      lab.left.level is IntegrityLabel.Level.TRUSTED, str(lab.left.level))

def A(claim, region_id="", mask=None):
    return attest_screen_value(AttestScreenValueParams(claim=claim, region_id=region_id),
                               {"accessibility_tree": post, "screenshot": b""}, mask).root.value


out = A("termcolor")
check("tool: a faithful read is ATTESTED", out.status == "ATTESTED", out.reason)
check("tool: attest reports how many nodes render it", out.n_nodes == 2)

out = A("requests")
check("tool: an invented value is NOT_ATTESTED", out.status == "NOT_ATTESTED", out.reason)

out = A("Install it with: pip install termcolor, and then run the setup script that the post "
        "describes below in order to finish the installation properly")
check("tool: prose is NOT_ATTESTABLE, a third verdict distinct from NOT_ATTESTED",
      out.status == "NOT_ATTESTABLE", out.reason)

lab = metadata_to_label(
    attest_screen_value(AttestScreenValueParams(claim="termcolor", region_id=""),
                        {"accessibility_tree": post, "screenshot": b""}, None).root.metadata)
check("tool: attest stamps trusted_public() for its VERDICT and relabels nothing",
      lab.left.level is IntegrityLabel.Level.TRUSTED, str(lab.left.level))

print(f"\n{len(PASSED)} checks passed")
