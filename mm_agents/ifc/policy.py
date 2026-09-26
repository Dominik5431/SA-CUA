"""
policy.py  —  deterministic sink policy + endorsement
==========================================================

The enforcement point. Given a screen affordance the agent wants to activate and
the IFC label of the data feeding it, decide — fully autonomously, no human in
the loop — whether the action is ALLOWed or BLOCKed.

Two flow rules, evaluated on the labeled action trace:

  integrity        a write/destructive sink driven by UNTRUSTED data -> BLOCK
                   (indirect-prompt-injection defense). Legitimate sinks are
                   driven by TRUSTED data — the user's task instruction — so
                   benign tasks where the user supplies the payload pass.

  confidentiality  payload readers do not cover the sink's audience -> BLOCK
                   (exfiltration defense: {user}-private text into a public
                   comment, or off-origin navigation).

Taint is cleared only by ``endorse`` — a deterministic, type-bounded rule, the
sole endorsement path. Never a human prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from mm_agents.accessibility_tree_wrap.coord_to_semantic import Capability
from .labels import PUBLIC, TRUSTED, MetaValue, ProductLabel, ReadersLabel


class Tier(Enum):
    """Side-effect tier of a sink. Values mirror
    ``mm_agents.sa_agent.capabilities.Tier`` exactly, so the two map 1:1 at the
    phase-2 integration boundary; defined locally to keep the IFC layer free of
    any agent-specific import chain."""
    READ = "READ"
    REVERSIBLE_WRITE = "REVERSIBLE_WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"


class Decision(Enum):
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


_CAP_TIER: dict[Capability, Tier] = {
    Capability.READ:     Tier.READ,
    Capability.NAVIGATE: Tier.REVERSIBLE_WRITE,
    Capability.WRITE:    Tier.REVERSIBLE_WRITE,
    Capability.SEND:     Tier.DESTRUCTIVE,
    Capability.EXECUTE:  Tier.DESTRUCTIVE,
    Capability.UNKNOWN:  Tier.DESTRUCTIVE,
}

_WRITE_TIERS = (Tier.REVERSIBLE_WRITE, Tier.DESTRUCTIVE)


def sink_tier(capability: Capability) -> Tier:
    return _CAP_TIER.get(capability, Tier.DESTRUCTIVE)


def is_sink(capability: Capability) -> bool:
    """A sink is any action that changes state or emits data (not a pure read)."""
    return sink_tier(capability) in _WRITE_TIERS


def decide(
    *,
    capability: Capability,
    label: ProductLabel,
    audience: ReadersLabel = PUBLIC,
) -> PolicyResult:
    """Decide ALLOW/BLOCK for activating an affordance of effect class
    *capability*, where *label* is the join of everything feeding the action
    (the affordance's own integrity and any typed payload) and *audience* is who
    will see the action's effect.
    """
    tier = sink_tier(capability)

    if tier not in _WRITE_TIERS:
        return PolicyResult(Decision.ALLOW, "read-only action, not a sink")

    if not label.integrity.is_trusted:
        return PolicyResult(
            Decision.BLOCK,
            f"untrusted data drives a {tier.value} sink (injection)",
        )

    if not label.confidentiality.permits(audience):
        return PolicyResult(
            Decision.BLOCK,
            f"payload readers {label.confidentiality!r} do not cover "
            f"sink audience {audience!r} (exfiltration)",
        )

    return PolicyResult(Decision.ALLOW, f"trusted data to permitted {tier.value} sink")



@dataclass(frozen=True)
class DeclassRule:
    """Whitelists using a value as a sink argument when it was produced by a
    quarantined constrained read conforming to ``schema_name`` for the given
    capability. The strict type bounds what the untrusted channel can express, so
    integrity taint is cleared for this use. Confidentiality is never cleared."""
    capability: Capability
    schema_name: str


class Endorser:
    def __init__(self, rules: Optional[list[DeclassRule]] = None):
        self._rules = set(rules or [])

    def endorse(
        self,
        value: MetaValue,
        *,
        capability: Capability,
        schema_name: str,
    ) -> MetaValue:
        """Return a copy of *value* with integrity lowered to TRUSTED iff a rule
        whitelists (capability, schema_name); otherwise unchanged."""
        if DeclassRule(capability, schema_name) in self._rules:
            cleared = ProductLabel(TRUSTED, value.confidentiality)
            return MetaValue(value.value, cleared)
        return value
