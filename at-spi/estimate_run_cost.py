#!/usr/bin/env python3
"""
estimate_run_cost.py -- what it costs, in tokens, to run selector_propose over a set of captures.

NOTHING IS CALLED. Every prompt a sweep sends is reconstructed offline from the same code the
sweep uses -- `build_sanitized_skeleton`, `SYSTEM_PROMPT_TEMPLATE`, `FOLLOW_UP_PROMPT_TEMPLATE` +
`extent_report`, and `verify_notes.VERIFY_PROMPT` + `_block` -- so the INPUT side is measured, not
modelled. Only the model's own output is estimated, and that is the part to read sceptically.

WHY A REFERENCE RULE SET IS NEEDED. Three of the prompt's five pieces depend on what the model
answers: the follow-up quotes the turn-1 rules back, `extent_report` measures them, and the audit
builds one evidence block per rule that covers a row. For a capture that has not been run there is
no answer to quote, so a reference directory's rules for that capture stand in. `llm_rules_container_v15`
covers all 160 captures with rules from this same pipeline, which makes the substitution a
same-shape sample rather than a guess -- but a model that proposes twice as many rules will spend
more than this says on the follow-up and the audit.

WHAT IS NOT MEASURED, and why the output column is a band:

  * REASONING TOKENS. Billed as output and, on this corpus with thinking on, the dominant output
    cost -- selector_propose's own measurement is ~20k completion tokens to emit 3.4k chars of
    JSON. Pass --reasoning_ratio to set completion tokens per output CHARACTER (default 0 = the
    floor: text only). Note that `biba._chat` only forwards `enable_thinking` to a vllm provider;
    over openrouter the flag reaches nothing and the model reasons or does not on its own.
  * RETRIES. The runner re-attempts a capture that returns no rules. Not modelled.
  * PROMPT CACHING. Turn 2 resends the whole turn-1 prompt, so half the input below is a verbatim
    prefix repeat, and the three audit samples are three copies of one prompt. Since 2026-09-20
    `selector_propose --prompt_cache` defaults ON (history=ALWAYS), so an OpenRouter sweep bills
    most of that repeat at the cached rate -- pass --cache_read to price it. The numbers below are
    uncached, which is what v15/v17/v18 ran under and what --no-prompt_cache still gives.
  * The per-DOMAIN site prior: one extra small call per domain not already in cache/site_priors.json
    for this model. Counted separately, it is noise next to the skeletons.

Chars are converted at --chars_per_token, default 4.47, measured on this corpus (see
selector_propose.CONTEXT_WARN_CHARS). That constant was measured with a Qwen tokenizer; for an
OpenAI model it is close enough for a budget and wrong enough not to quote to three digits.

Usage:
  python at-spi/estimate_run_cost.py                                  # what v17 has left to run
  python at-spi/estimate_run_cost.py --out_dir at-spi/llm_rules_container_v17_suites_or --done
  python at-spi/estimate_run_cost.py --price_in 1.25 --price_out 10   # $/Mtok -> dollars
  python at-spi/estimate_run_cost.py --reasoning_ratio 6 --per_file
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "labelers"))

DEFAULT_OUT_DIR = os.path.join(_HERE, "llm_rules_container_v17_suites_or")
DEFAULT_REFERENCE = os.path.join(_HERE, "llm_rules_container_v15")
PROPOSED_SUFFIX = ".proposed.json"
AUDIT_ANSWER_CHARS_PER_RULE = 200


def _have(rules_dir: str) -> set:
    out = set()
    for p in glob.glob(os.path.join(rules_dir, "*.json")):
        if p.endswith(PROPOSED_SUFFIX):
            continue
        stem = os.path.splitext(os.path.basename(p))[0]
        capture, _, _tag = stem.rpartition("__")
        out.add((capture or stem) + ".xml")
    return out


def _reference_rules(reference_dir: str, capture: str) -> list:
    """The reference directory's PRE-audit rules for one capture -- the audit's real input."""
    stem = capture[:-4] if capture.endswith(".xml") else capture
    for pat in (stem + "__*" + PROPOSED_SUFFIX, stem + "__*.json"):
        hits = [p for p in sorted(glob.glob(os.path.join(reference_dir, pat)))
                if pat.endswith(PROPOSED_SUFFIX) or not p.endswith(PROPOSED_SUFFIX)]
        if hits:
            with open(hits[0], encoding="utf-8") as f:
                return json.load(f)
    return []


def measure(capture: str, sp, vn, ls, universe: dict, reference_dir: str, model: str,
            priors: dict) -> dict:
    """Exact input chars for one capture's three calls, plus the output chars we can pin down."""
    row = sp._manifest_row(capture)
    skeleton = sp.build_sanitized_skeleton(capture)
    domain, category = row.get("domain"), row.get("category")
    site_description = (f"{domain} (category: {category})" if domain and category
                        else domain or "a page/application")
    prior = priors.get(site_description, "")
    system_prompt = sp.SYSTEM_PROMPT_TEMPLATE.format(
        skeleton=skeleton,
        current_user_context="CURRENT_USER: not provided for this calibration -- do not propose "
                             "any self_authored_check rules.",
        site_description=site_description,
        site_prior=sp._site_prior_block(prior),
        depth_rule="")
    turn1_in = len(system_prompt) + len(sp.FIRST_TURN_PROMPT)

    rules = sp.filter_rules(_reference_rules(reference_dir, capture), verbose=False)
    answer = json.dumps(rules, ensure_ascii=False, indent=1)
    fu = universe.get(capture)
    proposed_list = "\n".join(
        f"  [{k}] " + json.dumps({kk: vv for kk, vv in r.items() if not kk.startswith("_")},
                                 ensure_ascii=False)
        for k, r in enumerate(rules, start=1)) or "(none yet)"
    report = sp.extent_report(fu, rules) if (fu is not None and rules) else "  (no rules yet)"
    follow_up = sp.FOLLOW_UP_PROMPT_TEMPLATE.format(num_total=len(rules),
                                                    proposed_list=proposed_list,
                                                    extent_report=report)
    turn2_in = turn1_in + len(answer) + len(follow_up)

    audit_in, audited = 0, 0
    if fu is not None and rules:
        targets = vn.inheriting_rules(fu, rules, sp)
        audited = len(targets)
        if targets:
            blocks = "\n\n".join(vn._block(i, r, cov) for i, (_n, r, cov) in enumerate(targets))
            audit_in = len(vn.VERIFY_PROMPT.format(n=len(targets), blocks=blocks))
    return {"capture": capture, "domain": domain, "skeleton": len(skeleton),
            "turn1_in": turn1_in, "turn2_in": turn2_in,
            "audit_in": audit_in, "audit_samples": vn.VERIFY_SAMPLES, "audited": audited,
            "rules": len(rules),
            "in_chars": turn1_in + turn2_in + audit_in * vn.VERIFY_SAMPLES,
            "out_chars": 2 * len(answer) + vn.VERIFY_SAMPLES * audited * AUDIT_ANSWER_CHARS_PER_RULE,
            "prior_cached": bool(prior), "site_description": site_description}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out_dir", default=DEFAULT_OUT_DIR,
                    help="The run being costed. Default: the v17 suite directory.")
    ap.add_argument("--reference_dir", default=DEFAULT_REFERENCE,
                    help="Rule sets standing in for the unknown answers. Default: v15 (all 160).")
    ap.add_argument("--done", action="store_true",
                    help="Cost the captures ALREADY in --out_dir instead of the ones missing "
                         "from it -- the retrospective, where the reference rules are that run's "
                         "own and the input side is exact.")
    ap.add_argument("--model", default="openai/gpt-5.6-luna",
                    help="Only used to look up the cached site prior, which is part of the prompt.")
    ap.add_argument("--chars_per_token", type=float, default=4.47)
    ap.add_argument("--reasoning_ratio", type=float, default=0.0,
                    help="Completion TOKENS per output CHARACTER, covering reasoning. 0 (default) "
                         "prices text only and is a hard floor. selector_propose measured ~6 on "
                         "this corpus with thinking on (20k tokens for 3.4k chars).")
    ap.add_argument("--price_in", type=float, default=0.0, help="$ per million input tokens.")
    ap.add_argument("--price_out", type=float, default=0.0, help="$ per million output tokens.")
    ap.add_argument("--cache_read", type=float, default=None,
                    help="Price multiplier for a token served from cache, e.g. 0.1. Setting it "
                         "adds a cache-aware line. Omitted = no caching, which is what v15/v17/v18 "
                         "ran under and what selector_propose --no-prompt_cache still gives: the "
                         "policy sends prompt_cache_options.mode=explicit and switches "
                         "OpenRouter's automatic caching OFF. --prompt_cache, now the default, "
                         "turns it on.")
    ap.add_argument("--cache_write", type=float, default=1.25,
                    help="Price multiplier for the first send of a prefix. GPT-5.6+ bills a cache "
                         "write at 1.25x input (default); models with a free write take 1.0.")
    ap.add_argument("--per_file", action="store_true")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    import labeling_store as ls
    from labelers import selector_propose as sp
    from labelers import verify_notes as vn

    have = _have(args.out_dir)
    if args.done and args.reference_dir == DEFAULT_REFERENCE:
        args.reference_dir = args.out_dir
    manifest = [r["filename"] for r in json.load(open(os.path.join(_HERE, "manifest.json")))
                if r.get("status") == "ok"]
    known = set(manifest) | have
    targets = sorted(have) if args.done else sorted(f for f in known if f not in have)
    if not targets:
        sys.exit("nothing to cost")

    print(f"building universe (schema {ls.UNIVERSE_SCHEMA})...", file=sys.stderr)
    universe = ls.build_universe()
    priors = {}
    raw = json.load(open(sp.SITE_PRIOR_CACHE)) if os.path.exists(sp.SITE_PRIOR_CACHE) else {}
    for k, v in raw.items():
        m, _, rest = k.partition("||")
        if m == args.model:
            priors[rest.partition("||")[2]] = v

    rows, missing = [], []
    for capture in targets:
        if not os.path.exists(os.path.join(_HERE, "xml", capture)):
            missing.append(capture)
            continue
        rows.append(measure(capture, sp, vn, ls, universe, args.reference_dir, args.model, priors))

    cpt = args.chars_per_token
    tin = sum(r["in_chars"] for r in rows) / cpt
    tout_text = sum(r["out_chars"] for r in rows) / cpt
    tout = sum(r["out_chars"] for r in rows) * args.reasoning_ratio if args.reasoning_ratio \
        else tout_text
    repeat = sum(r["turn1_in"] + r["audit_in"] * (r["audit_samples"] - 1) for r in rows) / cpt
    new_domains = sorted({r["site_description"] for r in rows if not r["prior_cached"]})

    print(f"\n{'=' * 84}")
    print(f"{len(rows)} capture(s) {'already in' if args.done else 'missing from'} "
          f"{os.path.relpath(args.out_dir, _HERE)}")
    print(f"reference rule shapes from {os.path.relpath(args.reference_dir, _HERE)}"
          f"   |   {cpt} chars/token")
    print(f"{'=' * 84}")
    print(f"  skeleton chars total   {sum(r['skeleton'] for r in rows):>14,}")
    print(f"  INPUT tokens           {tin:>14,.0f}   "
          f"(turn1 {sum(r['turn1_in'] for r in rows)/cpt:,.0f} + turn2 "
          f"{sum(r['turn2_in'] for r in rows)/cpt:,.0f} + audit x{vn.VERIFY_SAMPLES} "
          f"{sum(r['audit_in'] for r in rows)*vn.VERIFY_SAMPLES/cpt:,.0f})")
    print(f"     of which repeated   {repeat:>14,.0f}   "
          f"{100*repeat/tin if tin else 0:.0f}% -- turn-1 prefix resent in turn 2, plus "
          f"{vn.VERIFY_SAMPLES - 1} extra audit copies")
    print(f"  OUTPUT tokens (text)   {tout_text:>14,.0f}   "
          f"{sum(r['rules'] for r in rows):,} rules + "
          f"{sum(r['audited'] for r in rows)*vn.VERIFY_SAMPLES:,} audit verdicts")
    if args.reasoning_ratio:
        print(f"  OUTPUT incl. reasoning {tout:>14,.0f}   "
              f"at --reasoning_ratio {args.reasoning_ratio}")
    billed_in = tin
    if args.cache_read is not None:
        unique = tin - repeat
        billed_in = unique * args.cache_write + repeat * args.cache_read
        print(f"  INPUT billed, cached   {billed_in:>14,.0f}   "
              f"{100 * (1 - billed_in / tin) if tin else 0:.0f}% less "
              f"(write {args.cache_write:g}x on {unique:,.0f}, read {args.cache_read:g}x on "
              f"{repeat:,.0f})")
    if args.price_in or args.price_out:
        cost = billed_in * args.price_in / 1e6 + tout * args.price_out / 1e6
        print(f"  COST  ${cost:,.2f}   (in ${billed_in*args.price_in/1e6:,.2f} + out "
              f"${tout*args.price_out/1e6:,.2f})")
    print(f"  site priors to fetch   {len(new_domains):>14}   "
          f"(one small call each; {len(rows) - len(new_domains)} captures reuse a cached prior)")
    if missing:
        print(f"  !! {len(missing)} capture(s) have no xml/ file and were skipped: "
              f"{', '.join(missing[:4])}")
    if args.per_file:
        print(f"\n  {'capture':<58} {'skel':>9} {'in tok':>9} {'aud':>4}")
        for r in sorted(rows, key=lambda r: -r["in_chars"]):
            print(f"  {r['capture']:<58} {r['skeleton']:>9,} {r['in_chars']/cpt:>9,.0f} "
                  f"{r['audited']:>4}")
    print()
    if args.json_out:
        json.dump({"rows": rows, "chars_per_token": cpt,
                   "input_tokens": tin, "output_tokens_text": tout_text,
                   "repeated_input_tokens": repeat, "new_domains": new_domains},
                  open(args.json_out, "w"), indent=2)
        print(f"wrote {args.json_out}")


if __name__ == "__main__":
    main()
