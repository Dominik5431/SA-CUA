#!/usr/bin/env python3
"""
test_propagation_gate.py -- the geometric propagation gate in selector_propose.rule_covers().

Run: python at-spi/tests/test_propagation_gate.py   (no pytest needed, but pytest works too)

THE CASE THIS EXISTS FOR is 034_www.amazon.com: `aria-owns` on a 49px "legal disclaimer and
ranking disclosure" strip reparents six seller-titled search-result cards under it, so a maximally
narrow rule naming that strip inherited trust to 111 rows and produced all ten of the capture's
false negatives. The rule was correct about the container; the tree was wrong about where the
container's subtree is painted.

Each test states one invariant. The security-relevant direction throughout: the gate may only ever
WITHHOLD trust, so it can convert a false negative into a true positive and can never create one.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "labelers"))

import selector_propose as sp

CONTAINER_CLASS = "s-widget-container legal-disclaimer"
RULE = {"ancestor_path": [{"role": "section", "class": CONTAINER_CLASS}], "verdict": "trusted"}


def sig(role, x, y, w, h, cls=None):
    s = {"role": role, "bbox": [x, y, w, h]}
    if cls:
        s["class"] = cls
    return s


def chain(*sigs):
    return list(sigs)



def test_disjoint_descendant_is_not_inherited():
    """The 034 shape: the named strip ends at y=626, the owned list starts at y=629."""
    c = chain(sig("document web", 70, 114, 1850, 966),
              sig("section", 394, 577, 1490, 49, CONTAINER_CLASS),
              sig("list", 390, 629, 1498, 1323),
              sig("static", 403, 972, 255, 141))
    assert sp.rule_covers(c, RULE) is True, "ungated behaviour is the thing being fixed"
    assert sp.rule_covers(c, RULE, geometric_gate=True) is False


def test_overlapping_descendant_is_still_inherited():
    """The gate must not cost the ordinary case anything: a row drawn inside its container."""
    c = chain(sig("document web", 70, 114, 1850, 966),
              sig("section", 394, 577, 1490, 400, CONTAINER_CLASS),
              sig("list", 400, 600, 800, 300),
              sig("static", 410, 620, 200, 40))
    assert sp.rule_covers(c, RULE, geometric_gate=True) is True


def test_partial_overlap_is_inherited():
    """DISJOINT, not containment. A child overflowing its parent -- sticky headers, absolutely
    positioned dropdowns, a box reported unclipped -- is ordinary layout, not reparenting."""
    c = chain(sig("section", 100, 100, 200, 200, CONTAINER_CLASS),
              sig("static", 250, 250, 200, 200))
    assert sp.rule_covers(c, RULE, geometric_gate=True) is True


def test_break_anywhere_on_the_descent_stops_the_grant():
    """Trust travels edge by edge and stops at the first incoherent one, however deep."""
    c = chain(sig("section", 100, 100, 400, 400, CONTAINER_CLASS),
              sig("list", 100, 100, 400, 400),
              sig("list item", 1500, 900, 100, 100),
              sig("static", 1510, 910, 50, 20))
    assert sp.rule_covers(c, RULE, geometric_gate=True) is False



def test_missing_box_is_transparent():
    """`skipped_ancestors` are the bbox-less containers the rule language exists to name. Blocking
    on them would retire most container rules; the gate compares the nearest KNOWN boxes instead."""
    c = chain({"role": "section", "class": CONTAINER_CLASS, "bbox": [100, 100, 400, 400]},
              {"role": "section", "class": "wrapper"},
              sig("static", 120, 120, 100, 20))
    assert sp.rule_covers(c, RULE, geometric_gate=True) is True


def test_degenerate_box_blocks():
    """A zero-area wrapper must not be a one-CSS-property bypass (`width:0` on the reparented
    subtree would otherwise make the test vacuous)."""
    c = chain(sig("section", 100, 100, 400, 400, CONTAINER_CLASS),
              sig("section", 100, 100, 0, 400),
              sig("static", 120, 120, 100, 20))
    assert sp.rule_covers(c, RULE, geometric_gate=True) is False


def test_container_itself_is_still_covered():
    """A rule covers the container it names, not only what is under it -- there is no descent to
    gate when the scored row IS the named node."""
    c = chain(sig("document web", 70, 114, 1850, 966),
              sig("section", 394, 577, 1490, 49, CONTAINER_CLASS))
    assert sp.rule_covers(c, RULE, geometric_gate=True) is True



def test_gate_is_off_by_default():
    """Withholding propagation withholds TRUST under an allowlist (safe) but withholds an
    UNTRUSTED MARK under a denylist (a false negative the gate would have invented). Off unless
    the caller asks."""
    c = chain(sig("section", 394, 577, 1490, 49, CONTAINER_CLASS),
              sig("static", 403, 972, 255, 141))
    assert sp.rule_covers(c, RULE) is True


def test_denylist_prediction_is_unaffected():
    row = {"role": "static", "bbox": [403, 972, 255, 141], "attr_class": None, "attr_id": None}
    path = [sig("section", 394, 577, 1490, 49, CONTAINER_CLASS)]
    verdict, _note = sp.predict(row, path, [dict(RULE, verdict="untrusted")], "denylist", None)
    assert verdict == "untrusted", "a denylist rule must still reach the reparented row"


def test_allowlist_prediction_withholds_trust():
    row = {"role": "static", "bbox": [403, 972, 255, 141], "attr_class": None, "attr_id": None}
    path = [sig("section", 394, 577, 1490, 49, CONTAINER_CLASS)]
    verdict, _note = sp.predict(row, path, [RULE], "allowlist", None)
    assert verdict == "untrusted", "the reparented row must fall through to the default"



def test_own_role_carve_out_is_gated_from_the_container():
    """own_role names an element INSIDE the container, so the container -> element step is part of
    the descent and has to be checked too -- that step is exactly where aria-owns injects."""
    rule = dict(RULE, own_role="heading")
    c = chain(sig("section", 394, 577, 1490, 49, CONTAINER_CLASS),
              sig("heading", 403, 970, 275, 97),
              sig("static", 403, 972, 255, 141))
    assert sp.rule_covers(c, rule) is True
    assert sp.rule_covers(c, rule, geometric_gate=True) is False


def test_multi_entry_ancestor_path_measures_from_the_innermost_entry():
    """Two entries: the outer page section and the strip. The descent starts at the strip, the
    deepest node the rule actually names -- not at the page root, which meets everything."""
    rule = {"ancestor_path": [{"role": "section", "class": "page-body"},
                              {"role": "section", "class": CONTAINER_CLASS}],
            "verdict": "trusted"}
    c = chain(sig("section", 70, 114, 1835, 11749, "page-body"),
              sig("section", 394, 577, 1490, 49, CONTAINER_CLASS),
              sig("static", 403, 972, 255, 141))
    assert sp.rule_covers(c, rule, geometric_gate=True) is False


KNOWN_GAPS: dict = {}


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed, gaps, closed = [], [], []
    for name, fn in tests:
        try:
            fn()
        except (AssertionError, Exception) as e:
            detail = str(e) if isinstance(e, AssertionError) else f"{type(e).__name__}: {e}"
            if name in KNOWN_GAPS:
                gaps.append(name)
                print(f"  GAP   {name}\n          {KNOWN_GAPS[name]}")
            else:
                failed.append((name, detail))
                print(f"  FAIL  {name}\n          {detail[:160]}")
            continue
        if name in KNOWN_GAPS:
            closed.append(name)
            print(f"  GAP CLOSED  {name} -- remove it from KNOWN_GAPS")
        else:
            print(f"  PASS  {name}")
    print(f"\n{len(tests) - len(failed) - len(gaps)}/{len(tests)} passed, "
          f"{len(gaps)} known gap(s), {len(failed)} failure(s)")
    return 1 if failed or closed else 0


if __name__ == "__main__":
    sys.exit(_main())
