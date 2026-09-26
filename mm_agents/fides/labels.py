from functools import reduce
from mm_agents.fides.lattice import (
    IntegrityLabel,
    PowersetLattice,
    InverseLattice,
    ProductLabel
)
from typing import FrozenSet, Any
from mm_agents.fides.pydantic_scheme import MetaValue

Lattice = ProductLabel[IntegrityLabel, InverseLattice[PowersetLattice[str]]]

def metadata_to_label(metadata: dict[str, Any]) -> Lattice:
    """
    Convert metadata to a label.
    """
    integrity = metadata["integrity"]
    confidentiality = metadata["confidentiality"]

    return ProductLabel(integrity, confidentiality)