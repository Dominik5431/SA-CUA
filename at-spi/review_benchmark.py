#!/usr/bin/env python3
"""
review_benchmark.py -- manual, visual spot-check of a labeling approach's trust verdicts, since
those labels are judgment calls (e.g. "is this Wikipedia paragraph editorial or UGC?") that are
much easier to verify by looking at the real rendered page than by reading text snippets out of a
JSON file. Works with any approach under at-spi/labelers/, picked with --source <name>, which
loads at-spi/labels/<name>.json -- e.g.:
  selector_allowlist_fulltree (default) -- labelers/selector_rules.py's allowlist verdicts
  selector_optimal                      -- labelers/selector_optimal.py's oracle ceiling
Every approach writes the same row schema (see full_tree_extraction.py + any labelers/*.py), and
every row must carry its own "bbox". Adding a new
one is just: write at-spi/labelers/<name>.py, have it write at-spi/labels/<name>.json in that
schema, then `--source <name>` here works with no changes to this script.

Sampling is per SCREENSHOT, not per element: --n picks that many source captures, and every
relevant row for each picked capture gets its own numbered box on that one screenshot (not a
single row plucked out of it) -- re-locates each row's element on its capture's real screenshot
(at-spi/screenshots/), draws a numbered box colored by the chosen source's verdict (green=trusted,
red=untrusted), and prints a matching numbered table (verdict/confidence/rationale, plus the
verdict alongside it) so you can cross-reference the
box against the claim in the context of the whole page at once.

Screenshots are ranked "risk-first", since uniform random sampling would mostly show you
already-agreed-upon pages: captures containing more mismatches (current classifier says
UNTRUSTED, the source's verdict says TRUSTED -- these justify relaxing the classifier, worth
confirming they're real) rank first, then captures with more medium-confidence rows (a source may
flag as explicit judgment calls). Within a picked screenshot, only its mismatch/medium-confidence
rows are boxed by default -- not every already-agreed-upon element -- to keep the image readable.
Pass explicit filters (--mismatch_only, --category, --confidence) to review
a specific slice instead; a picked screenshot then shows every row of its that matches the filter.

Usage:
  python at-spi/review_benchmark.py --n 10
  python at-spi/review_benchmark.py --mismatch_only --category reference --n 8
  python at-spi/review_benchmark.py --category docs --confidence medium --n 5
  python at-spi/review_benchmark.py --source selector_allowlist_fulltree --n 10

  # selector_llm_proposed is RUN-INDEXED: labels/selector_llm_proposed/ holds one file per run
  # (see at-spi/label_store.py), so the same file can carry several models' proposals AND several
  # runs of one model. Filter to one at a time so their boxes don't overlap on the same
  # screenshot, or list what's available first. With several runs present and no --run_id, the
  # NEWEST is used and which one is announced on stderr.
  python at-spi/review_benchmark.py --source selector_llm_proposed --list_runs
  python at-spi/review_benchmark.py --source selector_llm_proposed --list_models
  python at-spi/review_benchmark.py --source selector_llm_proposed --model "Qwen/Qwen3.6-35B-A3B"
  python at-spi/review_benchmark.py --source selector_llm_proposed --run_id 20260819T143422Z
Outputs:
  at-spi/review/{file}.png  -- one annotated screenshot per selected capture
  A numbered report printed to stdout, in the same order as each image's boxes.
"""
import argparse
import json
import os
import random
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFont


import label_store

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS_DIR = os.path.join(HERE, "labels")
SCREENSHOT_DIR = os.path.join(HERE, "screenshots")

_GT_COLOR = {"trusted": (34, 139, 69), "untrusted": (192, 57, 43)}


def _available_sources() -> list[str]:
    return label_store.available_sources()


def _available_models(source: str, run_id: str = None) -> list[str]:
    """Distinct row["model"] values present in a source -- only meaningful for sources that set
    one (currently just selector_llm_proposed, see selector_propose.py)."""
    return label_store.available_models(source, run_id)


def _load_font(size: int = 14) -> "ImageFont.ImageFont":
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "arial.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            pass
    return ImageFont.load_default()


def _select_screenshots(rows: list[dict], file: str, n: int, mismatch_only: bool, false_negative_only: bool,
                         category: str, confidence: str, model: str, seed: int,
                         show_all: bool) -> dict[str, list[dict]]:
    """Picks n source captures (not n elements) and returns {filename: [rows to box on it]}."""
    if file:
        rows = [r for r in rows if r["file"].startswith(file)]
    if category:
        rows = [r for r in rows if r["category"] == category]
    if confidence and confidence != "all":
        rows = [r for r in rows if r.get("confidence") == confidence]
    if model:
        rows = [r for r in rows if r.get("model") == model]
    if mismatch_only:
        rows = [r for r in rows if r["mismatch"]]
    if false_negative_only:
        rows = [r for r in rows if not r["current_untrusted"] and r["verdict"] == "untrusted"]

    if not rows:
        sys.exit("No labeled rows match the given filters.")

    narrow_filter = mismatch_only or false_negative_only
    if not narrow_filter and not show_all:
        risky = [r for r in rows if r["mismatch"] or r.get("confidence") == "medium"]
        rows = risky or rows

    by_file: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_file[row["file"]].append(row)

    rng = random.Random(seed)
    files = list(by_file)
    rng.shuffle(files)
    files.sort(key=lambda f: (
        -sum(1 for r in by_file[f] if r["mismatch"]),
        -sum(1 for r in by_file[f] if r.get("confidence") == "medium"),
    ))
    selected = files[:n] if n < len(files) else files
    return {f: by_file[f] for f in selected}


def _elem_index_of(filename: str) -> int:
    m = re.match(r"^(\d+)_", filename)
    return int(m.group(1)) if m else -1


def _load_rows(args) -> list[dict]:
    """Loads --source's labels/<source>.json. Every labeler writes the same schema (see
    extraction.py), so no per-source special-casing is needed here -- adding a new labeler under
    at-spi/labelers/ just means a new --source name becomes usable, no changes to this file."""
    rows = label_store.load_rows(args.source, args.run_id)
    if not rows:
        where = (f"--run_id {args.run_id!r} in " if args.run_id else "")
        sys.exit(f"No rows for {where}--source {args.source!r}.\n"
                  f"Available --source values: {', '.join(_available_sources())}")
    rows = [r for r in rows if r.get("verdict") in ("trusted", "untrusted")]
    if not rows:
        sys.exit(f"--source {args.source!r} has no labeled (trusted/untrusted) rows yet.")

    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="selector_allowlist_fulltree",
                     help="Which labels/<source>.json to review (default: selector_allowlist_fulltree). "
                          "Any file under at-spi/labels/ works -- run with --list_sources to see what's available.")
    ap.add_argument("--list_sources", action="store_true", help="Print available --source values and exit")
    ap.add_argument("--list_models", action="store_true",
                     help="Print --model values present in the selected --source and exit "
                          "(only meaningful for sources that tag rows with a model, currently "
                          "selector_llm_proposed -- see selector_propose.py's --model)")
    ap.add_argument("--list_runs", action="store_true",
                     help="Print the runs present in the selected --source and exit: run_id, "
                          "model, capture count, policy, rules_dir and the run's totals, read "
                          "from each run's .meta.json manifest (see at-spi/label_store.py).")
    ap.add_argument("--output_dir", default=os.path.join(HERE, "review"))
    ap.add_argument("--n", type=int, default=10, help="Number of screenshots (source captures) to sample (default: 10)")
    ap.add_argument("--file", type=str, default="006", help="Id of file to review.")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--category", default=None, help="Restrict to one sites.py category (e.g. reference, docs, qa)")
    ap.add_argument("--confidence", choices=["high", "medium", "all"], default="all",
                     help="Show only rows at this confidence level (e.g. --confidence medium to "
                          "see only the source's own judgment calls). Only meaningful for sources "
                          "that set a confidence (selector_optimal, selector_allowlist_fulltree). "
                          "selector_llm_proposed no longer sets one -- the field was dropped when "
                          "the prompt stopped asking for it -- so filtering that source by "
                          "confidence selects nothing and the risky-row pool falls back to the "
                          "full filtered set.")
    ap.add_argument("--model", default=None,
                     help="Show only rows proposed by this model (exact match against the row's "
                          "own 'model' field, e.g. 'Qwen/Qwen3.6-35B-A3B'). Only meaningful for "
                          "sources that tag rows with a model (selector_llm_proposed.json) -- "
                          "run --list_models to see what's available for the selected --source. "
                          "Required when a source has multiple models and you don't want their "
                          "boxes overlapping on the same screenshot.")
    ap.add_argument("--run_id", default=None,
                     help="Show only rows from this proposal run (see --list_runs). Runs of the "
                          "SAME model are separate results -- run-to-run non-determinism at "
                          "temperature=0.0 is real -- so they are stored separately and must be "
                          "reviewed one at a time. Default: the newest run present, announced on "
                          "stderr.")
    ap.add_argument("--mismatch_only", action="store_true",
                     help="Only sample rows where the current classifier says UNTRUSTED but the source's verdict says TRUSTED")
    ap.add_argument("--false_negative_only", action="store_true",
                     help="Only sample rows where the current classifier is NOT untrusted but the source's verdict says UNTRUSTED "
                          "(security-relevant: cases the current system already under-protects)")
    ap.add_argument("--all", dest="show_all", action=argparse.BooleanOptionalAction, default=True,
                     help="Box every labeled row on each selected screenshot (default: on -- a "
                          "confidently-resolved row was silently invisible otherwise, which for "
                          "full-tree sources where mismatch is always False could hide most of "
                          "the page; see plan finding on review_benchmark's risky-only default). "
                          "Pass --no-all for the old risk-first behavior (only mismatches/"
                          "medium-confidence rows) when a screenshot would otherwise be too dense "
                          "to read; combine with --confidence medium for exactly the judgment-call "
                          "subset regardless of --all.")
    args = ap.parse_args()

    if args.list_sources:
        for name in _available_sources():
            print(name)
        return

    if args.list_runs:
        runs = label_store.list_runs(args.source)
        if not runs:
            print(f"--source {args.source!r} is not run-indexed "
                  f"(only {label_store.RUN_INDEXED_SOURCE} is).")
        else:
            print(label_store.format_runs(runs))
        return

    if args.list_models:
        models = _available_models(args.source, args.run_id)
        if not models:
            print(f"No model-tagged rows in --source {args.source!r} "
                  f"(only {label_store.RUN_INDEXED_SOURCE} currently sets 'model').")
        for name in models:
            print(name)
        return

    all_rows = _load_rows(args)
    if args.model:
        available = sorted({r["model"] for r in all_rows if r.get("model")})
        if args.model not in available:
            sys.exit(f"--model {args.model!r} not found in --source {args.source!r}. "
                      f"Available: {available or '(this source has no model-tagged rows)'}")
        all_rows = [r for r in all_rows if r.get("model") == args.model]
    pre_narrow = all_rows
    all_rows, chosen_run = label_store.narrow_to_run(all_rows, args.run_id)
    if args.file and not any(r["file"].startswith(args.file) for r in all_rows):
        elsewhere = label_store.runs_covering(pre_narrow, args.file)
        if elsewhere and chosen_run not in elsewhere:
            sys.exit(f"Run {chosen_run!r} has no capture matching --file {args.file!r}. "
                      f"Runs that do: {elsewhere}. Re-run with --run_id <one of those>.")

    by_file = _select_screenshots(all_rows, args.file, args.n, args.mismatch_only, args.false_negative_only,
                                   args.category, args.confidence, args.model, args.seed, args.show_all)

    os.makedirs(args.output_dir, exist_ok=True)
    font = _load_font(14)

    report_lines = []
    n_written = 0
    for file_idx, (filename, file_rows) in enumerate(sorted(by_file.items())):
        index = _elem_index_of(filename)
        if file_rows and "bbox" not in file_rows[0]:
            print(f"SKIP {filename}: rows carry no 'bbox' -- interactable-only label sources were "
                  f"removed with the gen-1 universe", file=sys.stderr)
            continue
        bbox_of = lambda row: tuple(row["bbox"])

        screenshot_path = os.path.join(SCREENSHOT_DIR, os.path.splitext(filename)[0] + ".png")
        if not os.path.exists(screenshot_path):
            print(f"SKIP {filename}: no matching screenshot at {screenshot_path}", file=sys.stderr)
            continue
        image = Image.open(screenshot_path).convert("RGBA")
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        run_banner = f"  [run {chosen_run}]" if chosen_run else ""
        report_lines.append(f"\n{'=' * 100}\n{filename}{run_banner}  "
                            f"({args.output_dir}/{filename.replace('.xml', '.png')})\n{'=' * 100}")
        has_rule_compare = any("rule_verdict" in r for r in file_rows)
        has_model_column = (not args.model) and any(r.get("model") for r in file_rows)
        model_col = f" {'model':20}" if has_model_column else ""
        has_run_column = len({r.get("run_id") for r in file_rows}) > 1
        run_col = f" {'run_id':20}" if has_run_column else ""
        if has_rule_compare:
            header = f"{'#':>3}  {'elem_idx':8} {args.source:10} {'rule':10} {'current_level':24} {'mismatch':9}{model_col}{run_col} text"
        else:
            header = f"{'#':>3}  {'elem_idx':8} {'verdict':10} {'confidence':10} {'current_level':24} {'mismatch':9}{model_col}{run_col} text"
        report_lines.append(header)
        report_lines.append("-" * len(header))

        file_rows_sorted = sorted(file_rows, key=lambda r: (
            bbox_of(r)[2] * bbox_of(r)[3]
        ), reverse=True)

        for n, row in enumerate(file_rows_sorted, start=1):
            r, g, b = _GT_COLOR[row["verdict"]]
            x, y, w, h = bbox_of(row)
            draw.rectangle([x, y, x + w, y + h], fill=(r, g, b, 50), outline=(r, g, b, 235), width=3)
            tag = f"#{n} {row['verdict'].upper()}"
            tag_pos = (x + 2, y + 2)
            tag_bbox = draw.textbbox(tag_pos, tag, font=font)
            draw.rectangle(tag_bbox, fill=(0, 0, 0, 210))
            draw.text(tag_pos, tag, font=font, fill=(255, 255, 255, 255))

            row_model_col = f" {(row.get('model') or '-')[:20]:20}" if has_model_column else ""
            row_run_col = f" {(row.get('run_id') or '-')[:20]:20}" if has_run_column else ""
            if has_rule_compare:
                report_lines.append(
                    f"{n:>3}  {row['elem_index']:<8} {row['verdict']:10} {row.get('rule_verdict') or '-':10} "
                    f"{row['current_level']:24} {str(row['mismatch']):9}{row_model_col}{row_run_col} {row['text'][:80]!r}"
                )
            else:
                report_lines.append(
                    f"{n:>3}  {row['elem_index']:<8} {row['verdict']:10} {row.get('confidence') or '-':10} "
                    f"{row['current_level']:24} {str(row['mismatch']):9}{row_model_col}{row_run_col} {row['text'][:80]!r}"
                )
                if row.get("rationale"):
                    report_lines.append(f"      rationale: {row['rationale']}")

        result = Image.alpha_composite(image, overlay).convert("RGB")
        out_path = os.path.join(args.output_dir, filename.replace(".xml", ".png"))
        result.save(out_path)
        n_written += 1

    total_elements = sum(len(v) for v in by_file.values())
    print("\n".join(report_lines))
    print(f"\n{'=' * 100}")
    print(f"Sampled {n_written} screenshot(s), {total_elements} element(s) total -> {args.output_dir}/")
    print(f"Green box = TRUSTED, red box = UNTRUSTED (--source {args.source}). Box number matches the table above.")
    print(f"Spotted a wrong verdict? python at-spi/correct_ground_truth.py --file <file> "
          f"--elem_index <elem_idx column above> --verdict <trusted|untrusted> --reason \"...\"")


if __name__ == "__main__":
    main()
