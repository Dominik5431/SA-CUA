"""
trust.py  —  Intent-anchored authorization for computer-use actions
===================================================================

Trust is **not** a property an action carries on its own (the old model labelled
an action TRUSTED whenever its page origin was in a trusted-domain list and it
did not navigate away). Indirect prompt injection defeats that directly: the
attacker controls content *inside* trusted origins, controls the visible labels,
and most controls never expose where they lead.

Here trust is a **relation** between a trusted ``Intent`` (derived by the
privileged planner from the user's instruction — never from screen content) and
a screen affordance (an ``InteractableAction`` carrying provenance from
``coord_to_semantic``). ``authorize`` answers: *may this affordance be activated
in service of this intent?*

Three gates:
  1. Capability ceiling — opaque or irreversible effects (EXECUTE / SEND /
     UNKNOWN, or anything above the intent's ceiling) escalate to a human
     endorsement, regardless of page origin.
  2. Provenance — affordances rendered from page content (Zone.DOCUMENT) must
     satisfy a *structural* anchor. Their label is attacker-influenceable, so it
     can corroborate but never authorize.
  3. Chrome / OS affordances are structurally first-party, but gate 1 still
     applies (a terminal action is EXECUTE -> endorsement).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .coord_to_semantic import (
    Capability,
    InteractableAction,
    Zone,
    _CAP_RANK,
)


class Decision(str, Enum):
    ALLOW             = "allow"
    DENY              = "deny"
    NEEDS_ENDORSEMENT = "needs_endorsement"


@dataclass
class TrustedAnchor:
    """A target described in PLATFORM-ASSERTED terms, never in tainted label text.

    Every field references structure the renderer/platform asserts — which
    region, which position, which element role. ``label_hint`` is the one
    exception: it is corroborating evidence the planner may use to raise its own
    confidence, but it is deliberately never consulted by ``_matches_anchor``,
    because an attacker controls Zone.DOCUMENT labels.
    """
    region_role:  Optional[str] = None
    region_index: Optional[int] = None
    role:         Optional[str] = None
    label_hint:   Optional[str] = None


@dataclass
class Intent:
    """A single authorized step, produced by the privileged planner from the
    trusted user instruction.

    capability — what the user authorized (e.g. NAVIGATE, WRITE).
    anchor     — where, structurally.
    ceiling    — the maximum side-effect allowed before a human must endorse.
    """
    capability: Capability
    anchor:     TrustedAnchor
    ceiling:    Capability


_ESCALATE = frozenset({})

def authorize(intent: Intent, action: InteractableAction) -> Decision:
    """Decide whether *action* may be taken in service of *intent*."""
    cap = action.capability

    if cap in _ESCALATE or _CAP_RANK[cap] > _CAP_RANK[intent.ceiling]:
        return Decision.NEEDS_ENDORSEMENT

    if action.zone == Zone.DOCUMENT and not _matches_anchor(intent.anchor, action):
        return Decision.DENY

    return Decision.ALLOW


def _matches_anchor(anchor: TrustedAnchor, action: InteractableAction) -> bool:
    """Structural match only.

    ``label_hint`` is intentionally NOT consulted: the attacker controls the
    label of a Zone.DOCUMENT element, so a rename can neither satisfy nor break a
    match, and an injected control in a different region fails the region checks.
    """
    if anchor.region_role is not None and action.region_role != anchor.region_role:
        return False
    if anchor.region_index is not None and action.region_index != anchor.region_index:
        return False
    if anchor.role is not None and action.role != anchor.role:
        return False
    return True


def authorized_actions(
    intent: Intent,
    actions: list[InteractableAction],
) -> list[tuple[InteractableAction, Decision]]:
    """Annotate every candidate affordance with its authorization Decision."""
    return [(a, authorize(intent, a)) for a in actions]
