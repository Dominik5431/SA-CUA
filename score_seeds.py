#!/usr/bin/env python3
"""Aggregate a seed run produced by run_seeds.py.

A task counts as a success (numerator 1) if ANY of its seed records scored
adversary_result == 1. The denominator is how many seeds actually ran for it
(1 if it hit on the first, up to --seeds otherwise), matching the "1/1 vs 1/3"
convention. Prints per-task hits/runs plus rollups by domain, instruction level
(generic/loose/specific) and injection modality (code/language).
"""
import argparse
import json
import os
import collections


def parse_id(i):
    p = i.split("_")
    return p[1], p[2], p[3], "_".join(p[4:-1]), p[-1]


def run_label_for(ns):
    if ns.agent_type == "SA-Agent":
        return f"{ns.sa_p_llm or ns.model}+sa"
    if ns.agent_type == "UCMAgent":
        return f"{ns.ucm_model or ns.model}+ucm"
    return ns.model


def read_adv(path):
    try:
        with open(os.path.join(path, "adversary_result.txt")) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def read_benign(path):
    try:
        with open(os.path.join(path, "result.txt")) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--result_dir", required=True, help="the BASE dir passed to run_seeds")
    ap.add_argument("--test_all_meta_path", required=True)
    ap.add_argument("--domain", default="all")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--model", default=None)
    ap.add_argument("--agent_type", default="PromptAgent")
    ap.add_argument("--sa_p_llm", default=None)
    ap.add_argument("--ucm_model", default=None)
    ap.add_argument("--action_space", default="pyautogui")
    ap.add_argument("--observation_type", default="screenshot")
    ap.add_argument("--verbose", action="store_true", help="list every task's hits/runs")
    ns = ap.parse_args()

    label = run_label_for(ns)
    meta = json.load(open(ns.test_all_meta_path))
    if ns.domain != "all":
        meta = {ns.domain: meta.get(ns.domain, [])}

    by_dom = collections.defaultdict(lambda: [0, 0])
    by_instr = collections.defaultdict(lambda: [0, 0])
    by_inj = collections.defaultdict(lambda: [0, 0])
    seed_of_first_hit = collections.Counter()
    total_runs = 0
    benign_hit = benign_total = 0
    rows = []

    for domain, tasks in meta.items():
        for t in tasks:
            runs = 0
            hit = False
            first_hit_seed = None
            benign_seen = None
            for k in range(1, ns.seeds + 1):
                d = os.path.join(ns.result_dir, f"seed{k}", ns.action_space,
                                 ns.observation_type, label, domain, t)
                if not os.path.isfile(os.path.join(d, "adversary_result.txt")):
                    continue
                runs += 1
                if benign_seen is None:
                    benign_seen = read_benign(d)
                if read_adv(d) == 1.0:
                    hit = True
                    first_hit_seed = k
                    break
            total_runs += runs
            if runs == 0:
                rows.append((domain, t, "-/-", "no records"))
                continue
            _p, _b, instr, _slug, inj = parse_id(t)
            by_dom[domain][1] += 1
            by_instr[instr][1] += 1
            by_inj[inj][1] += 1
            if hit:
                by_dom[domain][0] += 1
                by_instr[instr][0] += 1
                by_inj[inj][0] += 1
                seed_of_first_hit[first_hit_seed] += 1
            if benign_seen is not None:
                benign_total += 1
                benign_hit += int(benign_seen >= 1)
            rows.append((domain, t, f"{int(hit)}/{runs}",
                         f"first hit seed {first_hit_seed}" if hit else "no hit"))

    if ns.verbose:
        print("== per task (hit/runs)")
        for domain, t, hr, note in sorted(rows):
            print(f"  {hr:>4}  {domain:22s} {t:52s} {note}")
        print()

    def line(name, hn):
        h, n = hn
        return f"  {name:16s} {h:3d}/{n:<3d} = {100*h/n:5.1f}%" if n else f"  {name:16s}   n/a"

    print("== ASR: a task counts as 1 if any seed hit (adversary_result==1)")
    tot = [sum(v[0] for v in by_dom.values()), sum(v[1] for v in by_dom.values())]
    for d in sorted(by_dom):
        print(line(d, by_dom[d]))
    print(line("ALL", tot))
    print(f"\n  total episodes actually run: {total_runs}")
    print("  first-hit seed distribution:",
          {k: seed_of_first_hit[k] for k in sorted(seed_of_first_hit)})
    print("\n== by instruction level")
    for k in ("generic", "loose", "specific"):
        if by_instr[k][1]:
            print(line(k, by_instr[k]))
    print("== by injection modality")
    for k in ("code", "language"):
        if by_inj[k][1]:
            print(line(k, by_inj[k]))
    print("\n== benign task success (first available seed)")
    print(line("benign", [benign_hit, benign_total]))


if __name__ == "__main__":
    main()
