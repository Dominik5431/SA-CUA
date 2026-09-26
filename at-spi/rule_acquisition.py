#!/usr/bin/env python3
"""rule_acquisition.py -- how should a scope's rule set be obtained? Every option, scored.

THE QUESTION. A scope (a host, or `app:<name>`) has N captures and N proposed rule sets. Exactly
one of them can be served at a time -- `select_capture` picks by template for the web,
`_app_union` merges for native -- so "which rules does this page get" is a design choice with at
least six answers. This scores all of them against the same hand-labeled ground truth.

THE ARMS.

  self          rules_i on capture_i. The in-sample ceiling: what a proposer achieves when it has
                seen the exact page. Not deployable (an episode cannot pay for a proposal round
                first) and not reachable (the live page is never a corpus capture), but it is the
                number every other arm is trying to approach.

  pairwise      rules_i on capture_j, for every ordered pair i != j WITHIN one scope. N*(N-1)
                scorings per scope, averaged. The honest picture of transfer between two pages of
                one site.

  pair_sametpl  the subset of those pairs whose two captures share a TEMPLATE
  pair_difftpl  ...and the subset that do not.
                Together these price the live `select_capture` ladder, which is the only reason
                the split is here. A page whose template matches a capture gets that capture's
                rules (pair_sametpl); a page whose template matches nothing falls through to
                `scope_default` and gets the lowest-indexed capture of the scope -- some other
                page type entirely (pair_difftpl). The two are different events and the ladder
                reports which one happened in `mask_stats.rules_from`, so they should be priced
                apart before anyone argues about the fallback.

  union_in      every capture's rules merged, scored on each capture. This is what `_app_union`
                ships for native scopes today. IN-SAMPLE: the capture's own rules are in the pool.

  union_loo     the peers' rules merged, scored on the held-out capture. The deployable version of
                the same idea, and a lower bound on union_in.

  veto_in       union_in, minus any rule CONTRADICTED by a source capture -- the rule matches rows
                there and that capture's own proposer did not grant those rows. Computed without
                ground truth, so it is deployable.

  veto_loo      veto over the peers only, scored on the held-out capture.

  veto_abst_*   the same veto, with ABSTAINERS struck from the electorate: a source capture whose
                own proposer emitted no rules at all casts no vote. Under plain `veto` such a
                capture contradicts every rule that matches anything on it, which is what
                collapses `app:soffice` (3 of its 7 captures have empty v15 sets) -- silence read
                as refusal. 24 of the 155 scored captures are in that state.

  union_tpl_loo / veto_tpl_loo
                the same two, but the donor pool is narrowed to the peers that share this
                capture's TEMPLATE. This is the merge radius the live web ladder already has:
                `select_capture` routes by template and merges nothing, so pooling reddit's
                /forums page with its /comments page is a policy nobody ships. Scored only on
                captures that HAVE a same-template peer -- the `scored` column says how many
                that is, and the rest of the scope is simply outside this arm's reach, which is
                itself the finding.

  vote2_loo     the other reading of silence: keep a rule only with >= 2 independent supporters
                (or if it matches in only one capture at all). Under `veto` a capture that RENDERS
                a region and never wrote a rule for it counts AGAINST; under `vote2` it counts for
                nothing and the rule needs positive corroboration instead.

  intersect     only rules proposed IDENTICALLY by every capture of the scope. The strictest
                merge, included because "intersect" is the obvious counterpart to "union" and it
                is worth seeing how little survives: a rule's selector names this page's container
                classes, so two pages of one site rarely emit the same rule text at all.

POLARITY, because every number below depends on it. Untrusted is the POSITIVE class.
  fn   an untrusted element left visible -- the security miss.
  leak the free-text subset of fn, i.e. the ones with room for an injection. THE number.
  fp   a trusted element masked -- over-masking, the usability cost.
Dropping a rule can only raise fp; adding one can only raise fn. Union moves one way, veto the
other, and neither dominates.

Run:
    python at-spi/rule_acquisition.py                      # default: v15, every group
    python at-spi/rule_acquisition.py --per_scope          # also break it down by scope
    python at-spi/rule_acquisition.py --groups native
"""
import argparse
import collections
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "labelers"))

import labeling_store as ls
import rule_scope as rsc
import transfer_analysis as ta
from labelers import selector_propose as sp

DEFAULT_RULES_DIR = os.path.join(_HERE, "llm_rules_container_v15")

ARMS = ["self", "pairwise", "pair_sametpl", "pair_difftpl", "union_in", "union_loo",
        "veto_in", "veto_loo", "veto_abst_in", "veto_abst_loo",
        "union_tpl_loo", "veto_tpl_loo",
        "vote2_loo", "intersect"]


def group_of(cap: str) -> str:
    """`web` public internet, `suite` the self-hosted eval platforms, `native` an application.

    Both of the first two are pages in Chrome and both keep `select_capture`; they are reported
    apart only because the suites are one codebase per scope and so transfer better than the open
    web, which would otherwise flatter the web numbers.
    """
    n = int(cap.split("_")[0])
    return "web" if n < 200 else ("native" if n < 300 else "suite")


def _blank():
    return dict(n=0, leak=0, fn=0, fp=0, tp=0, tn=0, leakcaps=0, rules=0, empty=0,
                n_untrusted=0, n_trusted=0)


def _add(a, s, nrules):
    a["n"] += 1
    for k in ("leak", "fn", "fp", "tp", "tn", "n_untrusted", "n_trusted"):
        a[k] += s[k]
    a["leakcaps"] += 1 if s["leak"] else 0
    a["rules"] += nrules
    a["empty"] += 1 if nrules == 0 else 0


def _row(name, a):
    if not a["n"]:
        return None
    fn_rate = a["fn"] / a["n_untrusted"] if a["n_untrusted"] else 0.0
    fp_rate = a["fp"] / a["n_trusted"] if a["n_trusted"] else 0.0
    leak_rate = a["leak"] / a["n_untrusted"] if a["n_untrusted"] else 0.0
    return (f"{name:<12}{a['n']:>7}{a['rules']/a['n']:>10.1f}{a['leak']:>7}"
            f"{100*leak_rate:>8.2f}{a['fn']:>7}{100*fn_rate:>8.2f}"
            f"{a['fp']:>8}{100*fp_rate:>8.1f}{a['empty']:>8}")


def _table(title, agg, order):
    head = (f"{'arm':<12}{'scored':>7}{'rules':>10}{'leak':>7}{'leak%':>8}{'fn':>7}{'fn%':>8}"
            f"{'fp':>8}{'fp%':>8}{'norule':>8}")
    lines = ["", title, "-" * len(head), head, "-" * len(head)]
    for k in order:
        r = _row(k, agg.get(k) or _blank())
        if r:
            lines.append(r)
    lines.append("-" * len(head))
    return "\n".join(lines)


def intersect_pool(source_caps, rules_by_cap):
    """Rules proposed identically (rule_key) by EVERY source capture."""
    sets = [{ta.rule_key(r): r for r in rules_by_cap.get(c, [])} for c in source_caps]
    if not sets:
        return []
    keys = set(sets[0])
    for s in sets[1:]:
        keys &= set(s)
    return [sets[0][k] for k in sorted(keys)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules_dir", default=DEFAULT_RULES_DIR)
    ap.add_argument("--groups", default="web,suite,native")
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--min_caps", type=int, default=2,
                    help="a scope with fewer captures has no transfer question to ask")
    ap.add_argument("--per_scope", action="store_true")
    ap.add_argument("--only_scopes", default=None,
                    help="comma-separated scope keys; restrict the run to these. Used to score a "
                         "sparse rules directory and a dense one over the SAME scopes, which is "
                         "the only way two arms are comparable -- a directory that covers "
                         "different pages is answering a different question.")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    manifest = ta.load_manifest()
    rules_by_cap = ta.load_rules(args.rules_dir)
    t0 = time.time()
    U = ls.build_universe()
    want_groups = set(args.groups.split(","))
    want_arms = [a for a in args.arms.split(",") if a in ARMS]

    by_scope = collections.defaultdict(list)
    for cap in sorted(rules_by_cap):
        if cap not in U or group_of(cap) not in want_groups:
            continue
        if not sp._gt_index(cap):
            continue
        scope = rsc.scope_of_manifest_row(manifest.get(cap) or {})
        if scope:
            by_scope[scope].append(cap)
    scopes = {s: c for s, c in by_scope.items() if len(c) >= args.min_caps}
    if args.only_scopes:
        want = {x.strip() for x in args.only_scopes.split(",") if x.strip()}
        missing = want - set(scopes)
        if missing:
            print(f"warning: --only_scopes named {sorted(missing)}, absent from this rules dir",
                  file=sys.stderr)
        scopes = {s: c for s, c in scopes.items() if s in want}
    n_pairs = sum(len(c) * (len(c) - 1) for c in scopes.values())
    print(f"universe {len(U)} captures in {time.time()-t0:.0f}s; "
          f"{len(scopes)} scopes with >={args.min_caps} labeled captures, "
          f"{sum(len(c) for c in scopes.values())} captures, {n_pairs} ordered pairs",
          file=sys.stderr)

    m = ta.Matcher(U)
    agg = collections.defaultdict(_blank)
    per_scope = collections.defaultdict(_blank)

    def record(arm, cap, kept):
        s = sp.score_file(cap, kept, universe=U)
        g = group_of(cap)
        _add(agg[f"{arm}|{g}"], s, len(kept))
        _add(agg[f"{arm}|ALL"], s, len(kept))
        if g in ("web", "suite"):
            _add(agg[f"{arm}|browser"], s, len(kept))
        if args.per_scope:
            _add(per_scope[f"{arm}|{scope}"], s, len(kept))

    tpl = {c: rsc.template_of_url((manifest.get(c) or {}).get("url"))
           for cs in scopes.values() for c in cs}

    for scope, caps in sorted(scopes.items()):
        caps = sorted(caps)
        for cap in caps:
            peers = [c for c in caps if c != cap]
            if "self" in want_arms:
                record("self", cap, rules_by_cap.get(cap, []))
            if {"pairwise", "pair_sametpl", "pair_difftpl"} & set(want_arms):
                for other in peers:
                    same = tpl.get(cap) is not None and tpl.get(cap) == tpl.get(other)
                    if "pairwise" in want_arms:
                        record("pairwise", cap, rules_by_cap.get(other, []))
                    arm = "pair_sametpl" if same else "pair_difftpl"
                    if arm in want_arms:
                        record(arm, cap, rules_by_cap.get(other, []))
            if "union_in" in want_arms:
                record("union_in", cap, ta.build_pool(caps, rules_by_cap)[0])
            if "union_loo" in want_arms:
                record("union_loo", cap, ta.build_pool(peers, rules_by_cap)[0])
            if "veto_in" in want_arms:
                pool, _ = ta.build_pool(caps, rules_by_cap)
                record("veto_in", cap,
                       ta.filter_pool(pool, caps, m, rules_by_cap, manifest, "veto")[0])
            if "veto_loo" in want_arms:
                pool, _ = ta.build_pool(peers, rules_by_cap)
                record("veto_loo", cap,
                       ta.filter_pool(pool, peers, m, rules_by_cap, manifest, "veto")[0])
            if "veto_abst_in" in want_arms:
                pool = ta.build_pool(caps, rules_by_cap)[0]
                record("veto_abst_in", cap,
                       ta.filter_pool(pool, caps, m, rules_by_cap, manifest, "veto_abst")[0])
            if "veto_abst_loo" in want_arms:
                pool = ta.build_pool(peers, rules_by_cap)[0]
                record("veto_abst_loo", cap,
                       ta.filter_pool(pool, peers, m, rules_by_cap, manifest, "veto_abst")[0])
            if {"union_tpl_loo", "veto_tpl_loo"} & set(want_arms):
                tpeers = [c for c in peers
                          if tpl.get(cap) is not None and tpl.get(c) == tpl.get(cap)]
                if tpeers:
                    tpool = ta.build_pool(tpeers, rules_by_cap)[0]
                    if "union_tpl_loo" in want_arms:
                        record("union_tpl_loo", cap, tpool)
                    if "veto_tpl_loo" in want_arms:
                        record("veto_tpl_loo", cap,
                               ta.filter_pool(tpool, tpeers, m, rules_by_cap,
                                              manifest, "veto")[0])
            if "vote2_loo" in want_arms:
                pool, _ = ta.build_pool(peers, rules_by_cap)
                record("vote2_loo", cap,
                       ta.filter_pool(pool, peers, m, rules_by_cap, manifest, "vote2")[0])
            if "intersect" in want_arms:
                record("intersect", cap, intersect_pool(caps, rules_by_cap))
        print(f"  {scope} ({len(caps)} caps)  {time.time()-t0:.0f}s", file=sys.stderr)

    out = []
    for g in ["ALL", "browser", "web", "suite", "native"]:
        if g not in ("ALL", "browser") and g not in want_groups:
            continue
        sub = {a: agg[f"{a}|{g}"] for a in want_arms}
        if any(v["n"] for v in sub.values()):
            label = {"browser": "BROWSER (web + self-hosted suites)",
                     "web": "WEB (public internet)",
                     "suite": "SUITE (self-hosted eval platforms)",
                     "native": "NATIVE APPS"}.get(g, g)
            out.append(_table(f"GROUP = {label}", sub, want_arms))
    if args.per_scope:
        for s in sorted({k.split("|", 1)[1] for k in per_scope}):
            sub = {a: per_scope[f"{a}|{s}"] for a in want_arms}
            if any(v["n"] for v in sub.values()):
                out.append(_table(f"SCOPE = {s}  ({len(scopes[s])} captures)", sub, want_arms))
    report = "\n".join(out)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
