"""
label_store.py  —  cross-step label propagation
================================================

Mirrors FIDES's ``VariablePassingPlanner.memory``: untrusted/confidential values
the agent has read are kept as named variables, and any value the agent derives
from them inherits the join of their labels. This is what gives the CUA the
memory the per-step affordance classifier lacked — "the text I'm about to type
into this reply box was derived from an injected comment I read three steps ago"
is expressed as a label on the derived payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .labels import MetaValue, ProductLabel, combine


@dataclass
class LabelStore:
    _vars: dict[str, MetaValue] = field(default_factory=dict)

    def put(self, handle: str, value: MetaValue) -> str:
        """Record a labeled value under *handle*; returns the handle."""
        self._vars[handle] = value
        return handle

    def get(self, handle: str) -> MetaValue:
        return self._vars[handle]

    def label_of(self, handle: str) -> ProductLabel:
        return self._vars[handle].label

    def derive_label(self, handles: Iterable[str]) -> ProductLabel:
        """Join the labels of the named variables — the label of anything the
        agent assembles out of them (e.g. the payload typed into a field)."""
        return combine(*(self._vars[h].label for h in handles))

    def __contains__(self, handle: str) -> bool:
        return handle in self._vars
