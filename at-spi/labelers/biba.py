#!/usr/bin/env python3
"""
biba.py -- helper library: the LLM client wrapper and the ancestor-chain utilities that the
selector-rule pipeline is built on.

THIS USED TO BE A LABELER. It ran an arXiv:2607.08147-inspired critical-path derivation over
extraction.py's interactable-only rows and wrote labels/biba.json. That labeler went with the
gen-1 universe; what survives is the part nothing else duplicates and three other modules import:

    _chat(...)                  the retry/empty-content-tolerant chat call, used by
                                selector_propose.py for every rule proposal
    _trim_to_content_root(...)  cuts an ancestor chain down to the page's content root, used by
                                selector_propose.py, selector_rules.py and verify_selector_rules.py

The prompt-construction and batching helpers below (_build_batch_prompt, _label_batch,
_resolve_control_group, ...) are kept with them: they are the calibrated wording and the
group/batch strategy the surviving callers were tuned against, and re-deriving them from a git
history that no longer has a runnable entry point would be worse than carrying them.

Not run directly any more -- there is no main().
"""
import argparse
import glob
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

_AT_SPI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_AT_SPI_DIR))
sys.path.insert(0, _AT_SPI_DIR)

from mm_agents.sa_agent.llm import build_client
from critical_path import build_ancestor_index, critical_path_for

XML_DIR = os.path.join(_AT_SPI_DIR, "xml")
MANIFEST_PATH = os.path.join(_AT_SPI_DIR, "manifest.json")

_SYSTEM_PROMPT_BODY = """\
You are determining the trust boundary for one or more elements on a webpage that share the SAME \
ancestor chain -- the sequence of container elements from the page's root down to their shared \
immediate parent. You are NOT shown the rest of the page, any other subtree, or any content \
outside this shared chain and the specific elements listed below it.

The test: TRUSTED means only the website's own owner/operator controls what an element's text \
says -- no visitor, commenter, seller, uploader, reviewer, or other outside party could have \
influenced it, even indirectly. UNTRUSTED means anyone other than the owner/operator could have.

You are given the shared ancestor chain, root first, each with its role, accessible name (if \
any), and any id/class attributes exposed by the page, followed by a list of elements that all \
sit directly beneath it. Look for a STRUCTURAL CUE anywhere in the ancestor chain: an ancestor \
whose role, name, id, or class indicates it is a container for outside/user-submitted content \
(e.g. a heading or landmark named "Reviews"/"Comments"/"Answers", a class or id like \
"review-card"/"comment-thread"/"user-content"/"listing"). If such a cue exists, report the \
shallowest (smallest-index) ancestor position where it first appears -- this ONE cue_depth \
describes the shared chain, not any single element. If no such cue exists anywhere in the chain, \
report cue_depth as null.

An ancestor tagged "size=page-spanning" occupies nearly the entire page (its own bounding box is \
far larger than the page's viewport) -- this means it is a whole-page wrapper (e.g. the page's \
<body>-level container), not a per-item content boundary, no matter what its name/id/class reads \
like. NEVER report such an ancestor's index as cue_depth. If the only apparent cue in the chain \
carries this tag, keep looking deeper in the chain for a smaller, more specific ancestor that \
actually scopes to the outside content; if none exists, treat the chain as having no cue at all.

An element may carry a "value_shape" hint: "computed" means this exact text has been verified to \
recur identically across other structurally-equivalent sibling instances elsewhere on the page \
(e.g. a vote-count badge, or a fixed control label like "Reply"/"Save"/"Share" that reads the same \
on every post) -- this is platform-generated chrome, not user-authored content, so it should \
default to TRUSTED even when an ancestor cue is present, unless something else about it (not \
merely sitting inside a cued container) gives you an affirmative reason to distrust it.

An element may separately carry a "same_text_elsewhere_on_page" hint (true/absent): this text also \
appears verbatim elsewhere on the page, WITHOUT the stricter cross-instance verification \
"value_shape=computed" requires. Treat this as weak, advisory evidence only -- suggestive of a \
fixed platform control when combined with a short, closed-vocabulary label and a role like \
button/toggle, but never sufficient by itself to override a genuine ancestor cue. Two identical \
posts from the same outside party would trigger this same signal, so it must not be treated as \
proof of platform origin the way "value_shape=computed" is.

An element may separately carry a "control_shape" hint (true/absent): its own role is a standard \
interactive control (toggle button, push button, combo box, checkbox, or radio button) and its \
own text is short (a handful of words at most) -- e.g. an "Up vote"/"Save"/"Follow"/"Copy" button \
attached to someone else's post. A control like this is rendered by the SITE'S OWN UI template \
regardless of whose item it sits on -- the poster cannot rename another user's vote/save/follow \
button, even though the ancestor cue correctly marks the surrounding CONTENT as untrusted. \
Treat control_shape=true the same way as value_shape=computed: default to TRUSTED for this \
element even under a cue, unless the shown text itself is unusually long, reads as free-form \
prose, or otherwise doesn't look like a fixed platform action label -- this hint concerns ONLY \
this element's own fixed label, never any adjacent free-text content sitting in the same \
container.

An element's own text may be shown as "[REDACTED]" instead of its real value. This means the text \
itself may be user-influenceable, so it has been withheld from you -- decide that element's \
verdict using ONLY its role and the ancestor chain, never by guessing what the redacted text says. \
A redacted element inside a cue's scope is UNTRUSTED; outside any cue, judge from role alone \
(TRUSTED only for a role that is clearly the site's own structural chrome, e.g. a frame/document \
title; UNTRUSTED otherwise, since you cannot verify unseen text is safe).

Then judge EACH element independently: if a cue was found, an element is UNTRUSTED regardless of \
its own text UNLESS its own value_shape=computed or control_shape hint overrides that (see \
above); if no cue was found, judge purely from that element's own role and text: TRUSTED only if \
it is clearly the site's own structural/editorial content (navigation, static UI labels, \
editorial prose); UNTRUSTED otherwise. Elements sharing the same ancestor chain can still get \
DIFFERENT verdicts from each other -- judge each on its own merits, never assume they all match.
"""

_SCHEMA_WITH_RATIONALE = """\
Return ONLY a JSON object of this exact shape, nothing else -- no markdown fences, no commentary. \
Never quote or repeat redacted text in your rationale, and give ONE shared rationale about the \
ancestor chain's cue determination, not one per element:
{"cue_depth": <integer ancestor index, or null if no cue was found>, "verdicts": {"<index>": "trusted" | "untrusted", ...}, "rationale": "<one short sentence>"}
Example with 2 elements: {"cue_depth": 4, "verdicts": {"0": "untrusted", "1": "trusted"}, "rationale": "..."}
Include exactly one entry in "verdicts" for every element index shown below.
"""

_SCHEMA_NO_RATIONALE = """\
Return ONLY a JSON object of this exact shape, nothing else -- no markdown fences, no commentary, \
no rationale or explanation of any kind:
{"cue_depth": <integer ancestor index, or null if no cue was found>, "verdicts": {"<index>": "trusted" | "untrusted", ...}}
Example with 2 elements: {"cue_depth": 4, "verdicts": {"0": "untrusted", "1": "trusted"}}
Include exactly one entry in "verdicts" for every element index shown below.
"""


def _build_system_prompt(include_rationale: bool) -> str:
    return _SYSTEM_PROMPT_BODY + "\n" + (_SCHEMA_WITH_RATIONALE if include_rationale else _SCHEMA_NO_RATIONALE)


_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _elem_index_of(filename: str) -> int:
    m = re.match(r"^(\d+)_", filename)
    return int(m.group(1)) if m else -1


_CONTROL_ROLES = frozenset({
    "toggle button", "push button", "combo box", "checkbox", "radio button",
})
_CONTROL_SHAPE_MAX_WORDS = 5
_CONTROL_SHAPE_MAX_CHARS = 40


def _is_control_shape(role: str, text: str) -> bool:
    """True for a short-labeled standard interactive control (see _CONTROL_ROLES). Computed
    purely in Python from role+text length -- never from a model judgment -- so it's known before
    deciding whether to redact, batch, or consult the cache at all."""
    text = (text or "").strip()
    if role not in _CONTROL_ROLES or not text:
        return False
    return len(text) <= _CONTROL_SHAPE_MAX_CHARS and len(text.split()) <= _CONTROL_SHAPE_MAX_WORDS


def _format_ancestor(i: int, sig: dict) -> str:
    bits = [f"role={sig['role']}"]
    if sig.get("name"):
        bits.append(f'name="{sig["name"]}"')
    if sig.get("id"):
        bits.append(f"id={sig['id']}")
    if sig.get("class"):
        bits.append(f"class={sig['class']}")
    if sig.get("oversized"):
        bits.append("size=page-spanning")
    return f"[{i}] {' '.join(bits)}"


def _build_batch_prompt(path: list, elements: list) -> str:
    """elements: ordered list of per-element dicts (role, shown_text, value_shape,
    same_text_elsewhere, control_shape) that all share ONE ancestor chain -- literal DOM siblings,
    or a single element for a batch of 1 (the common case when no sibling shares its exact path)."""
    ancestor_lines = "\n".join(_format_ancestor(i, sig) for i, sig in enumerate(path))
    elem_lines = []
    for i, el in enumerate(elements):
        value_shape_bit = f" value_shape={el['value_shape']}" if el["value_shape"] else ""
        same_text_bit = " same_text_elsewhere_on_page=true" if el["same_text_elsewhere"] else ""
        control_shape_bit = " control_shape=true" if el["control_shape"] else ""
        elem_lines.append(
            f'[{i}] role={el["role"]}{value_shape_bit}{same_text_bit}{control_shape_bit} '
            f'text="{el["shown_text"]}"'
        )
    elements_block = "\n".join(elem_lines)
    return (
        f"Ancestor chain (root first, SHARED by every element below):\n"
        f"{ancestor_lines or '(none -- elements sit at the document root)'}\n\n"
        f"Elements (judge each independently -- they share the ancestor chain above only, never "
        f"each other's text):\n{elements_block}\n\n"
        f"Label every element listed above."
    )


def _parse_batch_response(content: str, expected: list) -> "dict | None":
    content = (content or "").strip()
    fence = _FENCE_RE.match(content)
    if fence:
        content = fence.group(1).strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    cue_depth = data.get("cue_depth")
    if not isinstance(cue_depth, int):
        cue_depth = None
    verdicts_raw = data.get("verdicts")
    if not isinstance(verdicts_raw, dict):
        return None
    verdicts = {}
    for k, v in verdicts_raw.items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        v = str(v).strip().lower()
        if v in ("trusted", "untrusted"):
            verdicts[idx] = v
    if any(i not in verdicts for i in expected):
        return None
    rationale = data.get("rationale")
    return {"cue_depth": cue_depth, "verdicts": verdicts, "rationale": str(rationale) if rationale else None}


CONTEXT_SAFETY_TOKENS = 512
MIN_ANSWER_TOKENS = 256
_CHARS_PER_TOKEN = 3.9


def _clamp_to_context(client, messages: list, max_tokens: int) -> int:
    """Shrink max_tokens so input + output fits the endpoint's window. Returns the budget."""
    limit = getattr(client, "context_limit", None)
    if not limit:
        return max_tokens
    est_in = sum(len(str(m.get("content") or "")) for m in messages) / _CHARS_PER_TOKEN
    room = int(limit - est_in - CONTEXT_SAFETY_TOKENS)
    if room < MIN_ANSWER_TOKENS:
        raise ValueError(
            f"context_limit={limit} leaves only {room} output tokens after ~{est_in:,.0f} tokens "
            f"of input; need >= {MIN_ANSWER_TOKENS}. This capture does not fit this endpoint."
        )
    return min(max_tokens, room)


class TransientProviderError(RuntimeError):
    """A provider hiccup that is worth retrying but arrived without an HTTP status to prove it.

    OpenRouter sometimes answers 200 with an error body instead of an error status; the OpenAI SDK
    parses that into a ChatCompletion whose `choices` is None, and the old `response.choices[0]`
    died on it with a bare "'NoneType' object is not subscriptable" that killed the whole capture.
    It is the same overload the 429s report, so it belongs in the same backoff."""


RATE_LIMIT_RETRIES = 8
RATE_LIMIT_MAX_SLEEP = 120


def _retry_after(exc) -> "float | None":
    """Seconds the provider asked us to wait, if it said. None if this is not a retryable error."""
    if isinstance(exc, TransientProviderError):
        return 0.0
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status not in (408, 429, 500, 502, 503, 504):
        return None
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        meta = (body.get("error") or {}).get("metadata") or {}
        for key in ("retry_after_seconds", "retry_after"):
            try:
                if meta.get(key) is not None:
                    return float(meta[key])
            except (TypeError, ValueError):
                pass
    return 0.0


def _create_with_backoff(client, **kw):
    """A shared free-tier pool 429s constantly and the error is transient by construction.
    Without this every such blip loses the whole capture, and the runner's retry loop re-runs the
    sweep rather than the one call. Non-retryable errors (400 context overflow, 401) still raise
    immediately -- retrying those would just bury the real message."""
    for attempt in range(RATE_LIMIT_RETRIES):
        try:
            response = client._client.chat.completions.create(**kw)
            if not getattr(response, "choices", None):
                err = (getattr(response, "error", None)
                       or (getattr(response, "model_extra", None) or {}).get("error"))
                raise TransientProviderError(
                    f"provider returned no choices (error={str(err)[:200]})")
            return response
        except Exception as e:
            hinted = _retry_after(e)
            if hinted is None or attempt == RATE_LIMIT_RETRIES - 1:
                raise
            delay = min(max(hinted, 2.0 * (2 ** attempt)), RATE_LIMIT_MAX_SLEEP)
            print(f"    rate-limited/transient ({type(e).__name__}); retrying in {delay:.0f}s "
                  f"(attempt {attempt + 1}/{RATE_LIMIT_RETRIES})", file=sys.stderr)
            time.sleep(delay)


def _chat(client, messages: list, max_tokens: int, temperature: float, enable_thinking: bool) -> str:
    """Same vllm thinking-mode handling as labelers/llm.py -- see that module's _chat docstring
    for the reasoning-field/token-budget diagnosis this is based on."""
    kwargs = {}
    if client.provider == "vllm":
        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": enable_thinking}}
    budget = _clamp_to_context(client, messages, max_tokens)
    response = _create_with_backoff(
        client, model=client.model, messages=messages, max_tokens=budget,
        temperature=temperature, **kwargs,
    )
    choice = response.choices[0]
    content = (getattr(choice, "message", None) and choice.message.content) or ""
    if getattr(choice, "finish_reason", None) == "length":
        reasoning = getattr(choice.message, "reasoning", None) or ""
        print(f"    WARNING: response hit the {budget:,}-token output budget (finish_reason="
              f"length); {len(content):,} chars of answer, {len(reasoning):,} chars of reasoning. "
              f"An empty answer here means TRUNCATED, not 'nothing qualifies'.", file=sys.stderr)
    return content


def _label_batch(client, path: list, elements: list, system_prompt: str,
                  max_tokens: int, temperature: float, retries: int, enable_thinking: bool) -> "dict | None":
    """One LLM call judging every element in `elements` against the single shared `path`. Used
    uniformly for both a real batch (literal DOM siblings) and a lone element (batch of 1) -- the
    prompt/response shape doesn't change, only how many element lines/verdicts it carries."""
    prompt = _build_batch_prompt(path, elements)
    expected = list(range(len(elements)))
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]
    last_vetoed = None
    for attempt in range(retries):
        try:
            content = _chat(client, messages, max_tokens, temperature, enable_thinking)
        except Exception as e:
            print(f"    call failed (attempt {attempt + 1}/{retries}): {e}", file=sys.stderr)
            time.sleep(2)
            continue
        parsed = _parse_batch_response(content, expected)
        if parsed is None:
            print(f"    attempt {attempt + 1}/{retries}: unparseable/incomplete response for "
                  f"{len(elements)} element(s). Raw response head: {content[:300]!r}", file=sys.stderr)
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": (
                "That response was not valid JSON of the required shape, or was missing a verdict "
                "for some element index. Return ONLY the JSON object described above, with a "
                "verdict for every element index shown."
            )})
            continue

        cue_depth = parsed["cue_depth"]
        if cue_depth is not None and 0 <= cue_depth < len(path) and path[cue_depth].get("oversized"):
            print(f"    attempt {attempt + 1}/{retries}: cue_depth={cue_depth} is a page-spanning "
                  f"ancestor, rejecting", file=sys.stderr)
            last_vetoed = parsed
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": (
                f"Ancestor [{cue_depth}] is tagged size=page-spanning -- it is a whole-page wrapper, "
                "not a valid content boundary. Look deeper in the chain for a smaller, more specific "
                "ancestor, or report cue_depth: null if none exists. Return ONLY the JSON object "
                "described above."
            )})
            continue

        return parsed

    if last_vetoed is not None:
        note = " (cue ancestor was page-spanning; retries exhausted, treating as no cue found)"
        last_vetoed = dict(last_vetoed)
        last_vetoed["cue_depth"] = None
        last_vetoed["rationale"] = (last_vetoed.get("rationale") or "") + note
        return last_vetoed
    return None


def _trim_to_content_root(path: list) -> list:
    """Drops ancestors at/above the page's own content root (the first 'document web' node) --
    pure browser-window chrome (desktop frame, application, browser frame/panel, tab strip,
    toolbar...) that sits above every page's actual content and is IDENTICAL, node-for-node,
    across literally every element extracted from the same capture. It can never legitimately be
    a per-item trust cue, so leaving it in wastes prompt tokens on every call and -- much worse --
    turns the cache claim/pending protocol into near-total serialization: the first row to run
    claims these universally-shared prefixes as pending, and every other row in the file then
    blocks waiting for that one call, since their paths all start with the same node identity too
    (confirmed empirically: a 16-way concurrent run of 002_stackoverflow.com.xml sat at a single
    open connection for 9+ minutes, all other threads parked in cache_lock.wait()).
    Falls back to the path unchanged if no 'document web' ancestor is present."""
    for i, sig in enumerate(path):
        if sig.get("role") == "document web":
            return path[i + 1:]
    return path


def _claimable_depths(path: list) -> list:
    """Depths worth claiming/checking in the cache: excludes any 'oversized' (page-spanning)
    ancestor, since _label_batch's own veto/retry logic never lets the model report cue_depth
    there -- claiming one guarantees contention (it's shared by a large fraction of the page's
    elements) with zero chance a matching cache entry is ever written."""
    return [d for d in range(len(path)) if not path[d].get("oversized")]


def _ancestor_key(path: list, upto: int) -> tuple:
    """Hashable identity for 'this exact ancestor node, reached via this exact prefix' -- the
    cache key a resolved cue is stored under, and what later elements' paths are checked against
    prefix-by-prefix to see if they fall under an already-resolved subtree.

    Deliberately hashes only role/id/class/name -- NEVER fold in the purely geometric `oversized`
    flag (or any future signature field) here: geometry can jitter slightly across structurally-
    identical elements, which would break cache-key stability."""
    return tuple((sig["role"], sig.get("id"), sig.get("class"), sig.get("name")) for sig in path[:upto + 1])


def _group_key(path: list) -> tuple:
    """Hashable identity for 'these elements are literal DOM siblings' -- an exact match of the
    WHOLE ancestor chain (root through immediate parent). Two elements sharing this key have the
    identical parent node, so batching their judgment into one call changes nothing about what
    ancestor-chain content becomes visible to the model versus resolving them one at a time --
    it's the same information either way, just paid for once. Uses the same signature fields as
    _ancestor_key (role/id/class/name, no geometry) for consistency; two structurally-anonymous
    ancestors (e.g. two unlabeled <li> wrappers) collapsing into the same group is intentional,
    not a bug -- an ancestor with no name/id/class carries no cue information beyond what's already
    shared further up the chain, so treating them as equivalent for grouping loses nothing."""
    return tuple((sig["role"], sig.get("id"), sig.get("class"), sig.get("name")) for sig in path)


_PENDING = object()
_MAX_BATCH_SIZE = 1


def _element_input(row: dict, own_text: str, control_shape: bool, same_text_counts: "Counter") -> dict:
    redact = bool(row["region_id"]) and not control_shape
    same_text_elsewhere = bool(own_text) and same_text_counts.get(own_text, 0) >= 2
    return {
        "role": row["role"],
        "shown_text": "[REDACTED]" if redact else row["text"],
        "value_shape": row["region_value_shape"],
        "same_text_elsewhere": same_text_elsewhere,
        "control_shape": control_shape,
    }


def _output_row(row: dict, verdict: str, cue_depth, rationale) -> dict:
    return {
        "file": row["file"], "elem_index": row["elem_index"], "category": row["category"],
        "role": row["role"], "region_id": row["region_id"], "region_role": row["region_role"],
        "region_value_shape": row["region_value_shape"], "text": row["text"],
        "current_level": row["current_level"], "current_untrusted": row["current_untrusted"],
        "verdict": verdict,
        "confidence": "high" if cue_depth is not None else ("medium" if verdict != "error" else None),
        "rationale": rationale,
        "mismatch": row["current_untrusted"] and verdict == "trusted",
    }


def _resolve_control_group(client, path: list, rows: list, counters: dict, counters_lock: threading.Lock,
                            same_text_counts: "Counter", system_prompt: str, max_tokens: int,
                            temperature: float, retries: int, enable_thinking: bool,
                            batch_size: int = _MAX_BATCH_SIZE) -> list:
    """Resolve the control-shape members (see _is_control_shape) of ONE literal-sibling group,
    chunked into batched calls -- ALWAYS fresh, NEVER reading/writing the cache: each one's
    resolution reflects an override specific to its own fixed, short label (e.g. an "Up vote"
    toggle button), not a general boundary fact about the ancestor. Caching one would wrongly
    spread its TRUSTED verdict to non-control siblings (real UGC text) sharing the same ancestor
    -- exactly the over-tainting the control_shape carve-out exists to fix.

    Submitted by label_one_file as its OWN executor task, separate from any non-control siblings
    in the same DOM group (see _resolve_noncontrol_group) -- an earlier version ran both
    sequentially inside one task per group, which serialized two independent LLM calls onto a
    single thread and measurably hurt wall-clock (a 002_stackoverflow.com.xml run went from ~30s
    unbatched to 132s batched-but-serialized, despite needing FEWER total calls -- 27 vs 73 --
    because the biggest group's two calls now stacked instead of overlapping)."""
    own_texts = [(row["text"] or "").strip() for row in rows]
    out_rows = []
    for start in range(0, len(rows), batch_size):
        chunk_rows = rows[start:start + batch_size]
        chunk_texts = own_texts[start:start + batch_size]
        try:
            elements = [_element_input(r, t, True, same_text_counts) for r, t in zip(chunk_rows, chunk_texts)]
            result = _label_batch(client, path, elements, system_prompt,
                                   max_tokens, temperature, retries, enable_thinking)
            with counters_lock:
                counters["batch_calls"] += 1
                counters["batched_elements"] += len(chunk_rows)
                counters["control_calls"] += len(chunk_rows)
            if result is None:
                out_rows.extend(_output_row(r, "error", None, None) for r in chunk_rows)
                continue
            cue_depth, rationale = result["cue_depth"], result["rationale"]
            for i, r in enumerate(chunk_rows):
                out_rows.append(_output_row(r, result["verdicts"].get(i, "error"), cue_depth, rationale))
        except Exception as e:
            print(f"    control-shape chunk of {len(chunk_rows)} element(s) failed: {e}", file=sys.stderr)
            out_rows.extend(_output_row(r, "error", None, None) for r in chunk_rows)
    return out_rows


def _resolve_noncontrol_group(client, path: list, rows: list, cache: dict, cache_lock: "threading.Condition",
                               counters: dict, counters_lock: threading.Lock, same_text_counts: "Counter",
                               system_prompt: str, max_tokens: int, temperature: float, retries: int,
                               enable_thinking: bool, batch_size: int = _MAX_BATCH_SIZE) -> list:
    """Resolve the non-control members of ONE literal-sibling group, sharing ONE claim/pending
    cache lookup for the whole group (not one each). `cache_lock` is a Condition: the first group
    to reach a new, unresolved ancestor prefix marks its CLAIMABLE prefixes (see
    _claimable_depths) as _PENDING before releasing the lock to make its real call, and any other
    group reaching those same prefixes concurrently blocks on cache_lock.wait() instead of
    independently racing the same resolution.

    Only ever caches verdict="untrusted": a found cue is a fact about the ancestor chain
    (cacheable, and always means "untrusted by default"), but any individual member overriding to
    TRUSTED via its own value_shape=computed hint is a fact about THAT member alone -- caching it
    would let a later, non-overridden sibling under the same ancestor wrongly inherit a TRUSTED
    verdict it never earned (a real no-write-up violation an earlier version had: it cached
    whatever verdict came back regardless of which way it went)."""
    own_texts = [(row["text"] or "").strip() for row in rows]
    claimable = _claimable_depths(path)
    cached = None
    claim_keys = None
    with cache_lock:
        while True:
            cached = None
            blocked = False
            for depth in claimable:
                key = _ancestor_key(path, depth)
                val = cache.get(key)
                if val is _PENDING:
                    blocked = True
                elif val is not None:
                    cached = val
                    break
            if cached is not None or not blocked:
                break
            cache_lock.wait()
        if cached is None:
            claim_keys = [_ancestor_key(path, d) for d in claimable]
            for k in claim_keys:
                cache.setdefault(k, _PENDING)

    if cached is not None:
        with counters_lock:
            counters["cache_hits"] += len(rows)
        return [_output_row(r, cached["verdict"], cached["cue_depth"], cached["rationale"]) for r in rows]

    out_rows = []
    try:
        for start in range(0, len(rows), batch_size):
            chunk_rows = rows[start:start + batch_size]
            chunk_texts = own_texts[start:start + batch_size]
            try:
                elements = [_element_input(r, t, False, same_text_counts) for r, t in zip(chunk_rows, chunk_texts)]
                result = _label_batch(client, path, elements, system_prompt,
                                       max_tokens, temperature, retries, enable_thinking)
                with counters_lock:
                    counters["batch_calls"] += 1
                    counters["batched_elements"] += len(chunk_rows)
                if result is None:
                    out_rows.extend(_output_row(r, "error", None, None) for r in chunk_rows)
                    continue
                cue_depth, rationale = result["cue_depth"], result["rationale"]
                for i, r in enumerate(chunk_rows):
                    out_rows.append(_output_row(r, result["verdicts"].get(i, "error"), cue_depth, rationale))
                if cue_depth is not None and 0 <= cue_depth < len(path):
                    with cache_lock:
                        cache[_ancestor_key(path, cue_depth)] = {
                            "verdict": "untrusted", "cue_depth": cue_depth, "rationale": rationale,
                        }
            except Exception as e:
                print(f"    non-control chunk of {len(chunk_rows)} element(s) failed: {e}", file=sys.stderr)
                out_rows.extend(_output_row(r, "error", None, None) for r in chunk_rows)
    finally:
        if claim_keys is not None:
            with cache_lock:
                for k in claim_keys:
                    if cache.get(k) is _PENDING:
                        del cache[k]
                cache_lock.notify_all()
    return out_rows
