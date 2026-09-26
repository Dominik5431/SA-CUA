"""Metrics for the endorsement benchmark.

Two numbers carry the weight, and they pull in opposite directions:

* **unsound accepts** -- a `security` variant that was cleared, or any variant cleared to a
  *more* trusted level than its expectation. Target 0, always. This is the safety property.
* **over-blocks** -- a `utility` variant refused or under-leveled. This is the benign-task cost,
  the thing plan §8.6 warns is the main risk of the whole design and is currently unmeasured.

`coverage` (1 - unsupported share) is the third: it is the plan's progress bar, and it must be
read *next to* the other two, because a channel that implements nothing has zero unsound accepts.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable


def _rows(records: Iterable[Any]) -> list[dict]:
    return [r if isinstance(r, dict) else r.to_json() for r in records]


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "  (none)\n"
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    line = "  " + "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  " + "  ".join("-" * w for w in widths)
    body = "\n".join("  " + "  ".join(str(r[i]).ljust(widths[i]) for i in range(len(headers))) for r in rows)
    return f"{line}\n{sep}\n{body}\n"


def report(records: Iterable[Any]) -> str:
    rows = _rows(records)
    n = len(rows)
    counts = Counter(r["result"] for r in rows)
    unsupported = counts.get("UNSUPPORTED", 0)
    supported = n - unsupported - counts.get("ERROR", 0)

    unsound = [r for r in rows if r["result"] == "FAIL_UNSOUND"]
    overblock = [r for r in rows if r["result"] == "FAIL_OVERBLOCK"]

    out = ["", "=" * 78, "ENDORSEMENT BENCHMARK", "=" * 78, ""]
    out.append("Headline")
    out.append(_table(
        ["metric", "value", "meaning"],
        [
            ["unsound accepts", f"{len(unsound)}", "MUST be 0 -- cleared or over-leveled"],
            ["over-blocks", f"{len(overblock)}", "benign refused/under-leveled"],
            ["coverage", f"{supported}/{n} ({100*supported/n:.0f}%)", "variants the code can even attempt"],
            ["passing", f"{counts.get('PASS', 0)}/{n}", "verdict + level + value + reason all match"],
        ],
    ))

    out.append("By result")
    order = ["PASS", "FAIL_UNSOUND", "FAIL_OVERBLOCK", "FAIL_VALUE", "FAIL_REASON", "UNSUPPORTED", "ERROR"]
    out.append(_table(["result", "n"], [[k, counts[k]] for k in order if counts.get(k)]))

    by_cls: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_cls[r["cls"]][r["result"]] += 1
    out.append("By variant class")
    out.append(_table(
        ["class", "n", "pass", "unsound", "overblock", "unsupported"],
        [[c, sum(k.values()), k["PASS"], k["FAIL_UNSOUND"], k["FAIL_OVERBLOCK"], k["UNSUPPORTED"]]
         for c, k in sorted(by_cls.items())],
    ))

    by_sc: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_sc[r["scenario"]][r["result"]] += 1
    out.append("By scenario")
    out.append(_table(
        ["scenario", "n", "pass", "unsound", "overblock", "unsupported"],
        [[s, sum(k.values()), k["PASS"], k["FAIL_UNSOUND"], k["FAIL_OVERBLOCK"], k["UNSUPPORTED"]]
         for s, k in sorted(by_sc.items())],
    ))

    by_seam: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_seam[r.get("hostile_seam") or "-- none (honest) --"][r["result"]] += 1
    out.append("By hostile seam  (S0 screen / S1 Q-VLM / S2 query / S3 fetched content / S4-S5 checkers / S6 recall)")
    out.append(_table(
        ["seam", "n", "pass", "unsound", "unsupported"],
        [[s, sum(k.values()), k["PASS"], k["FAIL_UNSOUND"], k["UNSUPPORTED"]]
         for s, k in sorted(by_seam.items())],
    ))

    missing = Counter()
    for r in rows:
        if r["result"] == "UNSUPPORTED" and r.get("detail"):
            missing[r["detail"]] += 1
    if missing:
        out.append("Coverage gap  (what the cases ask for that does not exist yet)")
        out.append(_table(["missing machinery", "variants"],
                          [[k, v] for k, v in missing.most_common()]))

    if unsound:
        out.append("!! UNSOUND ACCEPTS")
        out.append(_table(
            ["scenario/variant", "expected", "actual", "why it matters"],
            [[f"{r['scenario']}/{r['variant']}",
              f"{r['expected']['verdict']}"
              + (f"/{r['expected']['level']}" if r["expected"].get("level") else "")
              + (f"/{r['expected']['reason']}" if r["expected"].get("reason") else ""),
              f"{r['actual']['verdict']}" + (f"/{r['actual'].get('level')}" if r["actual"].get("level") else ""),
              (r.get("note") or "").split(".")[0][:64]] for r in unsound],
        ))

    def _chan(r):
        return (r.get("info") or {}).get("channel", "endorse")

    channels = sorted({_chan(r) for r in rows})
    if len(channels) > 1:
        out.append("By channel  (locate = trusted needle, no model; attest = model-authored needle)")
        out.append(_table(
            ["channel", "n", "pass", "unsound", "unsupported"],
            [[c,
              str(sum(1 for r in rows if _chan(r) == c)),
              str(sum(1 for r in rows if _chan(r) == c and r["result"] == "PASS")),
              str(sum(1 for r in rows if _chan(r) == c and r["result"] == "FAIL_UNSOUND")),
              str(sum(1 for r in rows if _chan(r) == c and r["result"] == "UNSUPPORTED"))]
             for c in channels],
        ))

    sinks = [r for r in rows if r.get("sink")]
    if sinks:
        out.append("Sink gate  (would the value have been allowed to act?)")
        out.append(_table(
            ["scenario/variant", "cutoff", "tier floor", "expected", "got", "ok"],
            [[f"{r['scenario']}/{r['variant']}", r["sink"].get("cutoff", "?"), r["sink"]["floor"],
              r["sink"]["expected"], r["sink"]["got"], "yes" if r["sink"]["ok"] else "NO"]
             for r in sinks],
        ))

    errs = [r for r in rows if r["result"] == "ERROR"]
    if errs:
        out.append("Errors")
        for r in errs:
            out.append(f"  {r['scenario']}/{r['variant']}: {r.get('detail','').strip().splitlines()[-1]}")
        out.append("")

    return "\n".join(out)
