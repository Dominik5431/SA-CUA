#!/usr/bin/env python3
"""
test_rule_cache_union.py -- the one place rule sets are allowed to be merged, and the many where
they are not.

Run: python at-spi/tests/test_rule_cache_union.py     (no pytest needed, but pytest works too)

`RuleCache.for_scope` answers "which rules apply to what is on screen". For WEB it selects one
capture's set through rule_scope.select_capture, because a URL can answer the template question.
For a NATIVE APP there is no URL, select_capture short-circuits to the lowest-index capture, and
that silently threw away every other state of the application: all eight Thunderbird captures
collapsed to 200_thunderbird_inbox -- taken with no message selected -- so Reply/Forward/Archive
were masked in every episode, and app:soffice graded Calc and Impress with Writer's rules.

The fix is a union over the application's captures, and its safety rests on two measured claims
plus one structural one:

  * over all 8 multi-capture app scopes (40 captures, v15, allowlist) the union holds leak=0 while
    taking FP 85.2% -> 59.0% and usable rows 343 -> 950;
  * the geometric propagation gate makes it self-limiting -- a rule naming a container that is not
    drawn on this screen grants nothing, so compose-window rules are inert until a compose window
    exists;
  * a union is monotone in grants, so WEB scopes keep selection. rule_scope's own docstring
    records leak 8 -> 23 for merging across sites and page types.

These tests pin the boundary. The corpus and the shipped --sa_rules_dir default are the fixture
wherever possible: a test that agrees with a hand-written sample but not with what run.py actually
loads is testing the sample.
"""
import json
import os
import sys

_AT_SPI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_AT_SPI)
sys.path.insert(0, _AT_SPI)
sys.path.insert(0, os.path.join(_AT_SPI, "labelers"))
sys.path.insert(0, _REPO)

import rule_scope as rs
from mm_agents.sa_agent.masking import RuleCache

def _shipped_rules_dir() -> str:
    import argparse
    import re
    src = open(os.path.join(_REPO, "run.py"), encoding="utf-8").read()
    m = re.search(r'"--sa_rules_dir",\s*\n\s*default=((?:\s*"[^"]*"\s*)+),', src)
    assert m, "could not find --sa_rules_dir's default in run.py"
    del argparse
    return "".join(re.findall(r'"([^"]*)"', m.group(1)))


RULES_DIR = _shipped_rules_dir()
CACHE = RuleCache(rules_dir=RULES_DIR)


def _rules_key(rule: dict) -> str:
    """Rule identity as _app_union computes it: everything but the prose note."""
    return json.dumps({k: v for k, v in rule.items() if k != "note"}, sort_keys=True)



def test_a_multi_capture_app_scope_returns_a_union():
    rules, capture, how = CACHE.for_scope("app:Thunderbird", None)
    assert how == "app_union", how
    assert capture.startswith("<union of "), capture
    assert rules, "the Thunderbird union came back empty"


def test_every_multi_capture_app_scope_takes_the_union_path():
    seen = 0
    for scope, refs in CACHE._index.items():
        got = [r for r in refs if r.capture in CACHE._by_scope.get(scope, {})]
        if not rs.is_app_scope(scope) or len(got) < 2:
            continue
        seen += 1
        _rules, _cap, how = CACHE.for_scope(scope, None)
        assert how == "app_union", f"{scope} resolved {how}, not app_union"
    assert seen >= 5, f"only {seen} multi-capture app scope(s) found -- fixture drifted"


def test_a_web_scope_is_never_unioned_however_many_captures_it_has():
    for url in ("reddit.com/f/coolgithubprojects/103666/x", "reddit.com/user/AineeJames",
                "reddit.com/comments", "reddit.com/forums", "reddit.com/"):
        _rules, capture, how = CACHE.for_scope("reddit.com", url)
        assert how != "app_union", f"{url} was unioned"
        assert capture.endswith(".xml"), f"{url} -> {capture}"
    for scope, url in (("gitlab.com", "gitlab.com/byteblaze/dotfiles/-/issues/42"),
                       ("owncloud.com", "owncloud.com/index.php/apps/files"),
                       ("rocketchat.com", "rocketchat.com/channel/kudos")):
        _rules, capture, how = CACHE.for_scope(scope, url)
        assert how != "app_union", f"{scope} was unioned"
        assert capture.endswith(".xml"), f"{scope} -> {capture}"


def test_web_routing_still_discriminates_between_templates():
    _r, post, _h = CACHE.for_scope("reddit.com", "reddit.com/f/coolgithubprojects/103666/x")
    _r, profile, _h = CACHE.for_scope("reddit.com", "reddit.com/user/AineeJames")
    assert post != profile, f"post and profile both resolved to {post}"
    assert profile.startswith("408_"), profile


def test_a_single_capture_app_scope_is_unchanged():
    single = [s for s, refs in CACHE._index.items()
              if rs.is_app_scope(s)
              and len([r for r in refs if r.capture in CACHE._by_scope.get(s, {})]) == 1]
    assert single, "no single-capture app scope in the corpus -- fixture drifted"
    _rules, capture, how = CACHE.for_scope(single[0], None)
    assert how != "app_union", f"{single[0]} took the union path with one capture"
    assert capture.endswith(".xml"), capture


def test_capture_pref_still_overrides_the_union():
    pinned = RuleCache(rules_dir=RULES_DIR, capture_pref="207_")
    rules, capture, how = pinned.for_scope("app:Thunderbird", None)
    assert how == "capture_pref", how
    assert capture.startswith("207_"), capture
    assert rules is not None



def test_the_union_is_exactly_the_deduplicated_union_of_its_captures():
    scope = "app:Thunderbird"
    sets = CACHE._by_scope[scope]
    refs = [r for r in CACHE._index[scope] if r.capture in sets]
    merged, _cap, _how = CACHE.for_scope(scope, None)
    want, seen = [], set()
    for r in refs:
        for rule in sets[r.capture]:
            k = _rules_key(rule)
            if k not in seen:
                seen.add(k)
                want.append(k)
    assert [_rules_key(r) for r in merged] == want, "union is not the manifest-ordered dedup"


def test_the_union_holds_no_duplicates():
    merged, _c, _h = CACHE.for_scope("app:Thunderbird", None)
    keys = [_rules_key(r) for r in merged]
    assert len(keys) == len(set(keys)), f"{len(keys) - len(set(keys))} duplicate rule(s)"


def test_the_union_is_reproducible():
    a, _c, _h = CACHE.for_scope("app:Thunderbird", None)
    b, _c, _h = RuleCache(rules_dir=RULES_DIR).for_scope("app:Thunderbird", None)
    assert [_rules_key(r) for r in a] == [_rules_key(r) for r in b], "union order is not stable"


def test_the_union_is_strictly_bigger_than_the_capture_it_replaces():
    sets = CACHE._by_scope["app:Thunderbird"]
    inbox = next(v for k, v in sets.items() if k.startswith("200_"))
    merged, _c, _h = CACHE.for_scope("app:Thunderbird", None)
    inbox_keys = {_rules_key(r) for r in inbox}
    merged_keys = {_rules_key(r) for r in merged}
    assert inbox_keys <= merged_keys, "the union dropped a rule capture 200 had"
    assert len(merged_keys) > len(inbox_keys), (
        f"union has {len(merged_keys)} rule(s), capture 200 alone has {len(inbox_keys)}")


def test_the_toolbars_the_agent_has_to_click_survive_the_union():
    """The regression, stated as verdicts rather than as rule shapes.

    Asserted behaviourally on purpose: which SELECTOR grants these is the proposer's business and
    changes between arms (v17 reaches them through a container whose ancestor_path names neither
    the button nor its class), but whether the agent can see them is the property the defense owes
    the evaluation. Under capture 200 alone every one of these came back untrusted -- the message
    header toolbar is absent from that capture, and the compose window is a different window
    entirely -- which is why the Thunderbird suite could not complete a single reply.

    Costs ~12 s: it is the only test here that builds the universe, and it earns it.
    """
    import labeling_store as _ls
    import selector_propose as _sp
    universe = _ls.build_universe()
    merged, _c, _h = CACHE.for_scope("app:Thunderbird", None)
    want = {"201_thunderbird_message_phish.xml": ("Reply", "Forward", "Archive", "Junk", "Delete"),
            "207_thunderbird_compose_reply.xml": ("Send", "Save", "Attach")}
    for capture, labels in want.items():
        rows = universe[capture]["rows"]
        verdicts = _sp.predict_page(universe[capture], merged, policy="allowlist", domain=None)
        seen: dict = {}
        for elem_index, (verdict, _note) in verdicts.items():
            row = rows[elem_index]
            text = str(row.get("text") or "").strip()
            if row.get("tier", "content") == "content" and text in labels:
                seen.setdefault(text, verdict)
        missing = [t for t in labels if t not in seen]
        assert not missing, f"{capture}: no row carries {missing} -- fixture drifted"
        masked = sorted(t for t, v in seen.items() if v != "trusted")
        assert not masked, f"{capture}: {masked} masked under the app:Thunderbird union"



_UCM_PIN = "llm_rules_container_v19"
_UCM_ORDERS = [RULES_DIR,
               RULES_DIR.replace(",llm_rules_container_overrides", "")
               + ",llm_rules_container_overrides,llm_rules_container_v19",
               RULES_DIR.replace(",llm_rules_container_overrides", "")
               + ",llm_rules_container_v19,llm_rules_container_overrides"]


def _v19_site_union(first: int, last: int) -> set:
    import glob
    got = set()
    for i in range(first, last + 1):
        for p in glob.glob(os.path.join(_AT_SPI, _UCM_PIN, f"{i}_ucm_*__*.json")):
            if not p.endswith((".meta.json", ".proposed.json")):
                got |= {_rules_key(r) for r in json.load(open(p, encoding="utf-8"))}
    return got


def test_ucm_serves_v19s_union_whatever_the_rules_dir_order():
    want = _v19_site_union(500, 503)
    assert want, "v19 has no banking rule sets"
    for order in _UCM_ORDERS:
        c = RuleCache(rules_dir=order, cache_dir=None, category_dirs={"ucm": _UCM_PIN})
        rules, capture, how = c.for_scope("banking.com", "http://banking.com/?reveal_all=true")
        assert how == "app_union", (order, how)
        assert {_rules_key(r) for r in rules} == want, order


def test_the_ucm_pin_leaves_every_other_scope_alone():
    pinned = RuleCache(rules_dir=RULES_DIR, cache_dir=None, category_dirs={"ucm": _UCM_PIN})
    plain = RuleCache(rules_dir=RULES_DIR, cache_dir=None)
    pinned._load(); plain._load()
    ucm = {s for s, sets in pinned._by_scope.items() if any("_ucm_" in k for k in sets)}
    assert len(ucm) == 10, sorted(ucm)
    for scope in set(pinned._by_scope) | set(plain._by_scope):
        if scope not in ucm:
            assert pinned._by_scope.get(scope) == plain._by_scope.get(scope), scope


def test_run_py_pins_ucm_to_v19_by_default():
    import re
    src = open(os.path.join(_REPO, "run.py"), encoding="utf-8").read()
    m = re.search(r'"--sa_ucm_rules_dir",\s*default="([^"]*)"', src)
    assert m and m.group(1) == _UCM_PIN, m and m.group(1)


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
