#!/usr/bin/env python3
"""score_report.py -- per-version-family scores, one row per capture, aggregated.

Run ids carry a timestamp so that retries and re-runs stay distinguishable, but a timestamp is not
the unit anyone reasons in: the question is "how is v6 doing", not "how did the 14:19 run do". This
groups runs by their version prefix (v3, v5, v6, legacy-flat...), keeps the NEWEST run per
(family, capture), and reports the family.

Two aggregates, because they answer different questions:
  micro -- sum the cells, then compute rates. Weights each capture by how many elements it has, so
           a 900-row page dominates. This is "what fraction of all elements are wrong".
  macro -- mean of the per-capture rates. Each capture counts once. This is "how does the method do
           on a typical page", and it is the one that shows a single bad capture as a bad capture
           rather than letting a big clean one hide it.

Usage:
  python at-spi/score_report.py                 # every family
  python at-spi/score_report.py --family v6     # one family, with the per-capture table
  python at-spi/score_report.py --detail        # per-capture table for every family
"""
import argparse
import json
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import label_store

FAMILY_RE = re.compile(r"^(v\d+|legacy[-\w]*)", re.I)


def _family(run_id: str) -> str:
    m = FAMILY_RE.match(str(run_id or ""))
    return m.group(1).lower() if m else "other"


def collect() -> dict:
    """{family: {capture: (run_id, scores)}} -- newest run per (family, capture).

    Newest by the run's own manifest mtime rather than by run_id string order: ids are only
    chronologically sortable when they happen to share a prefix and a timestamp format, and hand-
    named runs (v3-prompt-000) break that silently.
    """
    best: dict = {}
    for run in label_store.list_runs("selector_llm_proposed"):
        rid = run.get("run_id")
        meta_path = run.get("meta_path")
        if not meta_path or not os.path.exists(meta_path):
            continue
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        when = os.path.getmtime(meta_path)
        fam = _family(rid)
        for capture, sc in (meta.get("files") or {}).items():
            if not sc or sc.get("scored", 0) == 0:
                continue
            prev = best.setdefault(fam, {}).get(capture)
            if prev is None or when > prev[2]:
                best[fam][capture] = (rid, sc, when)
    return best


def _rates(sc: dict) -> tuple:
    n_u, n_t = sc["tp"] + sc["fn"], sc["tn"] + sc["fp"]
    return (100.0 * sc["fn"] / n_u if n_u else None,
            100.0 * sc["fp"] / n_t if n_t else None)


_RENAMED = {"fn_free": "leak", "fn_bound": "exempt_grammar", "fn_bound_hand": "exempt_hand",
            "fn_free_rate": "leak_rate"}


def _read_split(sc: dict) -> dict:
    """Fills the current split keys from a pre-rename score dict, in place. -> the same dict."""
    for old, new in _RENAMED.items():
        if new not in sc and old in sc:
            sc[new] = sc[old]
    return sc


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--family", default=None, help="Only this family, e.g. v6")
    ap.add_argument("--detail", action="store_true", help="Per-capture table for every family")
    args = ap.parse_args()

    best = collect()
    if not best:
        sys.exit("No scored runs found. A run only lands here if it was run with --score.")

    for fam in sorted(best, key=lambda f: (not f.startswith("v"), f)):
        if args.family and fam != args.family.lower():
            continue
        caps = best[fam]
        tp = sum(s["tp"] for _r, s, _w in caps.values())
        fp = sum(s["fp"] for _r, s, _w in caps.values())
        tn = sum(s["tn"] for _r, s, _w in caps.values())
        fn = sum(s["fn"] for _r, s, _w in caps.values())
        fn_rates = [r for r, _ in map(_rates, (s for _r, s, _w in caps.values())) if r is not None]
        fp_rates = [r for _, r in map(_rates, (s for _r, s, _w in caps.values())) if r is not None]
        prec = tp / (tp + fp) if tp + fp else float("nan")
        rec = tp / (tp + fn) if tp + fn else float("nan")
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else float("nan")
        print(f"\n=== {fam}  ({len(caps)} capture(s), {tp+fp+tn+fn} scored elements) ===")
        print(f"  micro  TP={tp} FP={fp} TN={tn} FN={fn}   "
              f"FN={100*fn/(tp+fn) if tp+fn else 0:.1f}%  FP={100*fp/(tn+fp) if tn+fp else 0:.1f}%  "
              f"P={prec:.3f} R={rec:.3f} F1={f1:.3f}")
        for _r, sc, _w in caps.values():
            _read_split(sc)
        if any("leak" in s for _r, s, _w in caps.values()):
            free = sum(s.get("leak", 0) for _r, s, _w in caps.values())
            bound = sum(s.get("shown_by_rules_grammar",
                                s.get("exempt_grammar", 0)) for _r, s, _w in caps.values())
            hand = sum(s.get("shown_by_rules_hand",
                             s.get("exempt_hand", 0)) for _r, s, _w in caps.values())
            stale = sum(1 for _r, s, _w in caps.values() if "leak" not in s)
            print(f"         leaks={free}  shown by rules: grammar={bound}"
                  + (f", hand={hand}" if hand else "")
                  + f"   leak rate={100*free/(tp+fn) if tp+fn else 0:.2f}%"
                  + (f"   [{stale} capture(s) scored before the split, not included]"
                     if stale else ""))
        if any("bounded" in s for _r, s, _w in caps.values()):
            bd = {k: sum(s.get(k, 0) for _r, s, _w in caps.values()) for k in
                  ("bounded", "bounded_masked", "shown_by_rules", "shown_by_shortcut",
                   "shown_by_rules_bounded", "bounded_grammar", "bounded_hand",
                   "fp_avoided_by_shortcut")}
            if bd["shown_by_rules"] or bd["shown_by_shortcut"]:
                print(f"         shown by rules={bd['shown_by_rules']}"
                      f" (of which free={free}, bounded={bd['shown_by_rules_bounded']}),"
                      f"  shown by shortcut={bd['shown_by_shortcut']}")
            if bd["bounded"]:
                print(f"         bounded={bd['bounded']}"
                      f" (grammar={bd['bounded_grammar']}, hand={bd['bounded_hand']})"
                      f"  ->  masked anyway={bd['bounded_masked']}"
                      f" ({100*bd['bounded_masked']/bd['bounded']:.1f}%)")
            if bd["fp_avoided_by_shortcut"]:
                print(f"         over-masking avoided by the shortcut: "
                      f"{bd['fp_avoided_by_shortcut']} trusted row(s)")
        if fn_rates:
            print(f"  macro  FN={statistics.mean(fn_rates):.1f}%  FP={statistics.mean(fp_rates):.1f}%"
                  f"   (median FN={statistics.median(fn_rates):.1f}%, "
                  f"FP={statistics.median(fp_rates):.1f}%)")
        if args.detail or args.family:
            print(f"  {'capture':34s} {'TP':>4} {'FP':>4} {'TN':>4} {'FN':>4} {'FN%':>7} {'FP%':>7}  run")
            for cap, (rid, s, _w) in sorted(caps.items()):
                fr, pr = _rates(s)
                fr_s = "      -" if fr is None else f"{fr:6.1f}%"
                pr_s = "      -" if pr is None else f"{pr:6.1f}%"
                print(f"  {cap:34s} {s['tp']:>4} {s['fp']:>4} {s['tn']:>4} {s['fn']:>4} "
                      f"{fr_s:>7} {pr_s:>7}  {rid}")


if __name__ == "__main__":
    main()
