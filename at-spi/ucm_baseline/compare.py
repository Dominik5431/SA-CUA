#!/usr/bin/env python3
"""Put a UCM baseline run beside one of our own rule sets, on identical rows.

    python -m ucm_baseline.compare --run ucm_runs/<id> --rules llm_rules_container_v14 \
                                   --rules llm_rules_container_v15

The comparison is only meaningful if three things are held fixed, and this script is mostly the
machinery for holding them:

  THE SAME CAPTURES. Our labeler runs on all 160; UCM can only run on the 115 that have a DOM.
  Scoring our rules over 160 and UCM over 115 and printing the two totals side by side would be
  a comparison between different corpora. Our rules are re-scored here on UCM's 115.

  THE SAME SCORER. Both sides come out of selector_propose.score_file. UCM's verdicts arrive
  through its `page` argument; ours through predict_page as usual.

  THE SAME CAPACITY SETTING. sp.CAPACITY_EXEMPTION rewrites verdicts for capacity-bound rows and
  is a mechanism of ours that UCM does not have. UCM is therefore scored with it OFF, and the
  OFF column is the one our rules must be read from. The ON column is printed for our rules only,
  as the number the rest of this project quotes.

WHAT IS NOT HELD FIXED, deliberately: UCM is a denylist (default trusted, a selector marks a
region untrusted) and ours is an allowlist (default untrusted, a rule grants trust). That is the
substance of the difference, not a nuisance parameter -- it is why the interesting column is
the leak count, not F1.
"""
import argparse
import json
import os
import sys

_AT_SPI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _AT_SPI not in sys.path:
    sys.path.insert(0, _AT_SPI)
if os.path.join(_AT_SPI, "labelers") not in sys.path:
    sys.path.insert(0, os.path.join(_AT_SPI, "labelers"))

import labeling_store as ls
from labelers import selector_propose as sp

from . import run_ucm


def load_rules(rules_dir: str, filename: str) -> "list | None":
    stem = filename.replace(".xml", "")
    hits = [n for n in os.listdir(rules_dir)
            if n.startswith(stem + "__") and n.endswith(".json")
            and not n.endswith(".proposed.json")]
    if not hits:
        return None
    with open(os.path.join(rules_dir, sorted(hits)[0]), encoding="utf-8") as f:
        data = json.load(f)
    return data.get("rules", data) if isinstance(data, dict) else data


def score_rules(rules_dir: str, files: list, universe: dict, exemption: bool) -> list:
    saved = sp.CAPACITY_EXEMPTION
    sp.CAPACITY_EXEMPTION = exemption
    try:
        out = []
        for f in files:
            rules = load_rules(rules_dir, f)
            if rules is None:
                continue
            s = sp.score_file(f, rules, universe=universe)
            s.pop("errors", None)
            s["in_sample"] = False
            out.append(s)
        return out
    finally:
        sp.CAPACITY_EXEMPTION = saved


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="A ucm_runs/<id> directory with score.json")
    ap.add_argument("--rules", action="append", default=[],
                    help="A rules directory to score alongside; repeatable")
    args = ap.parse_args()

    with open(os.path.join(args.run, "score.json"), encoding="utf-8") as f:
        ucm = json.load(f)
    ucm_files = sorted(r["file"] for r in ucm["per_capture"])
    universe = ls.build_universe()

    hdr = (f"{'':44s} {'captures':>8s} {'leaks':>7s} {'shown by':>9s} {'shown by':>9s} "
           f"{'masked':>8s} {'over':>7s} {'over-mask':>10s} {'bounded':>8s} {'of those':>9s} "
           f"{'F1':>6s}")
    hdr2 = (f"{'configuration':44s} {'':8s} {'':7s} {'rules':>9s} {'shortcut':>9s} "
            f"{'correctly':>8s} {'masked':>7s} {'rate':>10s} {'rows':>8s} {'masked':>9s} "
            f"{'':6s}")

    def line(label, rows):
        t = run_ucm.totals(rows)
        return (f"{label:44s} {t['captures']:8d} {t['leak']:7d} {t['shown_by_rules']:9d} "
                f"{t['shown_by_shortcut']:9d} {t['tp']:8d} {t['fp']:7d} "
                f"{100 * t['fp_rate']:9.2f}% {t['bounded']:8d} "
                f"{100 * t['bounded_masked_rate']:8.1f}% {t['f1']:6.3f}")

    print(f"\n{len(ucm_files)} captures, identical rows on both sides\n")
    print("=" * len(hdr))
    print(hdr)
    print(hdr2)
    print("-" * len(hdr))
    print(line("UCM generic (exemption n/a)", ucm["per_capture"]))
    for rules_dir in args.rules:
        name = os.path.basename(rules_dir.rstrip("/"))
        for flag in (False, True):
            rows = score_rules(rules_dir, ucm_files, universe, flag)
            print(line(f"ours {name} (capacity {'ON ' if flag else 'OFF'})", rows))
    print("=" * len(hdr))
    print("\nleaks is the security invariant: an untrusted row left visible whose text could have\n"
          "carried an instruction. It is the only column that has to be zero.\n"
          "\nshown by rules  the LLM rules called an untrusted row trusted. leaks is its free-text\n"
          "                part; the rest is bounded content, which is a rule-quality finding.\n"
          "shown by shortcut  the capacity exemption released a bounded value on purpose. These\n"
          "                are held OUT of the confusion matrix -- a deliberate choice is not a\n"
          "                classification error, and counting it as one made F1 describe our\n"
          "                policy instead of the rule set.\n"
          "over-masked     a trusted row painted anyway: the cost side.\n"
          "bounded rows / of those masked  how much bounded content exists and how much the mask\n"
          "                covered regardless. Bounded is a fallback, never a licence to show.")


if __name__ == "__main__":
    main()
