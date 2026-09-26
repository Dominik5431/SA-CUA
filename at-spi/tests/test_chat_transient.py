"""biba._chat must survive the provider shapes a shared free-tier endpoint actually produces.

REGRESSION THIS LOCKS DOWN. OpenRouter sometimes answers HTTP 200 with an error body; the OpenAI
SDK turns that into a ChatCompletion whose `choices` is None, and `response.choices[0]` then died
with a bare "'NoneType' object is not subscriptable" that lost the whole capture (seen live on
044_superuser.com during the v18 sweep). It is the same upstream overload the 429s report, so it
has to go through the same backoff -- and the check has to live INSIDE the retry loop, since a
check after the call returns cannot retry anything.
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
AT_SPI = os.path.dirname(HERE)
sys.path[:0] = [AT_SPI, os.path.join(AT_SPI, "labelers"), os.path.dirname(AT_SPI)]

import biba

class Resp:
    def __init__(self, choices=None, error=None):
        self.choices = choices; self.error = error
class Msg:
    def __init__(self, c): self.content = c
class Choice:
    def __init__(self, c): self.message = Msg(c); self.finish_reason = "stop"

class FakeCompletions:
    def __init__(self, seq): self.seq = list(seq); self.n = 0
    def create(self, **kw):
        r = self.seq[self.n]; self.n += 1
        if isinstance(r, Exception): raise r
        return r
class FakeClient:
    provider = "openrouter"; model = "m"
    def __init__(self, seq):
        self._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions(seq)))

biba.RATE_LIMIT_MAX_SLEEP = 0.01
ok = fails = 0
def check(n, c):
    global ok, fails
    if c: ok += 1; print(f"  PASS  {n}")
    else: fails += 1; print(f"  FAIL  {n}")

cl = FakeClient([Resp(None, {"code": 502}), Resp([Choice("hello")])])
out = biba._chat(cl, [{"role":"user","content":"hi"}], 100, 0.0, False)
check("no-choices response is retried, not fatal", out == "hello")

cl = FakeClient([Resp(None, {"code": 502})] * biba.RATE_LIMIT_RETRIES)
try:
    biba._chat(cl, [{"role":"user","content":"hi"}], 100, 0.0, False)
    check("exhausted no-choices raises", False)
except biba.TransientProviderError:
    check("exhausted no-choices raises TransientProviderError (not TypeError)", True)
except TypeError:
    check("exhausted no-choices raises TransientProviderError (not TypeError)", False)

cl = FakeClient([Resp([Choice(None)])])
check("content=None yields empty string",
      biba._chat(cl, [{"role":"user","content":"hi"}], 100, 0.0, False) == "")

print(f"\n{ok} passed, {fails} failed")
sys.exit(1 if fails else 0)
