#!/usr/bin/env python3
"""extend_labels.py -- carry a domain's HAND-LABELED trust boundary onto its unlabeled captures,
by exact match, and abstain everywhere else.

The corpus is labeled unevenly: github and wikipedia are complete, stackoverflow has 4 of 13,
booking 3 of 16. Within one domain the header, sidebar and furniture repeat almost verbatim from
capture to capture, so an element that is *identical* to one a human already judged -- same role,
same first class, same text, same three-deep ancestor shape -- can inherit that judgement. Anything
that is not identical is LEFT UNLABELED for the human. There is no fallback guess.

WHY NOT ASK A MODEL. That was the first design and it was measured and rejected. Prompting
Qwen-AgentWorld with a domain's hand labels as worked examples and asking it to label a held-out
capture reproduced only 75.5% of the human's calls, and 20.1% of rows were "human said UNTRUSTED,
model said TRUSTED". That direction is not noise, it is corruption: a ground-truth row that wrongly
reads trusted turns a false negative into a true negative, so the system fails to mask a real
injection and the answer key says it was right. The corpus would keep reporting FN=0 while losing
the ability to detect a miss at all.

The failure was not a bad prompt. Two causes, both structural:

  * NOVEL WIDGETS. The held-out captures contain furniture absent from every exemplar in their
    domain (amazon's `mn-nav-card-product` Echo stripe, its rating-filter radio buttons,
    `imgTagWrapper`). Nothing to transfer, so the model guessed, and its prior is "looks like
    chrome -> trusted".
  * CONTEXT-DEPENDENT VERDICTS. Booking's `b7a691c583` images inside capture 028 alone split 1
    trusted / 9 untrusted -- same role, same class, same (empty) text. Only position on screen
    separates them, and that is precisely the evidence a text-only model does not have. This is
    what label_gt.py's docstring means by "the judgment being made is a *visual* one".

Exact match with abstention has neither failure mode, because it only ever answers when the
evidence is a human's own decision on an indistinguishable element. Measured on five held-out
hand-labeled captures (019 SO, 028 booking, 036 amazon, 003 github, 011 wikipedia):

    coverage 164/572 = 28.7%    precision 163/164 = 99.39%
    the single error is T->U (over-strict). ZERO U->T. No leak can be hidden by this table.

Requiring two exemplar captures instead of one does not help (26.6% coverage, same one error), so
the threshold is one.

WHAT THIS WRITES. mode="auto_exact", with the source captures in `note`. Hand labels are never
touched and never overwritten; a capture that already carries any hand label is skipped whole.
To remove every machine label and get today's ground truth back exactly:

    grep -v '"mode": "auto_exact"' labels/ground_truth_fulltree.jsonl > /tmp/gt && \
        mv /tmp/gt labels/ground_truth_fulltree.jsonl

Only content-tier rows are labeled -- exactly what label_gt.py `_labelable()` puts in front of a
human. Structural rows keep deriving from their descendants via labeling_store.derive_structural().
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)

import labeling_store as ls

MODE = "auto_exact"
ANCESTOR_DEPTH = 3
TEXT_KEY_CHARS = 60


def domain_of(filename: str) -> str:
    """Family key. Suite captures embed the page path in the filename, so every
    `owncloud_...files_dir_X` is one family; plain web captures are just their host."""
    stem = re.sub(r"^\d+_", "", filename[:-4] if filename.endswith(".xml") else filename)
    for fam in ("owncloud", "rocketchat", "reddit", "gitlab", "thunderbird", "vscode"):
        if stem.startswith(fam):
            return fam
    return stem


def content_rows(fu: dict) -> list[dict]:
    """label_gt.py's `_labelable()`, kept in one place rather than re-spelled."""
    return [r for r in fu["rows"] if r["tier"] == "content"]


def _cls(row: dict) -> str:
    c = row.get("attr_class") or ""
    return c.split()[0] if c else ""


def match_key(fu: dict, row: dict) -> tuple:
    """What makes two elements 'the same element on another capture of this site'.

    Text alone is far too loose -- an empty-text `section` would match every other one on the site.
    The ancestor shape is what carries the context that role+class+text throws away, and it is the
    reason the measured precision is 99.4% rather than a coin flip on the empty-text rows.
    """
    anc, p, rows = [], row["parent"], fu["rows"]
    while p is not None and len(anc) < ANCESTOR_DEPTH:
        q = rows[p]
        anc.append((q["role"], _cls(q)))
        p = q["parent"]
    return (row["role"], _cls(row), (row["text"] or "")[:TEXT_KEY_CHARS], tuple(anc))


def hand_labels() -> dict[str, dict[str, str]]:
    """Effective hand decisions per file. Machine rows are excluded so a second run can never
    bootstrap off its own output -- the table is always grounded in human judgement alone."""
    out: dict[str, dict[str, str]] = collections.defaultdict(dict)
    for line in open(ls.JOURNAL_PATH, encoding="utf-8"):
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("mode") == MODE:
            continue
        if rec["type"] == "set":
            out[rec["file"]][rec["sig"]] = rec["verdict"]
        elif rec["type"] == "clear":
            out[rec["file"]].pop(rec["sig"], None)
    return out


def transfer_table(universe: dict, labeled: dict, domain: str, exclude: str) -> dict:
    """key -> (verdict, [source files]), keeping only keys every source agrees on. A key judged
    both ways is dropped, not majority-voted: disagreement means the key does not capture what the
    human was actually looking at, which is exactly when a guess would be wrong."""
    votes: dict = collections.defaultdict(lambda: collections.defaultdict(list))
    for f, decisions in labeled.items():
        if f == exclude or f not in universe or domain_of(f) != domain or not decisions:
            continue
        fu = universe[f]
        for row in content_rows(fu):
            v = decisions.get(row["sig"])
            if v:
                votes[match_key(fu, row)][v].append(f)
    return {k: (next(iter(d.items()))) for k, d in votes.items() if len(d) == 1}


def transfer(universe: dict, labeled: dict, target: str) -> tuple[list[dict], int]:
    """Records to append for `target`, plus how many of its content rows were left for the human."""
    fu = universe[target]
    table = transfer_table(universe, labeled, domain_of(target), target)
    already = labeled.get(target, {})
    records, abstained = [], 0
    for row in content_rows(fu):
        if row["sig"] in already:
            continue
        hit = table.get(match_key(fu, row))
        if not hit:
            abstained += 1
            continue
        verdict, sources = hit
        note = "exact-match transfer from " + ",".join(sorted({s[:3] for s in sources}))
        records.append(ls.make_decision(target, row["elem_index"], row["sig"], verdict,
                                        MODE, note=note))
    return records, abstained


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write", action="store_true", help="append to the journal (default: dry run)")
    ap.add_argument("--domain", action="append", help="restrict to these families")
    args = ap.parse_args()

    universe = ls.build_universe()
    labeled = hand_labels()
    domains = {domain_of(f) for f, d in labeled.items() if d and f in universe}

    total_w = total_a = 0
    batch: list[dict] = []
    print(f"{'capture':56} {'filled':>7} {'left':>6} {'cover':>7}")
    for f in sorted(universe, key=lambda x: (ls._numeric_prefix(x), x)):
        if domain_of(f) not in domains or labeled.get(f):
            continue
        if args.domain and domain_of(f) not in args.domain:
            continue
        n = len(content_rows(universe[f]))
        if not n:
            continue
        recs, abst = transfer(universe, labeled, f)
        if not recs:
            continue
        batch += recs
        total_w += len(recs)
        total_a += abst
        print(f"{f[:56]:56} {len(recs):7} {abst:6} {len(recs)/n:6.1%}")
    print(f"\n{'TOTAL':56} {total_w:7} {total_a:6} "
          f"{total_w / (total_w + total_a) if total_w + total_a else 0:6.1%}")
    if args.write and batch:
        ls.append_records(batch)
        print(f"\nappended {len(batch)} records with mode={MODE!r}")
    elif batch:
        print("\ndry run -- pass --write to append")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
