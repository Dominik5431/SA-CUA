"""icl.py -- the P-LLM's worked examples, as swappable files rather than string constants.

WHY THIS EXISTS. The examples are the single biggest lever on how a masked planner behaves --
whether it queries a redacted region or concludes the page is empty, whether it endorses
before acting or after -- and they were module-level constants selected by a hand-written `if`
inside `build_sa_pllm_prompt`. Comparing two example sets meant editing prompts.py and losing
the old one, so nobody compared. A pack is a directory; swapping one is `--sa_icl_pack`, and the
set that was used is recoverable from the run.

A PACK is a directory of `.md` files, each one example, each starting with a front-matter block:

    ---
    id: masked_region_query
    slot: action_space
    order: 30
    modes: masked
    requires_all: query_masked_region
    requires_any: endorse_by_crosscheck, endorse_by_checksum
    ---
    ## WORKED EXAMPLE: ...

  id           -- stable name, used in logs and to override one file from another pack
  slot         -- `action_space` (appended to the tool catalog) or `examples` (the trailing
                  `## EXAMPLES` block). Ordering is per slot.
  order        -- ascending within a slot; ties break on id, so a pack renders identically twice
  modes        -- `masked`, `unmasked`, or `both` (default): which observation mode it applies to.
                  The two modes make flatly different claims about what the planner receives, and
                  an example written for one actively misdirects in the other.
  requires_all -- every tool named must be active or the example is dropped
  requires_any -- at least one must be active

GATING IS NOT COSMETIC. An example that walks through a tool the run disabled teaches a call the
planner cannot make, and the planner spends turns discovering that. Both `requires_*` are checked
against the SAME active-tool list that builds the tool-calling schema, so a disabled tool leaves
the prompt and its examples together.

Body text may use the `{{...}}` placeholders `build_sa_pllm_prompt` substitutes; they are filled
in on the assembled prompt, so an example never has to know which mode it landed in beyond its
own `modes:` line.
"""
from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("desktopenv.sa.icl")

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PACK = "icl"

SLOT_ACTION_SPACE = "action_space"
SLOT_EXAMPLES = "examples"
_SLOTS = (SLOT_ACTION_SPACE, SLOT_EXAMPLES)

_LIST_KEYS = ("requires_all", "requires_any", "modes")


@dataclass
class Example:
    id: str
    slot: str
    body: str
    order: int = 100
    modes: tuple = ("both",)
    requires_all: tuple = ()
    requires_any: tuple = ()
    path: str = ""

    def applies(self, active: frozenset, masked: bool) -> bool:
        mode = "masked" if masked else "unmasked"
        if "both" not in self.modes and mode not in self.modes:
            return False
        if self.requires_all and not set(self.requires_all) <= active:
            return False
        if self.requires_any and not (set(self.requires_any) & active):
            return False
        return True


@dataclass
class Pack:
    name: str
    path: str
    examples: list = field(default_factory=list)

    def select(self, slot: str, active: frozenset, masked: bool) -> list:
        chosen = [e for e in self.examples if e.slot == slot and e.applies(active, masked)]
        chosen.sort(key=lambda e: (e.order, e.id))
        return chosen


def _parse(path: str) -> "Example | None":
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not text.startswith("---"):
        logger.warning("[icl] %s has no front matter -- skipped", path)
        return None
    _, _, rest = text.partition("---")
    header, sep, body = rest.partition("\n---")
    if not sep:
        logger.warning("[icl] %s has an unterminated front matter block -- skipped", path)
        return None
    meta: dict = {}
    for line in header.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key in _LIST_KEYS:
            meta[key] = tuple(v.strip() for v in value.split(",") if v.strip())
        elif key == "order":
            try:
                meta[key] = int(value)
            except ValueError:
                logger.warning("[icl] %s: order %r is not an integer -- using the default", path, value)
        elif key:
            meta[key] = value
    slot = meta.get("slot", SLOT_ACTION_SPACE)
    if slot not in _SLOTS:
        logger.warning("[icl] %s names unknown slot %r -- skipped", path, slot)
        return None
    return Example(
        id=meta.get("id") or os.path.splitext(os.path.basename(path))[0],
        slot=slot,
        body=body.lstrip("\n").rstrip() + "\n",
        order=meta.get("order", 100),
        modes=meta.get("modes", ("both",)),
        requires_all=meta.get("requires_all", ()),
        requires_any=meta.get("requires_any", ()),
        path=path,
    )


def load_pack(pack: str | None = None) -> Pack:
    """Load a pack by name (resolved under mm_agents/sa_agent/) or by absolute path.

    A named pack that does not exist is an error rather than an empty pack: running with no
    examples at all is a different experiment from running with a different set of them, and the
    difference would otherwise show up only as a planner that has quietly got worse.
    """
    name = pack or DEFAULT_PACK
    base = name if os.path.isabs(name) else os.path.join(_HERE, name)
    if not os.path.isdir(base):
        raise FileNotFoundError(
            f"--sa_icl_pack names {base!r}, which is not a directory. Packs live under "
            f"mm_agents/sa_agent/ (the default is {DEFAULT_PACK!r}); pass an absolute path to "
            f"use one from elsewhere.")
    examples = []
    seen: dict = {}
    for path in sorted(glob.glob(os.path.join(base, "*.md"))):
        ex = _parse(path)
        if ex is None:
            continue
        if ex.id in seen:
            raise ValueError(f"duplicate ICL example id {ex.id!r} in {seen[ex.id]} and {path}")
        seen[ex.id] = path
        examples.append(ex)
    if not examples:
        raise ValueError(f"ICL pack {base!r} contains no usable .md examples")
    return Pack(name=name, path=base, examples=examples)


def describe(pack: Pack, active: frozenset, masked: bool) -> str:
    """One log line naming exactly which examples this run's prompt carries, and which were
    dropped for which reason -- the thing you need when a planner behaves differently between two
    runs and the packs are the only difference."""
    used, dropped = [], []
    for e in sorted(pack.examples, key=lambda e: (e.slot, e.order, e.id)):
        (used if e.applies(active, masked) else dropped).append(e.id)
    return (f"pack={pack.name} mode={'masked' if masked else 'unmasked'} "
            f"using=[{', '.join(used)}]" + (f" dropped=[{', '.join(dropped)}]" if dropped else ""))
