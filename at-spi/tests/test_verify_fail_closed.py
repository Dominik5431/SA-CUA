#!/usr/bin/env python3
"""The note audit must fail CLOSED when the auditor is unreachable.

REGRESSION THIS LOCKS DOWN. `verify()` used to infer "this sample vouched for rule n" from
"this sample did not drop rule n":

    dropped_here = {d["index"] for d in dropped_k}
    judged |= (audited - dropped_here)

That inference is sound only when the call completed. `_verify_once` returns `(rules, [])` on a
transport failure, so an audit request that never happened dropped nothing and was therefore
read as a vouch for EVERY audited rule. Three rate-limited samples in a row then dropped nothing,
logged nothing (the summary line is quiet when `dropped` is empty and verbose is off), and the
audit silently became a no-op -- on precisely the free-tier endpoints where 429s are routine.

The fix is that `_verify_once` reports what it actually RULED on, and `verify()` unions that
instead of inferring it. Everything here is about that distinction, so keep the failing-call
cases even if the parsing cases below are refactored away.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AT_SPI = os.path.dirname(HERE)
sys.path[:0] = [AT_SPI, os.path.join(AT_SPI, "labelers"), os.path.dirname(AT_SPI)]

import biba
import verify_notes as vn

RULES = [{"note": "rule A"}, {"note": "rule B"}]


def _row(text):
    return {"text": text, "capacity": None, "role": "label",
            "attr_class": "", "bbox": [0, 0, 10, 10]}


TARGETS = [(0, RULES[0], [_row("row1")]), (1, RULES[1], [_row("row2")])]
VOUCH_BOTH = '[{"index":0,"holds":true,"failed":null},{"index":1,"holds":true,"failed":null}]'

_ok, _fail = 0, 0


def check(name, cond):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}")


def run():
    vn.inheriting_rules = lambda fu, rules, sp: list(TARGETS)
    real_chat = biba._chat
    try:
        def always_fails(*_a, **_k):
            raise RuntimeError("Error code: 429 - rate-limited upstream")

        biba._chat = always_fails
        kept, dropped = vn.verify(None, {}, RULES, "m", None, samples=3, verbose=False)
        check("every sample fails -> nothing survives (audit is not a no-op)", kept == [])
        check("every sample fails -> both rules reported as dropped", len(dropped) == 2)
        check("every sample fails -> the reason names the missing verdict",
              all("no verdict" in d["reason"] for d in dropped))

        state = {"n": 0}

        def first_fails(*_a, **_k):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("Error code: 429 - rate-limited upstream")
            return VOUCH_BOTH

        biba._chat = first_fails
        kept, dropped = vn.verify(None, {}, RULES, "m", None, samples=3, verbose=False)
        check("one call fails, two vouch -> both rules survive",
              len(kept) == 2 and dropped == [])

        state["n"] = 0

        def second_condemns(*_a, **_k):
            state["n"] += 1
            if state["n"] == 2:
                return ('[{"index":0,"holds":false,"failed":"shows free text",'
                        '"reason":"bad"},{"index":1,"holds":true,"failed":null}]')
            return VOUCH_BOTH

        biba._chat = second_condemns
        kept, dropped = vn.verify(None, {}, RULES, "m", None, samples=3, verbose=False)
        check("one of three condemns -> that rule is dropped",
              len(kept) == 1 and kept[0]["note"] == "rule B")

        state["n"] = 0

        def first_garbage(*_a, **_k):
            state["n"] += 1
            return "I am not JSON" if state["n"] == 1 else VOUCH_BOTH

        biba._chat = first_garbage
        kept, dropped = vn.verify(None, {}, RULES, "m", None, samples=3, verbose=False)
        check("one unparseable sample, two vouch -> both rules survive",
              len(kept) == 2 and dropped == [])
    finally:
        biba._chat = real_chat

    print(f"\n{_ok}/{_ok + _fail} passed, {_fail} failure(s)")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(run())
