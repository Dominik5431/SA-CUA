#!/usr/bin/env python3
"""
mask_screenshot.py -- turns a labeling source's verdicts into an actual masked screenshot: every
row the source marks UNTRUSTED gets its bbox blacked out (or blurred) directly on the real
screenshot pixels. This is Phase 2 of the plan's live masking mechanism made concrete on this
corpus (see the plan's "Pixels" bullet under Design section 4): "black-box (or blur) the matched
node's own bbox region directly on the already-captured screenshot image before it's attached to
the prompt... no re-render". Trusted regions and anything the source has no row for at all are
left untouched.

Unlike review_benchmark.py (a manual spot-check tool: samples a handful of screenshots, shows
colored boxes + a text report for a human to eyeball), this script processes every row of every
selected file and produces the actual redacted image an agent would see -- no sampling, no
"risky-only" filtering, nothing left half-drawn for a human to judge.

Works with any source labelers/*.py has produced (labels/<source>.json) that covers the file(s)
you want to mask -- pick with --source (default: selector_llm_proposed, the real non-oracle
Phase-1 LLM output; see labelers/selector_propose.py). Since real Phase-2 masking needs to reach
the full raw AT-SPI tree (plan finding 5/6: interactable-only extraction misses ~98% of a page,
including most UGC prose), a full-tree source is what you actually want: selector_llm_proposed,
selector_allowlist_fulltree (oracle ceiling), or selector_denylist_fulltree. Sources that only
cover the interactable subset (rule_based, llm, ground_truth) still work but will leave most of
the page's actual UGC prose unmasked, since they never had a row for it in the first place --
expected, not a bug in this script.

selector_llm_proposed is run-indexed (labels/selector_llm_proposed/, one file per proposal run --
see at-spi/label_store.py), so a file can carry several models' proposals AND several runs of one
model. With more than one run present the NEWEST is masked and the choice is announced on stderr;
pass --run_id to mask a specific one. If a selected file still has rows from more than one model,
--model is required to disambiguate (otherwise the file is skipped with a warning), same as
review_benchmark.py.

Usage:
  python at-spi/mask_screenshot.py --file 006_unix.stackexchange.com.xml
  python at-spi/mask_screenshot.py --source selector_llm_proposed --list_runs
  python at-spi/mask_screenshot.py --source selector_llm_proposed --model "Qwen/Qwen3.6-35B-A3B" --file 006
  python at-spi/mask_screenshot.py --source selector_llm_proposed --run_id 20260819T143422Z --file 006
  python at-spi/mask_screenshot.py --source selector_allowlist_fulltree     # every file the source covers
  python at-spi/mask_screenshot.py --style blur --file 006

Outputs:
  at-spi/masked/{file}.png -- one screenshot per selected file, untrusted regions redacted, with
    numbered query regions drawn on top (--no_regions to suppress).
  at-spi/masked/{file}.regions.json -- the same regions as a manifest: id, bbox, container label,
    and how many masked elements each owns, for a planner to address by number.
  A short per-file summary printed to stdout (rows masked / total rows / approx. masked area %).
"""
import argparse
import glob
import json
import math
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageChops, ImageDraw, ImageFilter


import label_store
from labeling_store import _union_area
from labeling_store import occlusion_map

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS_DIR = os.path.join(HERE, "labels")
SCREENSHOT_DIR = os.path.join(HERE, "screenshots")
MASKED_DIR = os.path.join(HERE, "masked")

BLUR_RADIUS = 18


def _available_sources() -> list[str]:
    return label_store.available_sources()


def _available_models(source: str, run_id: str = None) -> list[str]:
    return label_store.available_models(source, run_id)


def _load_rows(source: str, run_id: str = None) -> list[dict]:
    """Via label_store, which reads the run-indexed directory AND the legacy flat file.

    Going through the shared loader is the point: a masker still reading only the flat file would
    keep rendering the pre-migration run forever, and would do it without any error -- silently
    masking a screenshot from rules that a newer run has already replaced.
    """
    rows = label_store.load_rows(source, run_id)
    if not rows:
        where = f"--run_id {run_id!r} in " if run_id else ""
        sys.exit(f"No rows for {where}--source {source!r}.\n"
                  f"Available --source values: {', '.join(_available_sources())}")
    rows = [r for r in rows if r.get("verdict") in ("trusted", "untrusted")]
    if not rows:
        sys.exit(f"--source {source!r} has no labeled (trusted/untrusted) rows yet.")
    return rows


def _elem_index_of(filename: str) -> int:
    import re
    m = re.match(r"^(\d+)_", filename)
    return int(m.group(1)) if m else -1


_UNIVERSE_CACHE = None


def _universe_for(filename: str, universe: "dict | None" = None) -> dict:
    """labeling_store's entry for one capture, built once per process.

    `universe` short-circuits the corpus lookup entirely: a live caller (the SA-Agent runtime)
    already holds the universe it built from the current a11y tree and has no corpus to look
    anything up in. Offline callers pass nothing and get the cached corpus build, unchanged.
    """
    if universe is not None:
        return universe
    global _UNIVERSE_CACHE
    if _UNIVERSE_CACHE is None:
        import labeling_store as _ls
        _UNIVERSE_CACHE = _ls.build_universe()
    return _UNIVERSE_CACHE.get(filename) or {}


CONTAINER_TAGS = {"section", "landmark", "list", "article", "panel", "table", "form", "tool-bar",
                  "group", "list-item", "document-web", "blockquote", "dialog"}
REGION_MAX_ROWS = 20
REGION_MIN_AREA_FRAC = 0.004
REGION_MAX_BOXES = 12
REGION_SPLIT_COVERAGE = 0.8
REGION_SPLIT_AREA_FRAC = 0.45
REGION_REPEAT_MIN = 3
REGION_REPEAT_TOL = 0.20
REGION_GROUP_COVERAGE = 0.5
REGION_GROUP_MIN = 2
REGION_PAIR_SLACK = 1.35
REGION_MIN_BOX_FRAC = 0.015
REGION_MERGE_GROWTH = 2.0
BAND_MAX_AREA_FRAC = 0.25
REGION_COVER_EXTRA = 8
REGION_COVER_ROW_FRAC = 0.9
REGION_CONTAIN_DROP = 0.7
_CP = "{https://accessibility.ubuntu.example.org/ns/component}"
_AT = "{https://accessibility.ubuntu.example.org/ns/attributes}"


def _region_label(e) -> str:
    """How a region names itself to the planner: its id, else its class, else its tag. Class
    attributes carry newlines and runs of padding, and this string is quoted back to the planner
    verbatim (mm_agents/sa_agent/policies.py:_masked_point), so whitespace is collapsed."""
    raw = e.get(_AT + "id") or e.get(_AT + "class") or e.tag or ""
    return " ".join(str(raw).split())[:60] or e.tag


def _box_union(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = min(ax, bx), min(ay, by)
    x1, y1 = max(ax + aw, bx + bw), max(ay + ah, by + bh)
    return (x0, y0, x1 - x0, y1 - y0)


def _box_gap(a, b) -> float:
    """Shortest distance between two rectangles; 0 when they touch or overlap."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    dx = max(0, max(ax, bx) - min(ax + aw, bx + bw))
    dy = max(0, max(ay, by) - min(ay + ah, by + bh))
    return (dx * dx + dy * dy) ** 0.5


def absorb_duplicate_remainders(kept: list, count_rows) -> list:
    """A remainder that restates a region already drawn is folded into it.

    The containment suppression inside regions_from_tree runs on the SELECTED boxes, before the
    coverage pass; the remainder fallback at the end of that pass has no such check, so it can lay
    a row's own rectangle straight over a named region -- measured live, reddit 103669 drew an
    unnamed [70,1030,1835,50] across the `site-footer` box inside it, two rectangles at the bottom
    of the picture naming one thing.

    Only remainders are absorbed, and only against a NAMED region. A named box inside another named
    box is usually a real distinction worth pointing at -- a toolbar inside a compose pane -- and
    collapsing those would cost the planner targets it has a reason to want.
    """
    for _ in range(len(kept)):
        hit = None
        for i, a in enumerate(kept):
            if a.get("kind") != "remainder":
                continue
            for j, b in enumerate(kept):
                if i == j or b.get("kind") == "remainder":
                    continue
                ab, bb = tuple(a["bbox"]), tuple(b["bbox"])
                if (_contained(ab, bb) >= REGION_CONTAIN_DROP
                        or _contained(bb, ab) >= REGION_CONTAIN_DROP):
                    hit = (i, j)
                    break
            if hit:
                break
        if hit is None:
            break
        i, j = hit
        u = _box_union(tuple(kept[i]["bbox"]), tuple(kept[j]["bbox"]))
        kept[j]["bbox"] = list(u)
        kept[j]["n"] = count_rows(u)
        kept.pop(i)
    return kept


def enforce_min_box(kept: list, view: int, bounds, count_rows) -> list:
    """No numbered region below REGION_MIN_BOX_FRAC of the view. See that constant.

    Two moves, in that order. MERGE: the smallest offending box joins the neighbour whose union
    grows the least -- nearest first, and never more than REGION_MERGE_GROWTH times the larger of
    the pair, so a sliver is absorbed by the region it sits against instead of drawing its own
    rectangle, and no box is dragged across the page to collect one. INFLATE: a box with no
    neighbour it may join is grown about its own centre until it clears the floor, clipped to
    `bounds`. Both keep the sliver's pixels inside a numbered box, which is the promise the
    coverage pass above makes; neither can uncover anything, because regions do not redact -- the
    mask is already painted and this only decides what the planner is able to NAME.

    `count_rows(box)` recounts the masked rows a box holds, so `n` still describes the box drawn
    rather than the one it grew out of.
    """
    floor = REGION_MIN_BOX_FRAC * view
    bx0, by0, bw, bh = bounds
    stuck: set = set()
    for _ in range(2 * len(kept) + 2):
        small = [i for i, d in enumerate(kept)
                 if d["bbox"][2] * d["bbox"][3] < floor and id(d) not in stuck]
        if not small:
            break
        i = min(small, key=lambda j: kept[j]["bbox"][2] * kept[j]["bbox"][3])
        a = tuple(kept[i]["bbox"])
        best = None
        for j, d in enumerate(kept):
            if j == i:
                continue
            b = tuple(d["bbox"])
            u = _box_union(a, b)
            grow = (u[2] * u[3]) / max(a[2] * a[3], b[2] * b[3], 1)
            if grow > REGION_MERGE_GROWTH:
                continue
            key = (_box_gap(a, b), grow)
            if best is None or key < best[0]:
                best = (key, j, u)
        if best is not None:
            _key, j, u = best
            host, sliver = kept[j], kept[i]
            host["bbox"] = list(u)
            if host.get("kind") == "remainder" and sliver.get("kind") != "remainder":
                host["label"], host["kind"] = sliver["label"], sliver["kind"]
            elif sliver.get("kind") != "remainder" and sliver["label"] not in str(host["label"]):
                host["label"] = f"{host['label']} + {sliver['label']}"[:60]
            host["n"] = count_rows(u)
            kept.pop(i)
            continue
        cx, cy = a[0] + a[2] / 2, a[1] + a[3] / 2
        k = (floor / max(a[2] * a[3], 1)) ** 0.5
        w = min(int(math.ceil(a[2] * k)), bw)
        h = min(int(math.ceil(floor / max(w, 1))), bh)
        if w * h < floor:
            w = min(int(math.ceil(floor / max(h, 1))), bw)
        x = int(min(max(cx - w / 2, bx0), bx0 + bw - w))
        y = int(min(max(cy - h / 2, by0), by0 + bh - h))
        grown = (x, y, w, h)
        if grown[2] * grown[3] <= a[2] * a[3]:
            stuck.add(id(kept[i]))
            continue
        kept[i]["bbox"] = list(grown)
        kept[i]["n"] = count_rows(grown)
    return kept


def _iou(a, b) -> float:
    """Intersection over union of two boxes -- used to drop a remainder band that merely restates
    a region already drawn."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _contained(inner, outer) -> float:
    """Fraction of `inner`'s area that lies inside `outer`."""
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    x = max(0, min(ix + iw, ox + ow) - max(ix, ox))
    y = max(0, min(iy + ih, oy + oh) - max(iy, oy))
    return (x * y) / (iw * ih) if iw * ih > 0 else 0.0


def _node_box(e):
    sc, sz = e.get(_CP + "screencoord"), e.get(_CP + "size")
    if not sc or not sz:
        return None
    x, y = (int(v) for v in sc.strip("()").split(","))
    w, h = (int(v) for v in sz.strip("()").split(","))
    return (x, y, w, h)


def _subtree_box(e):
    """The box a region should actually get: the union of its LAID-OUT descendants.

    Two problems disappear together.

    (a) Semantic blocks were unreachable. A container's own box is often absent -- 001 has 43
        classed containers with no geometry but with laid-out descendants, including `#answers`
        (164) and `.answer js-answer accepted-answer` (23). Those are exactly the blocks a planner
        wants to name, and using the node's own box left them invisible, so their contents
        scattered into one remainder band per text line.

    (b) Wrapper boxes were far too wide. 001's `#pb-anchor--wrapper` reports 1835px full width
        while the ad served into it occupies 729px centred, so the numbered box spanned the page
        and looked like it enclosed empty space.

    Descendants only, when there are any: the wrapper's own rectangle is the layout slot, the
    descendants are what is in it. Falls back to the node's own box for a laid-out leaf.
    """
    boxes = [b for d in e.iter() if d is not e
             for b in (_node_box(d),) if b and b[2] > 0 and b[3] > 0]
    if not boxes:
        own = _node_box(e)
        return own if own and own[2] > 0 and own[3] > 0 else None
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes); y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, x1 - x0, y1 - y0)


def _paints(e) -> bool:
    """Does anything in this subtree actually render?

    AT-SPI reports showing=true for collapsed ad slots and un-opened modals alike, so `showing` is
    not a usable test. 000's bottom-anchor ad slot is showing=true with every descendant
    zero-height -- no ad was served into it, nothing renders, and it still earned a number.
    """
    for d in e.iter():
        b = _node_box(d)
        if d is not e and b and b[2] > 0 and b[3] > 0:
            return True
    b = _node_box(e)
    return bool(b and b[2] > 0 and b[3] > 0 and not len(list(e)))


def query_regions(filename: str, rows: list[dict], universe: "dict | None" = None) -> list[dict]:
    """Numbered regions covering the masked elements of one capture -- corpus entry point.

    Loads the capture's tree off disk and delegates to regions_from_tree(), which holds the walk.

    `rows` are the SAME rows the mask was drawn from, so the numbering can never describe a
    different run's verdicts than the pixels do.
    """
    try:
        import full_tree_extraction as fte
        import labeling_store as ls
        root, _pm = fte._load_tree(filename)
        descs, _ = ls.resolve_content_roots(root, ls.platform_for(filename))
    except Exception:
        return []
    return regions_for_capture([d["node"] for d in descs],
                               _universe_for(filename, universe), rows)


def regions_from_tree(docweb, fu: dict, rows: list[dict], *, explain: bool = False,
                      root_index: int = 0) -> list[dict]:
    """Numbered regions covering the masked elements of ONE content root.

    BOTTOM-UP, from the masked rows outward -- not a top-down descent looking for containers that
    pass a filter. The old walk started at the content root, collected children that were an
    allowed tag AND painted AND exceeded a minimum area AND owned a masked row, and recursed only
    through the ones that failed. So a qualifying parent became a traversal barrier, and the useful
    container below it was never seen; and when the frontier held no good child, `splits_cleanly`
    went false and the whole page was emitted as one box. Both symptoms -- the giant box and the
    per-item boxes -- were the same defect: the right region existed in the tree and the search
    could not reach it.

    Every ancestor of every masked row is a candidate here, so reachability is not a question.
    What decides is scoring and then a single hierarchical choice per chain: keep the parent, or
    keep its children, never both.

    No disk access; shared verbatim with the SA-Agent runtime (mm_agents/sa_agent/masking.py).
    Returns [] rather than raising when there is nothing to work with -- no regions is a degraded
    picture, not an invalid one. With explain=True, returns diagnostic dicts instead of regions.
    """
    roots = fu.get("roots") or []
    if docweb is None or not roots or root_index >= len(roots):
        return []
    vx, vy, vw, vh = roots[root_index]["bbox"]
    view = vw * vh or 1

    def clip(b):
        x, y, w, h = b
        x0, y0 = max(x, vx), max(y, vy)
        x1, y1 = min(x + w, vx + vw), min(y + h, vy + vh)
        return None if x1 <= x0 or y1 <= y0 else (x0, y0, x1 - x0, y1 - y0)

    untrusted = {r["elem_index"] for r in rows if r["verdict"] == "untrusted"}
    if not untrusted:
        return []
    box_of = {r["elem_index"]: r["bbox"] for r in rows}
    import labeling_store as _ls
    nodes = [n for n, _b in _ls.row_nodes(docweb)]
    own_rows = [r for r in fu.get("rows", []) if r.get("root_index", 0) == root_index]
    index_of = {id(n): r["elem_index"] for r, n in zip(own_rows, nodes)}
    parent_of = {id(c): p for p in docweb.iter() for c in p}
    node_by_id = {id(n): n for n in docweb.iter()}

    owns: dict = {}
    for n in nodes:
        idx = index_of.get(id(n))
        if idx not in untrusted:
            continue
        cur = n
        while cur is not None:
            owns.setdefault(id(cur), set()).add(idx)
            cur = parent_of.get(id(cur))
    total_masked = len(untrusted)

    def masked_area(ids) -> int:
        """Exact union of the masked rows a candidate owns -- overlap counted once, so a stack of
        nested rows cannot inflate density past the pixels actually redacted."""
        return _union_area([tuple(box_of[i]) for i in ids if i in box_of])

    cands: dict = {}
    for nid, ids in owns.items():
        e = node_by_id.get(nid)
        if e is None:
            continue
        b = _subtree_box(e)
        cb = clip(b) if b else None
        if not cb:
            continue
        area = cb[2] * cb[3]
        if area <= 0:
            continue
        cands[nid] = {
            "node": e, "bbox": cb, "n": len(ids), "ids": ids, "area": area,
            "area_frac": area / view,
            "frac_nodes": len(ids) / total_masked,
            "density": min(1.0, masked_area(ids) / area),
            "container": e.tag in CONTAINER_TAGS,
            "paints": _paints(e),
        }

    def child_candidates(nid) -> list:
        """The MAXIMAL candidate descendants of a node: its nearest candidates on each branch."""
        out, stack = [], [node_by_id[nid]]
        first = True
        while stack:
            e = stack.pop()
            if not first and id(e) in cands:
                out.append(id(e))
                continue
            first = False
            stack.extend(list(e))
        return out

    def is_repeated_group(kids) -> bool:
        """Do these children look like list items -- enough of them, similar width, similar x?"""
        if len(kids) < REGION_REPEAT_MIN:
            return False
        widths = sorted(cands[k]["bbox"][2] for k in kids)
        xs = sorted(cands[k]["bbox"][0] for k in kids)
        mid_w = widths[len(widths) // 2] or 1
        mid_x = xs[len(xs) // 2]
        span = max(vw, 1)
        return (all(abs(w - mid_w) <= REGION_REPEAT_TOL * mid_w for w in widths)
                and all(abs(x - mid_x) <= REGION_REPEAT_TOL * span for x in xs))

    def _tmpl_key(e):
        """Which template a node is an instance of: its tag plus the FIRST class token.

        Keying on the whole class string splits a list by the STATE of its items -- reddit renders
        `submission submission--has-body` and `submission submission--has-url submission--collapsed`
        for the same kind of row, so seven submissions looked like two templates and came back as
        three boxes. The first token is conventionally the block name, and pairing it with the tag
        keeps a generic leading class (`flex`) from joining nodes that are not the same thing."""
        cls = (e.get(_AT + "class") or "").split()
        return (e.tag, cls[0]) if cls else None

    cls_count: dict = {}
    for n in docweb.iter():
        k_ = _tmpl_key(n)
        if k_:
            cls_count[k_] = cls_count.get(k_, 0) + 1

    def is_template(nid) -> bool:
        """One of several instances of the same page template? See REGION_REPEAT_MIN."""
        return cls_count.get(_tmpl_key(cands[nid]["node"]), 0) >= REGION_REPEAT_MIN

    def subgroup(kids, parent):
        """The largest set of sibling template instances -- or, failing that, of similarly-shaped
        siblings -- collapsed into ONE synthesized box, plus the children left over.

        This is is_repeated_group() applied to a SUBSET. That predicate is all-or-nothing: it keeps
        the parent whole when every child agrees, and otherwise the split emits one box per child.
        The common case on real pages is neither -- a list plus a heading, or a list plus a sort
        bar -- and it wants one box over the list and separate boxes for the rest.

        Candidate sets are tried largest first, and each must pass admissible(): a PAIR additionally
        has to be compact and modest in size, because a two-member group is the one case that can
        fire at the top of the tree and end the descent there with the whole page in one box.
        Returns (None, kids) when nothing qualifies.
        """
        span = max(vw, 1)

        def bounds(grp):
            bs = [cands[k]["bbox"] for k in grp]
            x0 = min(b[0] for b in bs)
            y0 = min(b[1] for b in bs)
            x1 = max(b[0] + b[2] for b in bs)
            y1 = max(b[1] + b[3] for b in bs)
            return (x0, y0, x1 - x0, y1 - y0), sum(b[2] * b[3] for b in bs)

        def admissible(grp):
            """Is this set of siblings one region? Size is the whole question for a PAIR: three
            items in a column are self-evidently a list and the gutters come with them, but two
            same-class siblings with a sidebar between them would union across it, and two
            top-level halves of a page would union into the page. Measured, admitting any pair at
            all collapsed 111 captures and took 71 of them to a single page-wide box."""
            if len(grp) >= REGION_REPEAT_MIN:
                return True
            bb, own = bounds(grp)
            area = bb[2] * bb[3]
            return area <= REGION_PAIR_SLACK * own and area < REGION_SPLIT_AREA_FRAC * view

        def candidates_by_class():
            by_key: dict = {}
            for k in kids:
                if is_template(k):
                    by_key.setdefault(_tmpl_key(cands[k]["node"]), []).append(k)
            return [g for g in by_key.values() if len(g) >= REGION_GROUP_MIN]

        def candidates_by_shape():
            out = []
            for anchor in kids:
                aw = cands[anchor]["bbox"][2] or 1
                ax = cands[anchor]["bbox"][0]
                grp = [k for k in kids
                       if abs(cands[k]["bbox"][2] - aw) <= REGION_REPEAT_TOL * aw
                       and abs(cands[k]["bbox"][0] - ax) <= REGION_REPEAT_TOL * span]
                if len(grp) >= REGION_GROUP_MIN:
                    out.append(grp)
            return out

        pool = ([(len(g), 0, g) for g in candidates_by_class()]
                + [(len(g), 1, g) for g in candidates_by_shape()])
        best = None
        for _n, _src, grp in sorted(pool, key=lambda t: (-t[0], t[1])):
            if not admissible(grp):
                continue
            ids = set().union(*(cands[k]["ids"] for k in grp))
            if len(ids) < REGION_GROUP_COVERAGE * parent["n"]:
                continue
            best = grp
            break
        if best is None:
            return None, kids
        bb, _own = bounds(best)
        ids = set().union(*(cands[k]["ids"] for k in best))
        area = bb[2] * bb[3] or 1
        seed = cands[best[0]]
        seed_label = _region_label(seed["node"])
        grp_box = {
            "node": seed["node"], "bbox": bb, "n": len(ids), "ids": ids, "area": area,
            "area_frac": area / view, "frac_nodes": len(ids) / total_masked,
            "density": min(1.0, masked_area(ids) / area),
            "container": True, "paints": True,
            "label": f"{seed_label[:40]} x{len(best)}", "group_of": len(best),
        }
        return grp_box, [k for k in kids if k not in best]

    def score(c) -> float:
        """Scoring, not gating. Tag and paint are features worth a little; grouping and density are
        worth much more, and sprawl is penalised -- which is what lets a pure-layout wrapper that
        groups a list well beat a semantically-tagged container that merely encloses the page."""
        return (5.0 * c["frac_nodes"] + 3.0 * c["density"]
                + (1.0 if c["container"] else 0.0) + (1.0 if c["paints"] else 0.0)
                - 4.0 * max(0.0, c["area_frac"] - REGION_SPLIT_AREA_FRAC))

    chosen: list = []

    def choose(nid, depth):
        c = cands[nid]
        kids = [k for k in child_candidates(nid) if cands[k]["area_frac"] >= REGION_MIN_AREA_FRAC]
        covered = len(set().union(*(cands[k]["ids"] for k in kids))) if kids else 0
        splits_cleanly = bool(kids) and covered >= REGION_SPLIT_COVERAGE * c["n"]
        too_big = c["n"] > REGION_MAX_ROWS or c["area_frac"] >= REGION_SPLIT_AREA_FRAC
        if depth > 0 and is_template(nid) and c["area_frac"] >= REGION_MIN_AREA_FRAC:
            chosen.append(c)
            return
        if too_big and splits_cleanly and not is_repeated_group(kids) and depth < 12:
            grp, rest = subgroup(kids, c)
            if grp is not None:
                chosen.append(grp)
                for k in rest:
                    choose(k, depth + 1)
                return
            for k in kids:
                choose(k, depth + 1)
            return
        if c["area_frac"] >= REGION_MIN_AREA_FRAC:
            chosen.append(c)
        elif kids:
            for k in kids:
                choose(k, depth + 1)

    if id(docweb) in cands:
        choose(id(docweb), 0)
    else:
        for k in child_candidates(id(docweb)):
            choose(k, 0)

    if explain:
        return sorted(({"label": (c.get("label") or _region_label(c["node"])),
                        "tag": c["node"].tag, "n": c["n"],
                        "bbox": list(c["bbox"]), "area_frac": round(c["area_frac"], 4),
                        "density": round(c["density"], 3), "frac_nodes": round(c["frac_nodes"], 3),
                        "container": c["container"], "paints": c["paints"],
                        "score": round(score(c), 3),
                        "chosen": any(d is c for d in chosen)}
                       for c in list(cands.values())
                       + [d for d in chosen if "group_of" in d]), key=lambda d: -d["n"])

    chosen.sort(key=lambda c: (-score(c), -c["n"]))

    survivors: list = []
    for c in chosen:
        if any(_contained(c["bbox"], k["bbox"]) >= REGION_CONTAIN_DROP for k in survivors):
            continue
        survivors.append(c)
    chosen = survivors

    kept = [{"bbox": list(c["bbox"]), "n": c["n"], "node": c["node"],
             "label": c.get("label") or _region_label(c["node"]),
             "kind": c["node"].tag} for c in chosen[:REGION_MAX_BOXES]]

    node_of_index = {index_of[id(n)]: n for n in nodes if id(n) in index_of}

    def _isect(a, b):
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        x0, y0 = max(ax, bx), max(ay, by)
        x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
        return None if x1 <= x0 or y1 <= y0 else (x0, y0, x1 - x0, y1 - y0)

    def unreachable():
        """Masked rows whose on-screen pixels are mostly outside every kept box, worst first."""
        boxes = [tuple(d["bbox"]) for d in kept]
        out = []
        for idx in untrusted:
            b = box_of.get(idx)
            cb = clip(tuple(b)) if b else None
            if not cb:
                continue
            area = cb[2] * cb[3]
            if area >= BAND_MAX_AREA_FRAC * view:
                continue
            covered = _union_area([c for r in boxes for c in (_isect(cb, r),) if c])
            if covered < REGION_COVER_ROW_FRAC * area:
                out.append((area - covered, idx))
        out.sort(reverse=True)
        return out

    budget = REGION_MAX_BOXES + REGION_COVER_EXTRA
    for _ in range(REGION_COVER_EXTRA):
        gaps = unreachable()
        if not gaps or len(kept) >= budget:
            break
        _deficit, idx = gaps[0]

        def _subsumes_kept(box) -> bool:
            """Would this box swallow a region already drawn? Climbing to the largest ancestor
            covers the most rows per box, but an ancestor that contains a box already on the
            picture just draws a second rectangle around it -- which is what the overlapping-boxes
            complaint looks like. Prefer the largest ancestor that adds area of its OWN."""
            for d in kept:
                inner = _isect(box, tuple(d["bbox"]))
                if inner and inner[2] * inner[3] >= 0.7 * d["bbox"][2] * d["bbox"][3]:
                    return True
            return False

        best, cur = None, node_of_index.get(idx)
        while cur is not None:
            c = cands.get(id(cur))
            if (c and REGION_MIN_AREA_FRAC <= c["area_frac"] < BAND_MAX_AREA_FRAC
                    and not _subsumes_kept(c["bbox"])):
                best = c
            cur = parent_of.get(id(cur))
        if best is not None:
            kept.append({"bbox": list(best["bbox"]), "n": best["n"], "node": best["node"],
                         "label": _region_label(best["node"]),
                         "kind": best["node"].tag})
            continue
        cb = clip(tuple(box_of[idx])) if idx in box_of else None
        if not cb:
            break
        kept.append({"bbox": list(cb), "n": 1, "node": node_of_index.get(idx),
                     "label": "remainder", "kind": "remainder"})

    def _rows_in(box) -> int:
        """Masked rows whose redacted pixels are mostly inside `box` -- how `n` is recomputed when
        enforce_min_box changes a rectangle."""
        n = 0
        for idx in untrusted:
            b = box_of.get(idx)
            cb = clip(tuple(b)) if b else None
            if not cb:
                continue
            inner = _isect(cb, tuple(box))
            if inner and inner[2] * inner[3] >= REGION_COVER_ROW_FRAC * cb[2] * cb[3]:
                n += 1
        return n

    kept = absorb_duplicate_remainders(kept, _rows_in)
    kept = enforce_min_box(kept, view, (vx, vy, vw, vh), _rows_in)

    for i, d in enumerate(kept, 1):
        d["id"] = i
        d.pop("node", None)
    return kept


def regions_for_capture(root_nodes: list, fu: dict, rows: list[dict]) -> list[dict]:
    """Regions for EVERY content root of a capture, numbered once across the whole picture.

    A desktop capture is not one surface. 207_thunderbird_compose_reply asks the planner to work in
    a compose window holding 131 masked rows, and regions computed from root 0 alone gave it twelve
    boxes -- `tree`, `messagePane`, `folderPane`, `status-bar` -- all naming elements of the Inbox
    window BEHIND it, drawn straight across the window in front. Nothing the planner could see was
    addressable and everything addressable was hidden.

    Regions of a window that is fully behind another are dropped, and one that is partly behind is
    trimmed where the trim is exact (see _trim_to_visible). What survives is renumbered in root
    order, so the ids on the picture and the ids in regions.json cannot disagree.

    REGION_MIN_BOX_FRAC is enforced per root, inside regions_from_tree, so it is measured against
    the window a region belongs to rather than the screen -- a box that is a fifth of a dialog is
    not a sliver because the dialog is small. Trimming here can still take a region below that
    floor, and that is the right answer: what is left of a mostly-hidden window IS a sliver, and
    growing it back would put the box over the window in front.
    """
    roots = fu.get("roots") or []
    occ = occlusion_map(roots)
    out: list[dict] = []
    for ri, node in enumerate(root_nodes):
        if node is None:
            continue
        hidden = occ.get(ri) or []
        for d in regions_from_tree(node, fu, rows, root_index=ri):
            box = _trim_to_visible(tuple(d["bbox"]), hidden)
            if box is None:
                continue
            d["bbox"] = list(box)
            d["root_index"] = ri
            out.append(d)
    for i, d in enumerate(out, 1):
        d["id"] = i
    return out


def _trim_to_visible(box, occluders) -> "tuple | None":
    """`box` minus the windows in front of it -- but only when the answer is still a rectangle.

    A region is a thing the planner points at by number, so it has to stay one box. An occluder
    that covers the region entirely removes it. One that cuts a full slab off a side (it spans the
    region's whole width or whole height) shortens it exactly. Anything else -- a window landing in
    the middle of a region, or clipping one corner -- leaves the region alone: a box that still
    names the right element and happens to reach behind another window is a drawing problem, and
    _draw_regions solves it by not inking the hidden part.
    """
    x0, y0, x1, y1 = box[0], box[1], box[0] + box[2], box[1] + box[3]
    for ox, oy, ow, oh in occluders:
        ox1, oy1 = ox + ow, oy + oh
        if ox1 <= x0 or x1 <= ox or oy1 <= y0 or y1 <= oy:
            continue
        if ox <= x0 and ox1 >= x1 and oy <= y0 and oy1 >= y1:
            return None
        if ox <= x0 and ox1 >= x1:
            if oy <= y0:
                y0 = oy1
            elif oy1 >= y1:
                y1 = oy
        elif oy <= y0 and oy1 >= y1:
            if ox <= x0:
                x0 = ox1
            elif ox1 >= x1:
                x1 = ox
        if x1 <= x0 or y1 <= y0:
            return None
    return (x0, y0, x1 - x0, y1 - y0)


def _draw_regions(image, regions: list[dict], hidden_for_root=None) -> None:
    """Numbered boxes on the masked image: orange for a real container, blue for a remainder band.

    The label is the container's own class/id, which is platform-authored markup -- the same
    property the trust model rests on -- so naming it to the planner leaks no user content.

    Ink lands only where the region's own window is visible. `hidden_for_root(ri)` returns the
    plane of pixels covered by a window in front of root `ri` (None when nothing is). Without it a
    region of a background window draws its outline and its number over the window on top -- the
    picture then shows a numbered box around content that is not the content it names.
    """
    groups: dict = defaultdict(list)
    for d in regions:
        groups[d.get("root_index", 0)].append(d)

    def _ink(target, items):
        rgba = target.mode == "RGBA"
        draw = ImageDraw.Draw(target)
        for d in items:
            x, y, w, h = d["bbox"]
            colour = (0, 170, 255) if d["kind"] == "remainder" else (255, 70, 0)
            colour = colour + (255,) if rgba else colour
            draw.rectangle([x, y, x + w, y + h], outline=colour, width=3)
            draw.rectangle([x, y, x + 34, y + 24], fill=colour)
            draw.text((x + 12, y + 7), str(d["id"]),
                      fill=(255, 255, 255, 255) if rgba else (255, 255, 255))

    for ri, items in groups.items():
        plane = hidden_for_root(ri) if hidden_for_root is not None else None
        if plane is None:
            _ink(image, items)
            continue
        import numpy as np
        layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        _ink(layer, items)
        alpha = np.array(layer.getchannel("A"))
        alpha[plane] = 0
        layer.putalpha(Image.fromarray(alpha, mode="L"))
        image.paste(layer, (0, 0), layer)


def _content_root_boxes(filename: str, universe: "dict | None" = None) -> list[dict]:
    """The capture's outermost content roots, from labeling_store -- the same descriptors that make
    "browser and OS chrome are trusted by construction" true in the first place.

    Returns [] when the capture is absent from the universe (the walled captures are excluded from
    it), which the caller treats as "skip default-deny", never as "deny everything".
    """
    return _universe_for(filename, universe).get("roots") or []


DOM_GEOM_DIR = os.path.join(HERE, "dom_geom")
LOW_COVERAGE_PCT = 90.0
MIN_TEXT_NODES = 20


def _text_coverage(filename: str, claimed, root_box) -> "tuple[int, int] | None":
    """(covered, total) visible DOM text nodes whose centre falls inside the page root.

    This is the empirical test of whether masking uncovered AREA is buying anything. The AT-SPI
    tree can cover a small fraction of a page's PIXELS while still covering nearly all of its
    visible STRINGS -- on 000_stackoverflow the boxes span 36.3% of the page area but contain 96 of
    98 visible DOM text nodes. Area coverage was the only signal available before `dom_geom/`
    existed, and it badly overstates how much text goes unseen.

    Returns None when there is no geometry for the capture (nothing to say), and the caller
    suppresses the number below MIN_TEXT_NODES rather than reporting a noisy percentage.
    """
    paths = glob.glob(os.path.join(DOM_GEOM_DIR, f"{filename[:3]}_*.json"))
    if not paths:
        return None
    try:
        with open(paths[0], encoding="utf-8") as f:
            geom = json.load(f)
    except (OSError, ValueError):
        return None
    rx, ry, rw, rh = root_box
    px = claimed.load()
    img_w, img_h = claimed.size
    covered = total = 0
    for frame in geom.get("frames", []):
        for node in frame.get("nodes", []):
            if not node.get("visible") or not (node.get("own_text") or "").strip():
                continue
            screen = node.get("screen")
            if not screen:
                continue
            x, y, w, h = screen
            if w <= 0 or h <= 0:
                continue
            cx, cy = int(x + w / 2), int(y + h / 2)
            if not (0 <= cx < img_w and 0 <= cy < img_h):
                continue
            if not (rx <= cx < rx + rw and ry <= cy < ry + rh):
                continue
            total += 1
            if px[cx, cy]:
                covered += 1
    return (covered, total)


def _bbox_of_factory(index: int, filename: str, full_tree_source: bool):
    """Resolves a row to its bbox. Every surviving source carries its own "bbox" field, so this is
    now a single lambda -- kept as a factory because mask_file() and the SA-Agent runtime both
    pass a `bbox_of` through mask_image(), and a future source that needs real work can slot in here.

    It used to branch: interactable-only sources (the retired extraction.py) carried no bbox and
    needed actions[] re-extracted against a SEPARATE elem_index namespace. Mixing those two
    namespaces was one of the three defects that retired the gen-1 universe -- rows that were
    scored and rows that were drawn could be different elements.
    """
    if not full_tree_source:
        raise ValueError(
            f"{filename}: interactable-only label sources were removed with the gen-1 universe; "
            "every source must carry its own 'bbox' field")
    return lambda row: tuple(row["bbox"])


_HAND_MODES = frozenset({"element", "page_bulk", "page_default", "subtree", "correction", "region"})


def _maskable(row: dict, biba: bool) -> bool:
    """Whether an untrusted row should actually redact pixels.

    Sources that carry no `label_mode` at all (every labelers/*.py output) are unaffected: they
    have no join semantics, so every untrusted row masks, exactly as before.
    """
    if biba or "label_mode" not in row:
        return True
    return row.get("label_mode") in _HAND_MODES


def mask_file(filename: str, rows: list[dict], style: str, output_dir: str,
              biba: bool = False, default_deny: bool = True,
              draw_regions: bool = True) -> "dict | None":
    """Corpus entry point: masks UNTRUSTED rows on filename's real screenshot, writes the result
    to output_dir, returns a small stats dict (or None if the screenshot/extraction is
    unavailable).

    Resolves everything off disk -- screenshot, bbox source, content roots, tree for the regions --
    and hands it to mask_image(), which holds the actual redaction. The split exists so the
    SA-Agent runtime redacts live screenshots through the SAME code rather than a second copy;
    see mm_agents/sa_agent/masking.py.

    By default only hand-asserted verdicts redact (see _maskable). Pass biba=True for the original
    behaviour, in which every untrusted row masks including joined ancestors -- useful for seeing
    the full taint closure, not for producing an image an agent should be shown.
    """
    index = _elem_index_of(filename)
    full_tree_source = bool(rows) and "bbox" in rows[0]
    try:
        bbox_of = _bbox_of_factory(index, filename, full_tree_source)
    except Exception as e:
        print(f"SKIP {filename}: could not re-extract ({e})", file=sys.stderr)
        return None

    screenshot_path = os.path.join(SCREENSHOT_DIR, os.path.splitext(filename)[0] + ".png")
    if not os.path.exists(screenshot_path):
        print(f"SKIP {filename}: no matching screenshot at {screenshot_path}", file=sys.stderr)
        return None

    pristine = Image.open(screenshot_path).convert("RGB")

    docweb = None
    root_nodes: list = []
    if draw_regions:
        try:
            import full_tree_extraction as fte
            import labeling_store as ls
            _root, _pm = fte._load_tree(filename)
            _descs, _ = ls.resolve_content_roots(_root, ls.platform_for(filename))
            root_nodes = [d["node"] for d in _descs]
            docweb = root_nodes[0] if root_nodes else None
        except Exception:
            docweb, root_nodes = None, []

    result = mask_image(pristine, rows, _universe_for(filename), style=style, biba=biba,
                        default_deny=default_deny, content_root=docweb, bbox_of=bbox_of,
                        root_nodes=root_nodes, text_coverage_file=filename)
    if result is None:
        return None

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, filename.replace(".xml", ".png"))
    result.image.save(out_path)
    if result.regions:
        with open(out_path[:-4] + ".regions.json", "w", encoding="utf-8") as f:
            json.dump({"file": filename, "screen": list(result.image.size),
                       "regions": result.regions}, f, indent=2)

    stats = dict(result.stats)
    stats["file"] = filename
    stats["out_path"] = out_path
    return stats


class MaskResult:
    """What one redaction produced: the picture, the boolean mask behind it, the regions, stats.

    `bits` is the part that did not exist before and is the reason this class does. mask_file()
    only ever needed pixels, so the resolved untrusted set lived implicitly in the drawing order
    and was thrown away. The runtime gate has to answer "is THIS point redacted?" for every click,
    and it must answer from the same resolution that produced the picture -- a second
    reimplementation that agrees today is a defense that silently stops agreeing later. So the
    renderer now returns the mask it effectively already computed.

    bits[y][x] is True where the pixel is redacted. Rows are y, columns are x.
    """

    __slots__ = ("image", "bits", "regions", "stats", "root_meta")

    def __init__(self, image, bits, regions, stats, root_meta=None):
        self.image, self.bits, self.regions, self.stats = image, bits, regions, stats
        self.root_meta = root_meta or {}

    def hit(self, x, y):
        """The redaction test the policy gate calls. Out-of-bounds is NOT redacted: a click
        outside the screenshot is not a click on hidden content, and treating it as one would
        block chrome the mask never covered."""
        xi, yi = int(x), int(y)
        if yi < 0 or xi < 0 or yi >= len(self.bits) or xi >= len(self.bits[0]):
            return False
        return bool(self.bits[yi][xi])

    def _covered(self, root_index, x, y) -> bool:
        """Is (x, y) inside a window known to be painted above content root `root_index`?"""
        meta = self.root_meta.get(root_index)
        for ox, oy, ow, oh in (meta or {}).get("hidden") or ():
            if ox <= x < ox + ow and oy <= y < oy + oh:
                return True
        return False

    def region_at(self, x, y):
        """The numbered region containing a point, or None. WHAT IS VISIBLE HAS PRIORITY.

        Containment alone is the wrong question on a stacked desktop, and answering it was a real
        defect rather than a cosmetic one. Eighteen Thunderbird compose windows accumulated in one
        episode; at turn 18 the point (365,277) sat inside SIX `composeContentBox` regions from six
        different roots, all 860x777, and the old smallest-area rule handed back the one belonging
        to a window opened twelve steps earlier. The planner had queried the raised window's region
        and was refused in the name of a window it could not see -- twice, with different ids.

        So the ladder is, in order:
          1. drop any region whose own window is covered at this point by a window above it. A box
             that reaches behind another window does not own those pixels, and `_trim_to_visible`
             deliberately leaves such a box untrimmed (it has to stay ONE rectangle). That is fine
             for drawing, which inks around the occluder; it was never fine here.
          2. prefer a region belonging to the ACTIVE window -- the one z-fact AT-SPI gives us, and
             the window the planner is working in.
          3. then smallest area, so a remainder band nested in a container still reports the band.

        Falls back to the old containment-only answer when the filter leaves nothing: the pixel is
        redacted by somebody, and naming the wrong region beats saying "a redacted area" with no
        handle at all. Byte-identical to the previous behaviour on any capture with no stacking,
        where `root_meta` carries no occluders and every region survives step 1.
        """
        def _contains(d):
            rx, ry, rw, rh = d["bbox"]
            return rx <= x < rx + rw and ry <= y < ry + rh

        def _rank(d):
            meta = self.root_meta.get(d.get("root_index")) or {}
            area = d["bbox"][2] * d["bbox"][3]
            return (0 if meta.get("active") else 1, area)

        inside = [d for d in self.regions if _contains(d)]
        if not inside:
            return None
        visible = [d for d in inside if not self._covered(d.get("root_index"), x, y)]
        return min(visible or inside, key=_rank)

    def focus_bbox(self):
        """The screen rectangle of the ACTIVE window, or None when nothing is raised.

        The active window is by definition the one nothing else is painted above, so its rectangle
        is entirely visible and needs no trimming. Used to scope a screen query to the window the
        planner is actually working in instead of the whole desktop -- see
        mm_agents/sa_agent/tools.query_screen.
        """
        for meta in self.root_meta.values():
            if meta.get("active") and meta.get("bbox"):
                return list(meta["bbox"])
        return None


def mask_image(pristine, rows: list[dict], fu: dict, *, style: str = "fill",
               biba: bool = False, default_deny: bool = True,
               content_root=None, bbox_of=None, root_nodes: "list | None" = None,
               text_coverage_file: "str | None" = None,
               draw_regions: bool = True) -> "MaskResult | None":
    """The redaction itself, over an in-memory image and an already-built universe. No disk access.

    Shared verbatim between the corpus CLI (mask_file above) and the SA-Agent runtime. `bbox_of`
    defaults to reading each row's own "bbox", which is what every full-tree source carries;
    mask_file passes a factory for the interactable-only sources that need re-extraction.
    """
    if bbox_of is None:
        bbox_of = lambda row: tuple(row["bbox"])
    pristine = pristine.convert("RGB")
    img_w, img_h = pristine.size
    filename = fu.get("file", "<live>")

    _roots = [tuple(d["bbox"]) for d in _content_root_boxes(filename, fu) if d.get("bbox")]
    if _roots:
        _cx0 = min(x for x, _y, _w, _h in _roots)
        _cy0 = min(y for _x, y, _w, _h in _roots)
        _cx1 = max(x + w for x, _y, w, _h in _roots)
        _cy1 = max(y + h for _x, y, _w, h in _roots)
    else:
        _cx0, _cy0, _cx1, _cy1 = 0, 0, img_w, img_h
    clip_x0, clip_y0 = max(0, _cx0), max(0, _cy0)
    clip_x1, clip_y1 = min(img_w, _cx1), min(img_h, _cy1)
    image = pristine.copy()
    draw = ImageDraw.Draw(image)

    rows_sorted = sorted(rows, key=lambda r: bbox_of(r)[2] * bbox_of(r)[3], reverse=True)

    _parent_of = {r["elem_index"]: r["parent"] for r in fu.get("rows", [])}

    ancestry = bool(rows) and "bbox" in rows[0] and bool(_parent_of)

    def _ancestors(index):
        """The row's own index plus every parent above it. `seen` guards a cyclic parent chain --
        a malformed tree must not spin here, it must simply stop granting."""
        seen = set()
        while index is not None and index not in seen:
            seen.add(index)
            index = _parent_of.get(index)
        return seen

    import numpy as np
    bits = np.zeros((img_h, img_w), dtype=bool)

    owner = np.zeros((img_h, img_w), dtype=np.int32)
    untrusted_painters: list = []

    _root_of = {r["elem_index"]: r.get("root_index") for r in fu.get("rows", [])}
    _occluders = occlusion_map(fu.get("roots") or []) if ancestry else {}
    _hidden_cache: dict = {}

    def _hidden_for_root(root_index):
        """Bool plane, True where a window above content root `root_index` covers it -- None when
        nothing is above it, which is every root of every single-window capture and every web
        capture. That None is the fast path: it keeps the original rectangle painting
        byte-identical wherever there is no stacking to resolve."""
        boxes = _occluders.get(root_index)
        if not boxes:
            return None
        plane = _hidden_cache.get(root_index)
        if plane is None:
            plane = np.zeros((img_h, img_w), dtype=bool)
            for ox, oy, ow, oh in boxes:
                x0, y0 = max(0, ox), max(0, oy)
                x1, y1 = min(img_w, ox + ow), min(img_h, oy + oh)
                if x1 > x0 and y1 > y0:
                    plane[y0:y1, x0:x1] = True
            _hidden_cache[root_index] = plane
        return plane

    _layer_of = {r["elem_index"]: r.get("layer") or 0 for r in fu.get("rows", [])}
    _roots = fu.get("roots") or []
    _layer_cache: dict = {}

    def _layer_plane(root_index, layer):
        """True where a row of `layer` may NOT paint or claim: inside any panel above it, and --
        for a pop-up layer -- outside its own panel. The second half is the backdrop: a modal's
        full-viewport overlay element is a row of the layer, but outside the panel its pixels are
        the (dimmed) page, which the page's own rows decide. Without it one redacted overlay row
        blacked out the whole page behind 512_ucm_forum_post_detail, header and all, and no page
        row could take it back (not an ancestor)."""
        if root_index is None or not (0 <= root_index < len(_roots)):
            return None
        panels = _roots[root_index].get("layers") or []
        if not panels or layer > len(panels):
            return None
        key = (root_index, layer)
        plane = _layer_cache.get(key)
        if plane is None:

            def _fill(pl, box, val):
                ox, oy, ow, oh = box
                x0, y0 = max(0, int(ox)), max(0, int(oy))
                x1, y1 = min(img_w, int(ox + ow)), min(img_h, int(oy + oh))
                if x1 > x0 and y1 > y0:
                    pl[y0:y1, x0:x1] = val
            if layer > 0:
                plane = np.ones((img_h, img_w), dtype=bool)
                _fill(plane, panels[layer - 1], False)
            else:
                plane = np.zeros((img_h, img_w), dtype=bool)
            for box in panels[layer:]:
                _fill(plane, box, True)
            _layer_cache[key] = plane
        return plane

    def _hidden_for(row):
        ei = row.get("elem_index")
        ri = _root_of.get(ei)
        win = _hidden_for_root(ri)
        lay = _layer_plane(ri, _layer_of.get(ei, 0)) if ancestry else None
        if lay is None:
            return win
        return lay if win is None else (win | lay)

    masked_area = 0
    n_masked = 0
    n_restore_blocked = 0
    n_occluded = 0
    for row in rows_sorted:
        x, y, w, h = bbox_of(row)
        if w <= 0 or h <= 0:
            continue
        box = [max(clip_x0, x), max(clip_y0, y), min(clip_x1, x + w), min(clip_y1, y + h)]
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        if not _maskable(row, biba):
            continue
        if row["verdict"] == "trusted":
            if not ancestry:
                image.paste(pristine.crop(box), box)
                bits[box[1]:box[3], box[0]:box[2]] = False
                continue
            hidden = _hidden_for(row)
            sub = owner[box[1]:box[3], box[0]:box[2]]
            anc = _ancestors(row.get("elem_index"))
            lut = np.zeros(len(untrusted_painters) + 1, dtype=bool)
            for slot, painter in enumerate(untrusted_painters, start=1):
                if painter is not None and painter in anc:
                    lut[slot] = True
            allowed = lut[sub]
            if hidden is not None:
                allowed &= ~hidden[box[1]:box[3], box[0]:box[2]]
                n_occluded += 1
            if bool(((sub != 0) & ~allowed).any()):
                n_restore_blocked += 1
            if not allowed.any():
                continue
            image.paste(pristine.crop(box), box,
                        Image.fromarray(allowed.astype(np.uint8) * 255, mode="L"))
            bits[box[1]:box[3], box[0]:box[2]] &= ~allowed
            continue
        hidden = _hidden_for(row)
        if hidden is not None:
            pbox = (box[0], box[1], min(box[2] + 1, img_w), min(box[3] + 1, img_h))
            if pbox[2] <= pbox[0] or pbox[3] <= pbox[1]:
                continue
            sel = ~hidden[pbox[1]:pbox[3], pbox[0]:pbox[2]]
            n_occluded += 1
            if not sel.any():
                continue
            stencil = Image.fromarray(sel.astype(np.uint8) * 255, mode="L")
            if style == "blur":
                image.paste(pristine.crop(pbox).filter(ImageFilter.GaussianBlur(radius=BLUR_RADIUS)),
                            pbox, stencil)
            else:
                image.paste((0, 0, 0), pbox, stencil)
            bits[pbox[1]:pbox[3], pbox[0]:pbox[2]] |= sel
            untrusted_painters.append(row.get("elem_index"))
            owner_sub = owner[pbox[1]:pbox[3], pbox[0]:pbox[2]]
            owner_sub[sel] = len(untrusted_painters)
            masked_area += int(sel.sum())
            n_masked += 1
            continue
        if style == "blur":
            region = pristine.crop(box)
            if region.width > 0 and region.height > 0:
                region = region.filter(ImageFilter.GaussianBlur(radius=BLUR_RADIUS))
                image.paste(region, box)
        else:
            draw.rectangle(box, fill=(0, 0, 0))
        bits[box[1]:min(box[3] + 1, img_h), box[0]:min(box[2] + 1, img_w)] = True
        untrusted_painters.append(row.get("elem_index"))
        owner[box[1]:min(box[3] + 1, img_h), box[0]:min(box[2] + 1, img_w)] = len(untrusted_painters)
        masked_area += w * h
        n_masked += 1

    uncovered_pct = 0.0
    if True:
        MAX_CLAIM_FRACTION = 0.5
        root_boxes = [tuple(d["bbox"]) for d in _content_root_boxes(filename, fu)
                      if d.get("bbox") and not d.get("deny_exempt")]
        _all_roots = _content_root_boxes(filename, fu)
        if not root_boxes and any(d.get("deny_exempt") for d in _all_roots):
            root_boxes = []
        elif not root_boxes:
            print(f"WARN {filename}: no content root from labeling_store -- skipping default-deny "
                  f"(element masks still applied)", file=sys.stderr)
            root_boxes = []
        root_area = sum(max(0, w) * max(0, h) for _x, _y, w, h in root_boxes) or (img_w * img_h)
        claimed_arr = np.zeros((img_h, img_w), dtype=bool)
        n_too_broad = 0
        for row in rows:
            if not _maskable(row, biba):
                continue
            x, y, w, h = bbox_of(row)
            if w <= 0 or h <= 0:
                continue
            if (w * h) > MAX_CLAIM_FRACTION * root_area:
                n_too_broad += 1
                continue
            cx0, cy0 = max(0, x), max(0, y)
            cx1, cy1 = min(img_w, x + w + 1), min(img_h, y + h + 1)
            if cx1 <= cx0 or cy1 <= cy0:
                continue
            hidden = _hidden_for(row)
            if hidden is None:
                claimed_arr[cy0:cy1, cx0:cx1] = True
            else:
                claimed_arr[cy0:cy1, cx0:cx1] |= ~hidden[cy0:cy1, cx0:cx1]
        claimed = Image.fromarray(claimed_arr.astype(np.uint8) * 255, mode="L").convert("1")
        deny = Image.new("1", (img_w, img_h), 0)
        ddraw = ImageDraw.Draw(deny)
        for x, y, w, h in root_boxes:
            if w > 0 and h > 0:
                ddraw.rectangle([x, y, x + w, y + h], fill=1)
        deny = ImageChops.logical_and(deny, ImageChops.invert(claimed.convert("L")).convert("1"))
        n_deny = sum(deny.point(lambda v: 1 if v else 0).getdata())
        uncovered_pct = 100.0 * n_deny / (img_w * img_h)
        if default_deny and n_deny:
            if style == "blur":
                image.paste(pristine.filter(ImageFilter.GaussianBlur(radius=BLUR_RADIUS)), (0, 0), deny)
            else:
                image.paste(Image.new("RGB", (img_w, img_h), (0, 0, 0)), (0, 0), deny)
            bits |= np.array(deny, dtype=bool)

        cov = (_text_coverage(text_coverage_file, claimed, root_boxes[0])
               if root_boxes and text_coverage_file else None)

    _region_nodes = list(root_nodes) if root_nodes else (
        [content_root] if content_root is not None else [])
    regions = regions_for_capture(_region_nodes, fu, rows) if _region_nodes else []
    if regions and draw_regions:
        _draw_regions(image, regions, hidden_for_root=_hidden_for_root)

    _root_meta = {
        ri: {"active": bool(r.get("active")), "bbox": list(r.get("bbox") or []),
             "hidden": [list(b) for b in (_occluders.get(ri) or [])]}
        for ri, r in enumerate(fu.get("roots") or []) if r.get("bbox")
    }

    return MaskResult(image, bits, regions, {
        "n_rows": len(rows),
        "n_untrusted": sum(1 for r in rows if r["verdict"] == "untrusted"),
        "n_masked": n_masked,
        "n_restore_blocked": n_restore_blocked,
        "n_occluded": n_occluded,
        "approx_masked_pct": 100.0 * masked_area / (img_w * img_h) if img_w and img_h else 0.0,
        "masked_pct": 100.0 * float(bits.mean()) if img_w and img_h else 0.0,
        "unverified_pct": uncovered_pct,
        "text_covered": cov[0] if cov else None,
        "text_total": cov[1] if cov else None,
        "text_covered_pct": (100.0 * cov[0] / cov[1]) if cov and cov[1] else None,
        "n_regions": len(regions),
    }, root_meta=_root_meta)


def _select_rows_by_file(all_rows: list[dict], file_filter: "str | None", model: "str | None") -> dict[str, list[dict]]:
    rows = all_rows
    if file_filter:
        rows = [r for r in rows if r["file"].startswith(file_filter)]
    if not rows:
        sys.exit("No labeled rows match the given --file filter.")

    by_file: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_file[row["file"]].append(row)

    result = {}
    for filename, file_rows in by_file.items():
        models_present = {r.get("model") for r in file_rows}
        if len(models_present) > 1:
            if model is None:
                available = sorted(m for m in models_present if m)
                print(f"SKIP {filename}: multiple models' proposals present ({available}) -- "
                      f"pass --model to disambiguate.", file=sys.stderr)
                continue
            file_rows = [r for r in file_rows if r.get("model") == model]
        result[filename] = file_rows
    return result


def _explain_regions(filename: str, rows: list[dict]) -> None:
    """Print the region-selection candidate table for one capture.

    The question this answers is the one that is otherwise unanswerable without a rebuild: "the
    obviously-right container is sitting there in the tree, why did it not get a box?" Every
    ancestor of every masked row is a candidate, so it WILL be in this table; `score` and `chosen`
    say what happened to it.
    """
    try:
        import full_tree_extraction as fte
        import labeling_store as ls
        root, _pm = fte._load_tree(filename)
        descs, _ = ls.resolve_content_roots(root, ls.platform_for(filename))
    except Exception as e:
        sys.exit(f"{filename}: could not load the tree ({type(e).__name__}: {e})")
    fu = _universe_for(filename, None)
    maskable = [r for r in rows if r["verdict"] == "untrusted" and _maskable(r, False)]
    cands = [c for ri, d in enumerate(descs)
             for c in regions_from_tree(d["node"], fu, rows, explain=True, root_index=ri)]
    print(f"{filename}: {len(rows)} rows, {len(maskable)} maskable untrusted, "
          f"{len(cands)} candidate containers over {len(descs)} content root(s)")
    print(f"{'own':>4} {'frac':>5} {'dens':>5} {'area%':>6} {'cont':>4} {'pnt':>3} "
          f"{'score':>6} {'kept':>4}  {'bbox':<26} label")
    for c in cands[:40]:
        print(f"{c['n']:4d} {c['frac_nodes']:5.2f} {c['density']:5.2f} {100*c['area_frac']:6.1f} "
              f"{('Y' if c['container'] else '-'):>4} {('Y' if c['paints'] else '-'):>3} "
              f"{c['score']:6.2f} {('KEPT' if c['chosen'] else ''):>4}  "
              f"{str(c['bbox']):<26} {c['tag']}.{str(c['label'])[:38]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="selector_llm_proposed",
                     help="Which labels/<source>.json to mask from (default: selector_llm_proposed). "
                          "Run with --list_sources to see what's available.")
    ap.add_argument("--list_sources", action="store_true", help="Print available --source values and exit")
    ap.add_argument("--list_models", action="store_true",
                     help="Print --model values present in the selected --source and exit.")
    ap.add_argument("--list_runs", action="store_true",
                     help="Print the proposal runs present in the selected --source and exit "
                          "(run_id, model, capture count, policy, rules_dir, totals).")
    ap.add_argument("--file", type=str, default=None,
                     help="Id/prefix of file(s) to mask (e.g. '006' or a full filename). "
                          "Default: every file the selected --source covers.")
    ap.add_argument("--model", default=None,
                     help="Which model's proposals to use when --source has more than one for a "
                          "given file (selector_llm_proposed only). Run --list_models to see "
                          "what's available.")
    ap.add_argument("--run_id", default=None,
                     help="Mask from this proposal run only (see --list_runs). Default: the "
                          "newest run present, announced on stderr.")
    ap.add_argument("--default_deny", action="store_true",
                     help="Also redact page area that NO row covers, not just explicitly-untrusted "
                          "rows. OFF by default. It was on until the DOM oracle could test its "
                          "premise -- that an uncovered gap hides content the a11y tree missed -- "
                          "and the premise turned out to be weak: across 111 captures the tree "
                          "covers a median 99.0%% of visible DOM text, with only one capture below "
                          "70%%. Uncovered AREA is mostly whitespace, not hidden strings. Turn this "
                          "on for a worst-case view, or for a capture whose text_covered%% column "
                          "is low.")
    ap.add_argument("--no_default_deny", action="store_true",
                     help=argparse.SUPPRESS)
    ap.add_argument("--biba", action="store_true",
                     help="Mask the full Biba taint closure: every untrusted row redacts, "
                          "including `container_join`/`derived` ancestors whose verdict was "
                          "inferred from their descendants rather than asserted by a human. This "
                          "was the original behaviour. It is correct as an information-flow view "
                          "(one user-supplied comment does taint its enclosing document root) but "
                          "wrong as a redaction: masking that root blacks out the whole "
                          "screenshot, leaving only the islands that trusted rows restore. "
                          "Default (without this flag) redacts only hand-asserted verdicts.")
    ap.add_argument("--no_regions", action="store_true",
                     help="Do not draw numbered query regions on the masked screenshot, and do not "
                          "write the .regions.json manifest. Regions are ON by default: a masked "
                          "screenshot alone gives a planner no way to NAME an area it cannot read, "
                          "which is what a quarantined follow-up query needs.")
    ap.add_argument("--style", choices=["fill", "blur"], default="fill",
                     help="fill = solid black rectangle (default, strongest guarantee -- no pixels "
                          "survive). blur = Gaussian blur in place (softer, keeps rough layout).")
    ap.add_argument("--output_dir", default=MASKED_DIR)
    ap.add_argument("--explain", action="store_true",
                    help="Diagnose region selection for --file instead of masking: every ancestor "
                         "of every masked row, with the rows it owns, its clipped area, density, "
                         "score and whether it was chosen. This is how you find a region that "
                         "'should obviously be there' -- it will be in the list, and the numbers "
                         "say why it lost.")
    args = ap.parse_args()

    if args.list_sources:
        for name in _available_sources():
            print(name)
        return

    if args.list_runs:
        runs = label_store.list_runs(args.source)
        print(label_store.format_runs(runs) if runs else
              f"--source {args.source!r} is not run-indexed "
              f"(only {label_store.RUN_INDEXED_SOURCE} is).")
        return

    if args.list_models:
        for name in _available_models(args.source, args.run_id):
            print(name)
        return

    if args.explain and not args.file:
        sys.exit("--explain needs --file <capture>")

    all_rows = _load_rows(args.source, args.run_id)
    if args.model:
        available = sorted({r["model"] for r in all_rows if r.get("model")})
        if args.model not in available:
            sys.exit(f"--model {args.model!r} not found in --source {args.source!r}. "
                      f"Available: {available or '(this source has no model-tagged rows)'}")
        all_rows = [r for r in all_rows if r.get("model") == args.model]
    pre_narrow = all_rows
    all_rows, chosen_run = label_store.narrow_to_run(all_rows, args.run_id)
    if args.explain:
        matches = sorted({r["file"] for r in all_rows if r["file"].startswith(args.file)})
        if not matches:
            sys.exit(f"--file {args.file!r} matched nothing in --source {args.source!r}")
        fn = matches[0]
        rows = [r for r in all_rows if r["file"] == fn]
        _explain_regions(fn, rows)
        return

    if args.file and not any(r["file"].startswith(args.file) for r in all_rows):
        elsewhere = label_store.runs_covering(pre_narrow, args.file)
        if elsewhere and chosen_run not in elsewhere:
            sys.exit(f"Run {chosen_run!r} has no capture matching --file {args.file!r}. "
                      f"Runs that do: {elsewhere}. Re-run with --run_id <one of those>.")

    by_file = _select_rows_by_file(all_rows, args.file, args.model)
    if not by_file:
        sys.exit("Nothing to mask (all matching files were skipped -- see warnings above).")

    os.makedirs(args.output_dir, exist_ok=True)

    stats = []
    for filename in sorted(by_file):
        stat = mask_file(filename, by_file[filename], args.style, args.output_dir,
                          biba=args.biba, default_deny=args.default_deny,
                          draw_regions=not args.no_regions)
        if stat:
            stats.append(stat)

    print(f"\n{'file':45} {'rows':>6} {'untrusted':>10} {'masked':>7} {'~area%':>8} {'unverif%':>9} "
          f"{'text_cov%':>10} {'regions':>8}  output")
    print("-" * 112)
    low = []
    for s in stats:
        pct, n = s.get("text_covered_pct"), s.get("text_total") or 0
        if pct is None or n < MIN_TEXT_NODES:
            cov_col = f"{'n/a' if pct is None else f'({n} nodes)'}"
        else:
            cov_col = f"{pct:.1f}%"
            if pct < LOW_COVERAGE_PCT:
                low.append((s["file"], pct, s["text_covered"], n))
        print(f"{s['file']:45} {s['n_rows']:>6} {s['n_untrusted']:>10} {s['n_masked']:>7} "
              f"{s['approx_masked_pct']:>7.1f}% {s.get('unverified_pct', 0.0):>8.1f}% {cov_col:>10} "
              f"{s.get('n_regions', 0):>8}  {s['out_path']}")

    if low:
        print(f"\nLOW TEXT COVERAGE (<{LOW_COVERAGE_PCT:.0f}% of visible DOM text sits on any AT-SPI box) "
              f"-- consider --default_deny for these:")
        for name, pct, c, n in sorted(low, key=lambda t: t[1]):
            print(f"  {pct:5.1f}%  {c:>4}/{n:<4} {name}")
    print(f"\nMasked {len(stats)} screenshot(s) (--source {args.source}"
          f"{', --model ' + repr(args.model) if args.model else ''}, --style {args.style}) -> {args.output_dir}/")


if __name__ == "__main__":
    main()
