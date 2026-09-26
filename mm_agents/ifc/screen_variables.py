"""
screen_variables.py  —  the action-list bridge (core "FIDES for CUA" contribution)
==================================================================================

FIDES assumes tool outputs arrive as discrete, labeled values it can drop into
variables. A computer-use agent has no such thing — its only "tool output" is the
holistic screen. This module manufactures the missing variables: it runs the
provenance extraction in
``mm_agents.accessibility_tree_wrap.coord_to_semantic`` and wraps every screen
affordance (the *action list*) and content region as a FIDES ``MetaValue`` with an
integrity + confidentiality label.

Source labeling rules (v1, a11y-tree only):
  integrity        Zone.CHROME / Zone.OS  -> TRUSTED   (first-party UI / native app)
                   Zone.DOCUMENT          -> UNTRUSTED  (page content; injectable)
  confidentiality  supplied by a per-task principal map; default PUBLIC. A rule
                   table keyed on the element URL marks the user's own pages
                   (account / settings) as confidential to {user}.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from mm_agents.accessibility_tree_wrap.coord_to_semantic import (
    Capability,
    InteractableAction,
    Zone,
    extract_actions_atspi,
)
from .labels import (
    PUBLIC,
    TRUSTED,
    UNTRUSTED,
    MetaValue,
    ProductLabel,
    ReadersLabel,
)

PrincipalMap = Callable[[InteractableAction], ReadersLabel]


def _default_principal_map(_action: InteractableAction) -> ReadersLabel:
    return PUBLIC


def url_principal_map(rules: list[tuple[str, ReadersLabel]]) -> PrincipalMap:
    """Build a principal map from (url-substring, readers) rules.

    First matching substring on the affordance's URL wins; default PUBLIC.
    """
    def _map(action: InteractableAction) -> ReadersLabel:
        url = action.url or ""
        for needle, label in rules:
            if needle in url:
                return label
        return PUBLIC
    return _map


def _integrity_of(action: InteractableAction):
    return TRUSTED if action.zone in (Zone.CHROME, Zone.OS) else UNTRUSTED


@dataclass
class LabeledAffordance:
    """One screen affordance as a labeled FIDES variable."""
    meta: MetaValue[InteractableAction]

    @property
    def action(self) -> InteractableAction:
        return self.meta.value

    @property
    def label(self) -> ProductLabel:
        return self.meta.label

    def __repr__(self) -> str:
        a = self.action
        return f"LabeledAffordance({a.semantic} @ {a.coordinate} :: {self.label!r})"


@dataclass
class ScreenView:
    """The labeled, discretized view of a screen — the CUA analog of FIDES's
    tool-output variable set."""
    affordances: list[LabeledAffordance] = field(default_factory=list)
    regions: dict[str, ProductLabel] = field(default_factory=dict)

    def region_label(self, region_id: Optional[str]) -> ProductLabel:
        if region_id is None:
            return ProductLabel()
        return self.regions.get(region_id, ProductLabel())


def label_screen(
    a11y_xml: str,
    principal_map: Optional[PrincipalMap] = None,
) -> ScreenView:
    """Turn a raw AT-SPI XML observation into a labeled ``ScreenView``."""
    pmap = principal_map or _default_principal_map
    view = ScreenView()
    for action in extract_actions_atspi(a11y_xml):
        label = ProductLabel(_integrity_of(action), pmap(action))
        view.affordances.append(LabeledAffordance(MetaValue(action, label)))
        if action.region_id is not None:
            prev = view.regions.get(action.region_id)
            view.regions[action.region_id] = label if prev is None else prev.join(label)
    return view
