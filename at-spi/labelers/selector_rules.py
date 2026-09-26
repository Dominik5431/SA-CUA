#!/usr/bin/env python3
"""
selector_rules.py -- turns ../verify_selector_rules.py's two hand-authored rule sets into the same
labelers/*.json row schema every other approach under at-spi/labelers/ writes, so
at-spi/review_benchmark.py can visually spot-check them like any other --source: any
labels/<name>.json in the shared row schema becomes a usable --source automatically.

Two policies, both scored/discussed in the plan's finding 7 ("false negatives are much worse than
false positives"):
  denylist  -- verify_selector_rules.RULES + predict(): default TRUSTED, an explicit rule denies.
  allowlist -- verify_selector_rules.TRUSTED_RULES + predict_default_untrusted(): default
               UNTRUSTED, an explicit rule (or a bare control_shape role) allows.

One element universe: full_tree_extraction.extract_full_tree_elements() -- every named, visible
node in the page's content root, regardless of role. There used to be a second, default one
(extraction.py's ~86-159 interactable rows per file, selected with --no-full_tree) and it went
with the gen-1 universe. It could only ever box interactive controls, so a page's actual prose body
-- paragraphs, headings, static text inside a question or answer, none of which carry an AT-SPI
action -- never got a box or a verdict, and looked "trusted" by omission even where a matched rule
already covered it.

Only 002_stackoverflow.com.xml, 006_unix.stackexchange.com.xml, 053_github.com.xml have calibrated
rules right now (verify_selector_rules.py's oracle-authored verification set, built with full
ground-truth access -- see that module's docstring for why that's a ceiling test, not a blind
Phase-1 simulation). Every other capture in the corpus gets ZERO applicable rules and falls
straight through to each policy's bare default -- confidence "medium" marks these fallback rows
(vs. "high" for an actual matched rule), so `--confidence medium` in review_benchmark.py shows
exactly the "we haven't calibrated this domain/template yet" surface. That's not a gap to hide:
under the allowlist policy in particular, an uncalibrated file shows up almost entirely UNTRUSTED
(everything except bare control_shape roles) -- which is exactly what the live mechanism's
miss-triggers-calibration design (plan section 2) intends to look like on a template it has never
seen, and is worth being able to see for real, not just assert.

Usage:
  python at-spi/labelers/selector_rules.py                 # full raw AT-SPI tree
  python at-spi/review_benchmark.py --source selector_denylist_fulltree --n 5 --category qa
  python at-spi/review_benchmark.py --source selector_allowlist_fulltree --n 5 --confidence medium
Outputs:
  at-spi/labels/selector_denylist_fulltree.json     -- denylist policy
  at-spi/labels/selector_allowlist_fulltree.json    -- allowlist policy
  Summary stats printed to stdout for each.
"""
import argparse
import glob
import json
import os
import re
import sys

_AT_SPI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_AT_SPI_DIR))
sys.path.insert(0, _AT_SPI_DIR)

from full_tree_extraction import XML_DIR
from critical_path import build_ancestor_index, critical_path_for
import biba
import full_tree_extraction as fte
import verify_selector_rules as vsr

LABELS_DIR = os.path.join(_AT_SPI_DIR, "labels")
OUT_PATHS = {
    "denylist": os.path.join(LABELS_DIR, "selector_denylist_fulltree.json"),
    "allowlist": os.path.join(LABELS_DIR, "selector_allowlist_fulltree.json"),
}


def _apply_policy(row, own_class, path, policy: str):
    rules = vsr.RULES.get(row["file"], [])
    trusted_rules = vsr.TRUSTED_RULES.get(row["file"], [])
    if policy == "denylist":
        verdict, note = vsr.predict(row, own_class, path, rules)
    else:
        verdict, note = vsr.predict_default_untrusted(row, own_class, path, trusted_rules)
    row["verdict"] = verdict
    row["confidence"] = "high" if note else "medium"
    row["rationale"] = note or (
        f"no calibrated rule for this domain/template -- {policy} policy default "
        f"({'trusted' if policy == 'denylist' else 'untrusted'})"
    )
    row["mismatch"] = row["current_untrusted"] and verdict == "trusted"


def label_file_full_tree(index: int, filename: str, policy: str, with_nodes: bool = False):
    """The universe: every named, visible node inside the page's content root -- see
    full_tree_extraction.py. There used to be a second, interactable-only `label_file()` here,
    over extraction.py's ~86-159 rows per file; it went with the gen-1 universe, which could not
    see ~98% of a page's actual UGC prose (measured on 002_stackoverflow.com: 2,455 named nodes in
    the content root, 41 of them reachable through the interactable rows)."""
    rows, nodes = fte.extract_full_tree_elements(index, filename, with_nodes=True)
    for row, node in zip(rows, nodes):
        own_class = fte.own_class_of(node)
        path = biba._trim_to_content_root(fte.ancestor_path_for(filename, node))
        _apply_policy(row, own_class, path, policy)

    return (rows, nodes) if with_nodes else rows


def _run_policy(policy: str, files: list) -> None:
    out_path = OUT_PATHS[policy]
    label_fn = label_file_full_tree
    all_rows: list[dict] = []
    for filename in files:
        m = re.match(r"^(\d+)_", filename)
        index = int(m.group(1)) if m else -1
        try:
            all_rows.extend(label_fn(index, filename, policy))
        except Exception as e:
            print(f"FAILED on {filename} ({policy}): {e}", file=sys.stderr)

    os.makedirs(LABELS_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, indent=2)

    total = len(all_rows)
    calibrated = [r for r in all_rows if r["confidence"] == "high"]
    print(f"\n=== {policy} -> {out_path} ===")
    print(f"Total labeled elements: {total}  "
          f"(from a calibrated rule: {len(calibrated)}, from bare policy default: {total - len(calibrated)})")

    untrusted = [r for r in all_rows if r["verdict"] == "untrusted"]
    print(f"Verdict split: {len(untrusted)} untrusted ({len(untrusted) / total * 100:.1f}%), "
          f"{total - len(untrusted)} trusted" if total else "n/a")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()

    files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(XML_DIR, "*.xml")))
    for policy in ("denylist", "allowlist"):
        _run_policy(policy, files)


if __name__ == "__main__":
    main()
