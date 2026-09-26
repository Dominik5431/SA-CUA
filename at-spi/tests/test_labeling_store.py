#!/usr/bin/env python3
"""
test_labeling_store.py -- the pathological trees labeling_store's rules exist for.

Run: python at-spi/tests/test_labeling_store.py     (no pytest needed, but pytest works too)

Each test isolates ONE decision and states the invariant in security terms where there is one.
The bias throughout is the project's: a false positive costs the labeler a keystroke, a false
negative costs a piece of untrusted content its only row and can never be recovered downstream.
"""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fixtures as fx
import labeling_store as ls

SCREEN = (1920, 1080)


def build(xml_text: str) -> list:
    root = ET.fromstring(xml_text)
    return ls.build_universe_from_root(root, platform="web", screen_size=SCREEN,
                                       filename="t.xml")["rows"]


def rows_at(rows, bbox):
    return [r for r in rows if r["bbox"] == list(bbox)]


def content_at(rows, bbox):
    return [r for r in rows_at(rows, bbox) if r["tier"] == "content"]



def test_union_area_is_exact():
    assert ls._union_area([]) == 0
    assert ls._union_area([(0, 0, 10, 10)]) == 100
    assert ls._union_area([(0, 0, 10, 10), (0, 0, 10, 10)]) == 100, "overlap counted twice"
    assert ls._union_area([(0, 0, 10, 10), (50, 50, 10, 10)]) == 200
    assert ls._union_area([(0, 0, 20, 20), (5, 5, 5, 5)]) == 400, "nested rect inflated the union"
    assert ls._union_area([(0, 0, 0, 10), (0, 0, 10, 0)]) == 0, "degenerate rects have no area"
    assert ls._union_area([(0, 0, 10, 10), (5, 0, 10, 10)]) == 150, "partial overlap"


def test_union_area_matches_brute_force():
    import random
    random.seed(4)
    for _ in range(200):
        rects = [(random.randint(0, 60), random.randint(0, 60),
                  random.randint(1, 15), random.randint(1, 15))
                 for _ in range(random.randint(0, 20))]
        px = set()
        for x, y, w, h in rects:
            px.update((i, j) for i in range(x, x + w) for j in range(y, y + h))
        assert ls._union_area(rects) == len(px), rects


def test_many_tiny_boxes_do_not_fake_coverage():
    """THE EVASION. Both _covered() callers DROP a candidate when coverage is True, so declaring
    a crowded region covered erases it. The old code returned True for >200 clipped rectangles
    without measuring, so scattering tiny named nodes over a photo removed the photo's only row --
    and content no row enumerates can never be labeled untrusted, or masked."""
    photo = (0, 0, 400, 400)
    tiny = [(7 * i % 380, 11 * i % 380, 4, 4) for i in range(250)]
    assert len(tiny) > 200
    covered_px = ls._union_area([c for c in (ls._clip(r, photo) for r in tiny) if c])
    assert covered_px < 0.10 * 400 * 400
    assert ls._covered(tiny, photo, 0.10) is False


def test_covered_bounds_agree_with_exact():
    """The sum and single-rect fast paths are exact implications; they must never disagree with
    the sweep they are standing in for."""
    import random
    random.seed(9)
    box = (0, 0, 100, 100)
    for _ in range(300):
        rects = [(random.randint(-20, 90), random.randint(-20, 90),
                  random.randint(1, 60), random.randint(1, 60))
                 for _ in range(random.randint(1, 12))]
        for frac in (0.10, 0.5, 0.9):
            inside = [c for c in (ls._clip(r, box) for r in rects) if c]
            exact = bool(inside) and ls._union_area(inside) >= frac * 100 * 100
            assert ls._covered(rects, box, frac) == exact, (rects, frac)



def test_icon_only_button_with_sr_only_label_keeps_a_row():
    """An icon button whose only named descendant is screen-reader-only, WORDED DIFFERENTLY from
    the button's own aria-label. _has_named_descendant() answered "yes" and refused the button
    draws_graphic, and the demotion chain then filed it under unpainted_name -- leaving the glyph
    with no labelable row. Where the two strings matched, _descendant_paints() already rescued it,
    which is why this only ever showed up when the wording drifted."""
    btn = fx.node("push button", 100, 100, 32, 32, "Delete item", name_from="attribute",
                  children=(fx.node("image", 108, 108, 16, 16)
                            + fx.node("static", 100, 100, 1, 1, "Delete", cls="sr-only")))
    rows = build(fx.page(btn))
    assert content_at(rows, (100, 100, 32, 32)), \
        "icon button lost its row to a screen-reader-only descendant"


def test_nested_geometry_duplicate_keeps_one_row():
    """KNOWN GAP -- see KNOWN_GAPS. A padded control whose label is drawn by a child at a DIFFERENT
    box: push button [100,100,120,32] "Save" over static [110,108,100,16] "Save".

    The echo rule requires parent.bbox == child.bbox exactly, so this pair is not an echo and both
    rows reach the labeler for one string on screen. The name is `attribute` on the button and
    `contents` on the child, and _descendant_paints() then blocks the unpainted_name demotion --
    correctly, because the string IS on screen; it just picks the wrong survivor.

    Costs a keystroke, not a region: the failure direction is a duplicate row (FP), never a lost
    one (FN), which is why it is tracked rather than urgent. Closing it means widening echo from
    "same box" to "child box inside parent box and same string", which changes which row survives
    and therefore needs the ground truth to be unlabeled -- i.e. now, or after the next reset."""
    btn = fx.node("push button", 100, 100, 120, 32, "Save", name_from="attribute",
                  children=fx.node("static", 110, 108, 100, 16, "Save"))
    rows = build(fx.page(btn))
    assert len(content_at(rows, (100, 100, 120, 32))) + len(content_at(rows, (110, 108, 100, 16))) == 1, \
        "the same string was made labelable twice"


def test_sr_only_text_is_not_content():
    rows = build(fx.page(fx.node("static", 100, 100, 200, 20, "Skip to main content", cls="sr-only")))
    r = rows_at(rows, (100, 100, 200, 20))[0]
    assert r["tier"] == "structural" and r["paint_status"] == ls.PAINT_NO


def test_offscreen_and_zero_area_are_paint_no():
    rows = build(fx.page(fx.node("static", -900, 100, 200, 20, "off to the left")
                         + fx.node("static", 400, 400, 0, 0, "collapsed")))
    for r in rows:
        if r["text"] in ("off to the left", "collapsed"):
            assert r["paint_status"] == ls.PAINT_NO, (r["text"], r["skip_reason"])



def test_nameless_photo_wrapper_becomes_a_visual_region():
    """Chrome drops CSS-background and presentational images, so a product photo reaches AT-SPI
    only as a nameless wrapper. If that gets no row, the image is unlabelable."""
    rows = build(fx.page(fx.node("section", 200, 200, 300, 300, "", cls="s-product-image")))
    got = rows_at(rows, (200, 200, 300, 300))
    assert got and got[0]["visual_region"], "nameless image wrapper produced no visual region"


def test_photo_survives_a_small_overlay_control():
    """A heart button on a hotel photo must not erase the photo -- the coverage test exists
    precisely because a presence test made this site-template-dependent."""
    photo = fx.node("section", 200, 200, 400, 400, "",
                    children=fx.node("push button", 560, 210, 30, 30, "Save", name_from="attribute"))
    rows = build(fx.page(photo))
    got = rows_at(rows, (200, 200, 400, 400))
    assert got and got[0]["visual_region"], "a 30x30 control erased a 400x400 photo"


def test_photo_with_a_thousand_text_descendants():
    """Scale, without the >200 cliff: many small named descendants that genuinely cover very
    little must leave the region intact."""
    kids = "".join(fx.node("static", 205 + (i % 40) * 9, 205 + (i // 40) * 9, 3, 3, f"x{i}")
                   for i in range(1000))
    rows = build(fx.page(fx.node("section", 200, 200, 400, 400, "", children=kids)))
    got = rows_at(rows, (200, 200, 400, 400))
    assert got and got[0]["visual_region"], "1000 tiny labels erased the region behind them"


def test_text_block_is_not_a_visual_region():
    """The converse: descendants that really do account for the pixels must suppress the wrapper,
    or every layout div becomes a box to judge."""
    kids = "".join(fx.node("static", 200, 200 + i * 20, 400, 20, f"line {i}") for i in range(20))
    rows = build(fx.page(fx.node("section", 200, 200, 400, 400, "", children=kids)))
    got = rows_at(rows, (200, 200, 400, 400))
    assert not (got and got[0]["visual_region"]), "a wrapper full of text claimed to be an image"


def test_degenerate_control_adopts_its_label_box():
    """A styled checkbox: the real <input> stays in the layout at 1x1 and the control is painted on
    the <label> beside it. AT-SPI reports both faithfully, so the NAME sits on a 1x1 node and the
    RECTANGLE on a nameless one, and the labeler is shown a 1-pixel target for text plainly on
    screen. Measured on 026_www.booking.com ('Very good: 8+', input at [454,814,1,1], label at
    [454,819,233,40]); 42 of the corpus's 74 degenerate named controls have this shape."""
    row = fx.node("section", 454, 814, 233, 50, "",
                  children=(fx.node("check box", 454, 814, 1, 1, "Very good: 8+: 2213 properties",
                                    name_from="attribute", cls="faadc60545")
                            + fx.node("label", 454, 819, 233, 40, "")))
    rows = build(fx.page(row))
    cb = [r for r in rows if r["role"] == "check box"]
    assert cb, "the checkbox vanished"
    assert cb[0]["bbox"] == [454, 819, 233, 40], \
        f"checkbox kept its 1x1 box instead of the label's: {cb[0]['bbox']}"
    assert cb[0]["tier"] == "content"


def test_control_keeps_its_own_box_when_there_is_no_label():
    """The converse: a genuinely tiny control with no label sibling must not borrow a box from
    anywhere. Adopting geometry is only sound when a nameless <label> is rendering the control."""
    row = fx.node("section", 454, 814, 233, 50, "",
                  children=fx.node("check box", 454, 814, 1, 1, "Standalone", name_from="attribute"))
    rows = build(fx.page(row))
    cb = [r for r in rows if r["role"] == "check box"]
    assert cb and cb[0]["bbox"] == [454, 814, 1, 1], "a control with no label borrowed a rectangle"


def test_nameless_graphic_inside_a_named_control_is_one_row():
    """025_www.booking.com's save-to-list heart: `push button [932,533,37,36]` named
    "Save ... to a trip list" wrapping a nameless `section` on the IDENTICAL rectangle, which
    _visual_regions() promotes because nothing named covers it. Both reached the content tier on
    the same pixels. The echo rule cannot see this -- it matches a child repeating its parent's
    NAME, and this child has no name at all."""
    btn = fx.node("push button", 932, 533, 37, 36, "Save to a trip list", name_from="attribute",
                  children=fx.node("section", 932, 533, 37, 36, ""))
    rows = build(fx.page(btn))
    at = content_at(rows, (932, 533, 37, 36))
    assert len(at) == 1, f"{len(at)} labelable rows for one icon button"
    assert at[0]["role"] == "push button", "the nameless graphic won over the control"



def test_exact_echo_keeps_one_row():
    """link "Foo" > static "Foo" on the identical box is one thing on screen."""
    rows = build(fx.page(fx.node("link", 100, 100, 200, 20, "Foo",
                                 children=fx.node("static", 100, 100, 200, 20, "Foo"))))
    assert len(content_at(rows, (100, 100, 200, 20))) == 1


def test_overlapping_independent_things_both_survive():
    """A photo and a button on top of it are two things a human can judge separately. Dedup must
    not merge them just because their rectangles intersect."""
    body = (fx.node("section", 200, 200, 400, 400, "", cls="photo")
            + fx.node("push button", 210, 210, 40, 40, "Add to cart", name_from="attribute"))
    rows = build(fx.page(body))
    assert rows_at(rows, (200, 200, 400, 400)), "photo lost"
    assert content_at(rows, (210, 210, 40, 40)), "button lost"



def test_nested_frames_do_not_multiply_rows():
    """A DFP ad arrives as SafeFrame > creative > inner frame. Every layer re-enumerating the same
    pixels is what produced 17 rows on one rectangle and 9 content roots on one page."""
    inner = fx.node("section", 300, 300, 300, 250, "", cls="creative",
                    children=fx.node("image", 300, 300, 300, 250, "Advertisement"))
    xml = fx.frame_page(fx.node("document web", 300, 300, 300, 250, "", children=inner),
                        fx=300, fy=300, fw=300, fh=250)
    rows = build(xml)
    assert len(content_at(rows, (300, 300, 300, 250))) <= 1, \
        f"one ad produced {len(content_at(rows, (300, 300, 300, 250)))} labelable rows"


def test_full_viewport_frame_retargets_to_its_card():
    """A consent manager sizes its iframe to the whole viewport and centres a small card. Labeling
    the frame's own box shows the labeler the entire page, most of it not the frame's content."""
    card = fx.node("section", 634, 281, 707, 632, "", cls="sp_message",
                   children=fx.node("static", 700, 400, 500, 40, "We value your privacy"))
    rows = build(fx.frame_page(card))
    full = [r for r in rows_at(rows, (70, 114, 1850, 966)) if r["tier"] == "content"]
    assert not full, "the full-viewport frame shell was offered as the thing to judge"


def test_content_root_is_outermost_document_web_only():
    inner = fx.node("document web", 300, 300, 300, 250, "", children=fx.node("static", 310, 310, 100, 20, "ad"))
    root = ET.fromstring(fx.page(inner))
    descs, _ = ls.resolve_content_roots(root, "web")
    assert len(descs) == 1, f"{len(descs)} content roots for one page with one nested frame"



def test_paint_fields_are_consistent_everywhere():
    trees = [
        fx.page(fx.node("static", 100, 100, 200, 20, "hello")),
        fx.page(fx.node("section", 200, 200, 300, 300, "", cls="photo")),
        fx.frame_page(fx.node("section", 634, 281, 707, 632, "", cls="card")),
    ]
    for xml in trees:
        for r in build(xml):
            expect = ls.paint_status_of(r["skip_reason"])
            assert (r["paint_status"], r["represented_elsewhere"]) == expect, r
            if r["tier"] == "content":
                assert r["paint_status"] == ls.PAINT_YES, \
                    f"content row claims paint_status={r['paint_status']}"


def test_nothing_is_dropped_silently():
    """Every bbox-carrying node inside the root must appear as a row -- structural rows record WHY
    rather than vanishing, which is what makes a missed decision recoverable later."""
    body = (fx.node("static", 100, 100, 200, 20, "visible")
            + fx.node("static", -900, 100, 200, 20, "offscreen")
            + fx.node("static", 400, 400, 0, 0, "zero area")
            + fx.node("static", 500, 500, 100, 20, "sr only", cls="sr-only"))
    rows = build(fx.page(body))
    for text in ("visible", "offscreen", "zero area", "sr only"):
        assert any(r["text"] == text for r in rows), f"{text!r} vanished from the universe"
        for r in rows:
            if r["text"] == text and r["tier"] == "structural":
                assert r["skip_reason"], f"{text!r} demoted with no recorded reason"



def _win(bbox, active=False, kind="native"):
    return {"kind": kind, "bbox": list(bbox), "active": active, "provenance": "t", "children": 1}


def _desktop(*windows) -> str:
    """A desktop-frame with a gnome-shell actor per window plus the app windows themselves.
    `windows` are (client_rect, hull_rect_or_None, active)."""
    shell = "".join(
        f'<panel cp:screencoord="({h[0]}, {h[1]})" cp:size="({h[2]}, {h[3]})"/>'
        for _c, h, _a in windows if h)
    apps = "".join(
        f'<frame name="w{i}" st:active="{str(a).lower()}" '
        f'cp:screencoord="({c[0]}, {c[1]})" cp:size="({c[2]}, {c[3]})"/>'
        for i, (c, _h, a) in enumerate(windows))
    return (f'<desktop-frame {fx.NS}>'
            f'<application name="gnome-shell">{shell}</application>'
            f'<application name="Thunderbird">{apps}</application></desktop-frame>')


def _roots(xml_text):
    descs, _ = ls.resolve_content_roots(ET.fromstring(xml_text), "app")
    return [{k: v for k, v in d.items() if k != "node"} for d in descs]



def _content_root(xml: str):
    """What build_universe_from_root() actually walks -- resolve_content_roots picks it, and the
    region walk is handed the same node."""
    descs, _ = ls.resolve_content_roots(ET.fromstring(xml), "web")
    return descs[0]["node"]


def _geometryless_text(payload: str) -> str:
    """A Text-interface node with a payload and no rectangle of its own -- gedit's `text`, a mail
    body pane. fixtures.node() always emits geometry, so this one is spelled out."""
    return f'<text {fx._SHOWING}>{payload}</text>'


def test_row_nodes_enumerates_exactly_the_rows_that_get_built():
    """The invariant regions_from_tree() depends on, stated directly. It maps tree nodes onto rows
    by ZIPPING a walk against universe["rows"], so a node the walk yields that builds no row (or a
    row with no node) misattributes every row after it -- silently, because zip just stops early."""
    body = fx.node("section", 100, 100, 900, 600, children=_geometryless_text("a mail body " * 30))
    xml = fx.page(body)
    root = ET.fromstring(xml)
    rows = ls.build_universe_from_root(root, platform="web", screen_size=SCREEN,
                                       filename="t.xml")["rows"]
    walked = ls.row_nodes(_content_root(xml))
    root0 = [r for r in rows if r.get("root_index") == 0]
    assert len(walked) == len(root0), (
        f"{len(walked)} walked nodes vs {len(root0)} rows -- the zip in regions_from_tree "
        "would pair rows with the wrong elements")


def test_a_text_node_without_geometry_still_gets_walked():
    """The exact divergence: it adopts its enclosing box and becomes a row, while a walk filtered
    on screencoord/size skips it. 35 of 160 corpus captures diverged this way."""
    body = fx.node("section", 100, 100, 900, 600, children=_geometryless_text("a mail body " * 30))
    root = _content_root(fx.page(body))
    walked = {id(n) for n, _b in ls.row_nodes(root)}
    text_node = next(n for n in root.iter() if n.tag == "text")
    assert id(text_node) in walked, "the adopted-box text node was not enumerated"
    assert not ls._node_bbox(text_node), "fixture no longer models a geometry-less node"


def test_row_nodes_agrees_with_the_universe_on_a_plain_tree():
    body = (fx.node("section", 100, 100, 400, 40, "Heading")
            + fx.node("push button", 100, 200, 120, 32, "Save")
            + fx.node("static", 100, 300, 400, 20, "some prose"))
    xml = fx.page(body)
    rows = ls.build_universe_from_root(ET.fromstring(xml), platform="web", screen_size=SCREEN,
                                       filename="t.xml")["rows"]
    assert len(ls.row_nodes(_content_root(xml))) == len([r for r in rows
                                                         if r.get("root_index") == 0])


def test_screen_bbox_recovers_the_window_decoration():
    roots = _roots(_desktop(((107, 101, 860, 800), (97, 56, 880, 855), True)))
    assert roots[0]["screen_bbox"] == [97, 56, 880, 855], \
        "the 45px title bar the window manager draws is not in the app's own tree"


def test_screen_bbox_falls_back_to_the_client_rect():
    roots = _roots(_desktop(((100, 100, 400, 400), None, True)))
    assert roots[0]["screen_bbox"] == [100, 100, 400, 400], \
        "no compositor actor -- over-mask by the client rect rather than invent decoration"


def test_a_hull_that_swallows_another_window_is_refused():
    roots = _roots(_desktop(((100, 100, 200, 200), (0, 0, 1920, 1080), True),
                            ((800, 800, 100, 100), None, False)))
    assert roots[0]["screen_bbox"] == [100, 100, 200, 200]


def test_occluder_is_the_screen_rect_not_the_client_rect():
    roots = _roots(_desktop(((70, 64, 1850, 1016), (70, 27, 1850, 1053), False),
                            ((107, 101, 860, 800), (97, 56, 880, 855), True)))
    assert ls.occlusion_map(roots) == {0: [[97, 56, 880, 855]]}, \
        "occluding by the client rect leaves a title-bar-high band the window behind paints into"


def test_active_window_occludes_the_ones_it_overlaps():
    roots = [_win([70, 64, 1850, 1016]), _win([107, 101, 860, 800], active=True)]
    assert ls.occlusion_map(roots) == {0: [[107, 101, 860, 800]]}, \
        "the raised window must be recorded as painted over the one behind it"


def test_the_active_window_is_never_occluded_by_anything():
    roots = [_win([0, 0, 1920, 1080]), _win([100, 100, 400, 400], active=True)]
    assert 1 not in ls.occlusion_map(roots), "nothing is above the window the WM raised"


def test_no_active_window_means_no_ordering():
    roots = [_win([0, 0, 800, 600]), _win([100, 100, 400, 400])]
    assert ls.occlusion_map(roots) == {}


def test_every_window_active_means_no_ordering():
    roots = [_win([0, 0, 800, 600], active=True), _win([100, 100, 400, 400], active=True)]
    assert ls.occlusion_map(roots) == {}, "a tree that raises everything orders nothing"


def test_non_overlapping_windows_do_not_occlude():
    roots = [_win([0, 0, 400, 400]), _win([800, 0, 400, 400], active=True)]
    assert ls.occlusion_map(roots) == {}, "tiled windows hide nothing from each other"


def test_web_roots_never_stack():
    roots = [_win([0, 0, 800, 600], kind="web"), _win([100, 100, 400, 400], active=True, kind="web")]
    assert ls.occlusion_map(roots) == {}


def test_a_root_without_geometry_is_ignored():
    roots = [{"kind": "native", "bbox": None, "active": False, "provenance": "t", "children": 0},
             _win([100, 100, 400, 400], active=True)]
    assert ls.occlusion_map(roots) == {}, "a window with no rectangle can neither hide nor be hidden"



def _swin(bbox, stack, active=False):
    w = _win(bbox, active=active)
    w["stack"] = stack
    return w


def _desktop_stacked(*windows) -> str:
    """A desktop-frame whose windows carry `st:stackorder`. `windows` are (client_rect, stack)."""
    apps = "".join(
        f'<frame name="w{i}" st:stackorder="{s}" '
        f'cp:screencoord="({c[0]}, {c[1]})" cp:size="({c[2]}, {c[3]})"/>'
        for i, (c, s) in enumerate(windows))
    return (f'<desktop-frame {fx.NS}>'
            f'<application name="Thunderbird">{apps}</application></desktop-frame>')


def test_stack_order_resolves_a_three_window_pile():
    inbox = _swin([70, 64, 1850, 1016], stack=0)
    compose_a = _swin([200, 150, 700, 600], stack=1)
    compose_b = _swin([300, 200, 700, 600], stack=2)
    above = ls.occlusion_map([inbox, compose_a, compose_b])
    assert 2 not in above, "the top window has nothing above it -- its Send must survive"
    assert above.get(1) == [[300, 200, 700, 600]], "compose_a is covered only by compose_b"
    assert sorted(above.get(0)) == sorted([[200, 150, 700, 600], [300, 200, 700, 600]]), \
        "the inbox behind is covered by BOTH compose windows"


def test_stack_order_top_window_is_never_occluded():
    roots = [_swin([0, 0, 1920, 1080], stack=0), _swin([100, 100, 800, 700], stack=5)]
    assert 1 not in ls.occlusion_map(roots)


def test_stack_order_is_read_from_the_tree():
    roots = _roots(_desktop_stacked(((70, 64, 1850, 1016), 0), ((200, 150, 700, 600), 3)))
    assert [r.get("stack") for r in roots] == [0, 3], "st:stackorder must reach the descriptor"
    assert 1 not in ls.occlusion_map(roots) and 0 in ls.occlusion_map(roots)


def test_stack_order_supersedes_a_stale_active_bit():
    inbox = _swin([70, 64, 1850, 1016], stack=0, active=True)
    compose = _swin([200, 150, 700, 600], stack=1, active=False)
    above = ls.occlusion_map([inbox, compose])
    assert 1 not in above, "a higher stack index beats a stale active bit -- the raised window shows"
    assert above.get(0) == [[200, 150, 700, 600]]


def test_one_unstacked_window_falls_back_to_the_active_bit():
    a = _swin([70, 64, 1850, 1016], stack=0, active=False)
    b = _win([200, 150, 700, 600], active=True)
    assert ls.occlusion_map([a, b]) == {0: [[200, 150, 700, 600]]}, \
        "with one window unstacked, fall back to st:active (here the raised compose occludes the inbox)"



def _frag(*pieces) -> list:
    """One parent holding `pieces`, each (text, bbox). Returns the finished rows."""
    kids = "".join(
        f'<static name="{t}" cp:screencoord="({b[0]}, {b[1]})" cp:size="({b[2]}, {b[3]})"/>'
        for t, b in pieces)
    xml = (f'<desktop-frame {fx.NS}><application name="Chrome">'
           f'<frame name="w" cp:screencoord="(0, 0)" cp:size="(800, 600)">'
           f'<document-web cp:screencoord="(0, 0)" cp:size="(800, 600)">'
           f'<static cp:screencoord="(0, 0)" cp:size="(800, 400)">{kids}</static>'
           f'</document-web></frame></application></desktop-frame>')
    return ls.build_universe_from_root(ET.fromstring(xml), platform="web",
                                       screen_size=SCREEN, filename="t.xml")["rows"]


def _frag_of(rows, text):
    return next(r["capacity_fragment"] for r in rows if r["text"] == text)


def test_a_token_sharing_its_line_with_prose_is_a_fragment():
    rows = _frag(("rand", (100, 700, 40, 16)), ("() %", (150, 700, 30, 16)),
                 ("256", (200, 700, 24, 16)))
    assert _frag_of(rows, "256") is True


def test_a_value_alone_on_its_line_is_not_a_fragment():
    rows = _frag(("27546", (100, 500, 54, 22)), ("rand", (100, 700, 40, 16)))
    assert _frag_of(rows, "27546") is False, "a sibling on ANOTHER line says nothing about this one"


def test_overlap_is_a_predicate_not_a_measurement():
    rows = _frag(("label", (100, 700, 40, 16)), ("256", (200, 716, 24, 16)))
    assert _frag_of(rows, "256") is False
    rows = _frag(("label", (100, 700, 40, 16)), ("256", (200, 715, 24, 16)))
    assert _frag_of(rows, "256") is True, "one pixel of shared band is the same rendered line"


def test_an_untextured_sibling_does_not_make_a_fragment():
    rows = _frag(("", (100, 700, 40, 16)), ("256", (200, 700, 24, 16)))
    assert _frag_of(rows, "256") is False


def test_the_gate_only_looks_at_capacity_bound_rows():
    rows = _frag(("rand", (100, 700, 40, 16)), ("hello there", (200, 700, 60, 16)))
    assert _frag_of(rows, "hello there") is False, \
        "a free-capacity row is masked anyway; flagging it would only confuse the diagnostics"


KNOWN_GAPS = {
    "test_nested_geometry_duplicate_keeps_one_row":
        "echo requires an exact bbox match, so a padded control and its own label both get rows "
        "(costs a keystroke, never a lost region)",
}


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
