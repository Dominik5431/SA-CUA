#!/usr/bin/env python3
"""
test_rule_scope.py -- which rule set applies to the surface in front of the agent.

Run: python at-spi/tests/test_rule_scope.py     (no pytest needed, but pytest works too)

Two questions, and a wrong answer to either is silent. Get the SCOPE wrong and a scored rule set
is simply never found -- which is how all 37 rule sets for the four self-hosted eval platforms sat
unreachable behind a manifest field that holds a path slug rather than a hostname. Get the
TEMPLATE wrong and a rule set IS found and is the wrong one: reddit's post page masked under the
forum listing's rules grants nothing it should, and the listing masked under the post page's rules
grants a comment container that listing never had.

The corpus itself is the fixture wherever possible -- a test that agrees with a hand-written
sample but not with manifest.json is testing the sample.
"""
import json
import os
import sys

_AT_SPI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _AT_SPI)
sys.path.insert(0, os.path.join(_AT_SPI, "labelers"))

import labeling_store as ls
import rule_scope as rs

MANIFEST = json.load(open(os.path.join(_AT_SPI, "manifest.json"), encoding="utf-8"))
INDEX = rs.index_manifest(MANIFEST)



def test_host_is_read_from_the_url_not_the_domain_field():
    row = next(r for r in MANIFEST if r["filename"].startswith("400_reddit"))
    assert row["domain"] != "reddit.com", "fixture drifted: 400's domain is no longer a path slug"
    assert rs.scope_of_manifest_row(row) == "reddit.com", rs.scope_of_manifest_row(row)


def test_every_capture_in_the_manifest_resolves_to_a_scope():
    missing = [r["filename"] for r in MANIFEST if not rs.scope_of_manifest_row(r)]
    assert not missing, f"{len(missing)} capture(s) with no scope, e.g. {missing[:3]}"


def test_the_four_eval_platforms_are_reachable_by_hostname():
    for host in ("reddit.com", "owncloud.com", "rocketchat.com", "gitlab.com"):
        assert INDEX.get(host), f"no captures indexed under {host}"


def test_scheme_less_omnibox_text_resolves():
    assert rs.scope_of_url("reddit.com/f/books") == "reddit.com"
    assert rs.scope_of_url("http://reddit.com/f/books") == "reddit.com"


def test_www_is_normalised_on_both_sides():
    assert rs.scope_of_url("www.booking.com/x") == rs.scope_of_url("booking.com/x") == "booking.com"


def test_app_captures_are_scoped_by_at_spi_application_name():
    row = next(r for r in MANIFEST if r["filename"].startswith("244_impress"))
    assert rs.scope_of_manifest_row(row) == "app:soffice", rs.scope_of_manifest_row(row)
    writer = next(r for r in MANIFEST if r["filename"].startswith("214_libreoffice_writer"))
    assert rs.scope_of_manifest_row(writer) == "app:soffice"


def test_an_app_scope_cannot_be_mistaken_for_a_hostname():
    assert rs.is_app_scope("app:soffice") and not rs.is_app_scope("soffice.com")



def test_ids_and_their_slugs_are_wildcarded():
    assert rs.template_of_url("reddit.com/f/coolgithubprojects/103666/i-made-a-thing") \
        == "/f/coolgithubprojects/#/*"


def test_a_leaf_under_a_section_is_an_instance():
    assert rs.template_of_url("rocketchat.com/channel/kudos") == "/channel/*"
    assert rs.template_of_url("rocketchat.com/channel/random") == "/channel/*"


def test_a_page_name_with_an_extension_stays_literal():
    assert rs.template_of_url("www.booking.com/searchresults.html") == "/searchresults.html"


def test_the_query_string_is_dropped_from_the_path_template():
    a = rs.template_of_url("owncloud.com/index.php/apps/files?dir=/Documents")
    b = rs.template_of_url("owncloud.com/index.php/apps/files?dir=/Photos")
    assert a == b, (a, b)



def test_only_a_declared_scope_has_an_identity_query():
    assert rs.identity_query("www.booking.com/searchresults.html?ss=Rome&checkin=2026-01-01") is None
    assert rs.identity_query("www.amazon.com/s?k=usb+c+cable") is None
    assert rs.identity_query("stackoverflow.com/questions/1/x?tab=votes&page=2") is None
    assert rs.identity_query("owncloud.com/index.php/apps/files?dir=/Photos") == "dir=/photos"


def test_the_identity_query_is_confined_to_its_own_app_path():
    assert rs.identity_query("owncloud.com/index.php/settings/personal") is None
    assert rs.identity_query("owncloud.com/index.php/settings/personal?dir=/Photos") is None


def test_a_missing_dir_is_the_root():
    assert rs.identity_query("owncloud.com/index.php/apps/files") == "dir=/"
    assert rs.identity_query("owncloud.com/index.php/apps/files/?dir=/&fileid=5") == "dir=/"


def test_the_identity_query_is_normalised_on_both_sides():
    stored = rs.identity_query("http://owncloud.com/index.php/apps/files/?dir=/Documents/Data%20Analysis&fileid=178")
    live = rs.identity_query("owncloud.com/index.php/apps/files?dir=/documents/data analysis/")
    assert stored == live == "dir=/documents/data analysis", (stored, live)


def test_the_identity_template_keeps_the_section_and_generalises_the_folder():
    assert rs.identity_query_template("dir=/documents/financials") == "dir=/documents/*"
    assert rs.identity_query_template("dir=/documents/research") == "dir=/documents/*"
    assert rs.identity_query_template("dir=/photos") == "dir=/photos"
    assert rs.identity_query_template("dir=/") == "dir=/"


def test_post_and_listing_are_different_templates():
    post = rs.template_of_url("reddit.com/f/books/7/a-post")
    listing = rs.template_of_url("reddit.com/f/books")
    assert post != listing, post


def test_loose_template_merges_sections_but_keeps_the_route():
    assert rs.loose_template(rs.template_of_url("reddit.com/f/technology/1/a")) \
        == rs.loose_template(rs.template_of_url("reddit.com/f/books/2/b"))
    assert rs.loose_template("/questions/#/*") != rs.loose_template("/questions/tagged/*")
    assert rs.loose_template("/users/#/*") != rs.loose_template("/questions/#/*")


def test_loose_template_does_not_collapse_a_listing_onto_a_post():
    assert rs.loose_template("/f/*") != rs.loose_template("/f/*/#/*")



def _pick(host, url):
    ref, how = rs.select_capture(INDEX.get(host, []), url)
    return (ref.capture if ref else None), how


def test_the_same_page_matches_exactly():
    cap, how = _pick("owncloud.com", "owncloud.com/index.php/apps/files?dir=/Documents/Research")
    assert how == rs.MATCH_EXACT, how
    assert cap.startswith("426_owncloud"), cap


def test_every_owncloud_folder_gets_its_own_capture():
    for want, folder in (("422", "/Documents/Financials"), ("423", "/Documents/Data%20Analysis"),
                         ("425", "/Documents/Admin"), ("426", "/Documents/Research"),
                         ("427", "/Photos"), ("428", "/Templates")):
        cap, how = _pick("owncloud.com", f"owncloud.com/index.php/apps/files?dir={folder}")
        assert cap.startswith(want) and how == rs.MATCH_EXACT, (folder, cap, how)


def test_an_unseen_owncloud_folder_lands_on_a_sibling_listing():
    cap, how = _pick("owncloud.com", "owncloud.com/index.php/apps/files?dir=/Documents/Q3")
    assert how == rs.MATCH_TEMPLATE, how
    assert cap.startswith("42") and not cap.startswith(("420", "421")), cap


def test_photos_does_not_borrow_documents_rules_at_the_template_tier():
    assert rs.identity_query_template("dir=/photos") != rs.identity_query_template("dir=/documents/x")



def test_the_pdf_viewer_is_never_handed_out_for_a_listing_url():
    overlays = [r for r in INDEX["owncloud.com"] if r.is_overlay]
    assert {r.capture[:3] for r in overlays} == {"420", "421"}, overlays
    for url in ("owncloud.com/index.php/apps/files",
                "owncloud.com/index.php/apps/files/?dir=/&fileid=5",
                "owncloud.com/index.php/apps/files?dir=/Documents",
                "owncloud.com/index.php/apps/files?dir=/Anything"):
        cap, how = _pick("owncloud.com", url)
        assert not cap.startswith(("420", "421")), (url, cap, how)


def test_an_overlay_is_still_used_when_the_scope_has_nothing_else():
    only = [r for r in INDEX["owncloud.com"] if r.capture.startswith("420")]
    ref, how = rs.select_capture(only, "owncloud.com/index.php/apps/files")
    assert ref is not None and ref.capture.startswith("420"), (ref, how)


def test_every_suite_capture_records_the_view_it_holds():
    missing = [r["filename"] for r in MANIFEST
               if int(r.get("index", 0) or 0) >= 400 and not r.get("view")]
    assert not missing, f"{len(missing)} suite capture(s) with no view, e.g. {missing[:3]}"


def test_no_other_scope_routes_by_a_query():
    for host, refs in INDEX.items():
        if host in rs.IDENTITY_QUERY or rs.is_app_scope(host):
            continue
        for r in refs:
            if "?" not in r.url:
                continue
            with_q = rs.select_capture(refs, r.url)
            without = rs.select_capture(refs, r.url.split("?")[0])
            assert with_q[0].capture == without[0].capture, (host, r.url, with_q, without)


def test_an_unseen_post_gets_the_post_templates_rules():
    cap, how = _pick("reddit.com", "reddit.com/f/coolgithubprojects/999999/brand-new")
    assert how == rs.MATCH_TEMPLATE, how
    assert cap.startswith("40"), cap


def test_an_unseen_forum_gets_the_listing_not_the_post():
    cap, how = _pick("reddit.com", "reddit.com/f/books")
    assert cap.startswith("403_") or cap.startswith("404_"), (cap, how)


def test_an_unseen_forums_post_still_lands_on_a_post_capture():
    cap, how = _pick("reddit.com", "reddit.com/f/books/7/some-post")
    assert how == rs.MATCH_TEMPLATE_LOOSE, how
    assert cap.startswith(("400_", "401_", "402_")), cap


def test_an_unrelated_path_falls_back_and_says_so():
    cap, how = _pick("rocketchat.com", "rocketchat.com/admin/rooms")
    assert how == rs.MATCH_DEFAULT, how
    assert cap is not None


def test_selection_is_deterministic():
    for _ in range(3):
        assert _pick("stackoverflow.com", "stackoverflow.com/questions/1/x") == \
               _pick("stackoverflow.com", "stackoverflow.com/questions/1/x")


def test_an_app_scope_selects_deterministically_without_a_url():
    cap, how = _pick("app:soffice", None)
    assert cap is not None and how == rs.MATCH_DEFAULT


def test_no_candidates_is_not_an_exception():
    assert rs.select_capture([], "example.com/") == (None, rs.MATCH_DEFAULT)



def _mixed_desktop(active):
    """A desktop-frame holding Chrome AND a terminal, with `active` focused.

    The live situation the corpus cannot hold: every capture is one application, so a bug that
    only appears when two are on screen at once cannot be caught by any of them.
    """
    import copy
    import xml.etree.ElementTree as ET
    st_active = "{https://accessibility.ubuntu.example.org/ns/state}active"
    web = ET.parse(os.path.join(_AT_SPI, "xml", "016_stackoverflow.com.xml")).getroot()
    term = ET.parse(os.path.join(_AT_SPI, "xml", "225_terminal_ls.xml")).getroot()
    browser = next(a for a in web if a.tag == "application"
                   and (a.get("name") or "").lower() in ls._BROWSER_APPS)
    terminal = next(a for a in term if a.tag == "application"
                    and a.get("name") == "gnome-terminal-server")
    root = ET.Element(web.tag, dict(web.attrib))
    for name, app in (("browser", browser), ("terminal", terminal)):
        clone = copy.deepcopy(app)
        for window in clone:
            if ls._node_bbox(window) is None:
                continue
            if name == active:
                window.set(st_active, "true")
            else:
                window.attrib.pop(st_active, None)
        root.append(clone)
    return root


def test_a_focused_native_app_is_not_called_web_just_because_chrome_is_running():
    root = _mixed_desktop("terminal")
    assert ls._infer_platform(root) == "web", "fixture drifted: offline inference already fixed?"
    assert rs.infer_platform_live(root, ls) == "app"


def test_a_focused_browser_is_still_web():
    assert rs.infer_platform_live(_mixed_desktop("browser"), ls) == "web"


def test_no_active_window_falls_back_to_the_offline_answer():
    root = _mixed_desktop("none")
    assert rs.infer_platform_live(root, ls) == ls._infer_platform(root)


def test_app_scope_of_tree_follows_focus():
    assert rs.app_scope_of_tree(_mixed_desktop("terminal"), ls) == "app:gnome-terminal-server"


def test_app_scope_of_a_corpus_capture_matches_its_manifest_row():
    import xml.etree.ElementTree as ET
    for stem in ("225_terminal_ls", "214_libreoffice_writer_notes", "208_vscode_app_py",
                 "220_nautilus_documents", "200_thunderbird_inbox"):
        row = next(r for r in MANIFEST if r["filename"].startswith(stem))
        root = ET.parse(os.path.join(_AT_SPI, "xml", row["filename"])).getroot()
        assert rs.app_scope_of_tree(root, ls) == rs.scope_of_manifest_row(row), stem


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
