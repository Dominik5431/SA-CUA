#!/usr/bin/env python3
"""
dom_capture.py -- the third artifact, alongside the AT-SPI tree and the screenshot.

Why this exists: the corpus is meant to support comparing the at-spi labeling method against
untrusted-content-masking (UCM), and the two identify an element in incompatible ways. UCM's unit
is a DOM node addressed by XPath, produced by running a hand-written or LLM-proposed CSS selector
against the live page (see its pipeline/analyze_selectors.py). at-spi's unit is an accessibility
node addressed by (role, bbox, text, ordinal). Nothing is shared, so nothing can be joined --
UNLESS geometry is recorded for the DOM side, because the bbox is the only property both sides
can be made to agree on.

Hence two outputs per capture:

  dom/NNN_domain.html        `page.content()` -- the serialized DOM, byte-for-byte the artifact
                             UCM's own fetch step saves. Its run_pipeline.sh already supports
                             evaluating against a saved file (USE_LOCAL_SERVER), so this makes the
                             corpus replayable through UCM's pipeline with no live fetch and no
                             page drift. Sub-frames land in NNN_domain.frameK.html.
  dom_geom/NNN_domain.json   every element's XPath + rect, plus the calibration that maps a rect
                             onto the AT-SPI screen coordinate system.

The XPath function below is COPIED VERBATIM from UCM's analyze_selectors.py GET_ELEMENTS_JS. That
is the point: identical inputs produce identical xpath strings, so joining our geometry to their
selector results is a dict lookup rather than a fuzzy match. Do not "improve" it -- a better xpath
that disagrees with theirs is worse than a flawed one that agrees.

CALIBRATION. A DOM rect is viewport-relative CSS pixels; an AT-SPI bbox is screen pixels including
browser chrome. The mapping is screen = rect * dpr + origin, and `origin` is measured rather than
assumed: hardcoding "the viewport starts at y=112" would silently produce a whole corpus of
misaligned geometry the first time a toolbar changes height. calibrate() recovers it by matching
DOM nodes to AT-SPI nodes on (text, size) -- the pairs that agree vote on an offset, and the modal
offset wins. The vote count is stored, so a capture whose calibration rested on three coincidences
is distinguishable from one where four hundred elements agreed.
"""
from __future__ import annotations

import collections
import contextlib
import json
import re
import signal
from typing import Any

GEOM_SCHEMA = 1

_COLLECT_JS = r"""
() => {
    function getXPath(element) {
        if (element.id !== '') {
            return '//*[@id="' + element.id + '"]';
        }
        if (element === document.body) return '/html/body';
        if (element === document.documentElement) return '/html';
        if (!element.parentNode) return null;
        // A parent can be a ShadowRoot or DocumentFragment, which has no tagName -- the recursion
        // below would then throw on undefined.toLowerCase() and take the whole frame's walk down
        // with it. UCM's original has the same hole; it just rarely reaches one, because it
        // queries specific selectors rather than '*'.
        if (!element.tagName) return null;
        let ix = 0;
        const siblings = element.parentNode.childNodes;
        for (let i = 0; i < siblings.length; i++) {
            const sibling = siblings[i];
            if (sibling === element) {
                const parentPath = getXPath(element.parentNode);
                if (!parentPath) return null;
                return parentPath + '/' + element.tagName.toLowerCase() +
                       '[' + (ix + 1) + ']';
            }
            if (sibling.nodeType === 1 && sibling.tagName === element.tagName) {
                ix++;
            }
        }
        return null;
    }
    const out = [];
    let failed = 0;
    for (const el of document.querySelectorAll('*')) {
      // Per-element, so one hostile or exotic node costs one node rather than the whole frame.
      try {
        let r;
        try { r = el.getBoundingClientRect(); } catch (e) { continue; }
        // Zero-area nodes are kept: at-spi keeps its own (tier="structural", skip_reason=
        // "zero_area"), and dropping them here would make the two universes disagree on what
        // exists before any comparison starts. `visible` records the distinction instead.
        let style = null;
        try { style = window.getComputedStyle(el); } catch (e) { style = null; }
        out.push({
            xpath: getXPath(el),
            tag: el.tagName.toLowerCase(),
            id: el.id || '',
            cls: (typeof el.className === 'string' ? el.className : ''),
            text: (el.textContent || '').trim().substring(0, 100),
            // Own text only -- textContent includes every descendant's, so a <body> would carry
            // the whole page and match nothing. This is what the calibration votes on.
            own_text: Array.from(el.childNodes)
                           .filter(n => n.nodeType === 3)
                           .map(n => n.textContent)
                           .join('').trim().substring(0, 200),
            rect: [r.x, r.y, r.width, r.height],
            visible: !!(r.width > 0 && r.height > 0 && style &&
                        style.visibility !== 'hidden' && style.display !== 'none' &&
                        style.opacity !== '0'),
        });
      } catch (e) { failed++; }
    }
    return {
        nodes: out,
        n_failed: failed,
        url: document.location.href,
        title: document.title,
        dpr: window.devicePixelRatio,
        scroll: [window.scrollX, window.scrollY],
        viewport: [window.innerWidth, window.innerHeight],
        doc_size: [document.documentElement.scrollWidth,
                   document.documentElement.scrollHeight],
    };
}
"""


class DomCaptureTimeout(RuntimeError):
    pass


@contextlib.contextmanager
def _deadline(seconds: int):
    """Hard wall-clock cap on a block of Playwright calls.

    Playwright's `frame.evaluate` and `frame.content` accept NO timeout, and against a browser
    that has half-died they never return -- measured the expensive way: a capture run sat in
    do_epoll_wait for 4.7 hours after Chrome crashed and was relaunched, having logged its
    screenshot and then nothing. Every other stage of a capture is bounded, so this was the only
    place a run could hang indefinitely.

    SIGALRM rather than a worker thread because sync Playwright is bound to the thread that
    created it -- a watchdog thread could not cancel the call anyway, only observe it.
    """
    def _fire(signum, frame):
        raise DomCaptureTimeout(f"DOM capture exceeded {seconds}s")

    old = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def capture_dom(page, max_frames: int = 12, timeout_s: int = 90,
                probe_ms: int = 5000) -> tuple[dict[str, str], dict[str, Any]]:
    """-> ({frame_label: html}, geometry_dict).

    Walks the top document and every child frame. Frames matter here for the same reason
    resolve_content_roots() goes out of its way to find every document-web node: a cross-origin
    iframe is where untrusted content actually lives, and a top-document-only capture would drop
    exactly the surfaces this corpus exists to label.
    """
    html_by_frame: dict[str, str] = {}
    frames_out: list[dict] = []

    try:
        if page.is_closed() or not page.context.browser.is_connected():
            raise DomCaptureTimeout("page/browser is not connected")
    except DomCaptureTimeout:
        raise
    except Exception:
        pass

    for n, frame in enumerate(page.frames[:max_frames]):
        is_top = frame is page.main_frame
        label = "" if is_top else f"frame{n}"
        try:
            frame.wait_for_function("() => true", timeout=probe_ms)
            data = frame.evaluate(_COLLECT_JS)
        except Exception as e:
            frames_out.append({"label": label, "url": getattr(frame, "url", None),
                               "is_top": is_top, "frame_offset": [0.0, 0.0] if is_top else None,
                               "error": f"{type(e).__name__}: {e}", "nodes": []})
            continue
        try:
            html_by_frame[label] = frame.content()
        except Exception:
            pass

        offset = [0.0, 0.0]
        if not is_top:
            try:
                box = frame.frame_element().bounding_box()
                if box:
                    offset = [box["x"], box["y"]]
            except Exception:
                offset = None

        frames_out.append({
            "label": label, "url": data["url"], "title": data["title"],
            "is_top": is_top, "frame_offset": offset,
            "dpr": data["dpr"], "scroll": data["scroll"],
            "viewport": data["viewport"], "doc_size": data["doc_size"],
            "n_failed": data.get("n_failed", 0),
            "nodes": data["nodes"],
        })

    top = next((f for f in frames_out if f.get("is_top")), None)
    geom = {
        "schema": GEOM_SCHEMA,
        "url": top["url"] if top else None,
        "dpr": top["dpr"] if top else None,
        "viewport": top["viewport"] if top else None,
        "n_frames": len(frames_out),
        "n_nodes": sum(len(f["nodes"]) for f in frames_out),
        "calibration": None,
        "frames": frames_out,
    }
    return html_by_frame, geom


_ATSPI_NODE_RE = re.compile(r"<([a-z-]+)\s([^>]*?)/?>")
_NAME_RE = re.compile(r'name="([^"]*)"')
_COORD_RE = re.compile(r'cp:screencoord="\((-?\d+), (-?\d+)\)"')
_SIZE_RE = re.compile(r'cp:size="\((-?\d+), (-?\d+)\)"')


def _atspi_nodes(xml: str) -> list[tuple[str, int, int, int, int]]:
    """(name, x, y, w, h) for every named, sized node. Deliberately regex, not ElementTree: this
    runs inside the capture loop on a multi-megabyte tree and only needs four attributes."""
    out = []
    for m in _ATSPI_NODE_RE.finditer(xml):
        attrs = m.group(2)
        name = _NAME_RE.search(attrs)
        coord = _COORD_RE.search(attrs)
        size = _SIZE_RE.search(attrs)
        if not (name and coord and size):
            continue
        text = name.group(1).strip()
        if not text:
            continue
        out.append((text, int(coord.group(1)), int(coord.group(2)),
                    int(size.group(1)), int(size.group(2))))
    return out


def cluster_votes(votes: collections.Counter, radius: int = 2) -> list[tuple[tuple[int, int], int]]:
    """Merges offsets that are within `radius` px of each other, returning [(center, votes), ...]
    strongest first.

    Subpixel layout means one true offset arrives as a little cloud: stackoverflow voted
    (70,113)x17, (69,113)x15, (70,114)x4 -- one answer worth 36, which an unclustered ranking
    reads as a 17-vs-15 near-tie and rejects. Genuine rivals are nowhere near each other (the same
    page also produced (82,-47300)), so a small radius separates rounding noise from disagreement
    without merging anything that actually disagrees. The center is the vote-weighted mean, so the
    heavily-supported member dominates.
    """
    cell = max(radius, 1)
    grid: dict[tuple[int, int], list[int]] = collections.defaultdict(list)
    clusters: list[dict] = []
    for (ox, oy), n in votes.most_common():
        cx, cy = ox // cell, oy // cell
        hit = None
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for ci in grid.get((gx, gy), ()):
                    c = clusters[ci]
                    if abs(ox - c["seed"][0]) <= radius and abs(oy - c["seed"][1]) <= radius:
                        hit = ci
                        break
                if hit is not None:
                    break
            if hit is not None:
                break
        if hit is not None:
            clusters[hit]["members"].append(((ox, oy), n))
            clusters[hit]["total"] += n
        else:
            grid[(cx, cy)].append(len(clusters))
            clusters.append({"seed": (ox, oy), "members": [((ox, oy), n)], "total": n})
    out = []
    for c in clusters:
        tx = sum(p[0] * n for p, n in c["members"])
        ty = sum(p[1] * n for p, n in c["members"])
        out.append(((round(tx / c["total"]), round(ty / c["total"])), c["total"]))
    out.sort(key=lambda t: -t[1])
    return out


def calibrate(geom: dict, xml: str, min_votes: int = 8, min_margin: int = 3,
              prior: collections.Counter | None = None) -> dict:
    """Recovers the DOM-rect -> AT-SPI-screen offset by consensus, and annotates `geom` in place.

    Every DOM node with own text is matched against AT-SPI nodes carrying the same text AND the
    same rendered size (within a pixel, after dpr scaling). Size agreement is what makes this
    trustworthy: text alone matches a nav link to its identical twin in the footer and votes for a
    wrong offset, whereas text+size pins the pair. Each surviving pair votes for one integer
    (dx, dy); the modal vote wins.

    `prior` is a Counter of origins already established during this run, and it is what makes a
    sparse page survivable. The origin is a property of the BROWSER WINDOW, not of the page -- the
    window does not move between sites -- so a page offering only a handful of matchable elements
    can borrow the run's established origin instead of going uncalibrated. Measured: github.com
    voted 58-7 for (70, 114) while npmjs managed only 13-8 for the same offset, and a page thinner
    than npmjs would have had nothing. Note (70, 114), not (0, 112): x=70 is the Ubuntu dock. That
    is the whole argument for measuring rather than assuming.

    Sets geom["calibration"] to {"origin", "source", "votes", "runner_up", "total_candidates",
    "agreement"} or, on failure, {"origin": None, "reason": ...}. `source` is "page" when this
    capture established the origin itself and "inherited" when it took the run's. Also writes a
    "screen" bbox onto every node it can place. Never raises -- an uncalibrated capture is still a
    usable DOM capture, and the caller has a page to finish.
    """
    top = next((f for f in geom.get("frames", []) if f.get("is_top")), None)
    if top is None or not top.get("nodes"):
        geom["calibration"] = {"origin": None, "reason": "no top frame captured"}
        return geom

    dpr = top.get("dpr") or 1.0
    atspi = _atspi_nodes(xml)
    if not atspi:
        geom["calibration"] = {"origin": None, "reason": "no sized+named nodes in the a11y tree"}
        return geom

    by_text: dict[str, list] = collections.defaultdict(list)
    for text, x, y, w, h in atspi:
        by_text[text].append((x, y, w, h))

    votes: collections.Counter = collections.Counter()
    candidates = 0
    for node in top["nodes"]:
        text = node.get("own_text") or ""
        if len(text) < 2:
            continue
        hits = by_text.get(text)
        if not hits:
            continue
        rx, ry, rw, rh = node["rect"]
        sw, sh = rw * dpr, rh * dpr
        if sw < 1 or sh < 1:
            continue
        candidates += 1
        agreed = {(round(ax - rx * dpr), round(ay - ry * dpr))
                  for ax, ay, aw, ah in hits
                  if abs(aw - sw) <= 1 and abs(ah - sh) <= 1}
        votes.update(agreed)

    def _inherit(reason: str, best_guess=None) -> dict:
        """Fall back to the run's established origin, or record why we could not."""
        if prior:
            (px, py), support = prior.most_common(1)[0]
            if support >= 3:
                return {"origin": [px, py], "source": "inherited", "votes": 0,
                        "runner_up": 0, "total_candidates": candidates,
                        "agreement": None, "dpr": dpr,
                        "inherited_from_n_pages": support, "page_reason": reason}
        out = {"origin": None, "source": None, "reason": reason,
               "total_candidates": candidates}
        if best_guess is not None:
            out["best_guess"] = list(best_guess)
        return out

    if not votes:
        geom["calibration"] = _inherit("no text+size agreement")
        return geom

    ranked = cluster_votes(votes)
    (ox, oy), best = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    if best < min_votes:
        geom["calibration"] = _inherit(f"only {best} votes (need {min_votes})", (ox, oy))
        return geom
    if best - runner_up < min_margin:
        geom["calibration"] = _inherit(
            f"margin too thin ({best} vs {runner_up}, need +{min_margin})", (ox, oy))
        return geom

    geom["calibration"] = {
        "origin": [ox, oy], "source": "page", "votes": best, "runner_up": runner_up,
        "total_candidates": candidates,
        "agreement": round(best / max(candidates, 1), 3),
        "dpr": dpr,
    }
    for frame in geom["frames"]:
        fx, fy = (frame.get("frame_offset") or [None, None])
        if fx is None:
            continue
        for node in frame["nodes"]:
            rx, ry, rw, rh = node["rect"]
            node["screen"] = [round((rx + fx) * dpr + ox), round((ry + fy) * dpr + oy),
                              round(rw * dpr), round(rh * dpr)]
    return geom


def write_dom_artifacts(dom_dir: str, geom_dir: str, stem: str,
                        html_by_frame: dict[str, str], geom: dict) -> tuple[int, int]:
    """-> (html_bytes_total, geom_bytes). `stem` is the shared 'NNN_domain' basename."""
    import os
    os.makedirs(dom_dir, exist_ok=True)
    os.makedirs(geom_dir, exist_ok=True)
    html_bytes = 0
    for label, html in html_by_frame.items():
        name = f"{stem}.html" if not label else f"{stem}.{label}.html"
        data = html.encode("utf-8")
        with open(os.path.join(dom_dir, name), "wb") as f:
            f.write(data)
        html_bytes += len(data)
    blob = json.dumps(geom, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(os.path.join(geom_dir, f"{stem}.json"), "wb") as f:
        f.write(blob)
    return html_bytes, len(blob)
