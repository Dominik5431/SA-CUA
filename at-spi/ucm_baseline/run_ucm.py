#!/usr/bin/env python3
"""Run UCM's automatic boundary detection over this corpus and score it on our ground truth.

    python -m ucm_baseline.run_ucm all --model "vllm|Qwen/Qwen3.8-27B" --vllm_port 8001

Three stages, separately invocable so a failure costs only its own stage:

  propose   per HOST: sanitize k captures, hand them to UCM's LLM loop, union the CSS selectors.
            This is the expensive stage (k * NUM_LLM_TURNS calls per host) and the only one that
            touches a model. Its output is a UCM llm_labels.json per host.
  match     per CAPTURE: run that host's selectors against the saved snapshot, record the xpath
            of every matched element. UCM's own element resolution, no model.
  score     per CAPTURE: xpath -> screen rectangle -> AT-SPI rows -> selector_propose.score_file.

WHY PER HOST AND NOT PER CAPTURE. UCM proposes one selector set per site from a handful of that
site's pages and applies it to the site. Asking it for a set per capture would be a different and
easier method than the one published, and would also cost 115 LLM conversations instead of 54.
The corpus has more pages per host than UCM's three, so the pages it did NOT see are kept and
reported separately: `in_sample` reproduces UCM's own protocol, where the selectors are evaluated
on the pages they were written from, and `held_out` is what the same selectors do on a page of
the same site they have not seen. Both are real numbers about UCM; only the second is a number
about generalisation.
"""
import argparse
import datetime
import json
import os
import sys
import traceback

_AT_SPI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _AT_SPI not in sys.path:
    sys.path.insert(0, _AT_SPI)
if os.path.join(_AT_SPI, "labelers") not in sys.path:
    sys.path.insert(0, os.path.join(_AT_SPI, "labelers"))

import labeling_store as ls
import rule_scope
from labelers import selector_propose as sp

from . import bridge, generic_site, llm_shim, match, ucm_import

RUNS_DIR = os.path.join(_AT_SPI, "ucm_runs")


def web_captures() -> list:
    """The captures UCM can even be pointed at: those with a DOM snapshot AND geometry.

    The other 45 are native applications -- Thunderbird, VS Code, LibreOffice, a terminal. UCM is
    a CSS-selector method over a DOM; there is no DOM, so there is no result to report, and
    counting them as failures would be scoring it on a task it does not claim.
    """
    universe = ls.build_universe()
    return [f for f in ls.corpus_files()
            if f in universe and match.has_snapshot(f) and bridge.has_geom(f)]


def group_by_host(files: list) -> dict:
    manifest = {r["filename"]: r for r in json.load(
        open(os.path.join(_AT_SPI, "manifest.json"), encoding="utf-8"))}
    out = {}
    for f in files:
        row = manifest.get(f, {})
        host = rule_scope.scope_of_manifest_row(row) or row.get("domain") or "unknown"
        out.setdefault(host, []).append(f)
    return {h: sorted(v) for h, v in sorted(out.items())}


def pick_pages(files: list, k: int) -> list:
    """Which captures of a host the model is shown.

    Evenly spaced over the host's captures rather than the first k. Captures are numbered in the
    order they were collected, which groups pages of the same type together, so the first three
    booking captures are three views of the same listing page while three spread across the range
    are a listing, a hotel and a search -- the spread UCM's own three hand-picked pages have.
    k <= 0 means every page.
    """
    if k <= 0 or len(files) <= k:
        return list(files)
    step = len(files) / float(k)
    return [files[min(len(files) - 1, int(i * step))] for i in range(k)]


def stage_propose(run_dir: str, hosts: dict, pages_per_host: int, args) -> dict:
    aue = llm_shim.install(args.model, args.vllm_port, temperature=args.temperature,
                           enable_thinking=args.enable_thinking, max_tokens=args.max_tokens)
    plan = {}
    for host, files in hosts.items():
        chosen = pick_pages(files, pages_per_host)
        plan[host] = {"captures": files, "in_sample": chosen,
                      "held_out": [f for f in files if f not in set(chosen)]}
        host_dir = os.path.join(run_dir, "hosts", host)
        cfg_path = os.path.join(host_dir, "llm_labels.json")
        if args.resume and os.path.exists(cfg_path):
            print(f"[propose] {host}: reusing {os.path.relpath(cfg_path, _AT_SPI)}")
            continue
        os.makedirs(host_dir, exist_ok=True)
        site = generic_site.for_host(host, clean_max_size=args.clean_max_size)
        all_selectors, seen, per_page = [], set(), {}
        for cap in chosen:
            with open(match.snapshot_path(cap), encoding="utf-8", errors="replace") as f:
                html = f.read()
            cleaned = site.clean_html_for_llm(html, max_size=site.CLEAN_HTML_MAX_SIZE)
            sanitized = site.sanitize_html_content(cleaned)
            with open(os.path.join(host_dir, f"sanitized_{cap.replace('.xml', '')}.html"),
                      "w", encoding="utf-8") as f:
                f.write(sanitized)
            print(f"[propose] {host} / {cap}: {len(html)} -> {len(cleaned)} -> "
                  f"{len(sanitized)} bytes", flush=True)
            try:
                sels = aue.analyze_with_llm({cap: sanitized}, "unused", site, site.NUM_LLM_TURNS)
            except Exception as e:
                print(f"[propose] {host} / {cap}: FAILED {type(e).__name__}: {e}", file=sys.stderr)
                traceback.print_exc()
                sels = []
            per_page[cap] = len(sels)
            for s in sels:
                key = s.get("css_selector", "")
                if key and key not in seen:
                    seen.add(key)
                    all_selectors.append(s)
        config = aue.generate_config(all_selectors, "sanitized")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        with open(os.path.join(host_dir, "llm_analysis_results.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"host": host, "pages_analyzed": chosen, "per_page_counts": per_page,
                       "selectors": all_selectors, "num_unique": len(all_selectors)}, f, indent=2)
        print(f"[propose] {host}: {len(all_selectors)} unique -> "
              f"{len(config['untrusted_selectors'])} after confidence filter", flush=True)
    with open(os.path.join(run_dir, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)
    return plan


def stage_match(run_dir: str, hosts: dict, args) -> None:
    out_dir = os.path.join(run_dir, "match")
    os.makedirs(out_dir, exist_ok=True)
    with match.SnapshotMatcher() as m:
        for host, files in hosts.items():
            cfg_path = os.path.join(run_dir, "hosts", host, "llm_labels.json")
            if not os.path.exists(cfg_path):
                print(f"[match] {host}: no llm_labels.json, skipping {len(files)} capture(s)",
                      file=sys.stderr)
                continue
            selectors = match.load_config(cfg_path)
            for cap in files:
                dest = os.path.join(out_dir, cap.replace(".xml", "") + ".json")
                if args.resume and os.path.exists(dest):
                    continue
                try:
                    res = m.run_config(cap, selectors)
                except Exception as e:
                    print(f"[match] {cap}: FAILED {type(e).__name__}: {e}", file=sys.stderr)
                    continue
                res["summary"]["host"] = host
                with open(dest, "w", encoding="utf-8") as f:
                    json.dump(res, f)
                s = res["summary"]
                print(f"[match] {cap:56s} selectors_hit={s['selectorsWithMatches']:3d}"
                      f"/{s['totalSelectors']:<3d} elements={s['uniqueElements']}", flush=True)


def stage_score(run_dir: str, hosts: dict, plan: dict, args) -> dict:
    universe = ls.build_universe()
    rows = []
    for host, files in hosts.items():
        in_sample = set((plan.get(host) or {}).get("in_sample") or [])
        for cap in files:
            mpath = os.path.join(run_dir, "match", cap.replace(".xml", "") + ".json")
            if not os.path.exists(mpath):
                continue
            with open(mpath, encoding="utf-8") as f:
                mres = json.load(f)
            geo = bridge.CaptureGeometry(cap)
            rects, rstats = bridge.rectangles(mres, geo)
            verdicts = bridge.verdicts(universe[cap], rects, eps=args.eps, mode=args.overlap,
                                       min_frac=args.min_frac)
            sc = sp.score_file(cap, [], universe=universe, page=verdicts)
            sc.update({"host": host, "in_sample": cap in in_sample,
                       "n_selectors": mres["summary"]["totalSelectors"],
                       "selectors_hit": mres["summary"]["selectorsWithMatches"],
                       "rects": len(rects), **{f"geo_{k}": v for k, v in geo.stats().items()},
                       **{f"res_{k}": v for k, v in rstats.items()}})
            sc["leak_errors"] = [
                {k: e[k] for k in ("elem_index", "role", "text", "bbox")}
                for e in sc.get("errors", [])
                if e["cell"] == "fn" and not e.get("capacity")]
            sc.pop("errors", None)
            rows.append(sc)
            print(f"[score] {cap:56s} TP={sc['tp']:5d} FP={sc['fp']:5d} TN={sc['tn']:5d} "
                  f"FN={sc['fn']:5d} (leak={sc['leak']:4d})", flush=True)
    return {"per_capture": rows, "totals": totals(rows)}


def totals(rows: list, predicate=None) -> dict:
    sel = [r for r in rows if predicate is None or predicate(r)]
    t = {k: sum(r.get(k, 0) for r in sel) for k in
         ("tp", "fp", "tn", "fn", "leak", "excluded_by_shortcut",
          "shown_by_rules", "shown_by_shortcut",
          "bounded", "bounded_grammar", "bounded_hand",
          "bounded_masked", "bounded_masked_grammar", "bounded_masked_hand",
          "shown_by_rules_bounded", "shown_by_rules_grammar", "shown_by_rules_hand",
          "shown_by_shortcut_bounded", "shown_by_shortcut_grammar", "shown_by_shortcut_hand",
          "fp_avoided_by_shortcut",
          "n_untrusted", "n_trusted", "fp_regions", "fn_regions", "rects")}
    t["captures"] = len(sel)
    t["precision"] = t["tp"] / (t["tp"] + t["fp"]) if t["tp"] + t["fp"] else 0.0
    t["recall"] = t["tp"] / t["n_untrusted"] if t["n_untrusted"] else 0.0
    t["f1"] = (2 * t["precision"] * t["recall"] / (t["precision"] + t["recall"])
               if t["precision"] + t["recall"] else 0.0)
    t["leak_rate"] = t["leak"] / t["n_untrusted"] if t["n_untrusted"] else 0.0
    t["fp_rate"] = t["fp"] / t["n_trusted"] if t["n_trusted"] else 0.0
    t["bounded_masked_rate"] = t["bounded_masked"] / t["bounded"] if t["bounded"] else 0.0

    def _per_capture(num, den):
        return sorted(r[num] / r[den] for r in sel if r.get(den))
    for name, num, den in (("leak", "leak", "n_untrusted"),
                           ("fp", "fp", "n_trusted"),
                           ("shown_by_rules", "shown_by_rules", "n_untrusted"),
                           ("bounded_masked", "bounded_masked", "bounded")):
        vals = _per_capture(num, den)
        t[f"{name}_rate_macro"] = sum(vals) / len(vals) if vals else 0.0
        t[f"{name}_rate_median"] = vals[len(vals) // 2] if vals else 0.0
        t[f"{name}_captures_clean"] = sum(1 for v in vals if v == 0)
        t[f"{name}_captures_scored"] = len(vals)
    return t


def report(result: dict) -> str:
    rows = result["per_capture"]
    lines, hdr = [], (f"{'':34s} {'caps':>5s} {'leaks':>7s} {'shown by':>9s} {'shown by':>9s} "
                      f"{'masked':>8s} {'over':>7s} {'correctly':>10s} {'leak':>7s} "
                      f"{'over-mask':>10s} {'bounded':>8s} {'of those':>9s} {'F1':>6s}")
    hdr2 = (f"{'configuration':34s} {'':5s} {'':7s} {'rules':>9s} {'shortcut':>9s} "
            f"{'correctly':>8s} {'masked':>7s} {'trusted':>10s} {'rate':>7s} "
            f"{'rate':>10s} {'rows':>8s} {'masked':>9s} {'':6s}")

    def line(label, t):
        return (f"{label:34s} {t['captures']:5d} {t['leak']:7d} {t['shown_by_rules']:9d} "
                f"{t['shown_by_shortcut']:9d} {t['tp']:8d} {t['fp']:7d} {t['tn']:10d} "
                f"{100 * t['leak_rate']:6.2f}% {100 * t['fp_rate']:9.2f}% "
                f"{t['bounded']:8d} {100 * t['bounded_masked_rate']:8.1f}% {t['f1']:6.3f}")

    lines += ["", "=" * len(hdr), hdr, hdr2, "-" * len(hdr)]
    lines.append(line("TOTAL", totals(rows)))
    lines.append(line("  in-sample (UCM protocol)", totals(rows, lambda r: r["in_sample"])))
    lines.append(line("  held-out", totals(rows, lambda r: not r["in_sample"])))
    lines += ["", "per host:"]
    for host in sorted({r["host"] for r in rows}):
        lines.append(line("  " + host, totals(rows, lambda r, h=host: r["host"] == h)))
    worst = sorted(rows, key=lambda r: -r["leak"])[:15]
    lines += ["", "captures with the most free-capacity false negatives:"]
    for r in worst:
        lines.append(f"  {r['file']:56s} leak={r['leak']:5d} shown_by_rules={r['shown_by_rules']:5d} "
                     f"tp={r['tp']:5d} fp={r['fp']:5d} selectors_hit={r['selectors_hit']}")
    unres = sum(r["res_unresolved"] for r in rows)
    matched = sum(r["res_matched_elements"] for r in rows)
    lines += ["", f"bridge: {matched} matched elements, "
                  f"{sum(r['res_resolved_xpath'] for r in rows)} resolved by xpath, "
                  f"{sum(r['res_resolved_key'] for r in rows)} by fallback key, "
                  f"{unres} unresolved ({100 * unres / max(matched, 1):.2f}%), "
                  f"{sum(r['res_zero_area'] for r in rows)} zero-area",
              f"frames UCM could not see: "
              f"{sum(r['geo_frames_ignored'] for r in rows)} over "
              f"{sum(1 for r in rows if r['geo_frames_ignored'])} captures"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["propose", "match", "score", "all"])
    ap.add_argument("--run", default=None, help="Run directory (default: a new timestamped one)")
    ap.add_argument("--model", default="vllm|Qwen/Qwen3.8-27B")
    ap.add_argument("--vllm_port", type=int, default=8001)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max_tokens", type=int, default=8000,
                    help="UCM passes 8000 explicitly; this is the fallback when it does not.")
    ap.add_argument("--enable_thinking", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--clean_max_size", type=int, default=None,
                    help="Cap on the cleaned HTML handed to the model. Default is "
                         "generic_site.CLEAN_HTML_MAX_SIZE (200k, UCM's modal value); their "
                         "gitlab profile uses 10M. Raise it to measure how much the cap costs.")
    ap.add_argument("--pages_per_host", type=int, default=3,
                    help="Captures shown to the model per host (UCM uses 3). 0 = all.")
    ap.add_argument("--hosts", default=None, help="Comma-separated host filter")
    ap.add_argument("--files", default=None, help="Comma-separated capture filter")
    ap.add_argument("--limit", type=int, default=0, help="Cap on captures, for smoke tests")
    ap.add_argument("--eps", type=int, default=bridge.DEFAULT_EPS)
    ap.add_argument("--overlap", choices=["contain", "frac"], default="contain")
    ap.add_argument("--min_frac", type=float, default=0.5)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    run_dir = args.run or os.path.join(
        RUNS_DIR, datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    os.makedirs(run_dir, exist_ok=True)

    files = web_captures()
    if args.files:
        want = {x if x.endswith(".xml") else x + ".xml" for x in args.files.split(",")}
        files = [f for f in files if f in want]
    hosts = group_by_host(files)
    if args.hosts:
        keep = set(args.hosts.split(","))
        hosts = {h: v for h, v in hosts.items() if h in keep}
    if args.limit:
        trimmed, n = {}, 0
        for h, v in hosts.items():
            if n >= args.limit:
                break
            take = v[:max(1, args.limit - n)]
            trimmed[h] = take
            n += len(take)
        hosts = trimmed
    n_caps = sum(len(v) for v in hosts.values())
    print(f"run dir: {run_dir}\n{n_caps} capture(s) over {len(hosts)} host(s)", flush=True)

    plan_path = os.path.join(run_dir, "plan.json")
    plan = json.load(open(plan_path, encoding="utf-8")) if os.path.exists(plan_path) else {}

    if args.stage in ("propose", "all"):
        plan = stage_propose(run_dir, hosts, args.pages_per_host, args)
        with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"argv": sys.argv, "hosts": {h: len(v) for h, v in hosts.items()},
                       "n_captures": n_caps, "pages_per_host": args.pages_per_host,
                       "clean_max_size": args.clean_max_size or generic_site.CLEAN_HTML_MAX_SIZE,
                       "llm": llm_shim.usage(), "ucm": ucm_import.provenance(),
                       "finished_at": datetime.datetime.now(
                           datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, f, indent=2)
    if args.stage in ("match", "all"):
        stage_match(run_dir, hosts, args)
    if args.stage in ("score", "all"):
        if not plan and os.path.exists(plan_path):
            plan = json.load(open(plan_path, encoding="utf-8"))
        result = stage_score(run_dir, hosts, plan, args)
        result["config"] = {"eps": args.eps, "overlap": args.overlap, "min_frac": args.min_frac}
        with open(os.path.join(run_dir, "score.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        text = report(result)
        with open(os.path.join(run_dir, "report.txt"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(text)


if __name__ == "__main__":
    main()
