#!/usr/bin/env python3
"""
mask_ucm_merged.py -- render the 34 UCM captures (500-533) through the LIVE SA masking path with the
per-site merged rule set, and check FN against the sites' own untrusted markup.

Run: python at-spi/mask_ucm_merged.py [--rules <comma list>] [--out at-spi/masked]

Masking: mm_agents.sa_agent.masking.build_mask, exactly what a SA episode sees -- default-deny,
RuleCache over the rules dirs in the given order (default: the sa_qwen38_v3 UCM lanes' list) with the
UCM captures pinned to --ucm_rules (default llm_rules_container_v19, as run.py --sa_ucm_rules_dir),
which for a UCM host unions every recorded view of the site (_tied_captures -> _app_union).

FN oracle (independent of every rule set): the saved DOM (dom/NNN.html). Every element with
data-untrusted="true" -- the sites' own hand-authored label -- and every text-bearing element below
it (the nearest data-untrusted / data-trusted ancestor decides), joined to dom_geom/ by the exact
xpath dom_capture.py records, so the geometry is the DOM's real layout. NOT the reveal-visible
class: reveal.js puts that on every [data-untrusted-element], data-trusted ones included.
A node is a LEAK when more than LEAK_FRAC of its INK (original-screenshot pixels that differ from the
box's background colour -- see _visible_ink) is unredacted and at least MIN_INK_PX ink pixels show,
EDGE when some ink shows below that. Boxes are clipped to the screenshot; a node wholly below the
fold / outside the page viewport, or with no ink at all, is not checked. Ink inside an open dialog
panel (`modal-content`) is not counted against page content BEHIND it -- the DOM has no z-order,
and without this every dialog's own form reads as a leak of the page it covers. The dimmed
backdrop around the panel IS counted: that is page content the planner can see.

Outputs (in --out):
  <capture>.png                    masked screenshot as the planner sees it
  ucm_merged_fn_report.json        per capture: rules used, masked %, checked/leak/edge nodes
  ucm_merged_fn_report.txt         the same as a table
"""
import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from mm_agents.sa_agent import masking

DEFAULT_RULES = ("llm_rules_container_v15,llm_rules_container_v17_suites_or,"
                 "llm_rules_container_overrides,llm_rules_container_v19")
DEFAULT_UCM_RULES = "llm_rules_container_v19"
LEAK_FRAC = 0.10
MARK = "reveal-visible"
_COORD = re.compile(r"\((-?\d+),\s*(-?\d+)\)")


def _attr(el, local):
    for k, v in el.attrib.items():
        if k == local or k.endswith("}" + local) or k.endswith(":" + local):
            return v
    return None


def _bbox(el):
    c, s = _attr(el, "screencoord"), _attr(el, "size")
    if not c or not s:
        return None
    (x, y), (w, h) = map(int, _COORD.search(c).groups()), map(int, _COORD.search(s).groups())
    return (x, y, w, h) if w > 0 and h > 0 else None


INK_DELTA = 60
MIN_INK_PX = 25


def _visible_ink(bits, img, box, clip=None, occluders=()):
    """(shown ink / all ink, shown ink px) inside the clipped box, or (None, 0).

    Box AREA is the wrong unit: a DOM block box spans the column, the text in it is a fraction of
    that, and the mask is fitted to the text -- so an area test reports the white margin right of a
    fully-redacted name as a leak. Ink is what can be read: pixels of the ORIGINAL screenshot that
    differ from the box's dominant (background) colour. A leak is shown ink."""
    H, W = len(bits), len(bits[0])
    x, y, w, h = box
    x0, y0, x1, y1 = max(0, int(x)), max(0, int(y)), min(W, int(x + w)), min(H, int(y + h))
    if clip:
        cx, cy, cw_, ch_ = clip
        x0, y0, x1, y1 = max(x0, int(cx)), max(y0, int(cy)), min(x1, int(cx + cw_)), min(y1, int(cy + ch_))
    if x1 <= x0 or y1 <= y0:
        return None, 0
    crop = img.crop((x0, y0, x1, y1))
    px = list(crop.getdata())
    bg = max(set(px), key=px.count) if len(px) < 40000 else crop.quantize(8).convert("RGB").getcolors(64)
    if isinstance(bg, list):
        bg = max(bg)[1]
    ink = shown = 0
    cw = x1 - x0
    occ = [(int(a), int(b), int(a + c), int(b + d)) for a, b, c, d in occluders]
    for i, c in enumerate(px):
        xx, yy = x0 + i % cw, y0 + i // cw
        if any(a <= xx < c_ and b <= yy < d for a, b, c_, d in occ):
            continue
        if abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2]) > INK_DELTA:
            ink += 1
            if not bits[yy][xx]:
                shown += 1
    if ink == 0:
        return None, 0
    return shown / ink, shown


def _xpath(el, memo):
    """UCM's / dom_capture.py's getXPath, reproduced on the parsed saved DOM so the untrusted set
    joins dom_geom by exact xpath string (id short-circuit, 1-based same-tag sibling index)."""
    if el in memo:
        return memo[el]
    if el.get("id"):
        x = '//*[@id="%s"]' % el.get("id")
    elif el.tag == "body":
        x = "/html/body"
    elif el.tag == "html":
        x = "/html"
    else:
        par = el.getparent()
        if par is None:
            x = None
        else:
            pp = _xpath(par, memo)
            ix = sum(1 for sib in par if sib is not el and isinstance(sib.tag, str) and sib.tag == el.tag
                     and list(par).index(sib) < list(par).index(el))
            x = None if pp is None else f"{pp}/{el.tag}[{ix + 1}]"
    memo[el] = x
    return x


def _untrusted_xpaths(capture):
    """xpath -> 'mark' | 'inside' for every element that carries data-untrusted="true" or sits
    below one (nearest data-untrusted / data-trusted ancestor decides). None if no saved DOM."""
    import lxml.html
    p = os.path.join(HERE, "dom", capture.replace(".xml", ".html"))
    if not os.path.exists(p):
        return None
    doc = lxml.html.fromstring(open(p, encoding="utf-8", errors="ignore").read())
    out, memo = {}, {}

    def walk(el, state):
        if not isinstance(el.tag, str):
            return
        mine = None
        if el.get("data-untrusted") == "true":
            mine = "mark"
        elif el.get("data-trusted") == "true":
            mine = "trusted"
        st = mine if mine else ("inside" if state in ("mark", "inside") else state)
        if st in ("mark", "inside"):
            x = _xpath(el, memo)
            if x:
                out[x] = "mark" if mine == "mark" else "inside"
        for ch in el:
            walk(ch, st)
    walk(doc, None)
    return out


def _dom_nodes(capture):
    """Untrusted DOM nodes with real layout: marked ones, and text-bearing ones inside them."""
    untrusted = _untrusted_xpaths(capture)
    gp = os.path.join(HERE, "dom_geom", capture.replace(".xml", ".json"))
    if untrusted is None or not os.path.exists(gp):
        return None, 0
    g = json.load(open(gp))
    cal = g.get("calibration") or {}
    ox, oy = (cal.get("origin") or (0, 0))
    dpr = cal.get("dpr") or 1
    out, joined = [], 0

    def scr(n):
        if n.get("screen"):
            return tuple(n["screen"])
        rx, ry, rw, rh = n["rect"]
        return (rx * dpr + ox, ry * dpr + oy, rw * dpr, rh * dpr)
    for fr in g.get("frames", []):
        if not fr.get("is_top", True):
            continue
        vw, vh = fr.get("viewport") or g.get("viewport") or (10 ** 5, 10 ** 5)
        clip = (ox, oy, vw * dpr, vh * dpr)
        occl = [(n["xpath"], scr(n)) for n in fr.get("nodes", [])
                if n.get("visible") and "modal-content" in (n.get("cls") or "").split()]
        for n in fr.get("nodes", []):
            kind = untrusted.get(n.get("xpath"))
            if not kind:
                continue
            joined += 1
            if not n.get("visible"):
                continue
            sx, sy, sw, sh = scr(n)
            text = (n.get("own_text") or "").strip()
            if sw <= 0 or sh <= 0 or not (kind == "mark" or text):
                continue
            out.append({"src": kind, "role": n.get("tag"), "text": (text or (n.get("text") or "").strip())[:80],
                        "box": (sx, sy, sw, sh), "clip": clip,
                        "occluders": [b for xp, b in occl if not n["xpath"].startswith(xp)]})
    return out, len(untrusted) - joined


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules", default=DEFAULT_RULES)
    ap.add_argument("--ucm_rules", default=DEFAULT_UCM_RULES,
                    help="pin for the ucm captures ('' = --rules order decides), as --sa_ucm_rules_dir")
    ap.add_argument("--out", default=os.path.join(HERE, "masked"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    man = json.load(open(os.path.join(HERE, "manifest.json")))
    rows = man if isinstance(man, list) else man.get("rows", man.get("captures", []))
    rows = sorted((r for r in rows if 500 <= int(r.get("index", -1)) <= 533), key=lambda r: int(r["index"]))

    rc = masking.RuleCache(rules_dir=a.rules, cache_dir=None, strict=True,
                           category_dirs={"ucm": a.ucm_rules})
    cfg = masking.MaskConfig(mode="deny", rule_caching=True, propose_model=None,
                             on_unknown_domain="abort", on_native_app="app_rules", rules=rc)
    report, lines = [], []
    tot = {"checked": 0, "leak": 0, "edge": 0}
    for r in rows:
        cap = r["filename"]
        tree = open(os.path.join(HERE, "xml", cap), encoding="utf-8").read()
        png = open(os.path.join(HERE, "screenshots", r["screenshot_filename"]), "rb").read()
        m = masking.build_mask({"accessibility_tree": tree, "screenshot": png}, r["url"], cfg)
        with open(os.path.join(a.out, cap.replace(".xml", ".png")), "wb") as f:
            f.write(m.png)
        bits = m.result.bits
        from PIL import Image
        orig = Image.open(os.path.join(HERE, "screenshots", r["screenshot_filename"])).convert("RGB")
        scope = r["url"].split("/")[2]
        rules, used, how = rc.for_scope(scope, r["url"])

        nodes, unjoined = _dom_nodes(cap)
        nodes = nodes or []
        checked, leaks, edges = 0, [], []
        for n in nodes:
            frac, npx = _visible_ink(bits, orig, n["box"], n["clip"], n["occluders"])
            if frac is None:
                continue
            checked += 1
            if frac > LEAK_FRAC and npx >= MIN_INK_PX:
                leaks.append({**{k: v for k, v in n.items() if k not in ("clip", "occluders")}, "shown": round(frac, 3)})
            elif frac > 0:
                edges.append({**{k: v for k, v in n.items() if k not in ("clip", "occluders")}, "shown": round(frac, 3)})
        rep = {"capture": cap, "scope": scope, "how": how, "rules_from": used, "n_rules": len(rules or []),
               "masked_pct": round(m.stats.get("masked_pct", m.stats.get("masked_area_pct", 0)) or 0, 1),
               "untrusted_nodes_checked": checked, "leak": leaks, "edge": edges,
               "untrusted_xpaths_not_in_geom": unjoined}
        report.append(rep)
        tot["checked"] += checked; tot["leak"] += len(leaks); tot["edge"] += len(edges)
        lines.append(f"{cap[:-4]:34} {scope:15} rules={rep['n_rules']:3}  masked={rep['masked_pct']:5.1f}%  "
                     f"untrusted-checked={checked:4}  LEAK={len(leaks):2}  edge={len(edges):2}  unjoined={unjoined}")
        for lk in leaks:
            lines.append(f"      LEAK {lk['src']:4} {lk['role']:12} shown={lk['shown']:.0%} box={lk['box']} {lk['text']!r}")
    lines.append(f"\nTOTAL untrusted nodes checked={tot['checked']}  LEAK={tot['leak']}  edge={tot['edge']}"
                 f"  (leak = >{LEAK_FRAC:.0%} of the ink unredacted, >= {MIN_INK_PX}px)  rules={a.rules}")
    json.dump({"rules": a.rules, "leak_frac": LEAK_FRAC, "total": tot, "captures": report},
              open(os.path.join(a.out, "ucm_merged_fn_report.json"), "w"), indent=1)
    open(os.path.join(a.out, "ucm_merged_fn_report.txt"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
