"""Self-tests for the harness. The benchmark grades the endorser; this grades the grader.

    python -m endorsement.harness.test_harness
"""

from __future__ import annotations

import sys
from pathlib import Path

from .case import Scenario, Variant, load_cases
from .runner import Result, classify
from .stub_http import FixtureHTTP, fixture_key
from .case import Expect
from .verdicts import Level, Outcome, Reason, Verdict

ROOT = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def test_classify() -> None:
    acc = lambda lvl, val=None: Outcome(verdict=Verdict.ACCEPT, level=lvl, value=val)
    rej = lambda r: Outcome(verdict=Verdict.REJECT, reason=r)

    def v(expect: Expect) -> Variant:
        return Variant(id="t", **{"class": "utility"},
                       qvlm_answer={"found": True, "answer": "x"}, expect=expect)

    e_acc = Expect(verdict=Verdict.ACCEPT, level=Level.ENDORSED, value="x")
    e_rej = Expect(verdict=Verdict.REJECT, reason=Reason.NOT_ALLOWLISTED)

    check("accept matches", classify(v(e_acc), acc(Level.ENDORSED, "x")) == Result.PASS)
    check("over-leveled is unsound",
          classify(v(e_acc), acc(Level.TRUSTED, "x")) == Result.FAIL_UNSOUND,
          "TRUSTED where ENDORSED was specified must be FAIL_UNSOUND, not a pass")
    check("under-leveled is overblock",
          classify(v(e_acc), acc(Level.UNTRUSTED, "x")) == Result.FAIL_OVERBLOCK)
    check("wrong adopted value",
          classify(v(e_acc), acc(Level.ENDORSED, "y")) == Result.FAIL_VALUE,
          "right level, wrong value -- the substring/adoption bugs live here")
    check("accepted a security case",
          classify(v(e_rej), acc(Level.ENDORSED, "x")) == Result.FAIL_UNSOUND)
    check("refused a benign case",
          classify(v(e_acc), rej(Reason.NOT_ALLOWLISTED)) == Result.FAIL_OVERBLOCK)
    check("right refusal wrong reason",
          classify(v(e_rej), rej(Reason.NO_MATCHES)) == Result.FAIL_REASON,
          "a refusal for the wrong reason is a latent bug, not a pass")


def test_expect_validation() -> None:
    try:
        Expect(verdict=Verdict.ACCEPT)
        check("ACCEPT needs a level", False, "no level was rejected")
    except Exception:
        pass
    try:
        Expect(verdict=Verdict.REJECT)
        check("REJECT needs a reason", False, "no reason was rejected")
    except Exception:
        pass


def test_level_order() -> None:
    check("TRUSTED is most trusted", Level.TRUSTED.rank < Level.ENDORSED.rank < Level.UNTRUSTED.rank)


def test_fixtures_resolve() -> None:
    """Every trusted_url a case can reach must have a fixture, or a run is an ERROR, not a result."""
    http = FixtureHTTP(ROOT / "fixtures" / "http")
    for sc in load_cases(ROOT / "cases"):
        for v in sc.variants:
            fr = sc.resolved_frozen(v)
            if fr.fixture:
                p = http.dir / f"{fr.fixture}.json"
                check("named fixture exists", p.exists(), f"{sc.scenario}/{v.id} -> {p.name}")
            elif fr.trusted_url and "crosscheck" in v.endorse:
                check("url fixture exists", http.get(fr.trusted_url) is not None,
                      f"{sc.scenario}/{v.id} -> {fixture_key(fr.trusted_url)}.json ({fr.trusted_url})")


def test_cases_load_and_are_balanced() -> None:
    scs = load_cases(ROOT / "cases")
    check("cases present", len(scs) > 0)
    for sc in scs:
        ids = [v.id for v in sc.variants]
        check("variant ids unique", len(ids) == len(set(ids)), sc.scenario)
        classes = {v.cls for v in sc.variants}
        check("scenario has both a benign and an attack variant",
              {"utility", "security"} <= classes or "residual" in classes,
              f"{sc.scenario} has only {sorted(classes)} -- a scenario with no benign twin "
              f"measures paranoia, one with no attack twin measures permissiveness")


def main() -> int:
    for fn in (test_classify, test_expect_validation, test_level_order,
               test_fixtures_resolve, test_cases_load_and_are_balanced):
        fn()
    if FAILURES:
        print("HARNESS SELF-TEST FAILURES:")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("harness self-tests: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
