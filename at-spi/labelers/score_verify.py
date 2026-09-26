#!/usr/bin/env python3
"""
score_verify.py -- what the note-audit pass (verify_notes.py) is worth, in rules and in rows.

This is the evidence for `selector_propose.py --verify` being on by default. It was an ad-hoc
script when the pass was written, which meant the numbers in verify_notes.py's docstring could be
quoted but not re-derived; that is the gap this file closes.

WHAT IT MEASURES, on every capture that has both hand-labeled ground truth and a proposed rule set:

  TABLE 1 (rules)  How many rules the audit drops, split into the ones we already know cause false
                   negatives and the ones we lose as collateral. The known-bad set is frozen below
                   and is specific to the v11 rules -- it is four notes that were traced, by hand,
                   to all 21 residual false negatives.

  TABLE 2 (rows)   The same captures scored with the rules AS PROPOSED and again AFTER the audit,
                   through selector_propose.score_file, i.e. the same scorer --score uses. This is
                   the number that matters: the rule count is a proxy, the row count is the claim.

Measured 2026-08-31 over the 15 captures that were hand-labeled at the time, v11 rules,
Qwen/Qwen-AgentWorld-35B-A3B:

    verify off   TP=763  FP=628  TN=590  FN=21   FN-rate=2.68%   FP-rate=51.56%
    verify on    TP=784  FP=677  TN=541  FN= 0   FN-rate=0.00%   FP-rate=55.58%
    known-bad rules dropped 4/4      good rules dropped 7/73

To reproduce exactly that, pin the file set (the eval set has grown since; by default this script
discovers it, so the totals here will be broader and will not match the line above):

    python at-spi/labelers/score_verify.py --vllm_port 8002 \\
      --file 000_github.com.xml --file 001_github.com.xml --file 002_github.com.xml \\
      --file 003_github.com.xml --file 008_en.wikipedia.org.xml --file 009_en.wikipedia.org.xml \\
      --file 016_stackoverflow.com.xml --file 017_stackoverflow.com.xml \\
      --file 024_www.booking.com.xml --file 025_www.booking.com.xml \\
      --file 028_www.booking.com.xml --file 032_www.amazon.com.xml \\
      --file 033_www.amazon.com.xml --file 034_www.amazon.com.xml --file 036_www.amazon.com.xml

Ordinary use -- the whole discovered set:

    python at-spi/labelers/score_verify.py --vllm_port 8002

POINT IT AT UNVERIFIED RULES, normally. The comparison is "as proposed" against "after the
audit", so a directory produced with `--verify` already on usually has nothing left to
measure: the audit reruns over its own survivors and both columns read the same. v11 is the
last unverified sweep and is the default for that reason.

THE ONE REASON TO POINT IT AT A VERIFIED DIRECTORY is to measure a CHANGED audit against the
one that produced it. Because the pass only ever drops, re-auditing v12's survivors with a
stronger setting isolates exactly what the change buys: "off" is v12 as shipped, "on" is v12
plus the new audit, and nothing else moved. That is how the thinking-on audit was evaluated
against the three false negatives v12 shipped with:

    python at-spi/labelers/score_verify.py --vllm_port 8002 \\
      --rules_dir at-spi/llm_rules_container_v12 \\
      --file 012_en.wikipedia.org.xml --file 018_stackoverflow.com.xml \\
      --file 019_stackoverflow.com.xml
"""
from __future__ import annotations

import argparse
import concurrent.futures
import glob
import json
import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_AT_SPI_DIR = os.path.dirname(_HERE)
sys.path.insert(0, os.path.dirname(_AT_SPI_DIR))
sys.path.insert(0, _AT_SPI_DIR)
sys.path.insert(0, _HERE)

from mm_agents import openrouter
from mm_agents.sa_agent.llm import build_client
import ground_truth_fulltree as gtf
import labeling_store as ls
import selector_propose as sp
import verify_notes as vn

DEFAULT_RULES_DIR = os.path.join(_AT_SPI_DIR, "llm_rules_container_v11")
DEFAULT_MODEL = "vllm|Qwen/Qwen-AgentWorld-35B-A3B"

KNOWN_BAD_NOTES = frozenset({
    "navigation stripe -- platform's own category and department links",
    "network sites sidebar list -- platform's own navigation to Stack Exchange sites",
    "repository header with watch, fork, star buttons and social counts -- operator interface "
    "and computed values",
    "copy code buttons -- platform controls for code blocks",
})


PROPOSED_SUFFIX = ".proposed.json"


def _rules_for(rules_dir: str, filename: str, proposed: bool = False) -> "list | None":
    """The proposed rules for one capture, whichever model produced them.

    Globbed rather than composed from a model tag: a rules directory holds one file per (capture,
    model) and the point of this script is to audit whatever sweep is on disk, not to re-derive
    which model wrote it. More than one match is ambiguous and is refused rather than guessed at.
    """
    stem = filename[:-4] if filename.endswith(".xml") else filename
    hits = sorted(glob.glob(os.path.join(rules_dir, f"{stem}__*.json")))
    hits = [h for h in hits
            if h.endswith(PROPOSED_SUFFIX) == proposed]
    if not hits:
        return None
    if len(hits) > 1:
        raise SystemExit(f"{stem}: {len(hits)} rule files in {rules_dir} "
                         f"({[os.path.basename(h) for h in hits]}) -- pick one directory per model")
    with open(hits[0], encoding="utf-8") as f:
        rules = json.load(f)
    return rules.get("rules", []) if isinstance(rules, dict) else rules


def discover(rules_dir: str, proposed: bool = False) -> list[str]:
    """Captures with BOTH hand-labeled ground truth and a proposed rule set, in corpus order.

    Ground truth comes from gtf.eval_rows(), which is hand modes only -- so the machine-extended
    labels (mode `auto_exact`) that now sit in the journal are excluded here exactly as they are
    excluded from --score. The eval set this reports on is the human's, unchanged.
    """
    labeled = {r["file"] for r in gtf.eval_rows()}
    have_rules = {os.path.basename(p).split("__")[0] + ".xml"
                  for p in glob.glob(os.path.join(rules_dir, "*.json"))
                  if p.endswith(PROPOSED_SUFFIX) == proposed}
    return sorted(labeled & have_rules, key=lambda f: (ls._numeric_prefix(f), f))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", action="append", dest="files", default=None,
                    help="Capture to score. Repeatable. Default: every capture with both hand "
                         "labels and a rule set in --rules_dir.")
    ap.add_argument("--rules_dir", default=DEFAULT_RULES_DIR,
                    help=f"Proposed rules to audit (default {os.path.relpath(DEFAULT_RULES_DIR, _AT_SPI_DIR)}). "
                         "Must be an UNVERIFIED sweep -- see the module docstring.")
    ap.add_argument("--proposed", action="store_true",
                    help="Audit the PRE-audit proposals (<stem>__<model>.proposed.json) rather "
                         "than the post-audit rule sets in the same directory. This is the honest "
                         "input for measuring an audit: the alternative is auditing a previous "
                         "audit's survivors.")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--vllm_port", type=int, default=8002)
    ap.add_argument("--policy", default="allowlist", choices=sp.POLICIES)
    ap.add_argument("--concurrency", type=int, default=4,
                    help="Audit this many captures at once. One call each, so this is pure "
                         "wall-clock; the tables below are assembled in corpus order regardless.")
    ap.add_argument("--max_tokens", type=int, default=sp.VERIFY_MAX_TOKENS)
    ap.add_argument("--enable_thinking", action=argparse.BooleanOptionalAction,
                    default=sp.VERIFY_ENABLE_THINKING,
                    help="Audit thinks before answering. Defaults to the shipped setting "
                         "(selector_propose.VERIFY_ENABLE_THINKING) so this harness measures "
                         "the path that actually runs.")
    ap.add_argument("--out", default=None, help="Also write the per-capture rows here as JSON.")
    args = ap.parse_args()

    universe = ls.build_universe()
    files = args.files or discover(args.rules_dir, args.proposed)
    files = [f for f in files if f in universe]
    if not files:
        sys.exit(f"Nothing to score: no capture has both hand labels and rules in {args.rules_dir}")

    client = build_client(args.model, args.vllm_port, history=openrouter.ALWAYS)
    model_name = args.model.split("|", 1)[-1]
    print(f"[score_verify] {len(files)} capture(s), rules from "
          f"{os.path.relpath(args.rules_dir, _AT_SPI_DIR)}, auditor {model_name} "
          f"on port {args.vllm_port}", file=sys.stderr)

    results: dict[str, dict] = {}
    lock = threading.Lock()

    def audit(filename: str) -> None:
        rules = _rules_for(args.rules_dir, filename, args.proposed)
        if rules is None:
            return
        kept, dropped = vn.verify(client, universe[filename], rules, model_name, sp,
                                  max_tokens=args.max_tokens,
                                  enable_thinking=args.enable_thinking, verbose=False)
        audited = {r.get("note") for _n, r, _c in vn.inheriting_rules(universe[filename], rules, sp)}
        dropped_notes = {d["note"] for d in dropped}
        off = sp.score_file(filename, rules, policy=args.policy, universe=universe)
        on = sp.score_file(filename, kept, policy=args.policy, universe=universe)
        with lock:
            results[filename] = {
                "n_rules": len(rules), "kept": len(kept), "dropped": len(dropped),
                "audited": len(audited),
                "bad_dropped": sorted(dropped_notes & KNOWN_BAD_NOTES),
                "good_dropped": sorted(n for n in dropped_notes if n not in KNOWN_BAD_NOTES),
                "bad_kept": sorted((audited & KNOWN_BAD_NOTES) - dropped_notes),
                "off": {k: off[k] for k in ("tp", "fp", "tn", "fn")},
                "on": {k: on[k] for k in ("tp", "fp", "tn", "fn")},
            }
            print(f"  [{len(results)}/{len(files)}] {filename}: kept {len(kept)}/{len(rules)}",
                  file=sys.stderr)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(audit, files))

    files = [f for f in files if f in results]

    print(f"\n{'=' * 96}")
    print(f"{'capture':44} {'rules':>5} {'kept':>5} {'drop':>5}   bad-dropped / good-dropped")
    print("=" * 96)
    tp = fp = fn_ = tn = 0
    for f in files:
        r = results[f]
        print(f"{f[:-4]:44} {r['n_rules']:5} {r['kept']:5} {r['dropped']:5}   "
              f"{len(r['bad_dropped'])} / {len(r['good_dropped'])}")
        tp += len(r["bad_dropped"])
        fn_ += len(r["bad_kept"])
        fp += len(r["good_dropped"])
        tn += r["audited"] - len(r["bad_dropped"]) - len(r["bad_kept"]) - len(r["good_dropped"])
    print("=" * 96)
    print(f"KNOWN-BAD rules correctly dropped : {tp}/{tp + fn_}")
    print(f"good rules wrongly dropped (cost) : {fp}/{fp + tn}")

    print(f"\n{'=' * 96}")
    print(f"{'capture':44} {'FN off':>7} {'FN on':>6} {'FP off':>7} {'FP on':>6}  dropped")
    print("=" * 96)
    tot = {"off": [0] * 6, "on": [0] * 6}
    for f in files:
        r = results[f]
        print(f"{f[:-4]:44} {r['off']['fn']:7} {r['on']['fn']:6} "
              f"{r['off']['fp']:7} {r['on']['fp']:6}  {r['dropped']}")
        for key in ("off", "on"):
            for i, k in enumerate(("tp", "fp", "tn", "fn", "leak", "shown_by_rules_bounded")):
                tot[key][i] += r[key][k]
    print("=" * 96)
    for key in ("off", "on"):
        a, b, c, d, d_free, d_bound = tot[key]
        prec = a / (a + b) if (a + b) else float("nan")
        rec = a / (a + d) if (a + d) else float("nan")
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else float("nan")
        print(f"verify {key:3}  TP={a:5} FP={b:5} TN={c:5} FN={d:4}  "
              f"prec={prec:.3f} rec={rec:.3f} f1={f1:.3f}  "
              f"FN-rate={d / (a + d):.2%} FP-rate={b / (b + c):.2%}")
        print(f"{'':11}FN free-text={d_free:4} ({d_free / (a + d):.2%})  "
              f"FN capacity-bound={d_bound:4}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"rules_dir": os.path.relpath(args.rules_dir, _AT_SPI_DIR),
                       "model": model_name, "policy": args.policy,
                       "n_captures": len(files), "per_capture": results}, fh, indent=1)
        print(f"\nwrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
