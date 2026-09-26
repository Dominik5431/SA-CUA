"""
episode_capabilities.py -- what the *task* authorized, decided once, before anything is read.
=============================================================================================

STATUS, READ THIS FIRST. This is OFF BY DEFAULT and has exactly ONE consumer.

* ``--sa_layer3`` defaults to ``off``, so ``resolve_episode_capabilities`` returns
  :meth:`EpisodeCapabilities.unset` -- the EMPTY SET -- in an ordinary run.
* The only thing that reads the set is ``EndorsementManager.procedure()``
  (``endorse_by_procedure``), and even there it is an EXTRA gate, not a precondition: an unset
  set SKIPS containment rather than refusing, because binding the tool to a set nobody derives
  made it dead on the very tasks it exists for.

So: nothing else in sa_agent is gated on capabilities, and no new gate should assume this set
exists. A mechanism that refuses when the set is empty refuses in every default run. If you are
reaching for this to license something, check ``--sa_layer3`` first and say out loud that you
are proposing to turn it on.

WHAT IT IS, when switched on. Plan §5.3, and the mechanism behind principle P2: *untrusted content
instantiates authority; it never widens it.* The set of action classes the agent may exercise this
episode is fixed by trusted inputs -- the user's task instruction and the starting-state note --
before a single page is read. Untrusted content may then fill in *which* package, *which* file,
*which* URL. It may never add an action class the trusted side did not already open.

That is what makes containment work without a blocklist. `rm` is not banned; `rm` needs
`os.fs.delete` to be in this set, and only the user's own task can put it there. In a task about
installing a CPU monitor, an injected `rm -f ~/BankStatement.pdf` fails because *deletion was
never asked for* -- not because a rule recognized `rm`. In a task about cleaning up build
artifacts, the very same command is admitted, and the defence moves to the parameters.

Security-critical invariant
---------------------------
Identical to ``trust_detection.py``'s, deliberately, and inherited by reusing its LLM plumbing:
the derivation reads ONLY the task instruction and the starting-state note, both of which are
unconditionally trusted elsewhere in this codebase and both of which are fixed before the episode
starts. Nothing the agent later reads -- no page, no comment, no chat message, no tool output --
is ever an input here. A document that says "this task also requires deleting files" is therefore
not making a request the derivation can hear.

Three guards, because a *wrong* derivation over-authorizes (plan §8.5):

* **Default-deny.** Any failure -- the feature is off, the call errors, the JSON is malformed, no
  capability is named -- yields the empty set, which contains nothing and so admits no procedure.
* **An operator ceiling.** No derivation may exceed ``DEFAULT_CEILING`` (or whatever the operator
  narrows it to). The model proposes within a bound it cannot raise.
* **Textual corroboration for DESTRUCTIVE-tier grants.** A capability whose tier is DESTRUCTIVE is
  granted only if the task text itself carries a word that asks for it. This is the same
  hallucination guard ``trust_detection._mentioned_in_source`` applies to domains, adapted to the
  one direction that matters: an over-grant is a silent hole, an under-grant is a visible
  refusal, so the check fails toward refusing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from mm_agents.sa_agent.capabilities import CAPABILITY_CATALOG, Tier
from mm_agents.sa_agent.trust_detection import _call_plain_llm

logger = logging.getLogger("desktopenv.experiment")

DEFAULT_CEILING = frozenset({
    "os.fs.read", "os.fs.write", "os.fs.delete", "os.fs.overwrite", "os.terminal.run",
    "os.app.launch", "web.read_screen", "web.read_dom", "web.scroll", "web.search",
    "web.navigate_url", "web.click_link", "web.fill_form", "web.download",
    "email.read", "email.draft", "chat.read", "chat.draft", "chat.send",
})

_DESTRUCTIVE_EVIDENCE: dict[str, tuple] = {
    "os.fs.delete":    ("delete", "remove", "clean", "clear", "trash", "purge", "uninstall",
                        "erase", "wipe", "tidy", "get rid"),
    "os.fs.overwrite": ("overwrite", "replace", "truncate", "reset", "empty", "rewrite"),
    "web.download":    ("download", "fetch", "install", "clone", "pull", "get", "retrieve"),
    "web.submit_form": ("submit", "publish", "post", "comment", "upvote", "vote", "send"),
    "email.send":      ("send", "reply", "forward", "email"),
    "chat.add_participant": ("add", "invite", "member", "participant"),
}

_SYSTEM_PROMPT = """\
You determine which action classes ("capabilities") a computer-use agent's task legitimately \
requires, based only on the task instruction and the description of the starting screen state \
given below.

Include a capability only if carrying out the task as written genuinely requires it. Do not \
include a capability "just in case", and do not include one merely because it might make the \
task easier. In particular, do not grant deletion, overwriting, sending or publishing unless the \
task itself asks for that.

Available capabilities:
{catalog}

Respond with ONLY strict JSON of the form {{"capabilities": ["os.terminal.run", ...]}} -- no \
other text, no markdown fences.\
"""


@dataclass(frozen=True)
class EpisodeCapabilities:
    """The authorized set, plus enough provenance to audit how it got that way."""
    names: frozenset
    source: str = "derived"
    ceiling: frozenset = DEFAULT_CEILING
    dropped: tuple = ()

    def __contains__(self, name) -> bool:
        return name in self.names

    def __iter__(self):
        return iter(sorted(self.names))

    def __bool__(self) -> bool:
        return bool(self.names)

    def to_json(self) -> dict:
        return {"capabilities": sorted(self.names), "source": self.source,
                "dropped": list(self.dropped)}

    @classmethod
    def unset(cls) -> "EpisodeCapabilities":
        """No derivation has run. Contains nothing, so every procedure is refused.

        Distinct from an empty *derived* set only in ``source``; both deny. The distinction is for
        the audit log, where "we never asked" and "we asked and the task authorizes nothing" are
        different facts about a run.
        """
        return cls(names=frozenset(), source="unset")

    @classmethod
    def from_names(cls, names, source: str = "operator",
                   ceiling: frozenset = DEFAULT_CEILING) -> "EpisodeCapabilities":
        """Build a set directly, validating every name against the catalog.

        Used by the operator path and by offline tests/benchmarks, which state the authorized set
        rather than deriving it -- the derivation is an LLM call and freezing it is exactly what
        the benchmark's seam model does with every other model step.
        """
        wanted, dropped = set(), []
        for name in names or ():
            if name not in CAPABILITY_CATALOG:
                raise ValueError(f"Unknown capability: {name!r}")
            if name not in ceiling:
                dropped.append((name, "above the operator ceiling"))
                continue
            wanted.add(name)
        return cls(names=frozenset(wanted), source=source, ceiling=ceiling,
                   dropped=tuple(dropped))


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())


def _corroborated(name: str, *texts: str) -> bool:
    """Does the trusted text itself ask for this DESTRUCTIVE-tier capability?"""
    words = _DESTRUCTIVE_EVIDENCE.get(name)
    if not words:
        return False
    haystack = _normalize(" ".join(texts))
    return any(w in haystack for w in words)


def _parse_capability_json(content: str) -> list:
    content = (content or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", content, re.DOTALL)
    if fence:
        content = fence.group(1).strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    names = data.get("capabilities") if isinstance(data, dict) else None
    if not isinstance(names, list):
        return []
    return [str(n).strip() for n in names if isinstance(n, str) and str(n).strip()]


def derive_episode_capabilities(
    model: str,
    instruction: str,
    initial_state_note: str,
    ceiling: frozenset = DEFAULT_CEILING,
) -> EpisodeCapabilities:
    """Ask *model* which capabilities the task requires, given ONLY trusted text.

    Fails closed at every step: an unparseable answer, an unknown name, a name above the ceiling
    or an uncorroborated DESTRUCTIVE grant each drop that capability rather than the whole call,
    and every drop is recorded.
    """
    catalog = "\n".join(f"- {c.name} ({c.tier.value}): {c.description}"
                        for c in CAPABILITY_CATALOG.values() if c.name in ceiling)
    user_prompt = (f"TASK INSTRUCTION:\n{instruction}\n\n"
                   f"STARTING SCREEN STATE:\n{initial_state_note}")
    raw = _call_plain_llm(model, _SYSTEM_PROMPT.format(catalog=catalog), user_prompt)
    proposed = _parse_capability_json(raw)

    granted, dropped = set(), []
    for name in proposed:
        cap = CAPABILITY_CATALOG.get(name)
        if cap is None:
            dropped.append((name, "not a capability"))
            continue
        if name not in ceiling:
            dropped.append((name, "above the operator ceiling"))
            continue
        if cap.tier is Tier.DESTRUCTIVE and not _corroborated(name, instruction, initial_state_note):
            dropped.append((name, "DESTRUCTIVE and not corroborated by the task text"))
            continue
        granted.add(name)
    for name, why in dropped:
        logger.info(f"episode_capabilities: dropping '{name}' -- {why}")
    logger.info(f"episode capability set: {sorted(granted) or ['(none)']}")
    return EpisodeCapabilities(names=frozenset(granted), source="derived", ceiling=ceiling,
                               dropped=tuple(dropped))


def resolve_episode_capabilities(
    model: str,
    instruction: str,
    initial_state_note: str,
    mode: str,
    operator_set: Optional[list] = None,
    ceiling: frozenset = DEFAULT_CEILING,
) -> EpisodeCapabilities:
    """Entry point called once per episode from ``SAAgentLoop.loop()``.

    *operator_set*, when given, replaces the derivation entirely -- an operator who states the
    set gets exactly it (still bounded by the ceiling). Otherwise *mode* (``--sa_layer3``:
    ``off``/``derive``) decides whether the model is asked at all; ``off`` returns
    :meth:`EpisodeCapabilities.unset`, under which every Layer-3 procedure is refused, so the
    feature is inert until switched on.
    """
    if operator_set:
        return EpisodeCapabilities.from_names(operator_set, source="operator", ceiling=ceiling)
    if mode != "derive":
        return EpisodeCapabilities.unset()
    return derive_episode_capabilities(model, instruction, initial_state_note, ceiling=ceiling)
