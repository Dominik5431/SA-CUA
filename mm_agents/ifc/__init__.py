"""
mm_agents.ifc  —  FIDES-style information-flow control for computer-use agents.

Tracks confidentiality + integrity labels on data flowing from untrusted screen
sources, through the agent, to sinks, and enforces a deterministic ALLOW/BLOCK
policy at the sink. See arXiv 2505.23643 and github.com/microsoft/fides.
"""

from .labels import (
    PUBLIC,
    TRUSTED,
    UNTRUSTED,
    IntegrityLabel,
    Lattice,
    MetaValue,
    ProductLabel,
    ReadersLabel,
    combine,
    readers,
)
from .screen_variables import (
    LabeledAffordance,
    ScreenView,
    label_screen,
    url_principal_map,
)
from .label_store import LabelStore
from .policy import (
    Decision,
    DeclassRule,
    Endorser,
    PolicyResult,
    decide,
    is_sink,
    sink_tier,
)

__all__ = [
    "PUBLIC", "TRUSTED", "UNTRUSTED", "IntegrityLabel", "Lattice", "MetaValue",
    "ProductLabel", "ReadersLabel", "combine", "readers",
    "LabeledAffordance", "ScreenView", "label_screen", "url_principal_map",
    "LabelStore",
    "Decision", "DeclassRule", "Endorser", "PolicyResult", "decide",
    "is_sink", "sink_tier",
]
