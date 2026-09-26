"""
labels.py  —  FIDES-style information-flow labels for computer-use agents
========================================================================

Mirrors the lattice primitives from Microsoft FIDES (arXiv 2505.23643,
github.com/microsoft/fides): a ``Lattice`` base, an integrity axis, a
confidentiality (reader-set) axis, their product, and a ``MetaValue`` carrier
that pins labels onto a value. Names follow the FIDES tutorial so the lineage is
explicit.

Two axes, both as join-semilattices where ``join`` is the least-upper-bound used
to *combine* information (taint propagation):

  integrity        TRUSTED  <  UNTRUSTED            join = max  (taint rises)
  confidentiality  readers ordered by reverse ⊆    join = ∩    (readers shrink)

Combining values takes the join on both axes, so anything mixed with untrusted
data becomes untrusted, and anything mixed with more-confidential data inherits
the smaller reader set — exactly FIDES's ``{Alice,Priya} ∩ {Alice,Marco}``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Generic, Optional, TypeVar


class Lattice(ABC):
    """Abstract join-semilattice element (FIDES ``Lattice``)."""

    @abstractmethod
    def leq(self, other: Any) -> bool:
        """self ⊑ other in the information order."""

    @abstractmethod
    def join(self, other: Any) -> "Lattice":
        """Least upper bound — used to combine information."""



class _IntLevel(IntEnum):
    TRUSTED = 0
    UNTRUSTED = 1


@dataclass(frozen=True)
class IntegrityLabel(Lattice):
    level: _IntLevel

    def leq(self, other: "IntegrityLabel") -> bool:
        return self.level <= other.level

    def join(self, other: "IntegrityLabel") -> "IntegrityLabel":
        return self if self.level >= other.level else other

    @property
    def is_trusted(self) -> bool:
        return self.level == _IntLevel.TRUSTED

    def __repr__(self) -> str:
        return self.level.name


TRUSTED = IntegrityLabel(_IntLevel.TRUSTED)
UNTRUSTED = IntegrityLabel(_IntLevel.UNTRUSTED)



@dataclass(frozen=True)
class ReadersLabel(Lattice):
    """Confidentiality as the set of principals allowed to read the value.

    ``public=True`` is the sentinel "everyone" set (the least-confidential bottom);
    its explicit ``readers`` frozenset is then ignored.
    """
    readers: frozenset[str] = field(default_factory=frozenset)
    public: bool = False

    def leq(self, other: "ReadersLabel") -> bool:
        if self.public:
            return True
        if other.public:
            return self.public
        return self.readers >= other.readers

    def join(self, other: "ReadersLabel") -> "ReadersLabel":
        if self.public:
            return other
        if other.public:
            return self
        return ReadersLabel(self.readers & other.readers)

    def permits(self, audience: "ReadersLabel") -> bool:
        """May a value with this confidentiality flow to a sink seen by *audience*?

        True iff every principal in the audience is allowed to read the value,
        i.e. audience.readers ⊆ self.readers (public audience needs public value).
        """
        if self.public:
            return True
        if audience.public:
            return False
        return audience.readers <= self.readers

    def __repr__(self) -> str:
        return "PUBLIC" if self.public else "{" + ",".join(sorted(self.readers)) + "}"


def readers(*principals: str) -> ReadersLabel:
    return ReadersLabel(frozenset(principals))


PUBLIC = ReadersLabel(public=True)



@dataclass(frozen=True)
class ProductLabel(Lattice):
    integrity: IntegrityLabel = TRUSTED
    confidentiality: ReadersLabel = PUBLIC

    def leq(self, other: "ProductLabel") -> bool:
        return (self.integrity.leq(other.integrity)
                and self.confidentiality.leq(other.confidentiality))

    def join(self, other: "ProductLabel") -> "ProductLabel":
        return ProductLabel(
            self.integrity.join(other.integrity),
            self.confidentiality.join(other.confidentiality),
        )

    def __repr__(self) -> str:
        return f"({self.integrity!r}, {self.confidentiality!r})"


def combine(*labels: ProductLabel) -> ProductLabel:
    """Join a sequence of labels (the label of any value derived from them)."""
    acc = ProductLabel()
    for lab in labels:
        acc = acc.join(lab)
    return acc



T = TypeVar("T")


@dataclass
class MetaValue(Generic[T]):
    value: T
    label: ProductLabel = field(default_factory=ProductLabel)

    @property
    def integrity(self) -> IntegrityLabel:
        return self.label.integrity

    @property
    def confidentiality(self) -> ReadersLabel:
        return self.label.confidentiality

    @classmethod
    def derive(cls, value: T, *sources: "MetaValue") -> "MetaValue[T]":
        """A new value computed from *sources* inherits the join of their labels."""
        return cls(value, combine(*(s.label for s in sources)))

    def __repr__(self) -> str:
        return f"MetaValue({self.value!r}, {self.label!r})"
