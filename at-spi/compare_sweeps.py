#!/usr/bin/env python3
"""compare_sweeps.py -- compare two or more selector sweeps on the captures they ALL cover.

WHY THE INTERSECTION IS THE ONLY HONEST UNIT. Every sweep ran a different capture set: v15 160,
v17 47 scored, v18 116 (the subset that fits a 32k context). A directory's own totals therefore
say as much about which captures it skipped as about the model that produced it -- a sweep that
dropped the text-heavy web pages looks better on every rate for free. So each table below is
computed on the set of captures common to the runs being compared, and the header always prints
how many that is and what each run had to start with.

THE FN SPLIT IS THE POINT. Pooled `fn` mixes two different events and must never be quoted alone:

  leak                   a FREE-TEXT row a rule showed. The one number that must be zero.
  shown_by_rules_bounded a capacity-BOUND row a rule showed. A rule-quality finding, not a leak:
                         the row cannot carry adversarial content in the first place.
  shown_by_shortcut      bounded rows capacity.py exempts on purpose. Never enters fn at all;
                         printed only so the exemption's size stays visible.

STAGES. `final` is post-note-audit, `proposed` is the model's own answer before it. The audit can
only ever DROP a rule, so the difference between the two stages is exactly what the audit bought
(closed leaks) and what it cost (extra over-masking). Both are printed.

Feed it the JSON that eval_rules.py writes:

  for d in llm_rules_container_v15 llm_rules_container_v17_suites_or llm_rules_container_v18; do
    for st in final proposed; do
      python eval_rules.py --rules_dir $d --policy allowlist --stage $st --json /tmp/eval_$d_$st.json
    done
  done
  python compare_sweeps.py v15=/tmp/eval_..._final.json,/tmp/eval_..._proposed.json ...

Usage:
  python compare_sweeps.py --dir /tmp/evals v15 v17 v18      # expects eval_<tag>_<stage>.json
  python compare_sweeps.py --dir /tmp/evals --stage final v15 v18
"""
import argparse
import json
import os
import sys

KEYS = ("tp", "fp", "tn", "fn", "leak", "shown_by_rules_bounded", "shown_by_shortcut",
        "bounded", "ambiguous", "orphaned")


def load(directory: str, tag: str, stage: str) -> "dict | None":
    path = os.path.join(directory, f"eval_{tag}_{stage}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        blob = json.load(fh)
    return {rec["file"]: rec for rec in blob["files"]}


def totals(files: dict, common) -> dict:
    out = {k: 0 for k in KEYS}
    for capture in common:
        elem = files[capture]["elem"]
        for k in KEYS:
            out[k] += elem.get(k, 0)
    return out


def scored(t: dict) -> int:
    """Rows that entered the confusion matrix. NOT the row count of the capture: shortcut-exempt
    rows and ambiguous same-pixel rows are excluded upstream by eval_rules."""
    return t["tp"] + t["fp"] + t["tn"] + t["fn"]


def report(directory: str, tags: list, stage: str) -> None:
    runs = {}
    for tag in tags:
        files = load(directory, tag, stage)
        if files is None:
            print(f"  (no {stage} data for {tag} in {directory}; skipping this stage)")
            return
        runs[tag] = files
    common = sorted(set.intersection(*(set(f) for f in runs.values())))
    if not common:
        print(f"  no captures common to {', '.join(tags)}")
        return

    width = 15
    print(f"\n{'=' * (10 + width * len(tags))}")
    print(f"STAGE = {stage}   common captures: {len(common)}   "
          + "   ".join(f"{t} covers {len(runs[t])}" for t in tags))
    print('=' * (10 + width * len(tags)))
    print(f"{'':<10}" + "".join(f"{t:>{width}}" for t in tags))

    rows = {t: totals(runs[t], common) for t in tags}
    n = {t: scored(rows[t]) for t in tags}

    def line(label, fmt):
        print(f"{label:<10}" + "".join(f"{fmt(rows[t], n[t]):>{width}}" for t in tags))

    line("scored", lambda r, k: f"{k:,}")
    line("TP", lambda r, k: f"{r['tp']:,}")
    line("FP", lambda r, k: f"{r['fp']:,} ({100 * r['fp'] / k if k else 0:.1f}%)")
    line("TN", lambda r, k: f"{r['tn']:,}")
    line("FN", lambda r, k: f"{r['fn']:,} ({100 * r['fn'] / k if k else 0:.1f}%)")
    print('-' * (10 + width * len(tags)))
    line("LEAK", lambda r, k: f"{r['leak']:,} ({100 * r['leak'] / k if k else 0:.2f}%)")
    line("fn-bound", lambda r, k: f"{r['shown_by_rules_bounded']:,}")
    line("shortcut", lambda r, k: f"{r['shown_by_shortcut']:,}")
    print('-' * (10 + width * len(tags)))
    for tag in tags:
        leaky = [c for c in common if runs[tag][c]["elem"].get("leak", 0)]
        head = ", ".join(sorted(leaky)[:4]) + (" ..." if len(leaky) > 4 else "")
        print(f"  {tag}: {len(leaky)} capture(s) with leak > 0" + (f"  [{head}]" if leaky else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tags", nargs="+", help="Run tags; each needs eval_<tag>_<stage>.json in --dir.")
    ap.add_argument("--dir", default=".", help="Directory holding the eval_rules --json dumps.")
    ap.add_argument("--stage", choices=("final", "proposed", "both"), default="both")
    args = ap.parse_args()

    stages = ("final", "proposed") if args.stage == "both" else (args.stage,)
    for stage in stages:
        report(args.dir, args.tags, stage)


if __name__ == "__main__":
    sys.exit(main())
