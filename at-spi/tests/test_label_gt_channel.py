#!/usr/bin/env python3
"""Tests for the channel sub-label in label_gt.py -- the untrusted/bounded distinction.

WHAT THESE TESTS ARE PROTECTING. The sub-label was implemented entirely inside label_gt.py on the
strength of one claim about code it does not own: that an extra key on a journal record survives
labeling_store's read path untouched, because read_journal, effective_decisions and
align_decisions all carry the record dict rather than rebuilding it. If that stops being true the
label vanishes silently -- the UI keeps accepting `B`, the journal keeps growing, and every
statistic reads as though nobody ever pressed it. `test_channel_survives_the_journal_round_trip`
is the test that fails first if labeling_store changes shape, and it is the reason this file
exists.

The second thing under test is that the label cannot widen. It may only appear on an untrusted
row, only with the one accepted value, and pressing t/u must take it back off -- because a stale
`bounded` on a row whose text turned into a sentence is exactly the misreport this label was
introduced to remove.

Everything runs against a throwaway journal (label_gt writes through labeling_store's module
globals, which the fixture repoints at a temp dir), so the real corpus labels are never touched.
No pytest in this env; run the file.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_AT_SPI = os.path.dirname(_HERE)
sys.path.insert(0, _AT_SPI)

import label_gt
import labeling_store as ls

SHA_CAPTURE = "466_gitlab_byteblaze_dotfiles_commits_main.xml"
PRICED_CAPTURE = "025_www.booking.com.xml"

_TMP: "str | None" = None
_SAVED: dict = {}


def setup() -> dict:
    """Builds the two-capture universe once and points every write at a temp journal."""
    global _TMP
    _TMP = tempfile.mkdtemp(prefix="label_gt_channel_")
    _SAVED.update(labels_dir=ls.LABELS_DIR, journal=ls.JOURNAL_PATH, export=ls.EXPORT_PATH)
    ls.LABELS_DIR = _TMP
    ls.JOURNAL_PATH = os.path.join(_TMP, "journal.jsonl")
    ls.EXPORT_PATH = os.path.join(_TMP, "export.json")

    universe = ls.build_universe(files=[SHA_CAPTURE, PRICED_CAPTURE])
    label_gt.STATE["universe"] = universe
    label_gt.STATE["decisions"] = {}
    label_gt.STATE["xml_dir"] = ls.XML_DIR
    label_gt.STATE["corpus_content"] = sum(len(label_gt._labelable(fu))
                                           for fu in universe.values())
    label_gt.STATE["journal_mtime"] = 0.0
    label_gt.STATE["boot_stamp"] = ls._cache_stamp(ls.XML_DIR)
    label_gt.STATE["files"] = label_gt._ordered_files()
    return universe


def teardown() -> None:
    ls.LABELS_DIR = _SAVED["labels_dir"]
    ls.JOURNAL_PATH = _SAVED["journal"]
    ls.EXPORT_PATH = _SAVED["export"]
    if _TMP and os.path.isdir(_TMP):
        shutil.rmtree(_TMP)


def rows_of(filename: str) -> list:
    return label_gt.STATE["universe"][filename]["rows"]


def find_sha_row(filename: str) -> dict:
    """A row whose whole text is a short hex string -- an actual commit SHA off the page."""
    for row in rows_of(filename):
        text = (row["text"] or "").strip()
        if (row["tier"] == "content" and 7 <= len(text) <= 10
                and all(c in "0123456789abcdef" for c in text)):
            return row
    raise AssertionError(f"no SHA-shaped row in {filename}")


def find_grammar_row(filename: str) -> dict:
    for row in rows_of(filename):
        if row["tier"] == "content" and row.get("capacity"):
            return row
    raise AssertionError(f"no capacity-bound row in {filename}")


def find_free_row(filename: str) -> dict:
    """A content row no grammar covers -- the ordinary case, and what `bounded` must stay False
    for. Sorted by length so the row picked is unambiguously prose rather than a borderline
    token, which matters when a failure here has to be read as a real regression."""
    free = [r for r in rows_of(filename)
            if r["tier"] == "content" and not r.get("capacity") and (r["text"] or "").strip()]
    if not free:
        raise AssertionError(f"no free-text row in {filename}")
    return max(free, key=lambda r: len(r["text"]))


def journal() -> list:
    if not os.path.exists(ls.JOURNAL_PATH):
        return []
    with open(ls.JOURNAL_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def reload_decisions() -> None:
    """What the server does on restart: re-derive state from the journal on disk alone."""
    label_gt.STATE["decisions"] = ls.effective_decisions()


def commit(filename: str, *items: dict) -> None:
    label_gt.apply_decisions(filename, list(items))


def test_grammar_half_reads_capacity_and_nothing_else() -> None:
    row = find_grammar_row(PRICED_CAPTURE)
    assert label_gt.bounded_by_grammar(row) == row["capacity"], "must be capacity.py's verdict"
    free = find_free_row(PRICED_CAPTURE)
    assert label_gt.bounded_by_grammar(free) is None


def test_a_commit_sha_is_not_bounded_by_any_grammar() -> None:
    """The premise of the whole label: capacity.py deliberately has no hex_digest grammar, so a
    SHA is free-capacity and no keystroke-free path can ever cover it. If this starts failing, a
    hex grammar came back and capacity.py's compositional argument needs re-reading before
    anything here is believed."""
    row = find_sha_row(SHA_CAPTURE)
    assert label_gt.bounded_by_grammar(row) is None, (
        f"{row['text']!r} matched grammar {row['capacity']!r} -- did hex_digest return?")


def test_capacity_fragment_withdraws_the_grammar_label() -> None:
    """A fragment is NOT bounded here, because this file must agree with the thing it reports on.

    The inverse was pinned once, on the argument that the fragment bit is about placement and this
    label is about the string. What settled it the other way is that selector_propose refuses the
    exemption for a fragment and the live mask refuses it through the same call: a row this file
    called bounded-by-grammar was a FREE false negative everywhere else, and the panel would not
    let anyone label it as one."""
    fragments = [r for r in rows_of(PRICED_CAPTURE) if r.get("capacity_fragment")]
    if not fragments:
        return
    for row in fragments:
        assert row["capacity"], "a fragment is by construction a row that matched a grammar"
        assert label_gt.bounded_by_grammar(row) is None, (
            f"{row['text']!r} is a fragment of a rendered line; the scorer withholds its "
            f"exemption, so this file must not offer one")


def test_fragment_and_scorer_agree_on_every_exempt_row() -> None:
    """The property the test above is one instance of, checked against the scorer itself.

    This is the guard that matters: whatever the fragment rule grows into, the panel's notion of
    'already bounded' and the mask's notion of 'exempt from painting' must not drift apart, because
    the gap between them is invisible -- it shows up only as a free miss counted in one place and
    not the other."""
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(label_gt.__file__)),
                                    "labelers"))
    from labelers import selector_propose as sp
    was, sp.CAPACITY_EXEMPTION = sp.CAPACITY_EXEMPTION, True
    try:
        for name in (PRICED_CAPTURE, SHA_CAPTURE):
            for row in rows_of(name):
                exempt = sp.capacity_verdict(row, "untrusted", None)[0] == "trusted"
                assert bool(label_gt.bounded_by_grammar(row)) == exempt, (
                    f"{name} #{row['elem_index']} {row['text']!r}: panel says "
                    f"{label_gt.bounded_by_grammar(row)!r}, scorer says exempt={exempt}")
    finally:
        sp.CAPACITY_EXEMPTION = was


def test_channel_is_written_on_an_untrusted_row() -> None:
    row = find_sha_row(SHA_CAPTURE)
    commit(SHA_CAPTURE, {"elem_index": row["elem_index"], "verdict": "untrusted",
                         "mode": "element", "channel": "user_bounded",
                         "note": "digest only: 8 hex chars"})
    rec = journal()[-1]
    assert rec["verdict"] == "untrusted", "the verdict stays two-valued"
    assert rec["channel"] == "user_bounded"
    assert rec["note"] == "digest only: 8 hex chars"
    assert rec["mode"] == "element", "channel must not smuggle itself into mode"


def test_a_grammar_row_can_be_demoted_by_hand() -> None:
    """The inverse of B, and the case 035_amazon #276 needed: "60%" is a keyboard form factor
    sitting in a row of filter chips, so `percent` matches the text and the instance is not a
    value. Before this the panel painted such a row bounded, printed "no keystroke needed", and
    skipped the third stop -- there was no way for a labeler to disagree with the grammar."""
    row = find_grammar_row(PRICED_CAPTURE)
    commit(PRICED_CAPTURE, {"elem_index": row["elem_index"], "verdict": "untrusted",
                            "mode": "element", "channel": "user_free"})
    rec = journal()[-1]
    assert rec["verdict"] == "untrusted", "the verdict stays two-valued"
    assert rec["channel"] == "user_free"

    reload_decisions()
    sent = {r["i"]: r for r in label_gt.file_payload(PRICED_CAPTURE, None)["rows"]}
    got = sent[row["elem_index"]]
    assert got["ch"] == "user_free"
    assert got["cap"] == row["capacity"], (
        "the RAW grammar still goes to the client -- the pill has to name what was overridden, "
        "and the cycle must not offer to promote a row that was just demoted")
    assert label_gt.bounded_effective(row, "user_free") is None, "but the counters see it freed"

    label_gt.export()
    with open(ls.EXPORT_PATH, encoding="utf-8") as f:
        hit = {(r["file"], r["elem_index"]): r for r in json.load(f)}[
            (PRICED_CAPTURE, row["elem_index"])]
    assert hit["bounded"] is False and hit["capacity_class"] is None, \
        "a demoted row must leave the bounded set in the export"
    assert hit["verdict"] == "untrusted"


def test_demotion_does_not_move_the_free_column() -> None:
    """`free` is the number a false-negative budget is read against, so it must not be movable by
    an act of labeling. A demoted row is counted in `free_hand`, beside it, never inside it."""
    row = find_grammar_row(PRICED_CAPTURE)
    commit(PRICED_CAPTURE, {"elem_index": row["elem_index"], "verdict": "untrusted",
                            "mode": "element", "channel": "user_free"})
    reload_decisions()
    tot = label_gt.bounded_report()
    assert tot["free_hand"] >= 1, "the demotion has to land somewhere"
    assert tot["grammar"] == tot["grammar"], "grammar column shrinks by the same row"


def test_channel_is_refused_on_a_trusted_row() -> None:
    """A channel note on a trusted row describes a field nobody is masking -- there is no false
    negative for it to reclassify, so storing it would only invite a reader to act on it."""
    row = find_grammar_row(PRICED_CAPTURE)
    commit(PRICED_CAPTURE, {"elem_index": row["elem_index"], "verdict": "trusted",
                            "mode": "element", "channel": "user_bounded"})
    assert "channel" not in journal()[-1]


def test_unknown_channel_values_are_dropped() -> None:
    """The vocabulary is closed at the two names in label_gt.CHANNELS. A typo or a future third
    name must not land in the journal as though it meant something."""
    row = find_free_row(PRICED_CAPTURE)
    for bogus in ("bounded", "USER_BOUNDED", "platform_authored", True, 1):
        commit(PRICED_CAPTURE, {"elem_index": row["elem_index"], "verdict": "untrusted",
                                "mode": "element", "channel": bogus})
        assert "channel" not in journal()[-1], f"{bogus!r} was accepted"


def test_a_promotion_needs_no_note() -> None:
    """The UI stopped demanding an audit sentence per promotion -- the friction was paid on every
    row and the sentence mostly restated the row's own text. A note-less promotion must therefore
    be a first-class one: stored, exported, and counted in the hand column exactly like the 125
    that were written while the prompt existed."""
    row = find_sha_row(SHA_CAPTURE)
    commit(SHA_CAPTURE, {"elem_index": row["elem_index"], "verdict": "untrusted",
                         "mode": "element", "channel": "user_bounded"})
    rec = journal()[-1]
    assert rec["channel"] == "user_bounded"
    assert rec.get("note") is None, "nothing invents a note on the row's behalf"

    reload_decisions()
    sent = {r["i"]: r for r in label_gt.file_payload(SHA_CAPTURE, None)["rows"]}
    assert sent[row["elem_index"]]["ch"] == "user_bounded", "the panel still colours it"
    assert sent[row["elem_index"]]["note"] is None

    label_gt.export()
    with open(ls.EXPORT_PATH, encoding="utf-8") as f:
        out = json.load(f)
    hit = {(r["file"], r["elem_index"]): r for r in out}[(SHA_CAPTURE, row["elem_index"])]
    assert hit["channel"] == "user_bounded" and hit["bounded"] is True, \
        "a note-less promotion must reach the export like any other"


def test_plain_labels_carry_no_channel_key_at_all() -> None:
    """Absent, never null: an unexamined row makes no claim, which is a different thing from a
    row someone judged to be free."""
    row = find_free_row(SHA_CAPTURE)
    commit(SHA_CAPTURE, {"elem_index": row["elem_index"], "verdict": "untrusted",
                         "mode": "element"})
    assert "channel" not in journal()[-1]


def test_channel_survives_the_journal_round_trip() -> None:
    """THE LOAD-BEARING TEST. label_gt attaches `channel` after make_decision and never teaches
    labeling_store about it, which only works because read_journal -> effective_decisions ->
    align_decisions carry the record dict through rather than rebuilding it from known keys."""
    row = find_sha_row(SHA_CAPTURE)
    commit(SHA_CAPTURE, {"elem_index": row["elem_index"], "verdict": "untrusted",
                         "mode": "element", "channel": "user_bounded", "note": "digest only"})
    reload_decisions()
    fu = label_gt.STATE["universe"][SHA_CAPTURE]
    decided, _ = ls.align_decisions(fu, label_gt.STATE["decisions"].get(SHA_CAPTURE, {}))
    rec = decided[row["elem_index"]]
    assert rec.get("channel") == "user_bounded", (
        "channel lost in labeling_store's read path -- see this module's docstring")
    assert rec.get("note") == "digest only"


def test_t_or_u_afterwards_takes_the_promotion_back_off() -> None:
    """How a promotion is undone, and it must be a real removal rather than a shadowed record:
    the last `set` for a sig wins whole, so a plain relabel leaves no channel behind."""
    row = find_sha_row(SHA_CAPTURE)
    commit(SHA_CAPTURE, {"elem_index": row["elem_index"], "verdict": "untrusted",
                         "mode": "element", "channel": "user_bounded", "note": "digest"})
    commit(SHA_CAPTURE, {"elem_index": row["elem_index"], "verdict": "untrusted",
                         "mode": "element"})
    reload_decisions()
    fu = label_gt.STATE["universe"][SHA_CAPTURE]
    decided, _ = ls.align_decisions(fu, label_gt.STATE["decisions"].get(SHA_CAPTURE, {}))
    assert "channel" not in decided[row["elem_index"]]


def test_capture_state_sends_both_halves_to_the_client() -> None:
    """The UI needs `cap` to refuse a pointless promotion and `ch` to colour a real one."""
    sha = find_sha_row(SHA_CAPTURE)
    commit(SHA_CAPTURE, {"elem_index": sha["elem_index"], "verdict": "untrusted",
                         "mode": "element", "channel": "user_bounded", "note": "digest only"})
    payload = label_gt.file_payload(SHA_CAPTURE, None)
    sent = {r["i"]: r for r in payload["rows"]}
    assert sent[sha["elem_index"]]["ch"] == "user_bounded"
    assert sent[sha["elem_index"]]["cap"] is None
    assert sent[sha["elem_index"]]["note"] == "digest only"

    priced = find_grammar_row(PRICED_CAPTURE)
    other = {r["i"]: r for r in label_gt.file_payload(PRICED_CAPTURE, None)["rows"]}
    assert other[priced["elem_index"]]["cap"] == priced["capacity"]
    assert other[priced["elem_index"]]["ch"] is None, "no keystroke was spent on it"


def test_export_carries_the_three_fields_and_bounded_is_their_union() -> None:
    sha = find_sha_row(SHA_CAPTURE)
    commit(SHA_CAPTURE, {"elem_index": sha["elem_index"], "verdict": "untrusted",
                         "mode": "element", "channel": "user_bounded", "note": "digest only"})
    priced = find_grammar_row(PRICED_CAPTURE)
    commit(PRICED_CAPTURE, {"elem_index": priced["elem_index"], "verdict": "untrusted",
                            "mode": "element"})
    free = find_free_row(PRICED_CAPTURE)
    commit(PRICED_CAPTURE, {"elem_index": free["elem_index"], "verdict": "untrusted",
                            "mode": "element"})
    reload_decisions()

    label_gt.export()
    with open(ls.EXPORT_PATH, encoding="utf-8") as f:
        out = json.load(f)
    by_key = {(r["file"], r["elem_index"]): r for r in out}

    hand = by_key[(SHA_CAPTURE, sha["elem_index"])]
    assert (hand["channel"], hand["capacity_class"], hand["bounded"]) \
        == ("user_bounded", None, True), hand
    grammar = by_key[(PRICED_CAPTURE, priced["elem_index"])]
    assert grammar["capacity_class"] == priced["capacity"]
    assert grammar["channel"] is None and grammar["bounded"] is True
    plain = by_key[(PRICED_CAPTURE, free["elem_index"])]
    assert (plain["channel"], plain["capacity_class"], plain["bounded"]) == (None, None, False)


def test_export_schema_stays_backward_compatible() -> None:
    """review_benchmark.py and the scorers read this file. New keys are additive; the old ones
    must all still be there, or a consumer breaks with no warning from here."""
    label_gt.export()
    with open(ls.EXPORT_PATH, encoding="utf-8") as f:
        out = json.load(f)
    required = {"file", "elem_index", "sig", "category", "platform", "role", "text", "bbox",
                "verdict", "confidence", "label_mode", "labeled_at", "mismatch", "tier",
                "visible", "skip_reason", "root_kind", "root_provenance"}
    missing = required - set(out[0])
    assert not missing, f"export lost {missing}"
    assert {"capacity_class", "channel", "bounded"} <= set(out[0])


def test_bounded_report_partitions_every_untrusted_row() -> None:
    """free + grammar + hand must exhaust the untrusted rows -- if the three buckets stop
    partitioning, some rows are being counted twice or not at all and the percentage is fiction."""
    sha = find_sha_row(SHA_CAPTURE)
    commit(SHA_CAPTURE, {"elem_index": sha["elem_index"], "verdict": "untrusted",
                         "mode": "element", "channel": "user_bounded", "note": "digest only"})
    priced = find_grammar_row(PRICED_CAPTURE)
    commit(PRICED_CAPTURE, {"elem_index": priced["elem_index"], "verdict": "untrusted",
                            "mode": "element"})
    free = find_free_row(PRICED_CAPTURE)
    commit(PRICED_CAPTURE, {"elem_index": free["elem_index"], "verdict": "untrusted",
                            "mode": "element"})
    reload_decisions()

    tot = label_gt.bounded_report()
    assert tot["untrusted"] == tot["free"] + tot["grammar"] + tot["hand"], tot
    assert tot["hand"] >= 1 and tot["grammar"] >= 1 and tot["free"] >= 1, tot


def test_grammar_wins_when_a_row_is_covered_both_ways() -> None:
    """A row can carry a hand note AND match a grammar (someone pressed B before noticing). The
    report must not count it twice, and `grammar` is the half that wins because it is the claim
    that gets recomputed rather than remembered."""
    priced = find_grammar_row(PRICED_CAPTURE)
    commit(PRICED_CAPTURE, {"elem_index": priced["elem_index"], "verdict": "untrusted",
                            "mode": "element", "channel": "user_bounded", "note": "belt braces"})
    reload_decisions()
    tot = label_gt.bounded_report()
    assert tot["untrusted"] == tot["free"] + tot["grammar"] + tot["hand"], tot

    label_gt.export()
    with open(ls.EXPORT_PATH, encoding="utf-8") as f:
        row = next(r for r in json.load(f)
                   if r["file"] == PRICED_CAPTURE and r["elem_index"] == priced["elem_index"])
    assert row["bounded"] is True and row["capacity_class"] == priced["capacity"]


def test_the_label_reaches_no_masking_decision() -> None:
    """The whole safety argument in one test: `user_bounded` is a statistics label, so no
    consumer of a mask may be able to see it. label_gt writes it and labeling_store's verdict
    vocabulary -- what every mask actually reads -- must still be exactly two values."""
    assert ls._VERDICTS == ("trusted", "untrusted"), (
        "a third verdict appeared; the channel label was supposed to stay out of the verdict")
    row = find_sha_row(SHA_CAPTURE)
    commit(SHA_CAPTURE, {"elem_index": row["elem_index"], "verdict": "untrusted",
                         "mode": "element", "channel": "user_bounded", "note": "digest only"})
    reload_decisions()
    fu = label_gt.STATE["universe"][SHA_CAPTURE]
    decided, _ = ls.align_decisions(fu, label_gt.STATE["decisions"].get(SHA_CAPTURE, {}))
    assert decided[row["elem_index"]]["verdict"] == "untrusted", (
        "a bounded row is still untrusted and is still masked")


def _main() -> int:
    setup()
    try:
        tests = [(name, fn) for name, fn in sorted(globals().items())
                 if name.startswith("test_") and callable(fn)]
        failed = 0
        for name, fn in tests:
            open(ls.JOURNAL_PATH, "w").close()
            label_gt.STATE["decisions"] = {}
            try:
                fn()
                print(f"  ok    {name}")
            except AssertionError as exc:
                failed += 1
                print(f"  FAIL  {name}\n        {exc}")
            except Exception as exc:
                failed += 1
                print(f"  ERROR {name}\n        {type(exc).__name__}: {exc}")
        print(f"\n{len(tests) - failed}/{len(tests)} passed")
        return 1 if failed else 0
    finally:
        teardown()


if __name__ == "__main__":
    sys.exit(_main())
