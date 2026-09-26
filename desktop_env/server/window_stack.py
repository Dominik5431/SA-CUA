"""window_stack.py -- the a11y server's window z-order helpers, kept apart from main.py so they are
unit-testable without an X server (main.py connects to X at import).

The pieces:
  * `window_stacking_order()` reads the WM's EWMH `_NET_CLIENT_LIST_STACKING` -- the total
    bottom-to-top order AT-SPI itself does not expose (getMDIZOrder/getLayer are constants). It is
    the ONLY part that touches X, and only when CALLED (importing this module opens no display).
  * `attach_stack_order()` stamps `st:stackorder` onto each top-level window element by matching its
    client rectangle to an X window, so a downstream consumer (labeling_store.occlusion_map) can
    resolve overlap between ANY two windows rather than only against the single `st:active` one.
  * `iou()` / `parse_coord_pair()` are the pure geometry the match rests on.

See the window-stacking occlusion notes in `at-spi/labeling_store.py::occlusion_map`.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import Xlib
from Xlib import X, display

logger = logging.getLogger("desktopenv.server.window_stack")


def parse_coord_pair(s: "Optional[str]") -> "Optional[tuple]":
    """`"(107, 101)"` -> `(107, 101)`; None on anything unparseable."""
    if not s:
        return None
    try:
        a, b = s.strip().lstrip("(").rstrip(")").split(",")
        return int(float(a)), int(float(b))
    except Exception:
        return None


def iou(a: "tuple", b: "tuple") -> float:
    """Intersection-over-union of two (x, y, w, h) boxes -- 1.0 when identical, 0.0 when they miss.

    IoU, not intersection-over-smaller: a fullscreen window (the root / VNC framebuffer often sits at
    the bottom of the stack at 1920x1080) CONTAINS every app window, so intersection-over-smaller
    returns 1.0 for it against everything and it wins every match -- observed live, where it pinned
    every window to the fullscreen background at stack 0. IoU divides by the union, so that oversized
    box scores low against a smaller app window while the X frame that actually bounds the window --
    the same size give or take a title bar -- scores near 1.0. The ~45px decoration offset between an
    AT-SPI client rect and its X frame rect costs only a few percent of IoU on a normal window, so
    the correct frame still wins comfortably."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def window_stacking_order() -> "List[dict]":
    """Bottom-to-top stacking order of managed top-level windows, from EWMH
    `_NET_CLIENT_LIST_STACKING`. Each entry is `{"stack": i, "bbox": (x,y,w,h), "name": str|None}`
    where a HIGHER `stack` is nearer the TOP of the pile. `[]` on any failure (no X, no WM,
    override-redirect only) -- the tree then carries no stackorder and the consumer falls back to the
    single `st:active` bit.

    Geometry is resolved to absolute screen coordinates by summing frame offsets up to the root, so a
    reparenting WM's decoration frame is accounted for. Read once per /accessibility call.
    """
    d = None
    try:
        d = display.Display()
        root = d.screen().root
        atom = d.intern_atom("_NET_CLIENT_LIST_STACKING")
        prop = root.get_full_property(atom, X.AnyPropertyType)
        if prop is None or not len(prop.value):
            return []
        out: List[dict] = []
        for stack_index, wid in enumerate(prop.value):
            try:
                win = d.create_resource_object("window", wid)
                geom = win.get_geometry()
                x, y = geom.x, geom.y
                cur = win
                for _ in range(16):
                    parent = cur.query_tree().parent
                    if parent is None or parent.id == root.id:
                        break
                    pg = parent.get_geometry()
                    x += pg.x
                    y += pg.y
                    cur = parent
                try:
                    name = win.get_wm_name() or None
                except Exception:
                    name = None
                out.append({"stack": stack_index,
                            "bbox": (int(x), int(y), int(geom.width), int(geom.height)),
                            "name": name})
            except Xlib.error.XError:
                continue
        return out
    except Exception as e:
        logger.warning("could not read _NET_CLIENT_LIST_STACKING: %s", e)
        return []
    finally:
        if d is not None:
            try:
                d.close()
            except Exception:
                pass


def attach_stack_order(xml_node, stacking: "List[dict]", *, st_ns: str, cp_ns: str) -> None:
    """Stamp `st:stackorder` on each top-level window element by matching its client bbox to the EWMH
    stacking list (best IoU; a title match breaks a tie). Higher value = nearer the top. A window
    that finds no match is left without the attribute -- the consumer then falls back to `st:active`
    for it. Never raises: a bad match is at worst the pre-existing single-bit behaviour.

    `xml_node` is the `desktop-frame`; its children are `application` elements whose children are the
    top-level windows. `st_ns`/`cp_ns` are the state/component namespace URIs (from main.py's map).
    """
    if not stacking:
        return

    def _bbox_of(el) -> "Optional[tuple]":
        sc = parse_coord_pair(el.get("{{{:}}}screencoord".format(cp_ns)))
        sz = parse_coord_pair(el.get("{{{:}}}size".format(cp_ns)))
        if sc is None or sz is None:
            return None
        return (sc[0], sc[1], sz[0], sz[1])

    for app_el in xml_node:
        if str(app_el.tag).split("}")[-1] != "application":
            continue
        for win_el in app_el:
            b = _bbox_of(win_el)
            if b is None:
                continue
            best, best_score = None, 0.0
            for s in stacking:
                score = iou(b, s["bbox"])
                nm = win_el.get("name") or ""
                if s.get("name") and nm and s["name"] == nm:
                    score += 0.01
                if score > best_score:
                    best, best_score = s, score
            if best is not None and best_score > 0.1:
                win_el.set("{{{:}}}stackorder".format(st_ns), str(best["stack"]))
