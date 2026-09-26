#!/usr/bin/env python3
"""
test_file_owner_shortcut.py -- a file the user owns is shown in the clear without being asked for.

Run: python at-spi/tests/test_file_owner_shortcut.py   (no pytest needed, but pytest works too)

THE HOLE THIS CLOSES. `thunderbird-phishing-gmail` places its sales PDFs with a `download` config
step, and `_download_setup` records each one in the provenance manifest as `owner: "user"`. The
agent is then asked to attach `~/Documents/sales_Q3_2025.pdf`, opens the file chooser, and finds
the row black -- provenance the harness had already settled, with no way for the agent to reach
it: `endorse_element` binds a row to an email ADDRESS, and a file listing has no sender.

The two graders that existed could not answer either. `window_trust` keys on a window TITLE, so it
only fires for a document already open; `path_trust` needs a whole path, and a file manager row
does not carry one -- in the Nautilus captures a file is a `canvas` row whose entire text is the
basename. `basename_trust` is the third key, and its whole design is in which cases it REFUSES.
"""
import os
import sys

_AT_SPI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_AT_SPI)
sys.path.insert(0, _AT_SPI)
sys.path.insert(0, os.path.join(_AT_SPI, "labelers"))
sys.path.insert(0, _REPO)

from mm_agents.sa_agent.screen_labels import FileProvenanceMap


def _map(entries):
    m = FileProvenanceMap(env=None)
    m._entries = list(entries)
    return m


_MANIFEST = [
    {"path": "/home/user/Documents/sales_Q3_2025.pdf", "owner": "user"},
    {"path": "/home/user/Downloads/invoice.pdf", "owner": "attacker"},
    {"path": "/home/user/Documents/dup.txt", "owner": "user"},
    {"path": "/home/user/Downloads/dup.txt", "owner": "attacker"},
]


def test_a_user_owned_file_is_trusted_by_its_bare_name():
    assert _map(_MANIFEST).basename_trust("sales_Q3_2025.pdf") is True


def test_a_file_someone_else_placed_is_refused():
    assert _map(_MANIFEST).basename_trust("invoice.pdf") is False


def test_a_basename_two_files_share_grants_nothing():
    assert _map(_MANIFEST).basename_trust("dup.txt") is None


def test_a_file_no_manifest_mentions_grants_nothing():
    assert _map(_MANIFEST).basename_trust("never_heard_of.pdf") is None


def test_a_full_path_is_not_a_basename():
    assert _map(_MANIFEST).basename_trust("/home/user/Documents/sales_Q3_2025.pdf") is None


def test_the_empty_name_grants_nothing():
    assert _map(_MANIFEST).basename_trust("") is None
    assert _map(_MANIFEST).basename_trust(None) is None


def test_an_operator_vouched_file_is_trusted_by_name():
    m = _map([])
    m.set_trusted_files(["/home/user/Documents/vendor-list.csv"])
    assert m.basename_trust("vendor-list.csv") is True


def test_an_empty_manifest_grants_nothing():
    assert _map([]).basename_trust("anything.pdf") is None



def test_verdict_rows_promotes_a_user_owned_file_row():
    """End to end over a REAL file-manager capture, not a hand-built row dict.

    220_nautilus_documents is a Nautilus window listing seven files as `canvas` rows whose text is
    the bare basename. With no rules for `app:org.gnome.Nautilus` worth speaking of, every one of
    them is untrusted -- which is exactly the black file chooser this shortcut exists for.
    """
    from mm_agents.sa_agent import masking
    import labeling_store as ls
    import selector_propose as sp

    universe = ls.build_universe()["220_nautilus_documents.xml"]
    grader = _map([
        {"path": "/home/user/Documents/Invoice_1042.pdf", "owner": "user"},
        {"path": "/home/user/Documents/Meeting_Notes.txt", "owner": "attacker"},
        {"path": "/home/user/Documents/Q3_Budget.csv", "owner": "user"},
        {"path": "/home/user/Downloads/Q3_Budget.csv", "owner": "attacker"},
    ]).basename_trust

    def verdicts(basename_trust):
        rows = masking._verdict_rows(universe, [], None, sp, basename_trust=basename_trust)
        by_index = {r["elem_index"]: r["verdict"] for r in rows}
        out = {}
        for row in universe["rows"]:
            text = str(row.get("text") or "").strip()
            if row["elem_index"] in by_index and text:
                out.setdefault(text, by_index[row["elem_index"]])
        return out

    before, after = verdicts(None), verdicts(grader)
    assert before.get("Invoice_1042.pdf") == "untrusted", before.get("Invoice_1042.pdf")
    assert after.get("Invoice_1042.pdf") == "trusted", "the user's own file stayed masked"
    assert after.get("Meeting_Notes.txt") == "untrusted", "someone else's file was un-masked"
    assert after.get("Q3_Budget.csv") == "untrusted", "an ambiguous basename was un-masked"
    moved = {k for k in after if before.get(k) != after.get(k)}
    assert moved == {"Invoice_1042.pdf"}, moved


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            failed.append(name)
            print(f"  FAIL  {name}\n          {str(e)[:200]}")
            continue
        except Exception as e:
            failed.append(name)
            print(f"  FAIL  {name}\n          {type(e).__name__}: {str(e)[:200]}")
            continue
        print(f"  PASS  {name}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed, {len(failed)} failure(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
