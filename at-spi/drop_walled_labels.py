"""Retire labels sitting on captures that are anti-bot challenge pages, not the requested site.

A label on a walled capture is not merely useless, it is wrong: it asserts a trust verdict about a
CAPTCHA widget while claiming to describe (say) reuters.com. Left in place it pollutes both the
ground truth and the --stats denominator.

Written as `drop` records through labeling_store, never by rewriting the journal, so the decision
stays auditable: `label_gt.py --history <file>` still shows what the verdict was and that it was
retired. Run detect_bot_walls.py first -- and preferably a recovery pass with
`collect_atspi_trees.py --warm_origin` -- so this only ever fires on captures that stayed walled.

    python drop_walled_labels.py            # dry run
    python drop_walled_labels.py --apply
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import labeling_store as ls
from detect_bot_walls import scan


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the drop records (default: dry run)")
    ap.add_argument("--wrong-content", action="store_true",
                    help="also drop labels on captures showing the wrong page (404s, interstitials)")
    ap.add_argument("--max-nodes", type=int, default=400)
    args = ap.parse_args()

    walls, wrong = scan(args.max_nodes)
    targets = walls + (wrong if args.wrong_content else [])
    rows = {r["index"]: r for r in
            json.load(open(os.path.join(_HERE, "manifest.json"), encoding="utf-8"))}
    effective = ls.effective_decisions(ls.read_journal())

    records, total = [], 0
    for index, domain, kinds, _nodes, _url in targets:
        filename = rows[index]["filename"]
        decided = effective.get(filename) or {}
        if not decided:
            continue
        why = "+".join(kinds)
        for sig in decided:
            records.append(ls.make_drop(
                filename, sig, note=f"capture is a bot wall ({why}), not {domain}"))
        total += len(decided)
        print(f"{index:03d} {domain:<32} {why:<28} drop {len(decided):>4} labels")

    print(f"\n{total} labels on {len({r['file'] for r in records})} walled captures")
    if not records:
        return
    if not args.apply:
        print("dry run -- re-run with --apply to write the drop records")
        return
    ls.append_records(records)
    print(f"wrote {len(records)} drop records to the journal")


if __name__ == "__main__":
    main()
