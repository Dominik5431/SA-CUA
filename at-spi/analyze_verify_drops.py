#!/usr/bin/env python3
"""
analyze_verify_drops.py -- what the note audit (verify_notes.py) removed from a finished sweep,
recovered from disk alone. No model call, no re-audit.

WHY THIS IS RECOVERABLE AT ALL. Since v13 every sweep writes both halves of the pass:

    <capture>__<model>.proposed.json   the rule set as PROPOSED, handed to the audit
    <capture>__<model>.json            the survivors

verify_notes never edits a rule and never adds one -- `kept = [r for n, r in enumerate(rules)
if n not in drop_idx]` -- so the final file is always an order-preserving SUBSEQUENCE of the
proposal, and the drop set is the exact complement. That is what --strict checks per capture;
if the alignment ever fails, the two files are not two stages of one pass and the diff is
meaningless. It holds on every v13..v17 capture as of 2026-09-15.

v11 and v12 have no proposals on disk and cannot be analysed this way; re-auditing their rule
files would audit the first audit's survivors, which is a different experiment.

TWO DENOMINATORS, and they answer different questions:

  dropped / proposed   share of the model's answer the audit threw away.
  dropped / audited    the audit's own hit rate. Only rules that actually reach >= MIN_COVERED_ROWS
                       content rows are shown to the auditor (verify_notes.inheriting_rules); a
                       rule covering nothing is never at risk, so it does not belong in the
                       denominator of "how often did the auditor rule against a rule".

`audited` and `rows` are RECOMPUTED TODAY against the current universe walk, not read off the
sweep. The drop counts are historical and exact; those two columns move if labeling_store's
geometry or content-root rules changed since the sweep ran. --no_rows skips the rebuild.

The sweep logs carry what disk cannot: the auditor's stated reason per drop, and the
ruled-against vs never-ruled-on split of a multi-sample audit.

    grep 'samples: kept' at-spi/logs/v15-newprompt-*.log

Usage:
  python at-spi/analyze_verify_drops.py                              # every dir with proposals
  python at-spi/analyze_verify_drops.py --rules_dir at-spi/llm_rules_container_v15 --per_file
  python at-spi/analyze_verify_drops.py --rules_dir ... --notes      # print every dropped note
  python at-spi/analyze_verify_drops.py --json drops.json
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
sys.path.insert(0, os.path.join(_HERE, "labelers"))

PROPOSED_SUFFIX = ".proposed.json"


def _canon(rule: dict) -> str:
    """Comparison key. Underscore-prefixed keys are bookkeeping the audit does not see."""
    return json.dumps({k: v for k, v in sorted(rule.items()) if not k.startswith("_")},
                      sort_keys=True, ensure_ascii=False)


def split_drops(pre: list, post: list) -> "tuple[list, list, bool]":
    """(dropped, kept, aligned) by order-preserving subsequence match.

    Positional, not set-based: two rules on a page can be equal as values, and matching by value
    alone would credit the wrong one. `aligned` is False when `post` is not a subsequence of
    `pre` -- i.e. when the pair is not one audit of one proposal.
    """
    dropped, kept, j = [], [], 0
    for rule in pre:
        if j < len(post) and _canon(post[j]) == _canon(rule):
            kept.append(rule)
            j += 1
        else:
            dropped.append(rule)
    return dropped, kept, j == len(post)


def pairs(rules_dir: str, model: str | None) -> "list[tuple[str, str, str]]":
    """[(capture xml, proposed path, final path)] for every capture with BOTH halves."""
    out = []
    for prop in sorted(glob.glob(os.path.join(rules_dir, "*" + PROPOSED_SUFFIX))):
        final = prop[: -len(PROPOSED_SUFFIX)] + ".json"
        if not os.path.exists(final):
            print(f"  WARNING: {os.path.basename(prop)} has no post-audit file -- skipped",
                  file=sys.stderr)
            continue
        stem = os.path.basename(prop)[: -len(PROPOSED_SUFFIX)]
        capture, _, tag = stem.rpartition("__")
        if not capture:
            capture, tag = stem, ""
        if model and model not in tag:
            continue
        out.append((capture + ".xml", prop, final))
    return out


def analyse(rules_dir: str, model: str | None, universe: "dict | None") -> dict:
    from labelers import selector_propose as sp
    from labelers import verify_notes as vn

    per_file, misaligned = [], []
    notes: list[tuple[str, dict]] = []
    for capture, prop_path, final_path in pairs(rules_dir, model):
        pre = json.load(open(prop_path))
        post = json.load(open(final_path))
        dropped, _kept, aligned = split_drops(pre, post)
        if not aligned:
            misaligned.append(capture)
        row = {"capture": capture, "proposed": len(pre), "kept": len(post),
               "dropped": len(dropped), "audited": None, "rows_dropped": None,
               "rows_dropped_free": None, "rows_covered": None}
        fu = (universe or {}).get(capture)
        if fu is not None:
            inheriting = vn.inheriting_rules(fu, pre, sp)
            row["audited"] = len(inheriting)
            by_index = {n: covered for n, _r, covered in inheriting}
            drop_idx = {n for n, rule in enumerate(pre)
                        if any(rule is d for d in dropped)}
            rows_dropped = {r["elem_index"]: r for n in drop_idx for r in by_index.get(n, [])}
            rows_all = {r["elem_index"] for _n, covered in by_index.items() for r in covered}
            row["rows_dropped"] = len(rows_dropped)
            row["rows_dropped_free"] = sum(1 for r in rows_dropped.values()
                                           if not r.get("capacity"))
            row["rows_covered"] = len(rows_all)
        per_file.append(row)
        for rule in dropped:
            notes.append((capture, rule))
    return {"rules_dir": rules_dir, "per_file": per_file, "misaligned": misaligned,
            "notes": notes}


def _totals(per_file: list) -> Counter:
    t = Counter()
    for r in per_file:
        for k in ("proposed", "kept", "dropped", "audited", "rows_dropped",
                  "rows_dropped_free", "rows_covered"):
            if r[k] is not None:
                t[k] += r[k]
    t["captures"] = len(per_file)
    t["captures_with_a_drop"] = sum(1 for r in per_file if r["dropped"])
    return t


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:5.1f}%" if b else "    --"


def report(res: dict, per_file: bool, notes: bool) -> Counter:
    t = _totals(res["per_file"])
    print(f"\n{os.path.basename(res['rules_dir'])}")
    print(f"  captures            {t['captures']:>6}   ({t['captures_with_a_drop']} lost at least one rule)")
    print(f"  rules proposed      {t['proposed']:>6}")
    from labelers.verify_notes import MIN_COVERED_ROWS
    known = any(r["audited"] is not None for r in res["per_file"])
    if known:
        print(f"  rules audited       {t['audited']:>6}   {_pct(t['audited'], t['proposed'])} of proposed"
              f"   (the rest reach fewer than {MIN_COVERED_ROWS} content row(s) and are never shown)")
    else:
        print("  rules audited            ?   (--no_rows: the universe was not rebuilt)")
    print(f"  rules DROPPED       {t['dropped']:>6}   {_pct(t['dropped'], t['proposed'])} of proposed"
          + (f"   {_pct(t['dropped'], t['audited'])} of audited" if known else ""))
    print(f"  rules kept          {t['kept']:>6}")
    if t["rows_covered"]:
        print(f"  content rows under an audited rule {t['rows_covered']:>6}")
        print(f"  ... under a DROPPED rule           {t['rows_dropped']:>6}   "
              f"{_pct(t['rows_dropped'], t['rows_covered'])}  (trusted pre-audit, masked after)")
        print(f"  ... of those, FREE TEXT            {t['rows_dropped_free']:>6}   "
              f"{_pct(t['rows_dropped_free'], t['rows_dropped'])} -- the only half that can "
              f"carry a leak; the rest is capacity-bound and exempt anyway")
    if t["proposed"] and not t["dropped"]:
        print("  -> ZERO drops over the whole directory: this sweep ran --no-verify, so the two "
              "files are one unaudited proposal written twice. Check the log's '### extra flags'.")
    if res["misaligned"]:
        print(f"  !! {len(res['misaligned'])} capture(s) where the final file is NOT a subsequence "
              f"of the proposal -- the diff is unreliable for them: "
              f"{', '.join(res['misaligned'][:5])}")
    if per_file:
        print(f"\n  {'capture':<44} {'prop':>5} {'aud':>5} {'drop':>5} {'rows':>6}")
        for r in sorted(res["per_file"], key=lambda r: -r["dropped"]):
            if not r["dropped"] and not per_file:
                continue
            print(f"  {r['capture']:<44} {r['proposed']:>5} "
                  f"{('-' if r['audited'] is None else r['audited']):>5} {r['dropped']:>5} "
                  f"{('-' if r['rows_dropped'] is None else r['rows_dropped']):>6}")
    if notes:
        print("\n  DROPPED RULES")
        for capture, rule in res["notes"]:
            where = " > ".join(f"{s.get('role','?')}{'.' + s['class'] if s.get('class') else ''}"
                               for s in rule.get("ancestor_path", [])) or "(page scope)"
            print(f"  [{capture}] {rule.get('verdict','?'):<9} {where}")
            print(f"      {str(rule.get('note') or '(no note)')[:140]}")
    return t


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules_dir", action="append", default=None,
                    help="Repeatable. Default: every at-spi/llm_rules_container* dir that has "
                         "proposals on disk.")
    ap.add_argument("--model", default=None, help="Substring of the model tag in the filename.")
    ap.add_argument("--per_file", action="store_true", help="One line per capture.")
    ap.add_argument("--notes", action="store_true", help="Print every dropped rule's note.")
    ap.add_argument("--no_rows", action="store_true",
                    help="Skip the universe rebuild: no `audited` and no row columns, ~15s faster.")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    dirs = args.rules_dir or [d for d in sorted(glob.glob(os.path.join(_HERE, "llm_rules_container*")))
                              if glob.glob(os.path.join(d, "*" + PROPOSED_SUFFIX))]
    if not dirs:
        sys.exit("no rules dir with .proposed.json files found")

    universe = None
    if not args.no_rows:
        import labeling_store as ls
        universe = ls.build_universe()

    out = {}
    for d in dirs:
        res = analyse(d, args.model, universe)
        if not res["per_file"]:
            print(f"\n{os.path.basename(d)}: no proposal/final pairs matched", file=sys.stderr)
            continue
        t = report(res, args.per_file, args.notes)
        out[os.path.basename(d)] = {"totals": dict(t),
                                    "per_file": res["per_file"],
                                    "misaligned": res["misaligned"]}
    print()
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(out, f, indent=2)
        print(f"wrote {args.json_out}")


if __name__ == "__main__":
    main()
