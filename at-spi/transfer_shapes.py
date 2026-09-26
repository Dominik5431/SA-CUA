#!/usr/bin/env python3
"""Why a pooled rule does or does not survive the trip to another capture.

transfer_analysis.py measures WHAT transfer costs. This measures WHY, by rule shape, because the
shape is the only thing a cache designer can act on: a rule whose ancestor_path names a DOM id is
an assertion about one page's markup, and a rule that names only roles is an assertion about a
template. If the second kind carries the transfer and the first kind is dead weight, the cache
should be built out of the second kind and the prompt should be told to write them.

Three numbers per shape:
  proposed   how many distinct rules of this shape exist in the pool
  fires      how many of them match at least one row on a capture OTHER than the one they came
             from -- a rule that never fires elsewhere costs nothing and buys nothing
  corroborated  how many are independently granted by >=2 source captures (the vote2 signal)
"""

import argparse
import collections
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "labelers"))

import labeling_store as ls
from transfer_analysis import (Matcher, build_pool, group_of, load_manifest,
                               load_rules, protocols, rule_key, site_of)


def shape_of(rule: dict) -> str:
    """What the rule is anchored on. `id` beats `class` beats `role` because it is the most
    page-specific thing present -- a rule is only as portable as its least portable step."""
    if rule.get("scope"):
        return "page-scope"
    steps = rule.get("ancestor_path") or []
    if not steps:
        return "own_role only"
    if any(s.get("id") for s in steps):
        return "id-anchored"
    if any(s.get("class") for s in steps):
        return "class-anchored"
    return "role-only"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules_dir", default=os.path.join(_HERE, "llm_rules_container_v15"))
    ap.add_argument("--groups", default="web,rtc-suite,native")
    args = ap.parse_args()

    manifest = load_manifest()
    rules_by_cap = load_rules(args.rules_dir)
    U = ls.build_universe()
    want = set(args.groups.split(","))
    caps = sorted(c for c in rules_by_cap if c in U and group_of(c) in want)
    m = Matcher(U)
    protos = protocols(caps, manifest)

    by_site = collections.defaultdict(list)
    for c in caps:
        by_site[site_of(c, manifest)].append(c)

    stats = collections.defaultdict(lambda: dict(proposed=0, fires=0, corroborated=0, sites=0))
    per_site_rows = []
    for site, members in sorted(by_site.items()):
        if len(members) < 2:
            continue
        pool, prov = build_pool(members, rules_by_cap)
        srow = collections.defaultdict(lambda: [0, 0, 0])
        for rule in pool:
            k = rule_key(rule)
            home = set(prov[k])
            others = [c for c in members if c not in home]
            fires = any(m.matched(c, rule) for c in others)
            support = 0
            for c in members:
                hit = m.matched(c, rule)
                if hit and hit <= m.own_trusted(c, rules_by_cap,
                                                (manifest.get(c) or {}).get("domain")):
                    support += 1
            sh = shape_of(rule)
            stats[sh]["proposed"] += 1
            stats[sh]["fires"] += 1 if fires else 0
            stats[sh]["corroborated"] += 1 if support >= 2 else 0
            srow[sh][0] += 1
            srow[sh][1] += 1 if fires else 0
            srow[sh][2] += 1 if support >= 2 else 0
        per_site_rows.append((site, len(members), dict(srow)))

    order = ["role-only", "class-anchored", "id-anchored", "own_role only", "page-scope"]
    hdr = f"{'shape':<16}{'proposed':>10}{'fires elsewhere':>18}{'corroborated >=2':>19}"
    print("\nPOOLED PER SITE (leave-one-out radius)")
    print("-" * len(hdr)); print(hdr); print("-" * len(hdr))
    for sh in order:
        a = stats.get(sh)
        if not a or not a["proposed"]:
            continue
        p = a["proposed"]
        print(f"{sh:<16}{p:>10}{a['fires']:>13} {100*a['fires']/p:>4.0f}%"
              f"{a['corroborated']:>14} {100*a['corroborated']/p:>4.0f}%")
    print("-" * len(hdr))

    print("\nPER SITE  (proposed / fires-elsewhere / corroborated)")
    w = f"{'site':<26}{'caps':>5}  " + "".join(f"{s:>22}" for s in order[:3])
    print("-" * len(w)); print(w); print("-" * len(w))
    for site, n, srow in per_site_rows:
        cells = ""
        for sh in order[:3]:
            v = srow.get(sh)
            cells += f"{(f'{v[0]}/{v[1]}/{v[2]}' if v else '-'):>22}"
        print(f"{site:<26}{n:>5}  {cells}")
    print("-" * len(w))


if __name__ == "__main__":
    main()
