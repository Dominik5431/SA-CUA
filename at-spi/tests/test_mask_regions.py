#!/usr/bin/env python3
"""
test_mask_regions.py -- the geometry mask_screenshot uses to keep numbered regions honest when a
capture holds more than one window.

Run: python at-spi/tests/test_mask_regions.py     (no pytest needed, but pytest works too)

A region is a promise: "the thing you can see inside box N is the thing I named". On a stacked
desktop that promise breaks two ways -- a region of a hidden window drawn over the window in
front, and a window in front with no regions of its own at all. These cover the first.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mask_screenshot as ms

trim = ms._trim_to_visible


def test_no_occluder_leaves_the_box_alone():
    assert trim((10, 10, 100, 100), []) == (10, 10, 100, 100)


def test_disjoint_occluder_leaves_the_box_alone():
    assert trim((10, 10, 100, 100), [(500, 500, 50, 50)]) == (10, 10, 100, 100)


def test_a_fully_covering_occluder_removes_the_region():
    assert trim((10, 10, 100, 100), [(0, 0, 500, 500)]) is None


def test_full_width_slab_trims_the_top():
    assert trim((10, 10, 100, 100), [(0, 0, 500, 40)]) == (10, 40, 100, 70)


def test_full_width_slab_trims_the_bottom():
    assert trim((10, 10, 100, 100), [(0, 80, 500, 500)]) == (10, 10, 100, 70)


def test_full_height_slab_trims_the_left():
    assert trim((10, 10, 100, 100), [(0, 0, 40, 500)]) == (40, 10, 70, 100)


def test_a_window_landing_in_the_middle_does_not_trim():
    assert trim((0, 0, 100, 100), [(40, 40, 20, 20)]) == (0, 0, 100, 100)


def test_a_corner_bite_does_not_trim():
    assert trim((0, 0, 100, 100), [(80, 80, 100, 100)]) == (0, 0, 100, 100)


def test_slabs_compose():
    assert trim((10, 10, 100, 100), [(0, 0, 500, 30), (0, 0, 30, 500)]) == (30, 30, 80, 80)


def test_slabs_that_meet_remove_the_region():
    assert trim((10, 10, 100, 100), [(0, 0, 500, 60), (0, 50, 500, 500)]) is None



VIEW = 1000 * 1000
BOUNDS = (0, 0, 1000, 1000)


def _regions(*boxes):
    return [{"bbox": list(b), "n": 1, "label": "r%d" % i, "kind": "section"}
            for i, b in enumerate(boxes)]


def _enforce(regions, bounds=BOUNDS):
    return ms.enforce_min_box(regions, VIEW, bounds, lambda box: 1)


def _area(d):
    return d["bbox"][2] * d["bbox"][3]


def test_a_box_over_the_floor_is_left_alone():
    out = _enforce(_regions((10, 10, 200, 200)))
    assert [d["bbox"] for d in out] == [[10, 10, 200, 200]]


def test_every_surviving_box_clears_the_floor():
    out = _enforce(_regions((10, 10, 33, 33), (500, 500, 40, 40), (0, 0, 300, 300)))
    assert all(_area(d) >= ms.REGION_MIN_BOX_FRAC * VIEW for d in out), [d["bbox"] for d in out]


def test_a_sliver_beside_a_region_is_absorbed_by_it():
    out = _enforce(_regions((20, 5, 30, 10), (10, 10, 200, 200)))
    assert len(out) == 1
    assert out[0]["bbox"] == [10, 5, 200, 205]


def test_a_sliver_is_not_dragged_across_the_page():
    out = _enforce(_regions((0, 0, 30, 30), (800, 800, 150, 150)))
    assert len(out) == 2
    assert out[0]["bbox"][0] < 400 and out[0]["bbox"][1] < 400


def test_absorbing_keeps_the_slivers_pixels_inside_a_box():
    sliver = (20, 5, 30, 10)
    out = _enforce(_regions(sliver, (10, 10, 200, 200)))
    x, y, w, h = out[0]["bbox"]
    assert x <= sliver[0] and y <= sliver[1]
    assert x + w >= sliver[0] + sliver[2] and y + h >= sliver[1] + sliver[3]


def test_inflating_keeps_the_slivers_pixels_inside_the_box():
    sliver = (400, 400, 30, 30)
    out = _enforce(_regions(sliver))
    x, y, w, h = out[0]["bbox"]
    assert x <= sliver[0] and y <= sliver[1]
    assert x + w >= sliver[0] + sliver[2] and y + h >= sliver[1] + sliver[3]


def test_inflation_stays_inside_the_viewport():
    out = _enforce(_regions((980, 980, 20, 20)))
    x, y, w, h = out[0]["bbox"]
    assert x >= 0 and y >= 0 and x + w <= 1000 and y + h <= 1000
    assert w * h >= ms.REGION_MIN_BOX_FRAC * VIEW


def test_a_wide_strip_grows_the_short_side_not_the_long_one():
    out = _enforce(_regions((100, 500, 424, 20)))
    x, y, w, h = out[0]["bbox"]
    assert w * h >= ms.REGION_MIN_BOX_FRAC * VIEW
    assert w < 600, "a strip should not have to double in width to clear the floor"


def test_a_viewport_smaller_than_the_floor_does_not_hang():
    out = _enforce(_regions((0, 0, 10, 10)), bounds=(0, 0, 50, 50))
    assert len(out) == 1


def test_the_absorbed_box_lends_its_name_to_a_remainder():
    regions = _regions((20, 5, 30, 10), (10, 10, 200, 200))
    regions[0]["label"], regions[0]["kind"] = "byline", "heading"
    regions[1]["label"], regions[1]["kind"] = "remainder", "remainder"
    out = _enforce(regions)
    assert out[0]["label"] == "byline" and out[0]["kind"] == "heading"



def _named(box, label="site-footer", kind="landmark"):
    return {"bbox": list(box), "n": 1, "label": label, "kind": kind}


def _remainder(box):
    return {"bbox": list(box), "n": 1, "label": "remainder", "kind": "remainder"}


def _absorb(regions):
    return ms.absorb_duplicate_remainders(regions, lambda box: 1)


def test_a_remainder_that_swallows_a_named_region_is_folded_into_it():
    out = _absorb([_named((84, 1044, 1807, 22)), _remainder((70, 1030, 1835, 50))])
    assert len(out) == 1
    assert out[0]["label"] == "site-footer"
    assert out[0]["bbox"] == [70, 1030, 1835, 50]


def test_a_remainder_inside_a_named_region_is_folded_into_it():
    out = _absorb([_named((0, 0, 400, 400)), _remainder((10, 10, 50, 50))])
    assert len(out) == 1 and out[0]["label"] == "site-footer"


def test_two_named_regions_are_left_alone_however_nested():
    out = _absorb([_named((107, 101, 860, 777), "composeContentBox", "section"),
                   _named((107, 102, 860, 25), "compose-toolbar", "tool-bar")])
    assert len(out) == 2


def test_two_remainders_are_left_alone():
    out = _absorb([_remainder((0, 0, 400, 400)), _remainder((10, 10, 50, 50))])
    assert len(out) == 2


def test_a_remainder_that_merely_touches_a_region_survives():
    out = _absorb([_named((0, 0, 100, 100)), _remainder((300, 300, 100, 100))])
    assert len(out) == 2



def _result(regions, root_meta=None):
    """A MaskResult with no picture -- region_at/focus_bbox touch neither the image nor the bits."""
    import numpy as np
    return ms.MaskResult(None, np.zeros((1, 1), dtype=bool), regions, {}, root_meta=root_meta)


def _reg(rid, box, root=0, label="box"):
    return {"id": rid, "label": label, "bbox": list(box), "root_index": root}


_TOP = [210, 154, 860, 777]
_STACK = [_reg(1, (70, 64, 1850, 1016), 0, "frame"),
          _reg(3, (107, 101, 860, 777), 1, "composeContentBox"),
          _reg(33, (197, 191, 860, 777), 5, "composeContentBox"),
          _reg(48, (242, 236, 860, 777), 7, "composeContentBox"),
          _reg(64, (120, 64, 860, 777), 9, "composeContentBox"),
          _reg(94, _TOP, 13, "composeContentBox")]
_STACK_META = {i: {"active": i == 13, "bbox": _TOP if i == 13 else [0, 0, 10, 10],
                   "hidden": [] if i == 13 else [_TOP]} for i in (0, 1, 5, 7, 9, 13)}


def test_a_hidden_window_may_not_claim_a_pixel_of_the_window_in_front():
    got = _result(_STACK, _STACK_META).region_at(365, 277)
    assert got["id"] == 94, f"reported region {got['id']} (root {got['root_index']})"


def test_a_pixel_outside_the_raised_window_still_names_its_owner():
    got = _result(_STACK, _STACK_META).region_at(150, 120)
    assert got["root_index"] == 1, got


def test_smallest_area_still_wins_when_nothing_is_stacked():
    plain = _result([_reg(1, (0, 0, 100, 100)), _reg(2, (10, 10, 20, 20), label="small")])
    assert plain.region_at(15, 15)["label"] == "small"


def test_the_active_window_wins_before_area_does():
    regions = [_reg(1, (0, 0, 400, 400), 0), _reg(2, (10, 10, 30, 30), 1, "band")]
    meta = {0: {"active": True, "bbox": [0, 0, 400, 400], "hidden": []},
            1: {"active": False, "bbox": [10, 10, 30, 30], "hidden": []}}
    assert _result(regions, meta).region_at(20, 20)["id"] == 1


def test_a_point_no_region_covers_is_still_None():
    assert _result(_STACK, _STACK_META).region_at(5, 5) is None


def test_a_fully_covered_point_falls_back_rather_than_going_silent():
    regions = [_reg(7, (0, 0, 100, 100), 1)]
    meta = {1: {"active": False, "bbox": [0, 0, 100, 100], "hidden": [[0, 0, 100, 100]]}}
    got = _result(regions, meta).region_at(50, 50)
    assert got is not None and got["id"] == 7


def test_focus_bbox_is_the_active_window_and_None_without_one():
    assert _result(_STACK, _STACK_META).focus_bbox() == _TOP
    assert _result(_STACK).focus_bbox() is None


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            failed.append(name)
            print(f"  FAIL  {name}\n          {str(e)[:160]}")
            continue
        print(f"  PASS  {name}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed, {len(failed)} failure(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
