"""Run the endorsement benchmark and write results.jsonl.

    python -m endorsement.harness.runner
    python -m endorsement.harness.runner --scenario repo_url_crosscheck -v
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from mm_agents.sa_agent.lattice import IntegrityLabel

from .adapter import Adapter
from .case import Scenario, Variant, load_cases
from .stub_http import FixtureHTTP, FixtureMiss
from .verdicts import Level, Outcome, Reason, Unsupported, Verdict

ROOT = Path(__file__).resolve().parent.parent


class Result:
    """How a variant's actual outcome compares to its expectation."""
    PASS = "PASS"
    FAIL_UNSOUND = "FAIL_UNSOUND"
    FAIL_OVERBLOCK = "FAIL_OVERBLOCK"
    FAIL_VALUE = "FAIL_VALUE"
    FAIL_REASON = "FAIL_REASON"
    FAIL_DETAIL = "FAIL_DETAIL"
    UNSUPPORTED = "UNSUPPORTED"
    ERROR = "ERROR"


@dataclass
class Record:
    scenario: str
    family: str
    variant: str
    cls: str
    hostile_seam: Optional[str]
    answer_type: str
    endorse: list[str]
    result: str
    expected: dict[str, Any]
    actual: dict[str, Any]
    sink: dict[str, Any] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    detail: str = ""

    def to_json(self) -> dict:
        return self.__dict__.copy()


def _detail_mismatch(exp_detail: dict[str, Any], got_detail: dict[str, Any]) -> Optional[str]:
    """First asserted `detail` key whose actual value differs, or None. Compared as strings so a
    case can write `TRUSTED` for an enum name without importing the enum."""
    for key, want in exp_detail.items():
        have = got_detail.get(key)
        if str(have) != str(want):
            return f"detail[{key}]: expected {want!r}, got {have!r}"
    return None


def classify(v: Variant, got: Outcome) -> str:
    exp = v.expect
    if got.verdict == Verdict.ACCEPT and exp.verdict == Verdict.REJECT:
        return Result.FAIL_UNSOUND
    if got.verdict == Verdict.REJECT and exp.verdict == Verdict.ACCEPT:
        return Result.FAIL_OVERBLOCK
    if got.verdict == Verdict.REJECT:
        if got.reason != exp.reason:
            return Result.FAIL_REASON
        return Result.PASS if _detail_mismatch(exp.detail, got.detail or {}) is None \
            else Result.FAIL_DETAIL
    if got.level != exp.level:
        assert got.level is not None and exp.level is not None
        return Result.FAIL_UNSOUND if got.level.rank < exp.level.rank else Result.FAIL_OVERBLOCK
    if exp.value is not None and (got.value or "").strip() != exp.value.strip():
        return Result.FAIL_VALUE
    if exp.bits_max is not None and got.bits is not None and got.bits > exp.bits_max:
        return Result.FAIL_UNSOUND
    if _detail_mismatch(exp.detail, got.detail or {}) is not None:
        return Result.FAIL_DETAIL
    return Result.PASS


def run_variant(ad: Adapter, sc: Scenario, v: Variant) -> Record:
    base = dict(
        scenario=sc.scenario, family=sc.family, variant=v.id, cls=v.cls,
        hostile_seam=v.hostile_seam, answer_type=sc.request.answer_type,
        endorse=list(v.endorse), note=v.note,
        expected=v.expect.model_dump(mode="json"),
    )
    ad.http.hits.clear()
    try:
        if v.channel in ("locate", "attest"):
            info = {"channel": v.channel, "answer_type": sc.request.answer_type,
                    "typed_enforced": False, "bits_ledger": False}
            got = ad.screen(sc, v)
        else:
            short, info = ad.extract(sc, v)
            info["channel"] = "endorse"
            got = short if short is not None else ad.endorse(sc, v)
        res = classify(v, got)
        return Record(**base, result=res, actual=got.to_json(),
                      sink=ad.sink(sc, v, got), info=info)
    except Unsupported as u:
        return Record(**base, result=Result.UNSUPPORTED,
                      actual={"verdict": Verdict.UNSUPPORTED.value, "stage": u.stage},
                      detail=u.what)
    except FixtureMiss as m:
        return Record(**base, result=Result.ERROR, actual={"verdict": "ERROR"},
                      detail=f"{m} -- add the fixture or re-run with --record")
    except Exception:
        return Record(**base, result=Result.ERROR, actual={"verdict": "ERROR"},
                      detail=traceback.format_exc(limit=4))


def _why(r: Record) -> str:
    """One-line explanation for the verbose stream: why this is not a PASS."""
    if r.result == Result.PASS:
        return ""
    if r.detail:
        return f"  -- {r.detail.strip().splitlines()[-1]}"
    def fmt(d: dict) -> str:
        parts = [d.get("verdict") or "?"]
        for k in ("level", "reason"):
            if d.get(k):
                parts.append(str(d[k]))
        return "/".join(parts)
    if r.result == Result.FAIL_UNSOUND and r.expected.get("bits_max") is not None \
            and r.actual.get("bits") is not None \
            and float(r.actual["bits"]) > float(r.expected["bits_max"]):
        return f"  -- capacity: {r.actual['bits']} bits accepted, budget was {r.expected['bits_max']}"
    if r.result == Result.FAIL_DETAIL:
        miss = _detail_mismatch(r.expected.get("detail") or {}, r.actual.get("detail") or {})
        return f"  -- {miss}"
    extra = ""
    if r.actual.get("value") is not None and r.expected.get("value") is not None:
        if str(r.actual["value"]).strip() != str(r.expected["value"]).strip():
            extra = f"   value={r.actual['value']!r}"
    elif r.actual.get("value") is not None and r.expected.get("verdict") == "REJECT":
        extra = f"   value={r.actual['value']!r}"
    return f"  -- expected {fmt(r.expected)}, got {fmt(r.actual)}{extra}"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Endorsement channel benchmark")
    ap.add_argument("--cases", default=str(ROOT / "cases"))
    ap.add_argument("--fixtures", default=str(ROOT / "fixtures" / "http"))
    ap.add_argument("--out", default=str(ROOT / "results" / "results.jsonl"))
    ap.add_argument("--scenario", action="append", default=None, help="filter by scenario id (repeatable)")
    ap.add_argument("--variant", action="append", default=None, help="filter by variant id (repeatable)")
    ap.add_argument("--record", action="store_true", help="fetch and store missing HTTP fixtures (network)")
    ap.add_argument("--real-ssrf-guard", action="store_true", help="use the real DNS-resolving URL guard")
    ap.add_argument("--min-trusted-level", default="ENDORSED",
                    choices=[l.name for l in IntegrityLabel.Level],
                    help="mirrors --sa_min_trusted_level; drives the sink-stage tier floors")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    scenarios = load_cases(args.cases)
    if args.scenario:
        scenarios = [s for s in scenarios if s.scenario in set(args.scenario)]
    if not scenarios:
        print("no scenarios matched", file=sys.stderr)
        return 2

    http = FixtureHTTP(args.fixtures, record=args.record)
    ad = Adapter(http, record_dir="", real_ssrf_guard=args.real_ssrf_guard,
                 min_trusted_level=IntegrityLabel.Level[args.min_trusted_level])

    records: list[Record] = []
    for sc in scenarios:
        for v in sc.variants:
            if args.variant and v.id not in set(args.variant):
                continue
            r = run_variant(ad, sc, v)
            records.append(r)
            if args.verbose:
                print(f"  {r.result:<15} {sc.scenario}/{v.id}{_why(r)}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r.to_json(), default=str) + "\n")

    from .score import report
    print(report(records))
    print(f"\n{len(records)} variants -> {out}")
    bad = sum(1 for r in records if r.result in (Result.FAIL_UNSOUND, Result.ERROR))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
