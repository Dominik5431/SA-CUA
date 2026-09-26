#!/usr/bin/env python3
"""
selector_optimal.py -- the ORACLE CEILING for selector-based untrusted-region detection: given the
hand-labeled ground truth, compute the best rule set that misses nothing (FN = 0) while flagging as
few trusted elements as possible, and report how close to that ceiling a real (greedy) search gets.

WHAT THIS IS AND IS NOT
  It derives its selectors FROM ground_truth_fulltree.json. Scoring the result against that same
  ground truth therefore measures nothing -- it is a ceiling, not a method, exactly as
  verify_selector_rules.py's hand-authored set is. Every emitted row carries `oracle: true` so this
  can never be mistaken for a blind Phase-1 result. Use --holdout to get a number that means
  something: rules are derived on one set of captures and measured on captures never seen.

THE TWO QUESTIONS IT ANSWERS
  1. Is FN = 0 even reachable, and at what FP cost? Two elements with the same selector signature
     but different verdicts cannot be separated by ANY rule in this language, so every untrusted
     element forces every trusted element sharing its signature to become a false positive. Summing
     those gives an exact lower bound on FP at FN = 0 -- computed by feasibility_floor(), before any
     search is attempted. Measured on 55 captures: 30.8% with own role+class only, 4.0% with one
     ancestor, 1.7% with three, and no further improvement at eight.
  2. What does a real search achieve? greedy_cover() runs weighted set cover, repeatedly taking the
     selector with the best (newly-covered untrusted) / (1 + newly-added trusted) ratio. Measured:
     FN 0.00%, FP 1.81% with 296 selectors -- within 6 elements of the 1.7% floor.

SELECTOR LANGUAGE
  (file, own_role, own_class_head, ancestor_chain) where ancestor_chain is the nearest `depth`
  ancestors as (role, class_head), depth 0..--max_depth. This mirrors what the production matchers
  can express: verify_selector_rules.predict() supports one ancestor predicate, and
  selector_propose._ancestor_path_matches() supports an ordered subsequence of several.
  `file` is part of the key by default, matching the "selectors are calibrated per site" design;
  --cross_site drops it and answers the harder generalisation question instead.

Usage:
  python at-spi/labelers/selector_optimal.py                      # ceiling + write labels
  python at-spi/labelers/selector_optimal.py --holdout 0.3        # honest generalisation number
  python at-spi/labelers/selector_optimal.py --max_depth 1        # what one ancestor can do
  python at-spi/labelers/selector_optimal.py --cross_site         # no per-file keying
  python at-spi/labelers/selector_optimal.py --floor_only         # analysis, write nothing

Outputs:
  labels/selector_optimal_fulltree.json  -- row verdicts, standard schema, every row oracle-tagged
  labels/selector_optimal_rules.json     -- the selectors themselves, inspectable and reusable
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import ground_truth_fulltree as gtf
import labeling_store as ls

ROWS_PATH = os.path.join(ls.LABELS_DIR, "selector_optimal_fulltree.json")
RULES_PATH = os.path.join(ls.LABELS_DIR, "selector_optimal_rules.json")


def _class_head(row: dict) -> "str | None":
    """First class token only. Sites append state/variant classes (`s-post s-post--hot`), so the
    head is the stable identity and the tail is noise that would fragment every signature.

    `class=" "` is a real thing in this corpus, so the truthiness test is not enough on its own --
    split() on it returns [] and indexing it raised."""
    parts = (row.get("attr_class") or "").split()
    return parts[0] if parts else None


def align_rows(universe: dict, rows: list[dict]) -> tuple[list, int]:
    """Re-point every ground-truth row at the element it was labeled ON, by `sig`.

    The exported elem_index is POSITIONAL: anything that adds a row to a capture renumbers
    everything after it, and the negative-coordinate parse fix alone brought 803 previously
    dropped nodes in. Indexing the universe with a stale elem_index does not error -- it silently
    derives the selector of whatever element now sits at that ordinal, so the ceiling would be
    computed over one set of elements and reported as if it were another. `sig` is content
    addressed (role, bbox, text, ordinal), so a row that still exists still matches and one that
    changed is dropped and counted rather than mis-joined.

    Returns (rows with elem_index refreshed, number dropped).
    """
    by_file: dict = {}
    for file, fu in universe.items():
        by_file[file] = {r["sig"]: r["elem_index"] for r in fu["rows"]}
    out, dropped = [], 0
    for r in rows:
        idx = by_file.get(r["file"], {}).get(r.get("sig"))
        if idx is None:
            dropped += 1
            continue
        out.append({**r, "elem_index": idx})
    return out, dropped


def ancestor_chain(fu: dict, elem_index: int, depth: int) -> tuple:
    out, rows = [], fu["rows"]
    parent = rows[elem_index]["parent"]
    while parent is not None and len(out) < depth:
        out.append((rows[parent]["role"], _class_head(rows[parent])))
        parent = rows[parent]["parent"]
    return tuple(out)


def selectors_for(fu: dict, file: str, elem_index: int, max_depth: int,
                  cross_site: bool) -> list[tuple]:
    """Every generalisation of one element, shallowest first -- the candidate set the search picks
    from. A shallower selector covers more untrusted elements but risks more trusted ones; letting
    the search choose per rule is the whole point."""
    row = fu["rows"][elem_index]
    scope = None if cross_site else file
    return [(scope, row["role"], _class_head(row), ancestor_chain(fu, elem_index, d))
            for d in range(max_depth + 1)]


def build_index(universe: dict, rows: list[dict], max_depth: int, cross_site: bool):
    """{selector: {(file, elem_index), ...}} for untrusted and trusted separately."""
    cover_u: dict = defaultdict(set)
    cover_t: dict = defaultdict(set)
    for r in rows:
        fu = universe.get(r["file"])
        if fu is None:
            continue
        key = (r["file"], r["elem_index"])
        target = cover_u if r["verdict"] == "untrusted" else cover_t
        for sel in selectors_for(fu, r["file"], r["elem_index"], max_depth, cross_site):
            target[sel].add(key)
    return cover_u, cover_t


def feasibility_floor(universe: dict, rows: list[dict], max_depth: int, cross_site: bool) -> dict:
    """Exact lower bound on false positives when FN must be 0, with no search involved.

    At the FINEST available signature, any trusted element sharing a signature with an untrusted
    one is unavoidable collateral: covering the untrusted element necessarily covers it too. The
    sum of those is what no rule set in this language can beat.
    """
    sig: dict = defaultdict(Counter)
    for r in rows:
        fu = universe.get(r["file"])
        if fu is None:
            continue
        finest = selectors_for(fu, r["file"], r["elem_index"], max_depth, cross_site)[-1]
        sig[finest][r["verdict"]] += 1
    forced = sum(c["trusted"] for c in sig.values() if c["untrusted"])
    n_trusted = sum(1 for r in rows if r["verdict"] == "trusted")
    return {"distinct_signatures": len(sig), "forced_fp": forced, "n_trusted": n_trusted,
            "floor_fp_rate": (forced / n_trusted) if n_trusted else 0.0,
            "colliding_signatures": sum(1 for c in sig.values() if c["untrusted"] and c["trusted"])}


def greedy_cover(cover_u: dict, cover_t: dict, need: set) -> tuple[list, set]:
    """Weighted set cover: take the selector with the best newly-covered-untrusted per
    newly-added-trusted ratio until every untrusted element is covered. Returns (rules, fp set).

    Greedy is the standard ln(n)-approximation here and lands within 6 elements of the exact floor
    on this corpus, so an exact solver would buy nothing worth the runtime."""
    chosen, fp = [], set()
    while need:
        best = None
        for sel, covered in cover_u.items():
            new = covered & need
            if not new:
                continue
            cost = len(cover_t.get(sel, set()) - fp)
            score = len(new) / (1 + cost)
            if best is None or score > best[0]:
                best = (score, sel, new)
        if best is None:
            break
        _score, sel, new = best
        chosen.append(sel)
        need -= new
        fp |= cover_t.get(sel, set())
    return chosen, fp



def container_chain(trees: list, elem_index: int) -> list:
    """(role, class, id) of every identifiable container at or above this row, innermost first.

    Only nodes carrying a class or an id qualify -- those are the ones the model can name from the
    sanitized skeleton, so the candidate set stays exactly what the rule language can express.
    """
    out, node = [], elem_index
    while node is not None:
        n = trees[node]
        if n.get("attr_class") or n.get("attr_id"):
            out.append((n["role"], n.get("attr_class"), n.get("attr_id")))
        node = n["parent"]
    return out


def container_selectors_for(trees: list, elem_index: int, scope, container_depth: int,
                            carve_out: bool) -> list:
    """Every container rule that would claim this row.

    container_depth=1 names one container. =2 additionally allows "this container, sitting inside
    that one" -- the same discriminating move `ancestor_path` makes for element selectors, since a
    class that is ambiguous alone (a generic `item`, `content`, `title`) often becomes decisive once
    its enclosing region is named.
    """
    chain = container_chain(trees, elem_index)
    role = trees[elem_index]["role"]
    out = []
    for i, sig in enumerate(chain):
        keys = [(scope, (sig,))]
        if container_depth >= 2:
            for anc in chain[i + 1:]:
                keys.append((scope, (sig, anc)))
        for k in keys:
            out.append(k + (None,))
            if carve_out:
                out.append(k + (role,))
    return out


def container_index(universe: dict, rows: list[dict], cross_site: bool, carve_out: bool = True,
                    container_depth: int = 1) -> dict:
    """{container_selector: {(file, elem_index), ...}} over the eval rows it would claim.

    A container claims itself and every eval row nested beneath it, at any depth -- the same
    semantics predict_trusted() applies to an ancestor_path rule.
    """
    cover: dict = defaultdict(set)
    by_file: dict = defaultdict(list)
    for r in rows:
        by_file[r["file"]].append(r)
    for filename, frows in by_file.items():
        fu = universe.get(filename)
        if fu is None:
            continue
        trees = fu["rows"]
        scope = None if cross_site else filename
        for r in frows:
            ei = r["elem_index"]
            if ei >= len(trees):
                continue
            for sel in container_selectors_for(trees, ei, scope, container_depth, carve_out):
                cover[sel].add((filename, ei))
    return cover


def apply_container_rules(universe: dict, rows: list[dict], rules: set, cross_site: bool,
                          carve_out: bool, container_depth: int, policy: str) -> dict:
    """{(file, elem_index): verdict} for rules fitted elsewhere -- the holdout path."""
    default = "untrusted" if policy == "allowlist" else "trusted"
    hit_verdict = "trusted" if policy == "allowlist" else "untrusted"
    out = {}
    for r in rows:
        fu = universe.get(r["file"])
        if fu is None or r["elem_index"] >= len(fu["rows"]):
            continue
        sels = container_selectors_for(fu["rows"], r["elem_index"],
                                       None if cross_site else r["file"],
                                       container_depth, carve_out)
        out[(r["file"], r["elem_index"])] = hit_verdict if any(s in rules for s in sels) else default
    return out


def container_ceiling(universe: dict, rows: list[dict], policy: str, cross_site: bool,
                      carve_out: bool = True, container_depth: int = 1) -> dict:
    """Greedy optimum over container rules, holding FN at 0.

    allowlist -- default untrusted, a rule TRUSTS. FN=0 therefore forbids any rule that claims an
                 untrusted row, so only homogeneous-trusted containers are admissible and FP is
                 whatever trusted content no admissible container reaches.
    denylist  -- default trusted, a rule marks UNTRUSTED. Every untrusted row must be claimed, and
                 FP is the trusted content dragged in by the containers that claim them.
    """
    cover = container_index(universe, rows, cross_site, carve_out, container_depth)
    verdict = {(r["file"], r["elem_index"]): r["verdict"] for r in rows}
    trusted = {k for k, v in verdict.items() if v == "trusted"}
    untrusted = {k for k, v in verdict.items() if v == "untrusted"}

    pure_t = {s: c for s, c in cover.items() if not (c & untrusted)}
    pure_u = {s: c for s, c in cover.items() if not (c & trusted)}
    mixed = len(cover) - len(pure_t) - len(pure_u)

    if policy == "allowlist":
        need, admissible = set(trusted), pure_t
        chosen, covered = [], set()
        while True:
            best, gain = None, 0
            for s, c in admissible.items():
                g = len(c - covered)
                if g > gain:
                    best, gain = s, g
            if best is None:
                break
            chosen.append(best)
            covered |= admissible[best]
            del admissible[best]
        fp, fn = len(need - covered), 0
        pred = {k: ("trusted" if k in covered else "untrusted") for k in verdict}
    else:
        chosen, claimed, fp_set = [], set(), set()
        need = set(untrusted)
        while need:
            best, score_ = None, 0.0
            for s, c in cover.items():
                new = c & need
                if not new:
                    continue
                cost = len((c & trusted) - fp_set)
                sc = len(new) / (1 + cost)
                if sc > score_:
                    best, score_ = s, sc
            if best is None:
                break
            chosen.append(best)
            need -= cover[best]
            claimed |= cover[best]
            fp_set |= cover[best] & trusted
        fp, fn = len(fp_set), len(need)
        pred = {k: ("untrusted" if k in claimed else "trusted") for k in verdict}

    n_t, n_u = len(trusted), len(untrusted)
    return {"policy": policy, "candidates": len(cover),
            "pure_trusted": len(pure_t), "pure_untrusted": len(pure_u), "mixed": mixed,
            "rules": len(chosen), "chosen": chosen, "pred": pred,
            "fp": fp, "fn": fn, "n_trusted": n_t, "n_untrusted": n_u,
            "fp_rate": fp / n_t if n_t else 0.0, "fn_rate": fn / n_u if n_u else 0.0,
            "container_depth": container_depth,
            "carve_out_rules": sum(1 for s in chosen if s[-1] is not None),
            "two_level_rules": sum(1 for s in chosen if len(s[1]) == 2)}


def apply_rules(universe: dict, rows: list[dict], rules: set, max_depth: int,
                cross_site: bool) -> dict:
    """{(file, elem_index): 'untrusted'|'trusted'} -- default trusted, any matching rule denies,
    which is exactly the denylist policy selector_rules.py implements."""
    out = {}
    for r in rows:
        fu = universe.get(r["file"])
        if fu is None:
            continue
        sels = selectors_for(fu, r["file"], r["elem_index"], max_depth, cross_site)
        hit = any(s in rules for s in sels)
        out[(r["file"], r["elem_index"])] = "untrusted" if hit else "trusted"
    return out


def score(pred: dict, rows: list[dict]) -> dict:
    c = Counter()
    for r in rows:
        truth = r["verdict"]
        guess = pred.get((r["file"], r["elem_index"]), "trusted")
        if guess == "untrusted" and truth == "untrusted":
            c["tp"] += 1
        elif guess == "untrusted":
            c["fp"] += 1
        elif truth == "untrusted":
            c["fn"] += 1
        else:
            c["tn"] += 1
    n_u, n_t = c["tp"] + c["fn"], c["tn"] + c["fp"]
    return {"tp": c["tp"], "fp": c["fp"], "tn": c["tn"], "fn": c["fn"],
            "fn_rate": c["fn"] / n_u if n_u else 0.0,
            "fp_rate": c["fp"] / n_t if n_t else 0.0,
            "n_untrusted": n_u, "n_trusted": n_t}


def write_rows(universe: dict, rows: list[dict], pred: dict, args) -> int:
    out = []
    for r in rows:
        guess = pred.get((r["file"], r["elem_index"]), "trusted")
        out.append({**{k: r[k] for k in ("file", "elem_index", "category", "role", "text", "bbox")},
                    "region_id": None, "region_role": None, "region_value_shape": None,
                    "cross_origin_embed": r.get("cross_origin_embed", False),
                    "current_level": "NOT_TRACKED", "current_untrusted": False,
                    "verdict": guess, "confidence": "high", "mismatch": False,
                    "oracle": True,
                    "derivation": f"selector_optimal max_depth={args.max_depth}"
                                  f"{' cross_site' if args.cross_site else ' per_site'}"})
    os.makedirs(ls.LABELS_DIR, exist_ok=True)
    with open(ROWS_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    return len(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max_depth", type=int, default=3,
                    help="Ancestor levels available to a selector (default 3 -- measured "
                         "saturation point; 8 gives an identical floor)")
    ap.add_argument("--cross_site", action="store_true",
                    help="Drop the filename from the selector key, so a rule must generalise "
                         "across captures instead of being calibrated per site")
    ap.add_argument("--holdout", type=float, default=0.0,
                    help="Fraction of CAPTURES (not elements) held out for evaluation. Splitting "
                         "by capture, never by element, is what makes the number honest -- "
                         "same-page elements are far too correlated to split randomly.")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--floor_only", action="store_true", help="Report the analysis, write nothing")
    ap.add_argument("--container", action="store_true",
                    help="Report the CONTAINER-rule ceiling instead -- the optimum for the rule "
                         "language selector_propose.py's LLM actually writes (name a container by "
                         "role/class/id; it and everything inside it takes the verdict). This is "
                         "the number an LLM run is fairly compared against; the element-selector "
                         "ceiling above is a different, narrower language.")
    ap.add_argument("--policy", choices=("allowlist", "denylist"), default="allowlist",
                    help="With --container: allowlist (default-untrusted, rules trust) or denylist")
    ap.add_argument("--no_carve_out", action="store_true",
                    help="With --container: containers only, no (container, own_role) candidates")
    ap.add_argument("--show", type=int, default=0, help="With --container: print the top N rules")
    ap.add_argument("--pixels", action="store_true",
                    help="With --container: also report the ceiling in PIXELS, through the same "
                         "selector_propose.pixel_confusion() eval_rules.py uses, so the ceiling "
                         "and the LLM's proposals are rasterised by one implementation.")
    ap.add_argument("--overlap", choices=("union", "specific"), default="union",
                    help="With --pixels: how a pixel covered by both verdicts is decided.")
    args = ap.parse_args()

    universe = ls.build_universe()
    rows = gtf.eval_rows()
    if not rows:
        raise SystemExit("No hand-labeled rows yet -- label some captures first.")
    rows, dropped = align_rows(universe, rows)
    if dropped:
        print(f"  {dropped} labeled row(s) no longer in the universe (capture re-shot since "
              f"labeling) -- dropped, never re-joined positionally; see align_rows()")
    files = sorted({r["file"] for r in rows})
    print(f"ground truth: {len(rows)} rows over {len(files)} captures "
          f"({sum(1 for r in rows if r['verdict']=='untrusted')} untrusted)")

    if args.container:
        r = container_ceiling(universe, rows, args.policy, args.cross_site,
                              carve_out=not args.no_carve_out)
        print(f"\nCONTAINER-RULE CEILING  (policy={r['policy']}"
              f"{', cross-site' if args.cross_site else ', per-site'}"
              f"{'' if not args.no_carve_out else ', no carve-out'})")
        print(f"  candidate containers        {r['candidates']:,}")
        print(f"    homogeneous trusted       {r['pure_trusted']:,}")
        print(f"    homogeneous untrusted     {r['pure_untrusted']:,}")
        print(f"    mixed (unusable whole)    {r['mixed']:,}")
        print(f"\n  greedy rule set             {r['rules']:,} rules"
              f"   ({r['carve_out_rules']:,} using an own_role carve-out)")
        print(f"    FN {r['fn']:,}/{r['n_untrusted']:,} = {100*r['fn_rate']:.2f}%")
        print(f"    FP {r['fp']:,}/{r['n_trusted']:,} = {100*r['fp_rate']:.2f}%")
        if r["fn"] == 0:
            print(f"    -> with FN held at 0, {100*(1-r['fp_rate']):.1f}% of trusted content is "
                  f"recoverable by container rules")
        if args.pixels:
            import selector_propose as sp
            by_file: dict = {}
            for row in rows:
                key = (row["file"], row["elem_index"])
                by_file.setdefault(row["file"], {})[row["elem_index"]] = (
                    row["verdict"], r["pred"].get(key, "trusted"))
            pix = Counter()
            for file, verdicts in by_file.items():
                fu = universe.get(file)
                if fu is None:
                    continue
                pc = sp.pixel_confusion(fu, verdicts, args.overlap)
                for k in ("tp", "fp", "tn", "fn"):
                    pix[k] += pc[k]
            n_u_px, n_t_px = pix["tp"] + pix["fn"], pix["tn"] + pix["fp"]
            print(f"\n  IN PIXELS (overlap={args.overlap}):")
            print(f"    FN {pix['fn']:,}/{n_u_px:,} = {100*pix['fn']/n_u_px if n_u_px else 0:.2f}%")
            print(f"    FP {pix['fp']:,}/{n_t_px:,} = {100*pix['fp']/n_t_px if n_t_px else 0:.2f}%")
            denom = pix["tp"] + pix["fp"] + pix["fn"]
            print(f"    IoU {pix['tp']/denom if denom else 0:.3f}")
        for sel in r["chosen"][:args.show]:
            scope, role, cls, node_id = sel[0], sel[1], sel[2], sel[3]
            extra = f"  own_role={sel[4]}" if len(sel) == 5 else ""
            print(f"      {str(scope or 'ANY')[:28]:28} role={role[:14]:14} "
                  f"id={str(node_id)[:18]:18} class={str(cls)[:34]:34}{extra}")
        return

    floor = feasibility_floor(universe, rows, args.max_depth, args.cross_site)
    print(f"\nfeasibility floor (max_depth={args.max_depth}"
          f"{', cross-site' if args.cross_site else ', per-site'}):")
    print(f"  distinct signatures        {floor['distinct_signatures']}")
    print(f"  signatures with BOTH verdicts (inseparable)  {floor['colliding_signatures']}")
    print(f"  minimum FP at FN=0         {floor['forced_fp']}/{floor['n_trusted']} "
          f"= {100*floor['floor_fp_rate']:.2f}%")

    train, test = rows, rows
    if args.holdout > 0:
        rng = random.Random(args.seed)
        shuffled = files[:]
        rng.shuffle(shuffled)
        n_test = max(1, int(len(shuffled) * args.holdout))
        test_files = set(shuffled[:n_test])
        train = [r for r in rows if r["file"] not in test_files]
        test = [r for r in rows if r["file"] in test_files]
        print(f"\nholdout: {len(files)-len(test_files)} train / {len(test_files)} test captures "
              f"({len(train)} / {len(test)} rows)")

    cover_u, cover_t = build_index(universe, train, args.max_depth, args.cross_site)
    need = {(r["file"], r["elem_index"]) for r in train if r["verdict"] == "untrusted"}
    chosen, _fp = greedy_cover(cover_u, cover_t, need)
    print(f"\ngreedy rule set: {len(chosen)} selectors"
          f" | ancestor depth used: {dict(Counter(len(s[3]) for s in chosen))}")

    ruleset = set(chosen)
    s_train = score(apply_rules(universe, train, ruleset, args.max_depth, args.cross_site), train)
    print(f"  on TRAIN (= the ceiling, derived from these labels):"
          f"  FN {100*s_train['fn_rate']:.2f}%  FP {100*s_train['fp_rate']:.2f}%")
    if args.holdout > 0:
        s_test = score(apply_rules(universe, test, ruleset, args.max_depth, args.cross_site), test)
        print(f"  on HELD-OUT captures (the number that means something):"
              f"  FN {100*s_test['fn_rate']:.2f}%  FP {100*s_test['fp_rate']:.2f}%"
              f"   (tp={s_test['tp']} fp={s_test['fp']} fn={s_test['fn']} tn={s_test['tn']})")
        if not args.cross_site:
            print("     NOTE: per-site keying means held-out captures can only match rules from "
                  "a capture of the SAME file, so this is near-vacuous unless --cross_site.")

    if args.floor_only:
        return
    pred = apply_rules(universe, rows, ruleset, args.max_depth, args.cross_site)
    n = write_rows(universe, rows, pred, args)
    with open(RULES_PATH, "w", encoding="utf-8") as f:
        json.dump([{"file": s[0], "own_role": s[1], "own_class_head": s[2],
                    "ancestor_chain": [list(a) for a in s[3]]} for s in chosen], f, indent=1)
    print(f"\nwrote {n} rows -> {os.path.relpath(ROWS_PATH, os.path.dirname(_HERE))}")
    print(f"wrote {len(chosen)} rules -> {os.path.relpath(RULES_PATH, os.path.dirname(_HERE))}")


if __name__ == "__main__":
    main()
