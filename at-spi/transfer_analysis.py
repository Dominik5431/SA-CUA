#!/usr/bin/env python3
"""Transfer and generalization of LLM-proposed trust rules across captures.

THE QUESTION THIS ANSWERS. Today every capture is labeled with rules proposed for that capture.
That is not deployable: an agent visiting a page cannot first pay for a proposal round. The
deployable shape is a CACHE -- rules proposed once, keyed by something the agent knows on arrival
(the host, or a family of related hosts), looked up and applied to a page nobody proposed for.
This script measures what that costs.

Two transfer radii, because "domain" is ambiguous and the two readings give different answers:

  site  (== "the same website")  leave-one-capture-out inside one host. stackoverflow.com rules
                                 scored on a held-out stackoverflow.com page. This is the radius
                                 a per-host cache actually has.
  family (== "the full domain")  rules pooled from OTHER hosts of the same family, scored on this
                                 host. stackoverflow+superuser+askubuntu+... rules on
                                 serverfault.com. Strictly harder: the source captures never saw
                                 this host. Defined only where a family has >1 host.

Both are measured against `self` -- the capture's own rules, i.e. what ships today -- which is the
ceiling transfer is being asked to approach.

POLARITY MATTERS FOR READING EVERY NUMBER HERE. Default untrusted; a rule GRANTS trust. So
  - dropping a rule can never create a leak, only over-masking (fp);
  - pooling rules can only ever ADD grants, so leaks move one way: up.
That asymmetry is why the filters below all subtract and none add.

THE FILTERS (cross-capture agreement, computed WITHOUT ground truth -- they have to be, a cache is
built before anyone has labeled the pages it will serve):

  none      pool everything.
  veto      drop a rule if ANY source capture contradicts it. "Contradicts" = the rule matches
            rows in that capture, and that capture's OWN proposer did not grant those rows trust.
            A rule that matches nothing elsewhere is kept -- absence of evidence is not evidence.
  vote2     the 2-of-N rule: keep only if at least two source captures independently grant the
            matched rows (support >= 2), OR the rule matches in only one capture at all.
  majority  keep if support > contra.

WHAT THIS MEASURED (v15 rules, 154 captures that have any donor at all):

    protocol/filter        rules/cap   leak    fn      fp
    self/none                    3.6      0   138    9331     what ships today
    site_loo/none               14.6    277   649    7330     naive pooling breaks FN=0
    site_loo/veto                6.7     13    49   10864
    family_all/veto              9.1      0    14   11063     <-- the recommendation
    adaptive/veto                7.8      3    21   11049

`family_all` + `veto` is the answer to the caching question: leak 0, fn an order of magnitude
below the per-capture baseline (138 -> 14), for 18.6% more over-masking. Every remaining fn is
capacity-bound, so nothing free-text is left visible on any capture.

MIN_VOTERS / `adaptive` IS KEPT ONLY AS THE EVIDENCE THAT IT LOSES. The reasoning behind it was
that a small host makes a weak jury -- true, and it does fix those: with one donor the veto is a
no-op (nothing can contradict the only proposer) and askubuntu leaked 10 rows under site_loo.
But narrowing back to the host wherever the host has >=3 captures costs 3 leaks that the always-
widen policy does not have: stackoverflow's own 13 pages agree with each other that an avatar
image and a username link are site chrome, and a jury drawn from one host cannot outvote a
belief that host's pages all share. Ten sibling Stack Exchange captures can, and do, for +9 fp.
So the rule is WIDEN ALWAYS, not widen-when-small: cross-host donors are not a fallback for
thin data, they are the only voters who can dissent from a host-local consensus.

`veto` and `vote2` differ in what silence means. Under `veto` a capture whose proposer simply
never wrote a rule for that region counts AGAINST the rule; under `vote2` it counts for nothing
and the rule needs positive corroboration instead. Since a proposer's silence is the single most
common thing in this corpus (see the coverage finding: most over-masking is missing rules, not
wrong ones), the two are expected to behave very differently, and that difference is the point.
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
from labelers import selector_propose as sp

DEFAULT_RULES_DIR = os.path.join(_HERE, "llm_rules_container_v15")


FAMILIES = {
    "stackexchange": {"stackoverflow.com", "superuser.com", "serverfault.com", "askubuntu.com",
                      "unix.stackexchange.com", "english.stackexchange.com"},
    "wikipedia": {"en.wikipedia.org", "simple.wikipedia.org"},
}
_HOST_TO_FAMILY = {h: f for f, hs in FAMILIES.items() for h in hs}

MIN_VOTERS = 3

_RTC_PLATFORMS = ("reddit", "owncloud", "rocketchat", "gitlab")
_NATIVE_APPS = ("thunderbird", "vscode", "libreoffice_impress", "libreoffice", "nautilus",
                "terminal", "gedit", "evince", "file_roller", "calculator", "settings",
                "system_monitor", "gimp", "vlc")


def group_of(cap: str) -> str:
    n = int(cap.split("_")[0])
    return "web" if n < 200 else ("native" if n < 300 else "rtc-suite")


def site_of(cap: str, manifest: dict) -> str:
    """The cache key a real agent would have on arrival."""
    g = group_of(cap)
    row = manifest.get(cap, {})
    if g == "web":
        return row.get("domain") or "?"
    stem = cap[4:-4] if cap.endswith(".xml") else cap[4:]
    if g == "rtc-suite":
        for p in _RTC_PLATFORMS:
            if stem.startswith(p):
                return p
        return stem
    for a in _NATIVE_APPS:
        if stem.startswith(a):
            return a
    return stem.split("_")[0]


def family_of(site: str) -> str:
    return _HOST_TO_FAMILY.get(site, site)


def load_manifest() -> dict:
    with open(os.path.join(_HERE, "manifest.json"), encoding="utf-8") as f:
        return {r["filename"]: r for r in json.load(f)}


def load_rules(rules_dir: str) -> dict:
    """{capture.xml: [rule]}. rpartition, not split -- model tags contain `__` and some capture
    stems end in `_`, so a left split silently drops captures (it dropped 425_owncloud once)."""
    import glob
    out = {}
    for path in sorted(glob.glob(os.path.join(rules_dir, "*.json"))):
        if path.endswith(".proposed.json"):
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        cap, _, _tag = stem.rpartition("__")
        cap = (cap or stem) + ".xml"
        with open(path, encoding="utf-8") as f:
            out[cap] = json.load(f)
    return out


def rule_key(rule: dict) -> str:
    """Identity of a rule for pooling. `note` is prose the model wrote and varies per capture for
    the same assertion, so it is excluded -- otherwise the pool never dedupes and every 'support'
    count is 1 by construction."""
    r = {k: v for k, v in rule.items() if k != "note"}
    return json.dumps(r, sort_keys=True)


class Matcher:
    """Caches the per-row ancestor chains of each capture; those are what rule matching walks and
    rebuilding them per rule is what makes the naive version quadratic-slow."""

    def __init__(self, universe: dict):
        self.U = universe
        self._paths: dict = {}
        self._own: dict = {}
        self._hits: dict = {}

    def paths(self, cap: str) -> list:
        if cap not in self._paths:
            fu = self.U[cap]
            self._paths[cap] = [
                (row["elem_index"],
                 sp.ancestor_path_rows(fu, row["elem_index"])
                 + [{"role": row["role"], "id": row.get("attr_id"),
                     "class": row.get("attr_class"), "bbox": row["bbox"]}])
                for row in fu["rows"]]
        return self._paths[cap]

    def matched(self, cap: str, rule: dict) -> set:
        """elem_indexes of `cap` this single rule covers. Page-scope rules cover everything.

        Memoized on (capture, rule identity): every filter mode asks the same question of the same
        (rule, capture) pairs, and that walk is the whole cost of this script. Uncached, a
        four-filter sweep pays for the same match four times."""
        ck = (cap, rule_key(rule))
        hit = self._hits.get(ck)
        if hit is not None:
            return hit
        if sp.is_page_scope(rule):
            hit = {i for i, _ in self.paths(cap)}
        elif not (rule.get("ancestor_path") or rule.get("own_role") or rule.get("own_class")):
            hit = set()
        else:
            hit = {i for i, p in self.paths(cap)
                   if sp.rule_covers(p, rule, geometric_gate=True)}
        self._hits[ck] = hit
        return hit

    def own_trusted(self, cap: str, rules_by_cap: dict, domain) -> set:
        """elem_indexes this capture's OWN rules grant trust to. The agreement signal."""
        if cap not in self._own:
            v = sp.predict_page(self.U[cap], rules_by_cap.get(cap, []), "allowlist", domain)
            self._own[cap] = {i for i, (verdict, _) in v.items() if verdict == "trusted"}
        return self._own[cap]


def build_pool(source_caps: list, rules_by_cap: dict) -> tuple:
    """(deduped rules, {rule_key: [source captures that proposed it]})."""
    pool, prov = {}, collections.defaultdict(list)
    for cap in source_caps:
        for rule in rules_by_cap.get(cap, []):
            k = rule_key(rule)
            prov[k].append(cap)
            pool.setdefault(k, rule)
    return list(pool.values()), prov


def filter_pool(pool: list, source_caps: list, m: Matcher, rules_by_cap: dict,
                manifest: dict, mode: str) -> tuple:
    """(kept rules, per-rule agreement stats). No ground truth is read anywhere in here."""
    if mode == "none":
        return pool, {}
    kept, stats = [], {}
    voters = source_caps
    if mode == "veto_abst":
        voters = [c for c in source_caps if rules_by_cap.get(c)]
    for rule in pool:
        support = contra = 0
        for cap in voters:
            hit = m.matched(cap, rule)
            if not hit:
                continue
            granted = m.own_trusted(cap, rules_by_cap, (manifest.get(cap) or {}).get("domain"))
            if hit <= granted:
                support += 1
            else:
                contra += 1
        stats[rule_key(rule)] = (support, contra)
        appear = support + contra
        if mode in ("veto", "veto_abst"):
            keep = contra == 0
        elif mode == "vote2":
            keep = support >= 2 or appear <= 1
        elif mode == "majority":
            keep = support > contra or appear == 0
        else:
            raise SystemExit(f"unknown filter {mode}")
        if keep:
            kept.append(rule)
    return kept, stats


def protocols(caps: list, manifest: dict) -> dict:
    """{name: {capture: [source captures]}}. A source list of [] means the protocol is undefined
    for that capture (a host with one capture has no leave-one-out); those are skipped, never
    scored as zero, and the capture count in the report says how many actually ran."""
    by_site = collections.defaultdict(list)
    for c in caps:
        by_site[site_of(c, manifest)].append(c)
    by_family = collections.defaultdict(list)
    for s, cs in by_site.items():
        by_family[family_of(s)].extend(cs)

    out = {"self": {c: [c] for c in caps}, "site_loo": {}, "family_cross": {},
           "family_all": {}, "adaptive": {}}
    for c in caps:
        s = site_of(c, manifest)
        peers = [x for x in by_site[s] if x != c]
        if peers:
            out["site_loo"][c] = peers
        fam = family_of(s)
        cross = [x for x in by_family[fam] if site_of(x, manifest) != s]
        if cross:
            out["family_cross"][c] = cross
        allpeers = [x for x in by_family[fam] if x != c]
        if allpeers:
            out["family_all"][c] = allpeers
        out["adaptive"][c] = peers if len(peers) >= MIN_VOTERS else (allpeers or peers)
    out["adaptive"] = {k: v for k, v in out["adaptive"].items() if v}
    return out


def _blank(): return dict(caps=0, leak=0, fn=0, fp=0, tp=0, tn=0, leakcaps=0, rules=0, norule=0)


def _add(a, s, nrules):
    a["caps"] += 1
    for k in ("leak", "fn", "fp", "tp", "tn"):
        a[k] += s[k]
    a["leakcaps"] += 1 if s["leak"] else 0
    a["rules"] += nrules
    a["norule"] += 1 if nrules == 0 else 0


def _table(title, agg, order):
    w = f"{'':<26}{'caps':>5}{'rules/cap':>11}{'leak':>7}{'leakcaps':>10}{'fn':>7}{'fp':>8}{'tp':>8}"
    lines = ["", title, "-" * len(w), w, "-" * len(w)]
    for k in order:
        a = agg.get(k)
        if not a or not a["caps"]:
            continue
        lines.append(f"{k:<26}{a['caps']:>5}{a['rules']/a['caps']:>11.1f}{a['leak']:>7}"
                     f"{a['leakcaps']:>10}{a['fn']:>7}{a['fp']:>8}{a['tp']:>8}")
    lines.append("-" * len(w))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rules_dir", default=DEFAULT_RULES_DIR)
    ap.add_argument("--protocols",
                    default="self,site_loo,family_cross,family_all,adaptive")
    ap.add_argument("--filters", default="none,veto,vote2,majority")
    ap.add_argument("--groups", default="web,rtc-suite,native")
    ap.add_argument("--per_site", action="store_true", help="also break the table down by host")
    ap.add_argument("--out", default=None, help="write the report here as well as to stdout")
    args = ap.parse_args()

    manifest = load_manifest()
    rules_by_cap = load_rules(args.rules_dir)
    t0 = time.time()
    U = ls.build_universe()
    want_groups = set(args.groups.split(","))
    caps = sorted(c for c in rules_by_cap if c in U and group_of(c) in want_groups)
    print(f"universe {len(U)} captures in {time.time()-t0:.0f}s; scoring {len(caps)}",
          file=sys.stderr)

    m = Matcher(U)
    protos = protocols(caps, manifest)
    want_p = [p for p in args.protocols.split(",") if p in protos]
    want_f = args.filters.split(",")

    agg = collections.defaultdict(_blank)
    per_site = collections.defaultdict(_blank)
    for pname in want_p:
        srcmap = protos[pname]
        for fname in want_f:
            if pname == "self" and fname != "none":
                continue
            for cap in caps:
                src = srcmap.get(cap)
                if not src:
                    continue
                pool, _prov = build_pool(src, rules_by_cap)
                kept, _stats = filter_pool(pool, src, m, rules_by_cap, manifest, fname)
                s = sp.score_file(cap, kept, universe=U)
                key = f"{pname}/{fname}"
                _add(agg[f"{key}|{group_of(cap)}"], s, len(kept))
                _add(agg[f"{key}|ALL"], s, len(kept))
                if args.per_site:
                    _add(per_site[f"{key}|{site_of(cap, manifest)}"], s, len(kept))
            print(f"  done {pname}/{fname}  ({time.time()-t0:.0f}s)", file=sys.stderr)

    combos = [f"{p}/{f}" for p in want_p for f in (["none"] if p == "self" else want_f)]
    out = []
    for g in ["ALL"] + [x for x in ("web", "rtc-suite", "native") if x in want_groups]:
        sub = {c: agg[f"{c}|{g}"] for c in combos}
        out.append(_table(f"GROUP = {g}", sub, combos))
    if args.per_site:
        sites = sorted({k.split("|")[1] for k in per_site})
        for s in sites:
            sub = {c: per_site[f"{c}|{s}"] for c in combos}
            if any(v["caps"] for v in sub.values()):
                out.append(_table(f"SITE = {s}", sub, combos))
    report = "\n".join(out)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
