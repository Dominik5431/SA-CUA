#!/usr/bin/env python3
"""dom_curation.py -- use the DOM capture, ONCE and OFFLINE, to drop boxes that are not on screen.

THE CONTRACT. The DOM is an oracle, not an input. It decides which rows are eligible to be
labeled and scored; it never informs a trust prediction. `labeling_store` stays a11y-only at
predict time, which is what keeps `dom_geom/` usable as held-out evidence for how much a
purely a11y-based method misses. The exclusion set this script writes is a frozen, committed
artifact -- not something recomputed on every build, or a re-capture would silently move the
denominators of every score.

WHAT IT CATCHES. Chrome reports plausible coordinates for elements that were never laid out.
On 001_stackoverflow the redirect-modal wrapper is announced at (861, 597) 253x207 -- dead
centre of the viewport, showing=true -- while the DOM places it at (544, 51757) with height 0
and visible=false, at the bottom of a 53,906px document. These are the boxes that "seem to
not display anything": there is nothing to look at, so a human cannot judge them and a label
for them means nothing. They are UNLABELABLE, not mislabeled.

No a11y-internal signal separates them. Measured over 1,102 textless regions against the
DOM's verdict: role class fires on 74.7% of ghosts and 66.7% of real ones; childlessness on
97.3% vs 97.9%; escaping the parent box on 11.1% vs 6.6%; a sibling covering the box on
34.5% vs 17.4%. Against a 53% base rate, the best precision available is 56%. The
information is simply not in the tree, which is the whole reason this script exists.

THE TEST is exact-rect, not overlap. AT-SPI boxes are copied from DOM layout boxes, so the
question is "was an element laid out at precisely this box", answered within a couple of
pixels. Overlap is useless: <html> covers the whole page, so every box overlaps something.

SCOPE -- textless rows only, and only the ones whose ink cannot be verified any other way:

  * A row with TEXT verifies itself. Its glyphs are the evidence. (It has its own failure
    mode -- screen-reader-only text IS laid out, that being the point of the `clip` trick --
    but layout cannot see that, and it is handled separately by the sr_only rule.)
  * A GRAPHIC row is NOT excluded here. An a11y image box is routinely the wrapper <div>
    rather than the <img>, so the <img> sits INSIDE the box instead of matching it and an
    exact-rect test calls a real product photo a ghost. Deciding those needs a containment
    test plus computed background/border, which dom_capture does not record yet.
  * That leaves textless GROUP/SURFACE rows -- the `section`/`panel`/`landmark` wrappers,
    which is exactly the modal class.

Laid out is not the same as painted, and this script only answers the first. Stack Exchange's
`li.pb24` sidebar spacer is a genuine 165x24 element that paints nothing; the DOM correctly
says "laid out" and it stays in. Catching that needs computed style, not layout.

Keyed by `sig`, like every decision in the journal, so the exclusion survives a universe
rebuild the same way a label does.

Usage:
  python at-spi/dom_curation.py --limit 20            # dry run over the first 20 captures
  python at-spi/dom_curation.py --limit 20 --apply    # write labels/dom_excluded.json
  python at-spi/dom_curation.py --apply               # whole corpus
  python at-spi/dom_curation.py --show 001            # what was excluded on one capture
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import labeling_store as ls
import role_classes as rc

GEOM_DIR = os.path.join(_HERE, "dom_geom")
EXCLUDED_PATH = os.path.join(_HERE, "labels", "dom_excluded.json")

SCHEMA = 2
TOLERANCE = 2

CURATED_CLASSES = frozenset({"GROUP", "SURFACE"})

MIN_COVER = 0.5
UNION_CAP = 400


def load_dom_boxes(filename: str, geom_dir: str = GEOM_DIR) -> tuple[set | None, str]:
    """-> (set of laid-out screen boxes, status). None when this capture cannot serve as an
    oracle, with the status saying why -- callers must treat that as "no opinion", never as
    "nothing is laid out"."""
    path = os.path.join(geom_dir, os.path.splitext(filename)[0] + ".json")
    if not os.path.exists(path):
        return None, "no_dom_capture"
    with open(path, encoding="utf-8") as f:
        geom = json.load(f)
    if not (geom.get("calibration") or {}).get("origin"):
        return None, "uncalibrated"
    boxes = {
        tuple(n["screen"])
        for frame in geom.get("frames", [])
        for n in frame.get("nodes", [])
        if n.get("screen") and n.get("visible") and n["screen"][2] > 1 and n["screen"][3] > 1
    }
    if not boxes:
        return None, "no_visible_dom_nodes"
    return boxes, "ok"


def laid_out(boxes: set, bbox: list, tol: int = TOLERANCE) -> bool:
    """Was an element laid out at PRECISELY this box?"""
    x, y, w, h = bbox
    for dx in range(-tol, tol + 1):
        for dy in range(-tol, tol + 1):
            for dw in (-1, 0, 1):
                for dh in (-1, 0, 1):
                    if (x + dx, y + dy, w + dw, h + dh) in boxes:
                        return True
    return False


def _union_area(rects: list) -> int:
    """Area covered by a set of rectangles, counting overlap once. Coordinate-compression
    sweep: exact, and O(n^2 log n) on a list that is short in every case that reaches here."""
    xs = sorted({r[0] for r in rects} | {r[0] + r[2] for r in rects})
    total = 0
    for x0, x1 in zip(xs, xs[1:]):
        spans = sorted((r[1], r[1] + r[3]) for r in rects if r[0] <= x0 and r[0] + r[2] >= x1)
        if not spans:
            continue
        h, cs, ce = 0, *spans[0]
        for s, e in spans[1:]:
            if s > ce:
                h += ce - cs
                cs, ce = s, e
            else:
                ce = max(ce, e)
        total += (x1 - x0) * (h + ce - cs)
    return total


def enclosed(boxes: set, bbox: list, pad: int = 1) -> list:
    """The laid-out boxes that sit entirely inside `bbox`."""
    x, y, w, h = bbox
    return [b for b in boxes
            if b[0] >= x - pad and b[1] >= y - pad
            and b[0] + b[2] <= x + w + pad and b[1] + b[3] <= y + h + pad]


def explained_by_contents(boxes: set, bbox: list, min_cover: float = MIN_COVER) -> bool:
    """Is this box accounted for by real content sitting inside it?

    The guard that keeps real content out of the exclusion set. An a11y box frequently belongs
    to a wrapper whose own rect the DOM reports slightly differently, so failing the exact
    match is not on its own evidence of a ghost -- but a wrapper around something real is
    FILLED BY that something. Amazon's `section.aok-relative` product-image container fails the
    exact match and is 99% covered by the <img> inside it.

    The test has to be coverage, not mere presence. A ghost box is announced dead centre of the
    viewport, and the centre of a page is where the content is, so "does this rectangle contain
    anything at all" is nearly always yes for reasons that have nothing to do with the box. On
    000_stackoverflow the redirect-modal wrapper -- the same 253x207 rectangle the DOM places
    at y=53465 with height 0 and visible=false -- encloses two `span.hljs-number` syntax-
    highlight spans from the code block behind it, together 2.7% of its area, and a presence
    test rescues it. The identical rectangle on 001 and 002 happens to land on wider code lines,
    encloses nothing, and is excluded. Same ghost, opposite verdicts, decided by the width of
    the text underneath.

    Coverage separates the two with room to spare. Measured over every candidate in the first
    20 captures: real wrappers land at 0.988-0.993 single-child coverage and 1.000 union; every
    ghost lands at or below 0.34. Nothing falls between 0.34 and 0.99, so the threshold is
    sitting in an empty band rather than being tuned against a tail.
    """
    x, y, w, h = bbox
    area = w * h
    if area <= 0:
        return False
    inside = enclosed(boxes, bbox)
    if not inside:
        return False
    if max(b[2] * b[3] for b in inside) >= min_cover * area:
        return True
    if len(inside) > UNION_CAP:
        return True
    return _union_area(inside) >= min_cover * area


def curate_file(fu: dict, boxes: set) -> list[dict]:
    """-> the rows of one capture the DOM says are not on screen: nothing laid out at the box,
    and nothing laid out inside it either."""
    out = []
    for row in fu["rows"]:
        if row["tier"] != "content" or not row["visible"] or row["text"]:
            continue
        if rc.paint_class(row["role"]) not in CURATED_CLASSES:
            continue
        if laid_out(boxes, row["bbox"]) or explained_by_contents(boxes, row["bbox"]):
            continue
        out.append({"sig": row["sig"], "elem_index": row["elem_index"], "role": row["role"],
                    "bbox": row["bbox"], "cls": row["attr_class"], "id": row["attr_id"]})
    return out


def build(universe: dict, limit: int | None = None) -> dict:
    files: dict[str, dict] = {}
    for filename, fu in sorted(universe.items()):
        if limit is not None and ls._numeric_prefix(filename) >= limit:
            continue
        boxes, status = load_dom_boxes(filename)
        if boxes is None:
            files[filename] = {"status": status, "excluded": []}
            continue
        files[filename] = {"status": "ok", "dom_boxes": len(boxes),
                           "excluded": curate_file(fu, boxes)}
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tolerance_px": TOLERANCE,
        "min_cover": MIN_COVER,
        "curated_classes": sorted(CURATED_CLASSES),
        "note": ("DOM-derived. Rows listed here had no laid-out DOM box and are excluded "
                 "from labeling and scoring. Curation only -- never an input to a "
                 "prediction. See dom_curation.py."),
        "files": files,
    }


def load_excluded(path: str = EXCLUDED_PATH) -> dict[str, set]:
    """-> {filename: {sig, ...}}. Empty when the artifact has not been generated, so the
    pipeline runs unchanged without it."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    return {fn: {e["sig"] for e in rec.get("excluded", [])}
            for fn, rec in doc.get("files", {}).items()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write labels/dom_excluded.json")
    ap.add_argument("--limit", type=int, help="only captures with index < N")
    ap.add_argument("--show", help="print what was excluded on captures matching this token")
    args = ap.parse_args()

    ls.DOM_CURATION_ENABLED = False
    universe = ls.build_universe(use_cache=False, write_cache=False)

    doc = build(universe, limit=args.limit)
    files = doc["files"]

    if args.show:
        for filename, rec in files.items():
            if args.show not in filename:
                continue
            print(f"=== {filename}  [{rec['status']}]")
            for e in rec["excluded"]:
                print(f"   #{e['elem_index']:<5} {e['role']:10} {str(e['bbox']):26} "
                      f"cls={(e['cls'] or '')[:38]}")
            if not rec["excluded"]:
                print("   (nothing excluded)")
        return

    status = collections.Counter(r["status"] for r in files.values())
    n_excl = sum(len(r["excluded"]) for r in files.values())
    by_role = collections.Counter(e["role"] for r in files.values() for e in r["excluded"])

    usable = [f for f, r in files.items() if r["status"] == "ok"]
    considered = sum(
        1 for f in usable for row in universe[f]["rows"]
        if row["tier"] == "content" and row["visible"] and not row["text"]
        and rc.paint_class(row["role"]) in CURATED_CLASSES)

    print(f"captures            {len(files)}")
    for k, v in status.most_common():
        print(f"   {v:4d}  {k}")
    print(f"\ncandidate rows      {considered}   (visible, content-tier, textless, GROUP/SURFACE)")
    print(f"excluded            {n_excl}"
          + (f"   ({n_excl / considered:.1%})" if considered else ""))
    for role, c in by_role.most_common(8):
        print(f"   {c:4d}  {role}")

    worst = sorted(((len(r["excluded"]), f) for f, r in files.items() if r["excluded"]),
                   reverse=True)[:12]
    if worst:
        print("\nmost excluded:")
        for n, f in worst:
            print(f"   {n:4d}  {f}")

    decided = ls.effective_decisions()
    hit = []
    for filename, rec in files.items():
        sigs = {e["sig"] for e in rec["excluded"]}
        for sig, d in (decided.get(filename) or {}).items():
            if sig in sigs:
                hit.append((filename, sig, d["verdict"], d["mode"]))
    if hit:
        print(f"\n{len(hit)} excluded rows already carry a decision (kept in the journal, "
              f"dropped from scoring):")
        for f, sig, v, m in hit[:15]:
            print(f"   {f[:38]:40} {sig}  {v:9} {m}")

    if args.apply:
        os.makedirs(os.path.dirname(EXCLUDED_PATH), exist_ok=True)
        with open(EXCLUDED_PATH, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
        print(f"\nwrote {EXCLUDED_PATH}")
        print("run `python at-spi/labeling_store.py --stats` or restart label_gt.py to pick it up")
    else:
        print("\ndry run -- nothing written. re-run with --apply")


if __name__ == "__main__":
    main()
