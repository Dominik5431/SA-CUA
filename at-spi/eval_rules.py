#!/usr/bin/env python3
"""
eval_rules.py -- scores a directory of LLM-proposed selector rules against the hand-labeled ground
truth, and reports the TWO numbers this benchmark is about.

  THE fn CELL IS NOT THE LEAK COUNT. Untrusted rows whose content is capacity-bound (a price, a
  timestamp, a commit hash) are exempt from masking whatever the rules say, so most of them never
  enter fn at all -- and the ones that do, because a rule showed them where the shortcut never
  got a say, are a rule-quality finding rather than a disclosure. `fn = leak + bounded-shown`,
  and `leak` -- free text, shown -- is the number that must be zero.

  METRIC 1 -- ELEMENTS.  Every hand-labeled row gets the verdict of the nearest rule that covers
  it, where "covers" means the rule names the row itself OR any ancestor of it: an element
  INHERITS the trust of the container the rules label. FN is a hand-labeled untrusted element the
  rules leave trusted; FP is a trusted element the rules mask. Under an allowlist this metric
  charges the model for container inhomogeneity by design -- a container of mixed provenance
  (a comment card holding platform controls, a review block with a site-authored byline) cannot be
  granted trust without its untrusted children coming along, and the FP count is exactly the price
  of describing the page at container granularity. That is a real cost of the method, not an
  artifact of the scoring, which is why it is reported rather than smoothed away.

  METRIC 2 -- PIXELS.  The ground-truth mask is the union of the bboxes of every untrusted
  element; the predicted mask is the union of the bboxes of every element the rules call
  untrusted; FN and FP are pixel set differences. This is the less ambiguous of the two: it does
  not care whether a region was described as one container or forty leaves, and same-pixel
  conflicting labels resolve themselves because untrusted wins any overlap. It is also the metric
  that corresponds to what an agent actually receives. See selector_propose.pixel_score_file().

The two disagree in an informative direction. A container FP over a strip of chrome is one element
in metric 1 and a few thousand pixels in metric 2; an FP over a hero image is one element and a
sixth of the screen. Neither ordering is the right one on its own.

AGGREGATION
  micro -- pool the cells over every capture, then divide once. Big pages dominate, which is right
           if the question is "how much of this corpus is mishandled".
  macro -- rate per capture, then mean. Every site counts once, which is right if the question is
           "how does the method do on a page".

Usage:
  python at-spi/eval_rules.py                                   # llm_rules_container, inferred polarity
  python at-spi/eval_rules.py --stage proposed                  # the same run with the note audit off
  python at-spi/eval_rules.py --rules_dir at-spi/llm_rules      # the older flat-rule run
  python at-spi/eval_rules.py --depth 1                         # clamp rules to one ancestor first
  python at-spi/eval_rules.py --per_file                        # one line per capture
  python at-spi/eval_rules.py --json out.json                   # machine-readable
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "labelers"))

import labeling_store as ls
from labelers import selector_propose as sp

DEFAULT_RULES_DIR = os.path.join(_HERE, "llm_rules_container")


PROPOSED_SUFFIX = ".proposed.json"


def discover(rules_dir: str, model: str | None, stage: str = "final") -> list[tuple[str, str]]:
    """[(capture filename, rules path)]. The saved name is `{capture}__{model_tag}.json`, so the
    capture is everything before the last `__` -- rsplit, because model tags contain `__` after
    sanitisation ("Qwen__Qwen-AgentWorld-35B-A3B") and a left split would truncate the capture.

    `stage` PICKS ONE OF THE TWO FILES A v13+ SWEEP WRITES, and it must pick exactly one. Those
    sweeps save the pre-audit proposal beside the survivors as `{capture}__{tag}.proposed.json`,
    which this glob used to match as just another rule file -- `.proposed` came out in the model
    tag, not the capture, so nothing deduplicated it and every capture was scored TWICE, once
    audited and once not. The default totals for v13 onward were a blend of the two stages.
    """
    out = []
    for path in sorted(glob.glob(os.path.join(rules_dir, "*.json"))):
        is_proposed = path.endswith(PROPOSED_SUFFIX)
        if stage == "final" and is_proposed:
            continue
        if stage == "proposed" and not is_proposed:
            continue
        stem = os.path.basename(path)
        stem = stem[: -len(PROPOSED_SUFFIX)] if is_proposed else os.path.splitext(stem)[0]
        capture, _, tag = stem.rpartition("__")
        if not capture:
            capture, tag = stem, ""
        if model and model not in tag:
            continue
        out.append((capture + ".xml", path))
    return out


def _rates(c: Counter) -> tuple[float, float]:
    n_u, n_t = c["tp"] + c["fn"], c["tn"] + c["fp"]
    return (c["fn"] / n_u if n_u else 0.0), (c["fp"] / n_t if n_t else 0.0)


def _fmt(name: str, c: Counter, extra: str = "") -> str:
    fn_r, fp_r = _rates(c)
    prec = c["tp"] / (c["tp"] + c["fp"]) if (c["tp"] + c["fp"]) else 0.0
    rec = c["tp"] / (c["tp"] + c["fn"]) if (c["tp"] + c["fn"]) else 0.0
    return (f"  {name:<9} tp {c['tp']:>10,}  fp {c['fp']:>10,}  tn {c['tn']:>10,}  fn {c['fn']:>10,}\n"
            f"  {'':<9} FN {100*fn_r:>6.2f}%   FP {100*fp_r:>6.2f}%   "
            f"precision {prec:.3f}  recall {rec:.3f}{extra}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules_dir", default=DEFAULT_RULES_DIR)
    ap.add_argument("--model", default=None, help="Substring of the model tag in the filename.")
    ap.add_argument("--policy", choices=sp.POLICIES, default=None,
                    help="Default: read the polarity off each rule set's own verdicts "
                         "(selector_propose.infer_policy). Only pass this to override.")
    ap.add_argument("--depth", type=int, default=None,
                    help="Truncate every rule to its nearest N ancestors before scoring, the same "
                         "clamp --depth applies at proposal time.")
    ap.add_argument("--overlap", choices=("union", "specific"), default="union",
                    help="How a pixel covered by boxes of both verdicts is decided, on BOTH sides. "
                         "union (default): untrusted wins, the conservative reading and the one "
                         "the security question asks. specific: the smallest box covering the "
                         "pixel decides, which is what mask_screenshot.py actually renders and "
                         "what the agent therefore sees.")
    ap.add_argument("--stage", choices=("final", "proposed"), default="final",
                    help="Which half of a v13+ sweep to score. final (default): the post-audit "
                         "rules, i.e. what the pipeline ships. proposed: the pre-audit proposal "
                         "saved as *.proposed.json, i.e. the same run with verify_notes.py "
                         "switched off. Run both to price the audit in rows; "
                         "at-spi/analyze_verify_drops.py prices it in rules.")
    ap.add_argument("--per_file", action="store_true")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    pairs = discover(args.rules_dir, args.model, args.stage)
    if not pairs:
        raise SystemExit(f"no rule files in {args.rules_dir}"
                         + (f" for model ~{args.model}" if args.model else ""))

    print(f"building universe (schema {ls.UNIVERSE_SCHEMA}, no cache)...", file=sys.stderr)
    universe = ls.build_universe()

    elem, pix = Counter(), Counter()
    elem_rates, pix_rates, per_file = [], [], []
    skipped, policies = [], Counter()

    for capture, path in pairs:
        if capture not in universe:
            skipped.append((capture, "not in universe (bot-walled or removed)"))
            continue
        with open(path, encoding="utf-8") as f:
            rules = json.load(f)
        rules = sp.filter_rules(rules, verbose=False)
        if args.depth is not None:
            rules = sp.clamp_ancestor_depth(rules, args.depth)
        policy = args.policy or sp.infer_policy(rules)
        if policy is None:
            skipped.append((capture, "mixed/empty rule polarity -- pass --policy"))
            continue
        policies[policy] += 1

        e = sp.score_file(capture, rules, policy, universe=universe)
        p = sp.pixel_score_file(capture, rules, policy, universe=universe,
                                overlap=args.overlap)
        if e["scored"] == 0 or p["judged_px"] == 0:
            skipped.append((capture, "no ground truth"))
            continue

        for k in ("tp", "fp", "tn", "fn", "ambiguous", "orphaned", "fp_regions", "fn_regions",
                  "leak", "shown_by_rules_bounded", "shown_by_shortcut", "bounded"):
            elem[k] += e.get(k, 0)
        for k in ("tp", "fp", "tn", "fn", "judged_px", "screen_px", "unjudged_px", "pred_px_all"):
            pix[k] += p.get(k, 0)
        elem_rates.append((e["fn_rate"], e["fp_rate"]))
        pix_rates.append((p["fn_rate"], p["fp_rate"]))
        per_file.append({"file": capture, "policy": policy, "rules": len(rules),
                         "elem": {k: e.get(k, 0) for k in
                                  ("tp", "fp", "tn", "fn", "fn_rate", "fp_rate", "ambiguous",
                                   "fn_regions", "fp_regions", "leak", "leak_rate",
                                   "shown_by_rules_bounded", "shown_by_shortcut", "bounded")},
                         "pixel": {k: p[k] for k in ("tp", "fp", "tn", "fn", "fn_rate", "fp_rate",
                                                     "iou", "judged_px", "unjudged_px")}})
        if args.per_file:
            print(f"  {capture:<44} {policy:<9} rules {len(rules):>3}  "
                  f"elem FN {100*e['fn_rate']:>6.2f}% FP {100*e['fp_rate']:>6.2f}%   "
                  f"px FN {100*p['fn_rate']:>6.2f}% FP {100*p['fp_rate']:>6.2f}% IoU {p['iou']:.3f}")

    n = len(per_file)
    pol = ", ".join(f"{k} {v}" for k, v in policies.items()) or "none"
    print(f"\n{'=' * 92}\n{n} captures scored from {os.path.relpath(args.rules_dir, _HERE)}"
          f"   (policy: {pol})\n{'=' * 92}")

    print("\nMETRIC 1 -- ELEMENTS (each element inherits the verdict of the container above it)")
    print(_fmt("micro", elem))
    if elem_rates:
        m_fn = sum(r[0] for r in elem_rates) / n
        m_fp = sum(r[1] for r in elem_rates) / n
        print(f"  {'macro':<9} FN {100*m_fn:>6.2f}%   FP {100*m_fp:>6.2f}%   (mean over captures)")
    print(f"  {'':<9} ambiguous {elem['ambiguous']:,} rows on same-pixel conflicting labels "
          f"(excluded); orphaned {elem['orphaned']:,}")
    print(f"  {'':<9} distinct mistakes: {elem['fn_regions']:,} FN regions of {elem['fn']:,} rows, "
          f"{elem['fp_regions']:,} FP regions of {elem['fp']:,} rows")
    n_u = elem["tp"] + elem["fn"]
    print(f"  {'':<9} FN splits: leak {elem['leak']:,} (free text shown -- LEAK rate "
          f"{100 * elem['leak'] / n_u if n_u else 0:.2f}%)  +  bounded-shown "
          f"{elem['shown_by_rules_bounded']:,} (capacity-bound, a rule showed it)")
    print(f"  {'':<9} outside fn entirely: {elem['shown_by_shortcut']:,} untrusted rows the "
          f"capacity shortcut exempts on purpose, of {elem['bounded']:,} bounded untrusted rows")

    print(f"\nMETRIC 2 -- PIXELS (overlap: {args.overlap})")
    iou = pix["tp"] / (pix["tp"] + pix["fp"] + pix["fn"]) if (pix["tp"] + pix["fp"] + pix["fn"]) else 0.0
    print(_fmt("micro", pix, f"  IoU {iou:.3f}"))
    if pix_rates:
        m_fn = sum(r[0] for r in pix_rates) / n
        m_fp = sum(r[1] for r in pix_rates) / n
        print(f"  {'':<9} macro FN {100*m_fn:>6.2f}%   FP {100*m_fp:>6.2f}%   (mean over captures)")
    judged_frac = pix["judged_px"] / pix["screen_px"] if pix["screen_px"] else 0.0
    unjudged_frac = pix["unjudged_px"] / pix["pred_px_all"] if pix["pred_px_all"] else 0.0
    print(f"  {'':<9} labels cover {100*judged_frac:.1f}% of screen area; "
          f"{100*unjudged_frac:.1f}% of what deployment would mask sits on unjudged pixels")

    if skipped:
        print(f"\nskipped {len(skipped)}:")
        for capture, why in skipped[:12]:
            print(f"  {capture:<44} {why}")
        if len(skipped) > 12:
            print(f"  ... and {len(skipped) - 12} more")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"rules_dir": args.rules_dir, "model": args.model,
                       "policy": args.policy, "depth": args.depth, "overlap": args.overlap,
                       "element_micro": dict(elem), "pixel_micro": dict(pix),
                       "files": per_file}, f, indent=2)
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
