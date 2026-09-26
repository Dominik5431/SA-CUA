#!/usr/bin/env python3
"""
label_gt.py -- hand-labeling UI for at-spi trusted/untrusted ground truth. Serves one page on
localhost; labeling_store.py owns the element universe and the decision journal, this file owns
only the interaction.

Why a browser rather than a terminal loop or a matplotlib window: the judgment being made ("is this
paragraph site chrome or user-generated content?") is a *visual* one that needs the element seen in
the context of the whole rendered page, and it has to work over SSH without an X display. The
screenshot is served once per capture and the boxes are an SVG overlay in the image's own
coordinate space -- so nothing is composited server-side and a keystroke costs one small POST, not
a re-render of a 1920x1080 PNG.

Two views, both over the same capture:
  PAGE (default landing)  Every content-tier element boxed at once, pre-set to trusted. This is the
                          fast path for the many captures that are wholly trusted: look once, press
                          Enter. Click a box (or type its number) to flip just that one first.
  ELEMENT (`e`)           One element at a time for the captures that need real thought, with the
                          full text, role, ancestor path and any previous verdict alongside.

Only content-tier elements are ever shown -- on-screen, sized, with readable text. Unnamed
containers, zero-area and off-screen nodes get derived labels (see labeling_store.derive_structural)
and never cost a keystroke.

THREE STATES TO CYCLE, TWO VERDICTS TO STORE. A row iterates trusted -> untrusted -> <the third
stop> -> trusted, by clicking it in the page view or pressing `space` in the element view; `t`, `u`
and `B` still jump straight to one. Only the first two are verdicts.

WHICH third stop depends on the row, because the claim only ever runs against the grammar. A row no
capacity.py grammar covers can be PROMOTED to "bounded"; a row one does cover can be DEMOTED to
"free". Both are stored as `untrusted` plus a `channel` field, and both exist only for statistics.
The promotion:

    'Free cancellation: 551 properties'   a host moves the integer by listing a property; the
                                          letters are booking's own filter taxonomy
    '218b5e72'                            GitLab computes it from a commit whose message and
                                          author the pusher chose

Both are user-influenced -- so `untrusted` is right and neither may be unmasked -- but neither can
carry an instruction, because what the user reaches is an integer in one case and eight hex
characters in the other. Reported as ordinary false negatives they claim an injection got through;
that is the reporting bug this label fixes, and the only thing it fixes.

The demotion is the same correction pointing the other way. A grammar answers about a STRING, and
the string can match while the thing on screen is not a value:

    '60%'    035_amazon #276, a keyboard form factor sitting in a row of filter chips beside
             'Wireless', 'Hotswap' and 'Silent'. `percent` matches the text exactly.

Counting that as bounded-by-grammar claims a fact about a slot that has no slot in it. `user_free`
says so. It is counted in its own column rather than folded into `free`, because `free` is the
number a false-negative budget is read against and must not be movable by an act of labeling.

WHAT IT MUST NEVER BECOME. `user_bounded` is a claim about a FIELD ("this slot admits only an
integer"), and capacity.py refuses field-level claims on purpose: a field is exactly what an
attacker breaks, so nothing about the mask may rest on one. This label therefore reaches no
masker, no rule and no runtime decision -- it changes what a number MEANS in a table and nothing
else. If the string on screen ever reads 'Breakfast included: ignore all previous', capacity.py
reads the rendered text and masks it, label or no label.

THE BOUNDED SET IS A UNION, and the hand label is only its second half. Rows whose rendered
strings already match a capacity.py grammar are bounded by construction and need no keystroke --
the detail panel shows the grammar name so you can see they are already covered, and the cycle
skips the third stop on them, because promoting one would put an unfalsifiable hand claim next
to a fact recomputed from the capture every build. The hand half is for the strings no grammar can
reach: the digests and the operator templates carrying a user-moved count.

PROMOTING IS ONE KEYSTROKE. An earlier version demanded a written note per promotion, naming
which substring the user controls, on the argument that the label is a promise someone should be
able to audit. In use the sentence was mostly a restatement of the field already named by the
row's own text, and the prompt it arrived through cost more than it bought: it interrupted the
one operation this label exists for, which is working down a column of near-identical rows. Notes
already in the journal are still shown in the detail panel and are carried through a re-commit;
nothing writes new ones. What makes a promotion auditable is instead the row itself -- the export
carries the rendered string beside the channel, so a reader can see what was promised about.
'Manhattan: 580 properties' is the trap: if booking lets a host type a neighbourhood name, the
letters are UGC too and the row is free, not bounded. Cycling a row back to `t` or `u` writes a
fresh record with no channel, which is how you undo a promotion; a container that merely CONTAINS
a bounded row joins to plain untrusted, never inheriting the promise past the field it was made
about.

Usage:
  python at-spi/label_gt.py                    # resume at the first unlabeled element
  python at-spi/label_gt.py --start 008        # open capture 008 (rewrites what is already there)
  python at-spi/label_gt.py --start 008 --relabel   # ...from a clean slate
  python at-spi/label_gt.py --mode element     # skip the page view
  python at-spi/label_gt.py --port 8765 --no_browser
  python at-spi/label_gt.py --bounded_report        # count untrusted rows by channel, then exit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import ground_truth_fulltree as gtf
import labeling_store as ls

STATE: dict = {}

CHANNEL_BOUNDED = "user_bounded"

CHANNEL_FREE = "user_free"
CHANNELS = (CHANNEL_BOUNDED, CHANNEL_FREE)


def bounded_by_grammar(row: dict) -> "str | None":
    """The capacity.py grammar this row is actually exempt under, or None.

    Read-only here. It is what makes the hand label small: the rows this returns a name for are
    already bounded without anyone pressing a key, so `B` only has to cover what no grammar can
    express.

    `capacity_fragment` IS consulted, and an earlier version of this function said in so many words
    that it was not -- on the argument that the fragment bit is about where a row sits while this
    is about what its string could say. That reading is defensible about capacity.py and wrong
    about this function, because of what the answer is used for. Three places consume it: the pill
    in the detail panel, the third stop of the keyboard cycle, and the grammar column of
    bounded_report. All three are claims about a row's EXEMPTION, and the exemption is exactly what
    the fragment gate withdraws -- selector_propose.capacity_verdict() refuses it for a fragment,
    and the live mask refuses it through the same call.

    So the old answer put this file in disagreement with the thing it reports on, in the one
    direction that matters. 016_stackoverflow is the case _mark_capacity_fragments was written for:
    a highlight.js C++ block gives every numeric literal its own node, so `0`, `128` and `32768`
    each match `number` while sharing their line with `rand`, `() %` and `;`. The scorer counts
    those as FREE false negatives. This file called them bounded-by-grammar, painted them teal,
    printed "no keystroke needed", and refused to let anyone say otherwise -- so a leak the scorer
    was counting could not be labelled as one here, and the report under-reported free misses.
    Consulting the bit makes a fragment an ordinary untrusted row: red, and free to be promoted by
    hand if someone judges that particular instance bounded after all.
    """
    return None if row.get("capacity_fragment") else row.get("capacity")


def bounded_effective(row: dict, channel: "str | None") -> "str | None":
    """The grammar carrying this row once a hand demotion is taken into account.

    Kept apart from bounded_by_grammar() rather than folded into it because the two answer
    different questions and both are needed at once. The panel has to know the string still
    matches `percent` -- otherwise the cycle would offer to promote a row someone just demoted,
    and the pill could not say WHICH grammar was overridden. The reporting has to know the row
    no longer counts as covered. So the raw answer goes to the client and this one to the
    counters.
    """
    return None if channel == CHANNEL_FREE else bounded_by_grammar(row)


def _ordered_files() -> list[str]:
    return sorted(STATE["universe"], key=lambda f: (ls._numeric_prefix(f), f))


def _refresh_decisions() -> None:
    """Re-reads the journal if something outside this process appended to it -- e.g. a bulk
    correction from `labeling_store.py --set_text`. Without this the server would keep serving its
    in-memory snapshot, and the next page commit would happily write the pre-correction verdicts
    straight back over the fix.

    apply_decisions() records its own mtime after writing, so the server never re-reads its own
    appends; this only fires for genuinely external edits.
    """
    try:
        mtime = os.path.getmtime(ls.JOURNAL_PATH)
    except OSError:
        mtime = 0.0
    if mtime != STATE.get("journal_mtime"):
        STATE["decisions"] = ls.effective_decisions()
        STATE["journal_mtime"] = mtime


def _corpus_changed() -> list[str]:
    """Capture files whose mtime differs from when this server built its universe.

    A re-capture while the server is running leaves it serving in-memory rows -- roles, bboxes and
    sigs -- computed from the PREVIOUS version of the file. Boxes then land on the wrong pixels,
    and worse, anything labeled in that state is written against sigs that no longer exist, so the
    decisions orphan on the next restart. Cheap to check (one stat per file) and worth surfacing
    loudly rather than letting it corrupt work silently.
    """
    try:
        now = ls._cache_stamp(STATE["xml_dir"])
    except OSError:
        return []
    boot = STATE.get("boot_stamp") or {}
    return sorted(f for f, v in now.items() if boot.get(f) != v)


def _labelable(fu: dict) -> list[dict]:
    return [r for r in fu["rows"] if r["tier"] == "content"]


def _content_parents(fu: dict) -> dict[int, int | None]:
    """{elem_index: nearest CONTENT-tier ancestor}. `row["parent"]` points at the nearest ancestor
    in the whole universe, which is usually a structural wrapper the client never receives -- so
    the client cannot derive containment on its own, and the page view needs it to separate leaves
    from containers."""
    rows = fu["rows"]
    content = {r["elem_index"] for r in rows if r["tier"] == "content"}
    out: dict[int, int | None] = {}
    for r in rows:
        if r["elem_index"] not in content:
            continue
        p = r["parent"]
        while p is not None and p not in content:
            p = rows[p]["parent"]
        out[r["elem_index"]] = p
    return out


def file_payload(filename: str, elem_index: int | None) -> dict:
    """Everything the client needs to draw one capture: its content-tier rows, the decisions
    currently in force, and where this capture sits in the corpus."""
    fu = STATE["universe"][filename]
    decided, orphans = ls.align_decisions(fu, STATE["decisions"].get(filename, {}))
    rows = _labelable(fu)
    cparent = _content_parents(fu)
    _rows = fu["rows"]
    has_content_kid = {p for i, p in cparent.items()
                       if p is not None and _rows[i]["text"]
                       and not _rows[p]["visual_region"]}

    files = STATE["files"]
    pos = files.index(filename)
    labeled = sum(1 for r in rows if r["elem_index"] in decided)
    total_labeled = sum(len(v) for v in STATE["decisions"].values())

    shot_v = 0
    if fu["has_screenshot"]:
        try:
            shot_v = int(os.path.getmtime(os.path.join(ls.SCREENSHOT_DIR, fu["screenshot"])))
        except OSError:
            shot_v = 0

    return {
        "file": filename,
        "screenshot": fu["screenshot"] if fu["has_screenshot"] else None,
        "shot_v": shot_v,
        "corpus_stale": _corpus_changed(),
        "screen": fu["screen"],
        "platform": fu["platform"],
        "category": fu["category"],
        "title": fu["title"],
        "url": fu["url"],
        "roots": fu["roots"],
        "pos": pos,
        "n_files": len(files),
        "prev_file": files[pos - 1] if pos > 0 else None,
        "next_file": files[pos + 1] if pos + 1 < len(files) else None,
        "labeled": labeled,
        "n_rows": len(rows),
        "n_structural": len(fu["rows"]) - len(rows),
        "orphans": len(orphans),
        "corpus_content": STATE["corpus_content"],
        "corpus_labeled": total_labeled,
        "elem": elem_index,
        "srows": [{
            "i": r["elem_index"], "sig": r["sig"], "role": r["role"], "text": r["text"],
            "bbox": r["bbox"], "parent": r["parent"], "cls": r["attr_class"],
            "id": r["attr_id"], "root": r["root_index"], "sym": r.get("symbolic", False),
            "vr": r.get("visual_region", False),
            "nlo": r["skip_reason"] == "not_laid_out",
            "cp": cparent.get(r["elem_index"]), "leaf": True, "st": True,
            "n_desc": len(ls.descendants(fu, r["elem_index"])),
            "verdict": decided.get(r["elem_index"], {}).get("verdict"),
            "mode": decided.get(r["elem_index"], {}).get("mode"),
            "at": decided.get(r["elem_index"], {}).get("at"),
            "ch": decided.get(r["elem_index"], {}).get("channel"),
            "cap": bounded_by_grammar(r),
        } for r in fu["rows"] if r["tier"] == "structural" and r["visible"]],
        "rows": [{
            "i": r["elem_index"], "sig": r["sig"], "role": r["role"], "text": r["text"],
            "bbox": r["bbox"], "parent": r["parent"], "cls": r["attr_class"],
            "sym": r.get("symbolic", False),
            "vr": r.get("visual_region", False),
            "cp": cparent.get(r["elem_index"]),
            "leaf": r["elem_index"] not in has_content_kid,
            "id": r["attr_id"], "root": r["root_index"],
            "n_desc": len(ls.descendants(fu, r["elem_index"])),
            "verdict": decided.get(r["elem_index"], {}).get("verdict"),
            "mode": decided.get(r["elem_index"], {}).get("mode"),
            "at": decided.get(r["elem_index"], {}).get("at"),
            "ch": decided.get(r["elem_index"], {}).get("channel"),
            "cap": bounded_by_grammar(r),
            "note": decided.get(r["elem_index"], {}).get("note"),
        } for r in rows],
    }


def apply_decisions(filename: str, items: list[dict], reset: bool = False) -> None:
    """Writes one commit to the journal and updates the in-memory state to match. `items` carry
    elem_index + verdict + mode (+ origin, note, channel); sigs are resolved here so a stale
    client can never write a decision against an element it thinks exists but doesn't.

    `channel` is attached after make_decision rather than passed through it, which keeps the whole
    sub-label inside this file: labeling_store's record shape and verdict vocabulary are untouched,
    and the extra key survives read_journal -> effective_decisions -> align_decisions because all
    three carry the record dict rather than rebuilding it. Only the two values in CHANNELS are
    accepted, and only on an untrusted row -- a channel note on a trusted row would describe a
    field nobody is masking, and silently dropping it is better than storing a claim no reader
    can act on.
    """
    fu = STATE["universe"][filename]
    by_index = {r["elem_index"]: r for r in fu["rows"]}
    records: list[dict] = []
    if reset:
        records.append(ls.make_reset(filename, note="relabel"))

    for item in items:
        row = by_index.get(int(item["elem_index"]))
        if row is None:
            continue
        rec = ls.make_decision(
            filename, row["elem_index"], row["sig"], item["verdict"],
            item.get("mode", "element"), origin=item.get("origin"), note=item.get("note"))
        if item.get("channel") in CHANNELS and item["verdict"] == "untrusted":
            rec["channel"] = item["channel"]
        records.append(rec)

    ls.append_records(records)
    if reset:
        STATE["decisions"][filename] = {}
    file_state = STATE["decisions"].setdefault(filename, {})
    for rec in records:
        if rec["type"] == "set":
            file_state[rec["sig"]] = rec
    try:
        STATE["journal_mtime"] = os.path.getmtime(ls.JOURNAL_PATH)
    except OSError:
        pass


def subtree_targets(filename: str, elem_index: int) -> list[int]:
    """Content-tier descendants of an element -- what `d` propagates a verdict across. Structural
    descendants are excluded because they take derived labels anyway."""
    fu = STATE["universe"][filename]
    kids = set(ls.descendants(fu, elem_index))
    return [r["elem_index"] for r in _labelable(fu) if r["elem_index"] in kids]


def ancestor_texts(filename: str, elem_index: int) -> list[dict]:
    fu = STATE["universe"][filename]
    rows = fu["rows"]
    chain, cur = [], rows[elem_index]["parent"]
    while cur is not None and len(chain) < 8:
        r = rows[cur]
        chain.append({"i": cur, "role": r["role"], "text": r["text"][:70],
                      "cls": r["attr_class"], "id": r["attr_id"]})
        cur = r["parent"]
    return chain


def resolve_entry(start: str | None, mode: str) -> tuple[str, int | None]:
    """Where the session opens: an explicit --start, else the first capture holding unlabeled
    content, else the first capture in the corpus."""
    if start:
        filename = ls.resolve_file(STATE["universe"], start)
        rows = _labelable(STATE["universe"][filename])
        return filename, (rows[0]["elem_index"] if rows and mode == "element" else None)
    nxt = ls.next_unlabeled(STATE["universe"], STATE["decisions"])
    if nxt:
        return nxt[0], (None if mode == "page" else nxt[1])
    return STATE["files"][0], None


def export() -> dict:
    """Materializes labels/ground_truth_fulltree.json in the schema every other labels/*.json uses,
    so review_benchmark.py --source ground_truth_fulltree works with no changes (it keys off the
    presence of a `bbox` field to pick the full-tree drawing path).

    Every bbox row in the corpus appears exactly once, with `label_mode` recording how its verdict
    was arrived at -- `element`/`page_bulk`/`page_default`/`subtree` for hand decisions, `derived`
    for containers joined over their descendants, `outside_content_root` never appearing here
    because such nodes are not in the universe at all (they are trusted by construction; see
    labeling_store.resolve_content_roots). Off-screen and undecidable rows carry verdict null so a
    consumer can tell "no ground truth here" from "judged trusted".

    THREE FIELDS CARRY THE CHANNEL, kept apart because they are three different claims and a
    scorer wants to be able to tell them apart:

      `capacity_class`  the capacity.py grammar covering every string the row publishes, or null.
                        A property of the STRING, recomputed from the capture every time the
                        universe is built, and true no matter who is asked.
      `channel`         the hand sub-label: "user_bounded", "user_free", or absent. A property of
                        the FIELD, and
                        therefore a human's promise -- see this module's docstring on why it is a
                        statistics label and never a mask input.
      `bounded`         their union, which is the thing a report wants: true when this row's text
                        cannot carry an instruction, whichever of the two establishes it.

    A false negative on a `bounded` row is a rule handing the agent a hash or a count; a false
    negative on a row where `bounded` is false is a rule handing it a sentence. Reporting them in
    one column is what makes the second number unreadable.
    """
    out, counts = [], {"hand": 0, "joined": 0, "derived": 0, "null": 0,
                       "bounded_grammar": 0, "bounded_hand": 0, "free_hand": 0}
    for filename in _ordered_files():
        fu = STATE["universe"][filename]
        decided, _ = ls.align_decisions(fu, STATE["decisions"].get(filename, {}))
        derived = ls.derive_structural(fu, decided)
        for row in fu["rows"]:
            rec = decided.get(row["elem_index"])
            if rec is not None:
                verdict, mode, at = rec["verdict"], rec["mode"], rec["at"]
                counts["hand" if mode in gtf.HAND_MODES else "joined"] += 1
            elif row["elem_index"] in derived:
                verdict, mode, at = derived[row["elem_index"]], "derived", None
                counts["derived"] += 1
            else:
                verdict, mode, at = None, None, None
                counts["null"] += 1
            channel = (rec or {}).get("channel")
            grammar = bounded_effective(row, channel)
            if verdict == "untrusted":
                if grammar:
                    counts["bounded_grammar"] += 1
                elif channel == CHANNEL_BOUNDED:
                    counts["bounded_hand"] += 1
                elif channel == CHANNEL_FREE:
                    counts["free_hand"] += 1
            out.append({
                "file": filename, "elem_index": row["elem_index"], "sig": row["sig"],
                "category": fu["category"], "platform": fu["platform"],
                "role": row["role"], "text": row["text"], "bbox": row["bbox"],
                "region_id": None, "region_role": None, "region_value_shape": None,
                "cross_origin_embed": row["root_index"] > 0,
                "root_kind": row["root_kind"], "root_provenance": row["root_provenance"],
                "tier": row["tier"], "skip_reason": row["skip_reason"], "visible": row["visible"],
                "current_level": "NOT_TRACKED", "current_untrusted": False,
                "verdict": verdict,
                "confidence": None if verdict is None else (
                    "high" if mode in gtf.HAND_MODES else "medium"),
                "label_mode": mode, "labeled_at": at, "mismatch": False,
                "capacity_class": grammar,
                "channel": channel,
                "bounded": bool(grammar) or channel == CHANNEL_BOUNDED,
            })

    os.makedirs(ls.LABELS_DIR, exist_ok=True)
    tmp = ls.EXPORT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, ls.EXPORT_PATH)
    return {"path": ls.EXPORT_PATH, "rows": len(out), **counts}


def bounded_report() -> dict:
    """Every untrusted row split three ways, per capture and in total. -> the totals dict.

    This is the labeling side of the split only: it says how many untrusted rows COULD carry an
    instruction, which is the denominator a false-negative count has to be read against. It says
    nothing about any rule set, because this file knows nothing about any rule set -- turning the
    split into a leak / exempt pair of columns is one read of the export's `bounded` field
    wherever the scoring happens.

    `hand` is listed apart from `grammar` rather than added to it because the two decay
    differently: a grammar verdict is recomputed from the capture every build and cannot go stale,
    while a hand promotion is a claim someone made about a field and stays exactly as true as the
    day it was made. Anyone auditing the number wants to know how much of it is the second kind.

    `freehand` is the fourth column and the opposite claim: a row a grammar covers that a labeler
    demoted anyway, because the string matches while the instance is not a value. It is kept out of
    `free` deliberately. `free` is the number a false-negative budget is read against, and it must
    not be movable by an act of labeling -- otherwise the one figure that has to stay at zero could
    be pushed off zero without any rule changing. Fold the two together at the point of reading if
    that is what you want; the export carries both.
    """
    tot = {"untrusted": 0, "free": 0, "grammar": 0, "hand": 0, "free_hand": 0,
           "captures": 0, "with_hand": 0}
    per_capture = []
    for filename in _ordered_files():
        fu = STATE["universe"][filename]
        decided, _ = ls.align_decisions(fu, STATE["decisions"].get(filename, {}))
        derived = ls.derive_structural(fu, decided)
        c = {"untrusted": 0, "free": 0, "grammar": 0, "hand": 0, "free_hand": 0}
        for row in fu["rows"]:
            rec = decided.get(row["elem_index"])
            verdict = rec["verdict"] if rec is not None else derived.get(row["elem_index"])
            if verdict != "untrusted":
                continue
            c["untrusted"] += 1
            channel = (rec or {}).get("channel")
            if bounded_effective(row, channel):
                c["grammar"] += 1
            elif channel == CHANNEL_BOUNDED:
                c["hand"] += 1
            elif channel == CHANNEL_FREE:
                c["free_hand"] += 1
            else:
                c["free"] += 1
        if not c["untrusted"]:
            continue
        per_capture.append((filename, c))
        for key in c:
            tot[key] += c[key]
        tot["captures"] += 1
        tot["with_hand"] += 1 if c["hand"] else 0

    width = max([len(f) for f, _ in per_capture] + [7])
    print(f"{'capture':{width}s} {'untrust':>8s} {'free':>7s} {'grammar':>8s} {'hand':>6s}"
          f" {'freehand':>9s}")
    print("-" * (width + 43))
    for filename, c in per_capture:
        if c["hand"] or c["free_hand"]:
            print(f"{filename:{width}s} {c['untrusted']:8d} {c['free']:7d} "
                  f"{c['grammar']:8d} {c['hand']:6d} {c['free_hand']:9d}")
    print("-" * (width + 43))
    print(f"{'TOTAL':{width}s} {tot['untrusted']:8d} {tot['free']:7d} "
          f"{tot['grammar']:8d} {tot['hand']:6d} {tot['free_hand']:9d}")
    print(f"\n{tot['captures']} captures carry untrusted rows; {tot['with_hand']} carry a hand "
          f"promotion; {tot['free_hand']} row(s) were demoted out of a grammar by hand.")
    if tot["untrusted"]:
        print(f"{100 * (tot['grammar'] + tot['hand']) / tot['untrusted']:.1f}% of untrusted rows "
              f"cannot carry an instruction "
              f"({tot['grammar']} by grammar, {tot['hand']} by hand).")
    return tot


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(json.dumps(payload).encode("utf-8"), "application/json", code)

    def do_GET(self) -> None:
        _refresh_decisions()
        parsed = urlparse(self.path)
        path, q = parsed.path, parse_qs(parsed.query)

        if path == "/":
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path.startswith("/shot/"):
            name = os.path.basename(path[len("/shot/"):])
            fp = os.path.join(ls.SCREENSHOT_DIR, name)
            if not os.path.exists(fp):
                self._send(b"no screenshot", "text/plain", 404)
                return
            with open(fp, "rb") as f:
                self._send(f.read(), "image/png", cache=True)
        elif path == "/api/state":
            token = q.get("file", [None])[0]
            if token and token in STATE["universe"]:
                filename = token
            elif token:
                try:
                    filename = ls.resolve_file(STATE["universe"], token)
                except SystemExit as e:
                    self._json({"error": str(e)}, 404)
                    return
            else:
                filename = STATE["entry"][0]
            elem = q.get("elem", [None])[0]
            self._json(file_payload(filename, int(elem) if elem not in (None, "") else None))
        elif path == "/api/boot":
            f, e = STATE["entry"]
            self._json({"file": f, "elem": e, "mode": STATE["mode"]})
        elif path == "/api/next_unlabeled":
            nxt = ls.next_unlabeled(STATE["universe"], STATE["decisions"],
                                    start_file=q.get("after", [None])[0])
            self._json({"file": nxt[0], "elem": nxt[1]} if nxt else {"file": None, "elem": None})
        elif path == "/api/context":
            filename = q.get("file", [""])[0]
            elem = int(q.get("elem", ["0"])[0])
            self._json({"ancestors": ancestor_texts(filename, elem),
                        "subtree": subtree_targets(filename, elem)})
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        path = urlparse(self.path).path

        if path == "/api/decide":
            filename = body["file"]
            apply_decisions(filename, body.get("items", []), reset=bool(body.get("reset")))
            self._json(file_payload(filename, body.get("elem")))
        elif path == "/api/export":
            self._json(export())
        else:
            self._json({"error": "not found"}, 404)


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>at-spi ground truth</title>
<style>
  * { box-sizing:border-box; }
  body { margin:0; font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;
         background:#14161a; color:#dfe3e8; display:flex; height:100vh; overflow:hidden; }
  #stage { flex:1; overflow:auto; position:relative; background:#0b0c0e; }
  #wrap { position:relative; }
  img { display:block; width:100%; }
  svg { position:absolute; inset:0; width:100%; height:100%; }
  rect.box { cursor:pointer; }
  #side { width:400px; flex:none; background:#1b1e24; border-left:1px solid #2b3038;
          padding:14px; overflow:auto; display:flex; flex-direction:column; }
  #list { flex:1; min-height:120px; overflow:auto; margin-top:10px;
          border-top:1px solid #2b3038; }
  #list .row { display:flex; gap:6px; padding:2px 4px; cursor:pointer; border-radius:3px;
               white-space:nowrap; overflow:hidden; align-items:baseline; }
  #list .row:hover, #list .row.hot { background:#2b3038; }
  #list .row.cur { background:#3a4252; }
  #list .n { color:#7f8794; width:44px; flex:none; text-align:right; }
  #list .chip { width:7px; height:7px; border-radius:2px; flex:none; align-self:center; }
  #list .rl { color:#7f8794; width:78px; flex:none; overflow:hidden; text-overflow:ellipsis; }
  #list .tx { overflow:hidden; text-overflow:ellipsis; }
  #tip { position:fixed; pointer-events:none; background:#0b0c0e; border:1px solid #444c58;
         padding:4px 8px; font-size:11.5px; max-width:420px; z-index:10; display:none;
         white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  h2 { font-size:13px; margin:0 0 8px; color:#fff; word-break:break-all; }
  .k { color:#7f8794; }
  .pill { display:inline-block; padding:1px 7px; border-radius:9px; font-size:11px; }
  .tr { background:#1d4b31; color:#7fe0a5; } .un { background:#4d2320; color:#ffa79c; }
  .none { background:#2b3038; color:#98a0ac; }
  .bd { background:#123c45; color:#8fd4e2; }
  table { width:100%; border-collapse:collapse; margin-top:6px; }
  td { padding:2px 0; vertical-align:top; } td.k { width:80px; }
  .txt { background:#0f1114; border:1px solid #2b3038; padding:8px; margin-top:8px;
         max-height:190px; overflow:auto; white-space:pre-wrap; word-break:break-word; }
  .armed { border-color:#ffd54a; color:#ffd54a; }
  .sym { border-color:#5a6b8c; color:#a9bad8; }
  #keys { margin-top:14px; border-top:1px solid #2b3038; padding-top:10px;
          color:#98a0ac; font-size:11.5px; }
  #keys b { color:#dfe3e8; font-weight:600; }
  #toast { position:fixed; left:50%; top:14px; transform:translateX(-50%); background:#2b3038;
           padding:7px 16px; border-radius:5px; opacity:0; transition:opacity .18s; z-index:9; }
  #toast.on { opacity:1; }
  .bar { height:4px; background:#2b3038; margin-top:6px; }
  .bar > i { display:block; height:100%; background:#3d8b5f; }
  a { color:#7fb0e0; }
</style></head><body>
<div id="stage"><div id="wrap"><img id="shot"><svg id="ov" preserveAspectRatio="none"></svg></div></div>
<div id="side">
  <h2 id="fname">loading…</h2>
  <div id="meta"></div>
  <div class="bar"><i id="prog"></i></div>
  <div id="detail"></div>
  <div id="list"></div>
  <div id="keys"></div>
</div>
<div id="toast"></div><div id="tip"></div>
<script>
const S = { file:null, elem:null, mode:"page", data:null, draft:{}, armed:false,
            digits:"", zoom:"fit", filter:"leaf", hover:null,
            sel:new Set(), anchor:null, drag:null };

// Which boxes are drawn. A capture's content rows nest: on 008, 230 boxes are 153 leaves inside
// 77 containers, and a median point on the page has 4 of them stacked. Drawing all of it at once
// is unreadable and makes clicking a lie -- you hit whichever box happens to be on top. "leaf"
// (default) draws only rows with no content-tier row beneath them, which is the layer that
// actually corresponds to something you can point at.
// "structural" reveals the unnamed containers that normally take derived labels. It exists for
// content the accessibility tree gives no element of its own -- most importantly images: Chrome
// omits presentational/CSS-background images entirely, so a product photo an attacker fully
// controls is reachable only through the unnamed <div> sitting on its exact bounds. Structural
// rows are labeled as deliberate exceptions (each click writes immediately, no page default), so
// revealing them never bulk-asserts anything about the thousands of containers you did not touch.
// "excluded" shows only the rows the DOM curation pass removed from labeling: boxes the
// accessibility tree announces at plausible coordinates for elements that were never laid out
// (closed modals, above all). They are drawn so the exclusion can be checked against the
// screenshot -- every one of them should sit on pixels that belong to something else.
const FILTERS = ["leaf", "container", "all", "structural", "excluded"];
function shown(){
  if(!S.data) return [];
  if(S.filter==="excluded") return (S.data.srows || []).filter(r=>r.nlo);
  if(S.filter==="structural") return (S.data.srows || []).filter(r=>!r.nlo);
  const rows = S.data.rows;
  if(S.filter==="leaf") return rows.filter(r=>r.leaf);
  if(S.filter==="container") return rows.filter(r=>!r.leaf);
  return rows;
}
const $ = s => document.querySelector(s);

const esc = s => (s??"").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function toast(m){ const t=$("#toast"); t.textContent=m; t.classList.add("on");
  clearTimeout(t._h); t._h=setTimeout(()=>t.classList.remove("on"), 1400); }

async function api(p, opt){ const r = await fetch(p, opt); return r.json(); }

async function load(file, elem, keepDraft){
  const q = new URLSearchParams({file}); if(elem!=null) q.set("elem", elem);
  const d = await api("/api/state?"+q);
  if(d.error){ toast(d.error); return; }
  S.data = d; S.file = d.file; S.elem = elem; S.digits = ""; S.armed = false; S.hover = null;
  $("#tip").style.display = "none";
  if(S.mode==="element" && elem!=null && !shown().some(r=>r.i===elem))
    S.elem = (shown()[0]||{}).i ?? elem;
  if(!keepDraft){
    // Page view opens with every element proposed TRUSTED, except ones already decided --
    // that is the whole point of the fast path: the common capture is wholly trusted and
    // committing it should be one keystroke, not N.
    S.draft = {}; for(const r of S.data.rows) S.draft[r.i] = draftValue(r);
  }
  render();
}

// THREE STATES IN THE DRAFT, TWO IN THE VERDICT. The draft (and only the draft) can hold
// "bounded", which is not a verdict -- it is "untrusted, and a user reaches only a bounded part
// of this string". verdictOf() collapses it back to "untrusted" so every consumer that compares
// against a verdict keeps working unchanged, and effectiveDraft() is the one place that turns it
// into verdict+channel on the way to the journal. Nothing downstream of that ever sees a third
// verdict; see this module's docstring on why the label stays out of the verdict.
function draftValue(r){
  if(r.ch === "user_bounded") return "bounded";
  if(r.ch === "user_free")    return "free";
  return r.verdict || "trusted";
}

// Structural rows have no draft -- they are never part of a page default, so what is stored IS
// their state, in both views.
function stateOf(r){
  if(S.mode === "page" && !r.st) return S.draft[r.i];
  if(r.ch === "user_bounded") return "bounded";
  if(r.ch === "user_free")    return "free";
  return r.verdict;
}
// Both sub-labels collapse to the same verdict. "free" is not a weaker untrusted, it is the
// ordinary one -- what the label says is that a grammar covering the text does not cover this
// instance, which leaves the row exactly where an unmatched row already is.
function verdictOf(r){ const v = stateOf(r);
  return (v === "bounded" || v === "free") ? "untrusted" : v; }

// trusted -> untrusted -> bounded -> trusted. A row a capacity.py grammar already covers skips
// the third stop: promoting it would put an unfalsifiable hand claim next to a fact recomputed
// from the capture every build, so those rows cycle between two states only.
function nextState(r){
  const v = stateOf(r);
  if(v === "trusted") return "untrusted";
  // The third stop is whichever claim the row does not already have. A row no grammar covers
  // can only be promoted; a row a grammar DOES cover can only be demoted, and that stop is
  // the one 035_amazon #276 needed -- "60%" as a keyboard form factor, which `percent`
  // matches and which is not a value. Cycling past it returns to trusted either way.
  if(v === "untrusted") return r.cap ? "free" : "bounded";
  if(v === "free")      return "trusted";
  return "trusted";
}

// paint() = overlay + list highlight only. Hover must never go through render(), which rebuilds
// the detail panel and fires the ancestor fetch -- that would be one request per mouse move.
// paint() restyles the EXISTING overlay nodes. It must never rebuild them: replacing innerHTML
// under the cursor re-fires mouseenter on the new node (endless repaint) and breaks clicking
// outright, because mousedown and mouseup then land on different elements and no click event is
// ever synthesised. Build once in drawOverlay(), restyle here.
function paint(){ styleOverlay(); syncList(); }

// Is this row's text incapable of carrying an instruction -- by grammar, or by hand? Only ever
// asked about untrusted rows: on a trusted one there is no masking decision for it to qualify.
function isBounded(r){
  if(stateOf(r) === "free") return false;   // demoted by hand: the grammar is overridden
  return stateOf(r) === "bounded" || (verdictOf(r) === "untrusted" && !!r.cap);
}

// Teal, not a shade of the untrusted red: a bounded row IS untrusted and the mask treats it
// exactly like every other untrusted row, so making it look like a weaker red would suggest a
// gradation in the verdict that does not exist. It is a different axis, so it gets a different hue.
const C_BOUND = "#1f7a8c";

function colOf(r){
  if(r.nlo) return "#e0670b";   // DOM says nothing was laid out here -- not a verdict, a removal
  const v = verdictOf(r);
  if(isBounded(r)) return C_BOUND;
  return v==="untrusted" ? "#c0392b" : v==="trusted" ? "#228b45" : (r.st ? "#8e6bd0" : "#8892a0");
}

function chipOf(r){
  const v = verdictOf(r);
  if(isBounded(r)) return C_BOUND;
  return v==="untrusted" ? "#c0392b" : v==="trusted" ? "#228b45" : "#4a5260";
}

function drawOverlay(){
  const d = S.data; if(!d) return;
  const src = d.screenshot ? "/shot/"+d.screenshot+"?v="+(d.shot_v||0) : "";
  if($("#shot").getAttribute("src") !== src) $("#shot").src = src;
  const wrap = $("#wrap"), ov = $("#ov");
  wrap.style.width = S.zoom==="fit" ? "100%" : d.screen[0]+"px";
  ov.setAttribute("viewBox", `0 0 ${d.screen[0]} ${d.screen[1]}`);

  // Big boxes first so small nested ones stay on top and win the hit test.
  const rows = shown().sort((a,b)=> b.bbox[2]*b.bbox[3] - a.bbox[2]*a.bbox[3]);
  const parts = [];
  rows.forEach(r => {
    const [x,y,w,h] = r.bbox;
    parts.push(`<rect class="box" data-i="${r.i}" pointer-events="all"
      x="${x}" y="${y}" width="${w}" height="${h}"></rect>`);
    // A box under ~6px on either side cannot be seen OR clicked: booking.com collapses each of
    // its popular-filter checkboxes to 2x1 ("Superb: 9+", "Free WiFi") with the styled 233x40
    // label drawn beside it, and at fit zoom a 2x1 rect is sub-pixel. The ring already existed as
    // a marker for small boxes; for these it becomes the hit target too, since the rect itself is
    // unclickable. 360 content rows corpus-wide, 1.7%.
    const tiny = w < 6 || h < 6;
    if(r.sym || w < 24 || h < 24)
      parts.push(`<circle class="ring" data-i="${r.i}" data-tiny="${tiny?1:0}"
        pointer-events="${tiny ? "all" : "none"}"
        cx="${x+w/2}" cy="${y+h/2}" r="${Math.max(w,h)/2 + 9}" fill="none"></circle>`);
  });
  // One reusable label and one selection rectangle, repositioned rather than recreated.
  parts.push(`<text id="hotlab" pointer-events="none" font-size="14" fill="#fff" stroke="#000"
     stroke-width="3" paint-order="stroke" style="display:none"></text>`);
  parts.push(`<rect id="band" pointer-events="none" fill="#38bdf8" fill-opacity=".18"
     stroke="#38bdf8" stroke-width="2" style="display:none"></rect>`);
  ov.innerHTML = parts.join("");
  styleOverlay();
}

function styleOverlay(){
  const ov = $("#ov"); if(!ov || !S.data) return;
  const byIdx = {}; for(const r of shown()) byIdx[r.i] = r;
  ov.querySelectorAll("rect.box").forEach(el => {
    const r = byIdx[+el.dataset.i]; if(!r) return;
    const v = verdictOf(r), col = colOf(r);
    const cur = S.mode==="element" && r.i===S.elem;
    const hot = r.i===S.hover, sel = S.sel.has(r.i);
    const fill = sel ? .30 : (v==="untrusted" || cur || hot) ? (hot ? .34 : cur ? .30 : .22) : 0;
    const dim  = (S.hover!=null && !hot && !sel) ? .35 : 1;
    el.setAttribute("fill", sel ? "#38bdf8" : col);
    el.setAttribute("fill-opacity", fill);
    el.setAttribute("stroke", sel ? "#38bdf8" : cur ? "#ffd54a" : hot ? "#ffffff" : col);
    el.setAttribute("stroke-width", sel ? 3 : cur ? 4 : hot ? 3 : 1.5);
    el.setAttribute("stroke-opacity", dim);
    if(r.mode && r.mode!=="element" && S.mode==="element") el.setAttribute("stroke-dasharray","7 5");
    else el.removeAttribute("stroke-dasharray");
  });
  ov.querySelectorAll("circle.ring").forEach(el => {
    const r = byIdx[+el.dataset.i]; if(!r) return;
    const cur = S.mode==="element" && r.i===S.elem, hot = r.i===S.hover;
    const tiny = el.dataset.tiny === "1";
    el.setAttribute("stroke", cur ? "#ffd54a" : hot ? "#fff" : colOf(r));
    el.setAttribute("stroke-width", (cur||hot) ? 3 : tiny ? 2 : 1.5);
    // A tiny box has no visible rect at all, so its ring is the only thing on screen for it.
    el.setAttribute("stroke-opacity", (cur||hot) ? 1 : tiny ? .95 : .6);
  });
  const lab = ov.querySelector("#hotlab"), hr = byIdx[S.hover];
  if(hr){ lab.setAttribute("x", hr.bbox[0]+3); lab.setAttribute("y", hr.bbox[1]+15);
          lab.textContent = hr.i; lab.style.display = ""; }
  else if(lab) lab.style.display = "none";
}

// ---- region selection -------------------------------------------------------------------
// Marking a Wikipedia article body one paragraph at a time is the pathological case for
// element-at-a-time labeling: dozens of sibling nodes, one obvious verdict. Two ways to take
// them in bulk, both feeding the same selection set, committed with t/u as mode "region":
//   drag on the screenshot  -- a rubber band; every box FULLY inside it is selected (hold Alt to
//                              take anything it merely touches). Full containment by default so
//                              a page-spanning wrapper is not swept in by a small drag.
//   shift-click in the list -- selects the contiguous run between the last clicked row and this
//                              one, in document order, which is exactly reading order.
function svgPt(ev){
  const b = $("#ov").getBoundingClientRect(), d = S.data.screen;
  return [(ev.clientX - b.left) * d[0] / b.width, (ev.clientY - b.top) * d[1] / b.height];
}
function inBand(r, band, touch){
  const [x,y,w,h] = r.bbox, [bx,by,bw,bh] = band;
  return touch ? (x < bx+bw && x+w > bx && y < by+bh && y+h > by)
               : (x >= bx && y >= by && x+w <= bx+bw && y+h <= by+bh);
}
function setSel(idxs){ S.sel = new Set(idxs); paint(); renderPanel(); }
function clearSel(){ if(S.sel.size){ S.sel.clear(); paint(); renderPanel(); } }

function initOverlay(){
  const ov = $("#ov");
  ov.addEventListener("mousedown", ev => {
    if(ev.button!==0) return;
    S.drag = {from: svgPt(ev), moved:false, touch: ev.altKey};
  });
  ov.addEventListener("mousemove", ev => {
    if(!S.drag){ return; }
    const p = svgPt(ev), f = S.drag.from;
    if(!S.drag.moved && Math.hypot(p[0]-f[0], p[1]-f[1]) < 5) return;   // still a click
    S.drag.moved = true;
    const band = [Math.min(f[0],p[0]), Math.min(f[1],p[1]),
                  Math.abs(p[0]-f[0]), Math.abs(p[1]-f[1])];
    const b = $("#band");
    b.setAttribute("x",band[0]); b.setAttribute("y",band[1]);
    b.setAttribute("width",band[2]); b.setAttribute("height",band[3]);
    b.style.display = "";
    setSel(shown().filter(r=>inBand(r, band, S.drag.touch)).map(r=>r.i));
  });
  window.addEventListener("mouseup", ev => {
    if(!S.drag) return;
    const wasDrag = S.drag.moved; S.drag = null;
    $("#band").style.display = "none";
    if(wasDrag){ toast(S.sel.size + " selected — t/u to label, Esc to clear"); }
  });
  // Hover + click by delegation, so the handlers survive every restyle.
  ov.addEventListener("mouseover", ev => {
    const el = ev.target.closest && ev.target.closest('rect.box, circle.ring[data-tiny="1"]'); if(!el) return;
    S.hover = +el.dataset.i; showTip(ev, S.hover); paint();
  });
  ov.addEventListener("mousemove", ev => { if(S.hover!=null && !S.drag) moveTip(ev); });
  ov.addEventListener("mouseout", ev => {
    const el = ev.target.closest && ev.target.closest('rect.box, circle.ring[data-tiny="1"]'); if(!el) return;
    S.hover = null; $("#tip").style.display="none"; paint();
  });
  ov.addEventListener("click", ev => {
    const el = ev.target.closest && ev.target.closest('rect.box, circle.ring[data-tiny="1"]'); if(!el) return;
    if(S.drag && S.drag.moved) return;
    ev.stopPropagation(); pick(+el.dataset.i);
  });
}

async function pick(i){
  const r = (shown().find(x=>x.i===i)) || {};
  // An excluded box is not a thing to judge -- the point of the view is to CHECK the exclusion
  // against the screenshot, and a verdict recorded here would be a verdict about pixels that
  // belong to some other element.
  if(r.nlo){ toast(`#${i} excluded — nothing was laid out here (dom_curation)`); return; }
  if(S.mode!=="page"){ S.elem = i; render(); scrollToElem(); return; }
  if(r.st){
    // A structural row has no page default to fall back on -- Enter never writes it, and leaving
    // it alone means "let derivation decide". So a click on one is the whole decision and is
    // committed on the spot; clicking again clears back to derived.
    const next = r.verdict === "untrusted" ? "trusted" : "untrusted";
    await commit([{elem_index:i, verdict:next, mode:"element"}]);
    toast(`#${i} ${next} (structural)`);
    return;
  }
  S.draft[i] = nextState(r); paint(); renderPage();
}

function showTip(ev, i){
  const r = S.data.rows.find(x=>x.i===i); if(!r) return;
  const t = $("#tip");
  t.innerHTML = `<b>#${r.i}</b> ${esc(r.role)}${r.leaf?"":" <span class='k'>(container)</span>"} — `
    + (r.text ? esc(r.text.slice(0,120)) : `<span class="k">${r.sym?"no text":"unnamed"}</span>`);
  t.style.display = "block"; moveTip(ev);
}
function moveTip(ev){
  const t = $("#tip");
  t.style.left = Math.min(ev.clientX+14, window.innerWidth-t.offsetWidth-8)+"px";
  t.style.top  = (ev.clientY+18)+"px";
}

function syncList(){
  document.querySelectorAll("#list .row").forEach(el => {
    const i = +el.dataset.i;
    el.classList.toggle("hot", i===S.hover);
    el.classList.toggle("cur", S.mode==="element" && i===S.elem);
    el.querySelector(".chip").style.background =
      chipOf(S.data.rows.find(x=>x.i===i) || {});
  });
}

// Every box also gets a row here, so a 6x16 icon is always reachable even when it is impossible
// to hit on the screenshot. Hovering a row highlights its box on the page and vice versa.
function renderList(){
  const rows = shown();
  $("#list").innerHTML = rows.map(r => {
    const col = chipOf(r);
    const txt = r.text ? esc(r.text.slice(0,80))
               : `<span class="k">&lt;${r.vr ? "visual region " + r.bbox[2] + "\u00d7" + r.bbox[3]
                                             : (r.sym ? "no text" : "unnamed")}&gt;</span>`;
    return `<div class="row" data-i="${r.i}"><span class="n">${r.i}</span>
      <span class="chip" style="background:${col}"></span>
      <span class="rl">${esc(r.role)}</span><span class="tx">${txt}</span></div>`;
  }).join("");
  document.querySelectorAll("#list .row").forEach(el => {
    const i = +el.dataset.i;
    el.onclick = (ev) => {
      if(ev.shiftKey && S.anchor!=null){
        const order = shown().map(r=>r.i);
        const a = order.indexOf(S.anchor), b = order.indexOf(i);
        if(a>=0 && b>=0){ const [lo,hi] = a<b ? [a,b] : [b,a];
          setSel(order.slice(lo, hi+1)); toast(S.sel.size+" selected"); return; }
      }
      S.anchor = i; clearSel(); pick(i);
    };
    el.onmouseenter = () => { S.hover = i; paint(); };
    el.onmouseleave = () => { S.hover = null; paint(); };
  });
  syncList();
}

function render(){
  const d = S.data; if(!d) return;
  drawOverlay();
  renderList();
  $("#fname").textContent = d.file;
  const pct = d.n_rows ? Math.round(100*d.labeled/d.n_rows) : 100;
  $("#prog").style.width = pct+"%";
  const cpct = d.corpus_content ? (100*d.corpus_labeled/d.corpus_content).toFixed(1) : "0";
  $("#meta").innerHTML = `<table>
    <tr><td class="k">capture</td><td>${d.pos+1} / ${d.n_files} &nbsp; ${esc(d.platform)}${d.category?" · "+esc(d.category):""}</td></tr>
    <tr><td class="k">corpus</td><td>${d.corpus_labeled} / ${d.corpus_content} <span class="k">(${cpct}%)</span></td></tr>
    <tr><td class="k">roots</td><td>${d.roots.length} ${d.roots.length>1?'<span class="pill none">multi-root</span>':""}</td></tr>
    <tr><td class="k">elements</td><td>${d.labeled} / ${d.n_rows} labeled &nbsp;<span class="k">(+${d.n_structural} derived)</span></td></tr>
    <tr><td class="k">showing</td><td><b>${shown().length}</b>
        &nbsp;<span class="pill none">${S.filter}</span>
        <span class="k">${S.filter==="excluded" ? "removed by DOM curation — not labeled, not scored"
          : S.filter==="structural" ? (d.srows||[]).filter(r=>!r.nlo).length + " on-screen structural"
          : d.n_rows + " content · " + (d.n_rows - d.rows.filter(r=>r.leaf).length) + " containers"}</span></td></tr>
    ${(d.corpus_stale||[]).length?`<tr><td class="k">STALE</td><td><span class="pill un">
        ${d.corpus_stale.length} capture(s) changed on disk</span>
        <span class="k">restart the server — boxes and sigs are from the old files</span></td></tr>`:""}
    ${d.orphans?`<tr><td class="k">orphans</td><td><span class="pill un">${d.orphans}</span> stored decisions no longer match</td></tr>`:""}
    </table>`;
  S.mode==="page" ? renderPage() : renderElem();
  $("#keys").innerHTML = (S.mode==="page"
    ? `<b>click</b> box or row cycles trusted→untrusted→bounded · <b>digits+↵</b> pick by number
       · <b>↵</b> commit page · <b>T</b>/<b>U</b> all trusted/untrusted · <b>e</b> element view`
    : `<b>t</b>/<b>u</b>/<b>B</b> trusted/untrusted/bounded · <b>space</b> cycles the three
       · <b>T</b>/<b>U</b> rest of page · <b>d</b> then t/u = subtree · <b>s</b> skip
       · <b>b</b> back · <b>a</b> page view`)
    + ` · <b>drag</b> region select · <b>shift-click</b> list range`
    + ` · <b>c</b> show ${FILTERS[(FILTERS.indexOf(S.filter)+1)%FILTERS.length]} · <b>←</b>/<b>→</b> capture (no commit)
       · <b>g</b> goto · <b>z</b> zoom (${S.zoom}) · <b>R</b> relabel · <b>x</b> export`;
}

function renderPanel(){ S.mode==="page" ? renderPage() : renderElem(); }

function renderPage(){
  if(S.filter==="excluded"){
    const ex = shown();
    $("#detail").innerHTML = `<div class="txt sym"><b>Excluded by DOM curation</b> —
      <b>${ex.length}</b> box(es) on this capture. The accessibility tree announces these at
      plausible coordinates, but the DOM says no element was ever laid out there and nothing is
      laid out inside them either — closed modal mounts, above all. There is nothing on screen
      to judge, so they are dropped from labeling and from scoring.<br><br>
      Check them against the screenshot: every orange box should sit on pixels that belong to
      something else. If one is real, that is a curation bug — say so rather than labeling it.
      <b>↵</b> and <b>T</b>/<b>U</b> are disabled here.</div>`;
    return;
  }
  if(S.filter==="structural"){
    const sr = shown(), set = sr.filter(r=>r.verdict);
    $("#detail").innerHTML = `<div class="txt sym"><b>Structural pass</b> — ${sr.length} on-screen
      containers the tree gives no content element for. Use this to reach things with no node of
      their own, above all <b>images</b>: Chrome drops presentational/CSS-background images
      entirely, so a product photo is reachable only as the unnamed box on its bounds.<br><br>
      <b>${set.length}</b> explicitly labeled here. Each click writes immediately; click again to
      clear back to derived. <b>↵</b> and <b>T</b>/<b>U</b> are disabled — press <b>c</b> to go
      back to the content pass.</div>`;
    return;
  }
  const n = S.data.rows.filter(r=>S.draft[r.i]==="untrusted").length;
  const nb = S.data.rows.filter(r=>S.draft[r.i]==="bounded").length;
  const nf = S.data.rows.filter(r=>S.draft[r.i]==="free").length;
  const hidden = S.data.n_rows - shown().length;
  if(S.sel.size){
    $("#detail").innerHTML = `<div class="txt" style="border-color:#38bdf8;color:#9fd8f5">
      <b>${S.sel.size}</b> element(s) selected.<br><b>t</b>/<b>u</b> labels all of them
      (recorded as <b>region</b>), <b>Esc</b> clears. Drag again to reselect;
      hold <b>Alt</b> while dragging to take boxes the band merely touches.</div>`;
    return;
  }
  $("#detail").innerHTML = `<div class="txt">Page view — every element proposed
    <span class="pill tr">trusted</span>. <b>${n}</b> flipped to untrusted${
      nb?`, <b>${nb}</b> to <span class="pill bd">bounded</span>`:""}${
      nf?`, <b>${nf}</b> freed from a grammar`:""}.
    ${hidden ? `<br><br><span class="k">${hidden} nested/${S.filter==="leaf"?"container":"leaf"}
      row(s) hidden. On commit they take the join of what is inside them — untrusted if any
      element they contain is, trusted otherwise — recorded as <b>container_join</b>, which is
      inference and is excluded from scoring. Press <b>c</b> to show and judge them yourself.
      </span>`:""}
    <br><br>Press <b>↵</b> to commit as shown.</div>`;
}

// What actually gets written on commit, including for rows the current filter hides. A hidden
// container cannot simply take the page default: a wrapper around a flipped-untrusted comment is
// itself tainted (the Biba join), and silently recording it as trusted would put a wrong label in
// the ground truth for an element nobody was shown. Those rows are marked container_join so they
// are never mistaken for a human judgment.
function effectiveDraft(){
  const rows = S.data.rows, sh = new Set(shown().map(r=>r.i)), kids = {}, memo = {};
  rows.forEach(r => { if(r.cp!=null) (kids[r.cp] = kids[r.cp] || []).push(r.i); });
  function join(i){
    if(memo[i] !== undefined) return memo[i];
    memo[i] = S.draft[i];                     // guards against a cycle in malformed parentage
    if(sh.has(i)) return memo[i] = S.draft[i];
    const ks = (kids[i] || []).map(join).filter(Boolean);
    // A hidden container wrapping a bounded child joins to plain untrusted, never to bounded:
    // the promise was about the child's string, and the wrapper publishes the child's text plus
    // whatever else it contains. Inheriting the promise upward is exactly how a field claim
    // would grow past the field it was made about.
    return memo[i] = ks.some(k => k === "untrusted" || k === "bounded" || k === "free")
                   ? "untrusted"
                   : (ks.length ? "trusted" : S.draft[i]);
  }
  return rows.map(r => {
    const v = join(r.i);
    const was = draftValue(r);
    const item = { elem_index: r.i,
                   verdict: (v === "bounded" || v === "free") ? "untrusted" : v,
                   mode: !sh.has(r.i) ? "container_join"
                       : (v === was ? "page_default" : "page_bulk") };
    if(v === "bounded") item.channel = "user_bounded";
    if(v === "free")    item.channel = "user_free";
    return item;
  });
}

function renderElem(){
  const r = S.data.rows.find(x=>x.i===S.elem); if(!r){ $("#detail").innerHTML=""; return; }
  const v = r.verdict;
  // The channel line, and why it says which of the four cases carries the row. A row `cap`
  // covers needs no keystroke, so showing the grammar name is what stops B being pressed on
  // 2,059 rows that were bounded before anyone arrived -- but it must also offer the way OUT,
  // because a grammar can be right about the text and wrong about the instance.
  const ch = r.ch==="user_free" ? `<span class="pill un">free</span> <span class="k">by hand —
                      grammar <b>${esc(r.cap||"?")}</b> overridden for this row</span>`
           : r.cap ? `<span class="pill bd">bounded</span> <span class="k">by grammar ·
                      ${esc(r.cap)} — no keystroke needed, <b>B</b> if this instance is not a
                      value</span>`
           : r.ch==="user_bounded" ? `<span class="pill bd">bounded</span>
                      <span class="k">by hand${r.note?" · "+esc(r.note):""}</span>`
           : v==="untrusted" ? `<span class="k">free — <b>B</b> if a user reaches only a bounded
                      part of this string</span>`
           : "";
  const prev = v ? `<span class="pill ${v==="untrusted"?"un":"tr"}">${v}</span>
      <span class="k">${esc(r.mode)}${r.at?" · "+esc(r.at.replace("T"," ").replace("+00:00","")):""}</span>`
    : `<span class="pill none">unlabeled</span>`;
  $("#detail").innerHTML = `<table>
      <tr><td class="k">elem</td><td>#${r.i} <span class="k">of ${S.data.n_rows}</span></td></tr>
      <tr><td class="k">role</td><td>${esc(r.role)}</td></tr>
      <tr><td class="k">bbox</td><td>${r.bbox.join(", ")}</td></tr>
      ${r.cls?`<tr><td class="k">class</td><td>${esc(r.cls.slice(0,90))}</td></tr>`:""}
      ${r.id?`<tr><td class="k">id</td><td>${esc(r.id.slice(0,60))}</td></tr>`:""}
      <tr><td class="k">subtree</td><td>${r.n_desc} descendants</td></tr>
      <tr><td class="k">current</td><td>${prev}</td></tr>
      ${ch?`<tr><td class="k">channel</td><td>${ch}</td></tr>`:""}
    </table>
    ${S.armed?`<div class="txt armed">SUBTREE ARMED — next t/u applies to this element and its
       content-tier descendants.</div>`:""}
    ${r.vr
      ? `<div class="txt sym"><b>Visual region</b> — a textless box (${esc(r.role)}), which is how
           an image reaches the tree when the browser exposes no element for it (product photos,
           CSS backgrounds, map tiles). Judge what is <i>drawn</i> here: a seller-supplied product
           photo is untrusted, a site logo is not. <b>z</b> zooms 1:1.</div>`
      : r.sym
      ? `<div class="txt sym">no readable text — ${r.text ? `name is just <b>${esc(r.text)}</b>`
           : "unnamed"} (${esc(r.role)}).<br>Judge it from the pixels: the box is ringed on the
           screenshot. Press <b>z</b> to zoom 1:1 if it is too small to make out.</div>`
      : `<div class="txt">${esc(r.text)}</div>`}
    <div id="anc" class="k" style="margin-top:8px"></div>`;
  fetch(`/api/context?file=${encodeURIComponent(S.file)}&elem=${r.i}`).then(x=>x.json()).then(c=>{
    $("#anc").innerHTML = "ancestors: " + (c.ancestors.map(a =>
      esc(a.role) + (a.text?` "${esc(a.text.slice(0,28))}"`:"") ).join(" ‹ ") || "(root)");
  });
}

function scrollToElem(){
  const r = S.data.rows.find(x=>x.i===S.elem); if(!r) return;
  const st = $("#stage"), sc = st.clientWidth / S.data.screen[0];
  st.scrollTop = Math.max(0, (r.bbox[1]+r.bbox[3]/2)*sc - st.clientHeight/2);
}

// NOTES ARE CARRIED, NEVER ASKED FOR. Promoting a row is one keystroke; nothing here writes a
// new note. This exists only so a re-commit does not DESTROY an old one: pressing Enter on a
// capture whose rows are already promoted sends them again with no note attached, and
// make_decision would write that absence straight over the sentence someone wrote by hand. So a
// promotion the client is re-sending picks its stored note back up first. Only rows that are
// already promoted have one -- a fresh promotion simply has none, and the panel prints the
// channel without it.
function carryNotes(items){
  const rows = (S.data && S.data.rows) || [];
  for(const x of items){
    if(x.channel !== "user_bounded" || x.note) continue;
    const r = rows.find(y => y.i === x.elem_index);
    if(r && r.ch === "user_bounded" && r.note) x.note = r.note;
  }
  return items;
}

async function commit(items, opt={}){
  items = carryNotes(items);
  S.data = await api("/api/decide", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({file:S.file, items, elem:S.elem, reset:!!opt.reset})});
  // Reseed from what came BACK, not from what was sent: a promotion the server refused (a
  // trusted row, an unknown channel) must show as refused rather than linger in the draft.
  for(const r of S.data.rows) S.draft[r.i] = draftValue(r) || S.draft[r.i] || "trusted";
  render();
}

// Element-view navigation walks only what is drawn, so `c` narrows the queue as well as the
// picture -- stepping onto an invisible box would leave nothing highlighted on screen.
const idx = () => shown().findIndex(r=>r.i===S.elem);
function step(delta){
  const rows = shown(), i = idx()+delta;
  if(i>=0 && i<rows.length){ S.elem = rows[i].i; render(); scrollToElem(); }
  else if(delta>0) nextCapture(); else prevCapture();
}
function firstUnlabeled(){
  const r = shown().find(x=>!x.verdict);
  if(r){ S.elem=r.i; render(); scrollToElem(); } else nextCapture();
}
async function nextCapture(){
  if(S.data.next_file) await load(S.data.next_file, S.mode==="element"?0:null);
  else toast("end of corpus");
  if(S.mode==="element") firstUnlabeled();
}
async function prevCapture(){
  if(S.data.prev_file) await load(S.data.prev_file, S.mode==="element"?0:null);
  else toast("start of corpus");
}

document.addEventListener("keydown", async ev => {
  if(ev.metaKey||ev.ctrlKey||ev.altKey) return;
  const k = ev.key, rows = S.data ? S.data.rows : [];
  if(!S.data) return;

  if(/^[0-9]$/.test(k) && S.mode==="page"){ S.digits += k; toast("#"+S.digits); return; }

  switch(k){
    case "Enter":
      if(S.mode==="page"){
        if(S.digits){ const i=+S.digits; S.digits="";
          if(shown().some(x=>x.i===i)){ pick(i); } else toast("no element #"+i);
          return; }
        // effectiveDraft() treats anything not currently drawn as a hidden container and writes
        // it as container_join. In structural mode NO content row is drawn, so an unguarded Enter
        // here would rewrite the entire page that way -- a silent mass-corruption of a capture you
        // may have already labeled by hand.
        if(S.filter==="structural" || S.filter==="excluded"){
          toast(S.filter+" pass — press c to return to content, then ↵ to commit");
          return; }
        const items = effectiveDraft();
        await commit(items);
        const joined = items.filter(x=>x.mode==="container_join").length;
        toast(`committed ${S.file}${joined?` (${joined} joined)`:""}`);
        await nextCapture();
      }
      return;
    case "t": case "u": {
      const v = k==="t" ? "trusted" : "untrusted";
      if(S.sel.size){
        const items = [...S.sel].map(i=>({elem_index:i, verdict:v, mode:"region"}));
        const n = items.length; S.sel.clear();
        for(const it of items) S.draft[it.elem_index] = v;
        await commit(items); toast(`${n} element(s) -> ${v}`); renderPanel(); return;
      }
      if(S.mode!=="element") return;
      const r = rows.find(x=>x.i===S.elem); if(!r) return;
      if(S.armed){
        const c = await api(`/api/context?file=${encodeURIComponent(S.file)}&elem=${r.i}`);
        const items = [{elem_index:r.i, verdict:v, mode:"element"}].concat(
          c.subtree.map(i=>({elem_index:i, verdict:v, mode:"subtree", origin:r.i})));
        S.armed = false; await commit(items); toast(`${v} · ${c.subtree.length} descendants`);
        // The whole subtree just resolved -- stepping by one would walk back through elements
        // that were the entire point of propagating, so jump past them.
        firstUnlabeled(); return;
      }
      await commit([{elem_index:r.i, verdict:v, mode:"element"}]);
      step(1); return;
    }
    // Promote to untrusted-but-bounded. Element and region only, never a page sweep: the label is
    // a per-string promise and there is no such thing as a page of them. It commits directly --
    // the row is the record of what was promised, so there is nothing to interrupt for.
    case "B": {
      const targets = S.sel.size ? [...S.sel]
                    : (S.mode==="element" && rows.some(x=>x.i===S.elem) ? [S.elem] : []);
      if(!targets.length){ toast("B: pick an element or drag a region first"); return; }
      // B OVERRIDES THE BOUNDED AXIS, in whichever direction the row is not already in. A row no
      // grammar covers gets the hand promise; a row a grammar DOES cover gets the hand denial,
      // because promising a second time what capacity.py recomputes every build would add an
      // unfalsifiable claim and nothing else, while denying it is the only thing a labeler
      // looking at 035_amazon #276 can say -- "60%" there is a keyboard form factor sitting in a
      // row of filter chips, and `percent` matching its text does not make it a value.
      const dn = targets.filter(i => (rows.find(x=>x.i===i)||{}).cap);
      const up = targets.filter(i => !(rows.find(x=>x.i===i)||{}).cap);
      const items = up.map(i => ({elem_index:i, verdict:"untrusted",
                                  mode: S.sel.size ? "region" : "element",
                                  channel:"user_bounded"}))
        .concat(dn.map(i => ({elem_index:i, verdict:"untrusted",
                              mode: S.sel.size ? "region" : "element",
                              channel:"user_free"})));
      S.sel.clear();
      for(const i of up) S.draft[i] = "bounded";
      for(const i of dn) S.draft[i] = "free";
      await commit(items);
      toast(`${up.length?`${up.length} bounded`:""}${up.length&&dn.length?" · ":""}`
            + `${dn.length?`${dn.length} freed from grammar`:""}`);
      if(S.mode==="element" && !S.sel.size && items.length===1) step(1); else renderPanel();
      return;
    }
    case "T": case "U": {
      const v = k==="T" ? "trusted" : "untrusted";
      if(S.filter==="structural" || S.filter==="excluded"){
        toast("T/U is disabled in the "+S.filter+" pass"); return; }
      if(S.mode==="page"){ for(const r of rows) S.draft[r.i]=v;
        await commit(rows.map(r=>({elem_index:r.i, verdict:v, mode:"page_bulk"})));
        toast(`whole capture ${v}`); await nextCapture(); }
      else { const from = idx();
        const rest = shown().slice(from).filter(r=>!r.verdict || r.i===S.elem);
        await commit(rest.map(r=>({elem_index:r.i, verdict:v, mode:"page_bulk"})));
        toast(`${rest.length} remaining → ${v}`); await nextCapture(); }
      return;
    }
    // The three states, iterated rather than addressed. t/u/B still jump straight to one; this
    // is for working down a column of near-identical rows where the question is which of the
    // three each one is.
    case " ": {
      ev.preventDefault();
      const targets = S.sel.size ? [...S.sel]
                    : (S.mode==="element" && rows.some(x=>x.i===S.elem) ? [S.elem] : []);
      if(!targets.length){ toast("space: pick an element or drag a region first"); return; }
      const items = targets.map(i => {
        const r = rows.find(x=>x.i===i) || {};
        const v = nextState(r);
        return { elem_index:i, verdict: (v==="bounded"||v==="free") ? "untrusted" : v,
                 mode: S.sel.size ? "region" : "element",
                 ...(v==="bounded" ? {channel:"user_bounded"} : {}),
                 ...(v==="free"    ? {channel:"user_free"}    : {}) };
      });
      S.sel.clear();
      await commit(items);
      const shownState = stateOf(S.data.rows.find(x=>x.i===targets[0]) || {});
      toast(items.length===1 ? `#${targets[0]} → ${shownState}`
                             : `${items.length} element(s) cycled`);
      return;
    }
    case "d":
      if(S.mode==="element"){ S.armed=!S.armed; render(); }
      return;
    case "s": if(S.mode==="element") step(1); return;
    case "b": if(S.mode==="element") step(-1); return;
    case "e": if(S.mode==="page"){ S.mode="element"; S.elem = (rows.find(r=>!r.verdict)||rows[0]||{}).i;
                render(); scrollToElem(); } return;
    case "a": if(S.mode==="element"){ S.mode="page"; render(); } return;
    // Arrows browse the corpus WITHOUT writing anything. load() only fetches state and reseeds
    // the page-view draft in memory; nothing reaches the journal unless you commit, so leaving a
    // capture by arrow is a true no-op on it. This is the "look at the next screenshot" path, as
    // distinct from Enter, which commits the page and then advances.
    case "n": case "ArrowRight": ev.preventDefault(); await nextCapture(); return;
    case "p": case "ArrowLeft":  ev.preventDefault(); await prevCapture(); return;
    case "z": S.zoom = S.zoom==="fit" ? "full" : "fit"; render();
              if(S.mode==="element") scrollToElem(); return;
    case "c": {
      S.filter = FILTERS[(FILTERS.indexOf(S.filter)+1) % FILTERS.length];
      // The element cursor must stay on something that is actually drawn.
      if(S.mode==="element" && !shown().some(r=>r.i===S.elem))
        S.elem = (shown()[0]||{}).i ?? S.elem;
      S.hover = null; render(); toast("showing: "+S.filter); return;
    }
    case "R": {
      if(!confirm(`Discard every label on ${S.file} and start it over?`)) return;
      await commit([], {reset:true});
      for(const r of rows) S.draft[r.i]="trusted";
      toast("reset — clean slate"); render(); return;
    }
    case "g": {
      const t = prompt("go to capture (number or name):"); if(!t) return;
      try { await load(t.trim(), S.mode==="element"?0:null); }
      catch(e){ toast("no match"); }
      return;
    }
    case "x": { const r = await api("/api/export", {method:"POST"});
      toast(`exported ${r.rows} rows (${r.hand} hand, ${r.derived} derived)`); return; }
    case "Escape": S.digits=""; S.armed=false; S.sel.clear(); render(); return;
  }
});

(async () => {
  const boot = await api("/api/boot");
  S.mode = boot.mode;
  initOverlay();
  await load(boot.file, boot.elem);
  if(S.mode==="element" && boot.elem!=null) scrollToElem();
})();
</script></body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xml_dir", default=ls.XML_DIR)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--start", default=None, help="Open this capture (numeric prefix or substring)")
    ap.add_argument("--mode", choices=["page", "element"], default="page")
    ap.add_argument("--relabel", action="store_true",
                    help="With --start: discard that capture's existing labels first")
    ap.add_argument("--no_browser", action="store_true")
    ap.add_argument("--cache", action="store_true",
                    help="Reuse labels/.universe_cache.json instead of rebuilding (~15s). OFF by "
                         "default: the cache key cannot see code changes, so a stale one silently "
                         "serves the universe from before your last edit.")
    ap.add_argument("--export", action="store_true", help="Materialize the export and exit")
    ap.add_argument("--bounded_report", action="store_true",
                    help="Split every untrusted row into free / bounded-by-grammar / "
                         "bounded-by-hand, per capture, and exit")
    args = ap.parse_args()

    print(f"building element universe (schema {ls.UNIVERSE_SCHEMA}"
          f"{', cached' if args.cache else ', no cache'})…", file=sys.stderr)
    _t0 = time.time()
    STATE["universe"] = ls.build_universe(args.xml_dir, use_cache=args.cache,
                                          write_cache=args.cache)
    STATE["decisions"] = ls.effective_decisions()
    STATE["mode"] = args.mode
    STATE["xml_dir"] = args.xml_dir
    STATE["boot_stamp"] = ls._cache_stamp(args.xml_dir)
    STATE["files"] = _ordered_files()
    STATE["corpus_content"] = sum(len(_labelable(fu)) for fu in STATE["universe"].values())
    print(f"built {len(STATE['universe'])} captures · {STATE['corpus_content']} labelable "
          f"elements · {time.time() - _t0:.1f}s", file=sys.stderr)

    if args.export:
        print(json.dumps(export(), indent=2))
        return

    if args.bounded_report:
        bounded_report()
        return

    if args.relabel:
        if not args.start:
            raise SystemExit("--relabel needs --start (it resets one capture, not the corpus)")
        filename = ls.resolve_file(STATE["universe"], args.start)
        apply_decisions(filename, [], reset=True)
        print(f"reset {filename} — its previous labels are archived in the journal", file=sys.stderr)

    STATE["entry"] = resolve_entry(args.start, args.mode)
    cov = ls.coverage(STATE["universe"], STATE["decisions"])["totals"]
    print(f"{cov['files']} captures · {cov['content']} labelable elements · "
          f"{cov['labeled']} already labeled", file=sys.stderr)
    print(f"opening at {STATE['entry'][0]}"
          f"{'' if STATE['entry'][1] is None else ' #' + str(STATE['entry'][1])}", file=sys.stderr)

    url = f"http://{args.host}:{args.port}/"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"\n  {url}\n  (ssh -L {args.port}:localhost:{args.port} … if this is a remote box)\n",
          file=sys.stderr)
    if not args.no_browser and (os.environ.get("DISPLAY") or sys.platform == "darwin"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nexporting…", file=sys.stderr)
        print(json.dumps(export(), indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
