#!/usr/bin/env python3
"""
ground_truth_fulltree.py -- the loader and scorer for the hand-labeled full-tree ground truth
(labels/ground_truth_fulltree.json, written by label_gt.py). Sibling to ground_truth.py, which
serves the older interactable-only ground_truth.json; the two are separate on purpose and must
never be mixed (see "Joining" below).

THE EVALUATION SET IS RESTRICTED, AND THAT IS THE POINT OF THIS MODULE.
An exported row can carry its verdict for three very different reasons, and only one of them is
ground truth:

  hand            a human looked at the element on the screenshot and pressed a key.   <- SCORED
  derived         a structural container's label joined over its descendants          <- NOT scored
                  (labeling_store.derive_structural).
  container_join  same join, but for a content-tier container the page view had        <- NOT scored
                  filtered out of the drawing (label_gt.py's `c` filter) -- so it was
                  never actually shown to anyone.
  null            off-screen, zero-area or otherwise undecidable from the capture.    <- NOT scored
                  No verdict exists at all.

The two join modes are correct by construction, but they are inferences this repo made, not
evidence: scoring a labeler against them measures agreement with our own propagation rule rather
than with reality.

eval_rows() returns only the first kind: visible, hand-labeled elements. Everything that scores a
labeler goes through it, so a method can never be credited (or penalised) for a region no human
ever judged. score() additionally reports how many of a labeler's own rows fell outside the eval
set, so "it got 0.95" is never quietly computed over a fraction of what that labeler actually
claimed.

JOINING A LABELER'S ROWS TO THIS GROUND TRUTH.
Not by elem_index. Three incompatible numbering namespaces exist over this corpus:
  extraction.py            interactable affordances only
  full_tree_extraction.py  named, visible document-web nodes
  labeling_store.py        every bbox-carrying node in every content root (this one)
All three start at 0 for the same file and none of them agree. Rows are therefore joined on
(file, role, bbox, ordinal) -- both sides derive role and bbox from the same two XML attributes
(cp:screencoord/cp:size), so the match is exact; see critical_path.py's module docstring. Text is
deliberately NOT part of the key: full_tree_extraction.py truncates names at 200 chars while this
universe keeps them whole, so a long paragraph would fail to match on text alone.

Usage:
  python at-spi/ground_truth_fulltree.py --coverage
  python at-spi/ground_truth_fulltree.py --score selector_denylist_fulltree
  python at-spi/ground_truth_fulltree.py --score llm --per_file
  python at-spi/ground_truth_fulltree.py --score rule_based --errors 20
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import labeling_store as ls

HAND_MODES = frozenset({"element", "page_bulk", "page_default", "subtree", "correction", "region"})


def load(path: str | None = None) -> list[dict]:
    path = ls.EXPORT_PATH if path is None else path
    if not os.path.exists(path):
        raise SystemExit(
            f"No {os.path.relpath(path, _HERE)} yet -- run at-spi/label_gt.py to label, or "
            f"`python at-spi/label_gt.py --export` to materialize what is already in the journal.")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def eval_rows(rows: list[dict] | None = None) -> list[dict]:
    """The only rows any scoring may look at: visible, hand-labeled, with a real verdict, and
    sitting in the CONTENT tier.

    The tier condition is what makes the metric mean "did the labeler get the screen right".
    An agent sees pixels, so a row only belongs in the denominator if something is drawn at its
    box that no other scored row already accounts for -- which is exactly what
    labeling_store's tiering decides. Rows it routes to `structural` are excluded here for a
    reason recorded on each one (`skip_reason`):

      painted_by_descendants  an internal node whose `contents` name is the accname algorithm
                              concatenating what its children draw. The string is on screen; its
                              children paint it, and they are scored instead. Counting the parent
                              too judged the same pixels twice, and since every rate here is
                              row-weighted, a deeply-wrapped paragraph scored once per wrapper.
      unpainted_name          the name came from aria-label / aria-labelledby / title and the
                              node draws no graphic of its own, so the string is on no screen
                              anywhere -- landmarks ("Topbar", "sidebar"), the document title.
                              Icon controls named the same way are NOT excluded: they render a
                              graphic, so there is something to judge.
      injected_name           Chrome's own accessibility text, never rendered. 52 of the 55 web
                              nodes with no name-from at all are the single string "To get
                              missing image descriptions, open the context menu."
      embedded_frame          the interior of a cross-origin frame, which is one labelable thing
                              (an ad, a challenge widget); its own row carries the verdict and
                              the matcher propagates it down.
      echo / zero_area / offscreen / unnamed   as before.

    Labels on excluded rows are NOT lost -- they stay in the journal against their content sig and
    return automatically if the tiering ever changes.

    ONE EXCEPTION, and the reason the tier test is not simply `== "content"`: `skip_reason ==
    "unnamed"`. Every other reason above means SOME OTHER SCORED ROW ALREADY ACCOUNTS FOR THESE
    PIXELS -- the control for `control_paints`, the outer row for `echo`, the children for
    `painted_by_descendants`, the frame's own row for `embedded_frame`. `unnamed` is the single
    reason that means the opposite: nothing else covers this rectangle, which is exactly why
    label_gt.py has a structural pass and why its help text points at product photos ("reachable
    only as the unnamed box on its bounds"). Admitting a row a human deliberately clicked there
    adds no double-counting -- it adds the only pixels the content tier cannot reach.

    Measured over the journal when this was added: 2,600 hand-mode decisions sit on structural
    rows, of which 1,602 control_paints / 326 embedded_frame / 169 echo / 144
    painted_by_descendants / 109 unpainted_name / 90 sr_only / 64 other stay excluded, and 96
    `unnamed` come in.

    NAIVE ON PURPOSE, to be reworked: a photo now counts only if someone clicked it in the
    structural pass, so captures labeled before this convention under-count photos against ones
    labeled after. That is a labeling-effort dependency in a new place and belongs in the method
    section until the tiering itself reaches these rows.
    """
    rows = load() if rows is None else rows
    return [r for r in rows
            if r.get("verdict") in ("trusted", "untrusted")
            and r.get("label_mode") in HAND_MODES
            and r.get("visible")
            and (r.get("tier") == "content" or r.get("skip_reason") == "unnamed")]


def _key(file: str, role: str, bbox, ordinal: int = 0) -> tuple:
    return (file, role, tuple(bbox), ordinal)


def _index_by_key(rows: list[dict]) -> dict[tuple, dict]:
    """Keys rows by (file, role, bbox, ordinal-within-file), assigning ordinals in list order so
    the handful of genuinely identical (role, bbox) pairs in a file stay distinguishable."""
    seen: Counter = Counter()
    out = {}
    for r in rows:
        base = (r["file"], r["role"], tuple(r["bbox"]))
        out[_key(r["file"], r["role"], r["bbox"], seen[base])] = r
        seen[base] += 1
    return out


def load_source(name: str) -> list[dict]:
    path = os.path.join(ls.LABELS_DIR, f"{name}.json")
    if not os.path.exists(path):
        available = sorted(os.path.splitext(os.path.basename(p))[0]
                           for p in glob.glob(os.path.join(ls.LABELS_DIR, "*.json")))
        raise SystemExit(f"No labels/{name}.json. Available: {', '.join(available)}")
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    missing = [r for r in rows if "bbox" not in r]
    if missing:
        raise SystemExit(
            f"labels/{name}.json has {len(missing)} row(s) with no bbox -- it is an "
            f"extraction.py-universe source (interactable affordances only). Those cannot be "
            f"joined to the full-tree ground truth positionally; score them against "
            f"ground_truth.py instead.")
    return rows


def score(source: str, gt: list[dict] | None = None,
          positive: str = "untrusted") -> dict:
    """Scores labels/<source>.json against the hand-labeled, visible ground truth.

    `positive` is the class precision/recall are computed for -- untrusted by default, since that
    is the security-relevant one (a missed untrusted element is a real failure; a missed trusted
    one is a usability cost).

    Everything outside the eval set is reported rather than silently ignored: `pred_unmatched` are
    rows the labeler produced for elements no human judged (off-screen content, containers,
    decorative nodes), and `gt_unmatched` are hand-labeled elements the labeler said nothing about
    -- the latter is the number that decides whether a headline score means anything.
    """
    gt_rows = eval_rows(gt)
    gt_index = _index_by_key(gt_rows)
    pred_index = _index_by_key(load_source(source))

    counts = Counter()
    per_file: dict[str, Counter] = defaultdict(Counter)
    errors: list[dict] = []

    for key, g in gt_index.items():
        p = pred_index.get(key)
        if p is None:
            counts["gt_unmatched"] += 1
            per_file[g["file"]]["gt_unmatched"] += 1
            continue
        truth, guess = g["verdict"], p.get("verdict")
        if guess not in ("trusted", "untrusted"):
            counts["gt_unmatched"] += 1
            per_file[g["file"]]["gt_unmatched"] += 1
            continue
        if guess == positive and truth == positive:
            cell = "tp"
        elif guess == positive and truth != positive:
            cell = "fp"
        elif guess != positive and truth != positive:
            cell = "tn"
        else:
            cell = "fn"
        counts[cell] += 1
        per_file[g["file"]][cell] += 1
        if cell in ("fp", "fn"):
            errors.append({"file": g["file"], "elem_index": g["elem_index"], "cell": cell,
                           "role": g["role"], "bbox": g["bbox"], "truth": truth, "pred": guess,
                           "text": g["text"][:90]})

    counts["pred_unmatched"] = sum(1 for k in pred_index if k not in gt_index)

    tp, fp, tn, fn = counts["tp"], counts["fp"], counts["tn"], counts["fn"]
    n = tp + fp + tn + fn
    prec = tp / (tp + fp) if (tp + fp) else float("nan")
    rec = tp / (tp + fn) if (tp + fn) else float("nan")
    return {
        "source": source, "positive": positive,
        "gt_eval_rows": len(gt_rows), "compared": n,
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": prec, "recall": rec,
        "f1": 2 * prec * rec / (prec + rec) if (prec + rec) else float("nan"),
        "accuracy": (tp + tn) / n if n else float("nan"),
        "gt_unmatched": counts["gt_unmatched"], "pred_unmatched": counts["pred_unmatched"],
        "per_file": {f: dict(c) for f, c in per_file.items()},
        "errors": errors,
    }


def coverage(rows: list[dict] | None = None) -> dict:
    rows = load() if rows is None else rows
    by_mode = Counter(r.get("label_mode") for r in rows)
    ev = eval_rows(rows)
    return {
        "exported_rows": len(rows),
        "eval_rows": len(ev),
        "eval_trusted": sum(1 for r in ev if r["verdict"] == "trusted"),
        "eval_untrusted": sum(1 for r in ev if r["verdict"] == "untrusted"),
        "by_label_mode": {str(k): v for k, v in by_mode.most_common()},
        "captures_with_eval_rows": len({r["file"] for r in ev}),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", metavar="SOURCE", help="Score labels/<SOURCE>.json against the ground truth")
    ap.add_argument("--positive", choices=["untrusted", "trusted"], default="untrusted")
    ap.add_argument("--coverage", action="store_true", help="What the ground truth currently covers")
    ap.add_argument("--per_file", action="store_true", help="With --score: per-capture breakdown")
    ap.add_argument("--errors", type=int, default=0, help="With --score: print this many FP/FN rows")
    args = ap.parse_args()

    if args.coverage or not args.score:
        cov = coverage()
        print(f"exported rows        {cov['exported_rows']}")
        print(f"SCORED (hand+visible){cov['eval_rows']:>7}"
              f"   {cov['eval_trusted']} trusted / {cov['eval_untrusted']} untrusted"
              f"   across {cov['captures_with_eval_rows']} captures")
        print("\nby label_mode (only the hand modes are ever scored):")
        for mode, n in cov["by_label_mode"].items():
            mark = "  <- scored" if mode in HAND_MODES else ""
            print(f"  {mode:16} {n:7}{mark}")
        if not args.score:
            return

    r = score(args.score, positive=args.positive)
    print(f"\n{r['source']}  (positive class = {r['positive']})")
    print(f"  ground truth eval rows   {r['gt_eval_rows']}")
    print(f"  compared                 {r['compared']}")
    print(f"  TP={r['tp']} FP={r['fp']} TN={r['tn']} FN={r['fn']}")
    print(f"  precision={r['precision']:.3f}  recall={r['recall']:.3f}  "
          f"f1={r['f1']:.3f}  accuracy={r['accuracy']:.3f}")
    print(f"  gt rows this source did not label   {r['gt_unmatched']}")
    print(f"  source rows outside the eval set    {r['pred_unmatched']}  "
          f"(off-screen / container / decorative -- correctly not scored)")
    if r["gt_unmatched"] > r["compared"] * 0.25:
        print(f"  !! {r['gt_unmatched']} of {r['gt_eval_rows']} hand-labeled elements went "
              f"unscored -- treat the headline numbers as covering a subset only.")

    if args.per_file:
        print(f"\n{'file':44} {'TP':>4} {'FP':>4} {'TN':>4} {'FN':>4} {'unmatched':>9}")
        print("-" * 76)
        for f, c in sorted(r["per_file"].items()):
            print(f"{f:44} {c.get('tp',0):4} {c.get('fp',0):4} {c.get('tn',0):4} "
                  f"{c.get('fn',0):4} {c.get('gt_unmatched',0):9}")

    if args.errors:
        print(f"\nfirst {args.errors} disagreements:")
        for e in r["errors"][:args.errors]:
            print(f"  {e['cell'].upper()}  {e['file']} #{e['elem_index']:<5} {e['role'][:16]:16} "
                  f"truth={e['truth']:10} pred={e['pred']:10} {e['text']!r}")


if __name__ == "__main__":
    main()
