#!/usr/bin/env python3
"""Seed-repeating wrapper around run.py.

Runs each task up to --seeds times, storing every seed in its OWN result tree so
records never overwrite each other:

    <result_dir>/seed1/<action_space>/<obs>/<run_label>/<domain>/<task>/...
    <result_dir>/seed2/...
    <result_dir>/seed3/...

A task is dropped as soon as one seed scores adversary_result == 1 -- so an attack
that succeeds on the first try gets 1 record (reported 1/1), one that only succeeds
on the third gets 3 records (1/3), and one that never succeeds gets 3 (0/3).

Without --again this is resumable and fills up to --seeds records per task, skipping
tasks that already hit or that already have a record in the seed being run. With
--again every task is run for all --seeds seeds regardless (no early stop, no skip).

Everything this wrapper does not consume is forwarded verbatim to run.py, so any
run.py flag (--model, --vllm_port, --max_steps, --sa_*, ...) works unchanged. Only
--result_dir and --task_ids are managed by the wrapper.

Example:
  python run_seeds.py --seeds 3 \
      --test_all_meta_path evaluation_examples/test_all_rtc_hard.json \
      --result_dir ./results_rtc_hard_seeds \
      --model "vllm | Qwen/Qwen3.8-27B" --vllm_port 8002 \
      --max_steps 50 --max_tokens 12000
"""
import argparse
import json
import os
import subprocess
import sys


def run_label_for(ns):
    """Mirror run.py's run_label derivation so we can locate results on disk."""
    if ns.agent_type == "SA-Agent":
        return f"{ns.sa_p_llm or ns.model}+sa"
    if ns.agent_type == "FidesAgent":
        return f"{ns.model}+fides"
    if ns.agent_type == "CamelAgent":
        return f"{ns.model or ns.camel_p_llm_model}+camel"
    if ns.agent_type == "HybridAgent":
        return f"{ns.model}+hybrid"
    if ns.agent_type == "UCMAgent":
        return f"{ns.ucm_model or ns.model}+ucm"
    if ns.agent_type == "HumanAgent":
        return "human"
    return ns.model


def task_dir(base, seed, ns, label, domain, task):
    return os.path.join(base, f"seed{seed}", ns.action_space,
                        ns.observation_type, label, domain, task)


def read_adv(path):
    p = os.path.join(path, "adversary_result.txt")
    try:
        with open(p) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def has_record(path):
    return (os.path.isfile(os.path.join(path, "result.txt"))
            and os.path.isfile(os.path.join(path, "adversary_result.txt")))


def is_hit(path):
    return read_adv(path) == 1.0


def main():
    ap = argparse.ArgumentParser(add_help=True, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", type=int, default=3, help="max records per task (default 3)")
    ap.add_argument("--result_dir", default="./results_seeds",
                    help="BASE dir; each seed goes under <result_dir>/seedK")
    ap.add_argument("--test_all_meta_path", required=True)
    ap.add_argument("--domain", default="all")
    ap.add_argument("--task_ids", default=None,
                    help="restrict to these task ids (comma-separated)")
    ap.add_argument("--task_filter", default=None)
    ap.add_argument("--again", action="store_true",
                    help="run all seeds for every task (no early stop, no skip)")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--model", default=None)
    ap.add_argument("--agent_type", default="PromptAgent")
    ap.add_argument("--sa_p_llm", default=None)
    ap.add_argument("--camel_p_llm_model", default=None)
    ap.add_argument("--ucm_model", default=None)
    ap.add_argument("--action_space", default="pyautogui")
    ap.add_argument("--observation_type", default="screenshot")
    ap.add_argument("--dry_run", action="store_true",
                    help="print the per-seed plan and the run.py commands, run nothing")
    ns, rest = ap.parse_known_args()

    label = run_label_for(ns)

    meta = json.load(open(ns.test_all_meta_path))
    if ns.domain != "all":
        meta = {ns.domain: meta.get(ns.domain, [])}
    universe = [(d, t) for d, ts in meta.items() for t in ts]
    if ns.task_ids:
        keep = {x.strip() for x in ns.task_ids.split(",")}
        universe = [(d, t) for d, t in universe if t in keep]
    if ns.task_filter:
        universe = [(d, t) for d, t in universe if ns.task_filter in t]
    if not universe:
        print("run_seeds: no tasks after filtering.", file=sys.stderr)
        sys.exit(1)

    print(f"run_seeds: {len(universe)} tasks x up to {ns.seeds} seeds, "
          f"run_label={label!r}, base={ns.result_dir}")

    for k in range(1, ns.seeds + 1):
        pending = []
        for d, t in universe:
            if not ns.again:
                prior_hit = any(is_hit(task_dir(ns.result_dir, j, ns, label, d, t))
                                for j in range(1, k))
                if prior_hit:
                    continue
                if has_record(task_dir(ns.result_dir, k, ns, label, d, t)):
                    continue
            pending.append((d, t))

        if not pending:
            print(f"[seed {k}] nothing pending -- skipping.")
            continue

        seed_result_dir = os.path.join(ns.result_dir, f"seed{k}")
        ids = ",".join(t for _d, t in pending)
        cmd = [ns.python, "run.py",
               "--test_all_meta_path", ns.test_all_meta_path,
               "--result_dir", seed_result_dir,
               "--action_space", ns.action_space,
               "--observation_type", ns.observation_type,
               "--agent_type", ns.agent_type,
               "--task_ids", ids] + rest
        if ns.model is not None:
            cmd += ["--model", ns.model]
        if ns.sa_p_llm is not None:
            cmd += ["--sa_p_llm", ns.sa_p_llm]
        if ns.camel_p_llm_model is not None:
            cmd += ["--camel_p_llm_model", ns.camel_p_llm_model]
        if ns.ucm_model is not None:
            cmd += ["--ucm_model", ns.ucm_model]
        if ns.domain != "all":
            cmd += ["--domain", ns.domain]

        print(f"\n[seed {k}] {len(pending)} tasks -> {seed_result_dir}")
        if ns.dry_run:
            print("  " + " ".join(
                (f'"{c}"' if " " in c or "|" in c else c) for c in cmd))
            continue
        rc = subprocess.call(cmd)
        print(f"[seed {k}] run.py exit {rc}")

    if not ns.dry_run:
        print("\nrun_seeds: done. Aggregate with:  python score_seeds.py "
              f"--result_dir {ns.result_dir} --test_all_meta_path {ns.test_all_meta_path}"
              + (f" --domain {ns.domain}" if ns.domain != "all" else ""))


if __name__ == "__main__":
    main()
