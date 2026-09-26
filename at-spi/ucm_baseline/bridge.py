"""Join UCM's matched DOM elements to this corpus's AT-SPI rows.

UCM's unit is a DOM node addressed by XPath. This corpus's unit is an accessibility node
addressed by (role, bbox, text, ordinal). Nothing is shared between those two names, so the join
has to go through the one property both sides can be made to agree on: the rectangle on screen.
dom_capture.py built half of it -- every DOM element's xpath and its screen rect, with the getXPath
walk copied from UCM so the strings match. This module is the other half.

THE MASKING SEMANTICS BEING MODELLED. UCM does not merely annotate a matched element: reveal.js
sets `display:none` on its children and paints a placeholder over it, so everything inside the
matched element disappears too. The right predicate is therefore geometric containment, not
identity -- an AT-SPI row is masked by UCM iff it sits inside a rectangle UCM matched. That also
makes the join robust to the two trees disagreeing about how many nodes a widget is.

WHY THERE IS A FALLBACK KEY. Reloading a saved snapshot re-parses it, and re-parsing normalises
invalid markup: ownCloud nests <a> inside <a> and GitLab nests <p> inside <p>, and the parser
un-nests both, which renumbers their xpaths. The elements still exist and still match the same
selectors, but their addresses moved (0.888 xpath agreement on 460_gitlab against 1.000 on
wikipedia and github). Dropping those would silently under-report the baseline, so an xpath that
does not resolve is retried on (tag, id, class, textPreview) -- accepted only when that key is
unique on both sides, so the recovery can never bind an element to the wrong rectangle.
"""
import json
import os

_AT_SPI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEOM_DIR = os.path.join(_AT_SPI, "dom_geom")

DEFAULT_EPS = 2


def geom_path(filename: str) -> str:
    return os.path.join(GEOM_DIR, filename.replace(".xml", "") + ".json")


def has_geom(filename: str) -> bool:
    return os.path.exists(geom_path(filename))


def _fallback_key(tag, node_id, cls, text) -> tuple:
    return (tag or "", node_id or "", (cls or "").strip(), (text or "")[:100].strip())


class CaptureGeometry:
    """One capture's xpath -> screen rectangle table, top frame only.

    Screen rectangles are read off the stored `screen` field where dom_capture.py wrote one, and
    recomputed from `rect` otherwise. 9 of the 115 captures have no stored screen field: 8 whose
    calibration fell short of quorum (4-7 votes against a floor of 8) and one whose calibration
    was inherited from sibling pages after the node-annotation pass had already run. Their
    geometry is not unknown, only unconfirmed -- the offset dom_capture.py guessed for all 8 is
    [70, 114], which is the offset 105 of the 107 confirmed captures measured independently, and
    the remaining two differ from it by a single pixel. Excluding them would drop every RocketChat
    channel and two ownCloud directories from the baseline for a doubt worth one pixel, so they
    are kept and `origin_source` records which ones rest on the guess.
    """

    CONSENSUS_ORIGIN = (70, 114)

    def __init__(self, filename: str):
        with open(geom_path(filename), encoding="utf-8") as f:
            geom = json.load(f)
        self.filename = filename
        cal = geom.get("calibration") or {}
        self.calibration = cal
        self.n_frames = geom.get("n_frames", 0)
        self.frames_ignored = max(0, self.n_frames - 1)

        top = next((fr for fr in geom["frames"] if fr.get("is_top")), None) or {}
        nodes = top.get("nodes", [])

        origin = cal.get("origin") or cal.get("best_guess")
        self.origin = tuple(origin) if origin else None
        self.origin_source = ("calibration" if cal.get("origin")
                              else "best_guess" if cal.get("best_guess") else "none")
        self.origin_agrees_with_corpus = (self.origin == self.CONSENSUS_ORIGIN
                                          if self.origin else None)
        self.dpr = cal.get("dpr") or top.get("dpr") or 1
        fx, fy = (top.get("frame_offset") or [0.0, 0.0])
        self.n_screen_stored = sum(1 for n in nodes if n.get("screen") is not None)
        self.n_screen_derived = 0

        self.by_xpath = {}
        counts = {}
        for node in nodes:
            if node.get("screen") is None and self.origin is not None and fx is not None:
                rx, ry, rw, rh = node["rect"]
                node["screen"] = [round((rx + fx) * self.dpr + self.origin[0]),
                                  round((ry + fy) * self.dpr + self.origin[1]),
                                  round(rw * self.dpr), round(rh * self.dpr)]
                self.n_screen_derived += 1
            xp = node.get("xpath")
            if xp and xp not in self.by_xpath:
                self.by_xpath[xp] = node
            key = _fallback_key(node.get("tag"), node.get("id"), node.get("cls"),
                                node.get("text"))
            counts[key] = counts.get(key, 0) + 1

        self.by_key = {}
        for node in nodes:
            key = _fallback_key(node.get("tag"), node.get("id"), node.get("cls"),
                                node.get("text"))
            if counts.get(key) == 1:
                self.by_key[key] = node

    def stats(self) -> dict:
        return {"origin": list(self.origin) if self.origin else None,
                "origin_source": self.origin_source,
                "origin_agrees_with_corpus": self.origin_agrees_with_corpus,
                "dpr": self.dpr, "n_frames": self.n_frames,
                "frames_ignored": self.frames_ignored,
                "screen_stored": self.n_screen_stored,
                "screen_derived": self.n_screen_derived,
                "dom_nodes_top_frame": len(self.by_xpath)}

    def rect_for(self, xpath: str, info: dict = None):
        """-> ([x, y, w, h] in AT-SPI screen pixels, how_it_resolved) or (None, "unresolved")."""
        node = self.by_xpath.get(xpath)
        if node is not None and node.get("screen") is not None:
            return node["screen"], "xpath"
        if info:
            key = _fallback_key(info.get("tagName"), info.get("id"), info.get("className"),
                                info.get("textPreview"))
            node = self.by_key.get(key)
            if node is not None and node.get("screen") is not None:
                return node["screen"], "key"
        return None, "unresolved"


def rectangles(match_result: dict, geo: CaptureGeometry) -> tuple:
    """-> (list of (rect, selector, tagName), stats).

    One entry per matched element per selector; duplicates across selectors are kept because the
    note on a masked row should be able to say which selector reached it.
    """
    rects, stats = [], {"matched_elements": 0, "resolved_xpath": 0, "resolved_key": 0,
                        "unresolved": 0, "zero_area": 0, "offscreen": 0}
    for entry in match_result.get("results", []):
        info_map = entry.get("element_info") or {}
        for xpath in entry.get("element_ids", []):
            stats["matched_elements"] += 1
            rect, how = geo.rect_for(xpath, info_map.get(xpath))
            if rect is None:
                stats["unresolved"] += 1
                continue
            stats["resolved_" + how] += 1
            x, y, w, h = rect
            if w <= 0 or h <= 0:
                stats["zero_area"] += 1
                continue
            rects.append(((int(x), int(y), int(w), int(h)),
                          entry.get("selector", ""), entry.get("tagName", "")))
    return rects, stats


def _contains(outer, inner, eps: int) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return (ix >= ox - eps and iy >= oy - eps
            and ix + iw <= ox + ow + eps and iy + ih <= oy + oh + eps)


def _overlap_frac(outer, inner) -> float:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    if iw <= 0 or ih <= 0:
        return 0.0
    w = min(ox + ow, ix + iw) - max(ox, ix)
    h = min(oy + oh, iy + ih) - max(oy, iy)
    return (w * h) / float(iw * ih) if w > 0 and h > 0 else 0.0


def verdicts(fu: dict, rects: list, eps: int = DEFAULT_EPS, mode: str = "contain",
             min_frac: float = 0.5) -> dict:
    """-> {elem_index: (verdict, note)} in the shape predict_page() returns.

    UCM is a DENYLIST: a selector marks a region untrusted and everything else stays visible. So
    the default here is "trusted", which is the opposite polarity from this project's allowlist
    labeler. That asymmetry is the substance of the comparison, not an artifact of the bridge --
    a denylist can only mask what someone thought to write a selector for.

    `mode="contain"` is the faithful one (see the masking semantics in the module docstring).
    `mode="frac"` exists so the containment predicate can be shown not to be carrying the result:
    it masks a row once `min_frac` of its area is covered, which is strictly more generous.
    """
    out = {}
    ordered = sorted(rects, key=lambda r: r[0][2] * r[0][3])
    for row in fu["rows"]:
        bbox = tuple(row["bbox"])
        hit = None
        for rect, selector, tag in ordered:
            ok = (_contains(rect, bbox, eps) if mode == "contain"
                  else _overlap_frac(rect, bbox) >= min_frac)
            if ok:
                hit = (selector, tag)
                break
        if hit is None:
            out[row["elem_index"]] = ("trusted", "ucm: no selector matched")
        else:
            out[row["elem_index"]] = ("untrusted", f"ucm[{hit[1]}] {hit[0]}"[:200])
    return out
