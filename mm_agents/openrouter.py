"""OpenRouter settings shared by every agent that talks to it.

Prompt caching is ON while the trajectory prefix still grows and OFF once the history
window starts sliding. GPT-5.6 and later bill a cache WRITE at 1.25x the input price even
under OpenAI's automatic caching (openrouter.ai/docs/features/prompt-caching), so caching
only pays where a later request can read the write back, and the agents' request shape
splits an episode cleanly in two:

* Steps 1..n+1 (`n` = `--max_trajectory_length`): step k re-sends the k-1 past turns, so
  each prompt is the previous step's prompt plus a suffix -- exactly the prefix a cache
  needs. The gpt-5.6-luna runs show the hits.
* Step n+2 onwards: the window is full and drops its oldest turn every step, so the only
  thing still matching is the system message -- and that is TOO SMALL TO CACHE. OpenAI
  caches a prefix only from 1024 tokens up, and PromptAgent's system message is 621,
  UCMAgent's 582, the SA Q-LLM's 132. Measured on two pre-policy gpt-5.6-luna episodes
  with caching ON and the window sliding: cached_tokens is 0 on every call from step 9 to
  step 19, while each call still writes its whole ~16k prompt at 1.25x and reads nothing
  back. A longer window only makes those writes bigger.

  THE GENERAL TEST, for a client added later: past the cutoff, caching pays iff
      r*P + 1.25*R  <  P + R      i.e.   R < 4*(1-r)*P
  with P the byte-stable prefix, R the sliding remainder and r the cached-input price
  ratio (~0.1). Every SLIDING client here fails it by 3x or more -- HybridAgent is the
  only one whose prefix even clears the 1024 minimum (1,695 tokens against R ~= 20,000).
  But a client with a LARGE stable prefix would pass it easily, and the SA planner is
  exactly that shape: 45,582 tokens of system prompt + tool schema, which would pass at
  any window size. If the planner is ever switched from GROWING to a bounded history,
  do NOT let it fall through to the SLIDING branch -- this test, not the history shape,
  is what decides it there.

That split is about the *sliding window* the screenshot-trajectory agents re-send. The fides
planner is the other shape: `PlannerState.history` is append-only and the refreshed observation
rides on a throwaway copy of it, so its prefix never stops matching and it stays cached for the
whole episode (`history=GROWING`).

The SA planner used to be that shape too. Since its trajectory became a bounded 10-element
window (`--sa_history_window`, default `--max_trajectory_length`) its history slides like every
other agent's -- but it is the first client here for which the general test above passes anyway,
because what sits in front of the sliding part is 26.7k of system prompt and tool schema rather
than a 600-token system message. It is `WINDOWED`, and caching stays on all episode. See that
constant.

OpenAI's automatic breakpoints are the only cache OpenRouter applies unasked (Anthropic
needs `cache_control`, which nothing here sends). `"mode": "explicit"` turns them off, so a
request without `prompt_cache_breakpoint` markers is not cached at all. Providers that have
no such option ignore the field.

Every entry point must call `configure(max_trajectory_length)` at startup -- `run.py` and,
since 2026-09-20, the standalone `mm_agents/sa_agent/run.py` and `mm_agents/fides/run.py`.
One that does not is not neutral: `_window` stays None, `caching_pays_off` returns False for
EVERY history mode including GROWING, and the policy then sends `{"mode": "explicit"}`,
which switches OFF the automatic caching OpenRouter would have applied unasked. Omitting the
call disables caching rather than leaving it at its default.
"""

from typing import Any, Dict, Optional

from mm_agents import token_usage

NO_PROMPT_CACHE: Dict[str, Any] = {"prompt_cache_options": {"mode": "explicit"}}

_window: Optional[int] = None


def configure(max_trajectory_length: Optional[int]) -> None:
    """Tell the policy how many past turns each request carries (`--max_trajectory_length`)."""
    global _window
    _window = None if max_trajectory_length is None else int(max_trajectory_length)


SLIDING = "sliding"
GROWING = "growing"
ALWAYS = "always"
WINDOWED = "windowed"


def caching_pays_off(history: str = SLIDING) -> bool:
    """True while this request's prompt still extends the previous one from the same client.

    A `GROWING` history (the fides planner: `self.history` is only ever appended to, and the
    refreshed observation goes on a throwaway copy) keeps matching for the whole episode. A
    `SLIDING` one stops at step n+2, where the window is full and drops its oldest turn. A
    `WINDOWED` one does slide, but over a prefix so large that the general test passes anyway
    -- see the constant.

    The step comes from `token_usage`, which every agent loop already sets once per step.
    Calls outside the agent's step sequence -- setup work at step 0, the evaluators' LLM
    judges at step -1 -- are one-shot prompts with no prefix to hit, so they stay uncached.
    """
    if history == ALWAYS:
        return True
    if _window is None:
        return False
    step = token_usage.current_step()
    return step >= 1 and (history in (GROWING, WINDOWED) or step <= _window + 1)


def prompt_cache_extra_body(history: str = SLIDING) -> Dict[str, Any]:
    """The `extra_body` fragment that applies the policy to one request."""
    return {} if caching_pays_off(history) else NO_PROMPT_CACHE


def apply_prompt_cache_policy(client, history: str = SLIDING):
    """Make every `client.chat.completions.create(...)` follow the policy; returns `client`.

    `history` says how this client builds its prompt -- `SLIDING` (the default: a window of
    the last `--max_trajectory_length` turns), `GROWING` (append-only, so caching stays on
    past step n+1), or `ALWAYS` (not an agent loop; every request is cacheable).

    The policy is read per request, not here: a client built once at episode start still
    switches caching off at step n+2. The class method is looked up at call time for the
    same reason -- token_usage patches it once per process in run_single_example, i.e.
    after the agents have built their clients, and a method bound now would skip the
    accounting.
    """
    completions = client.chat.completions

    def create(*args, extra_body=None, **kwargs):
        return type(completions).create(
            completions, *args,
            extra_body={**prompt_cache_extra_body(history), **(extra_body or {})}, **kwargs)

    completions.create = create
    return client
