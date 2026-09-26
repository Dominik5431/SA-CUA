"""Verdict and reason vocabulary shared by the adapter, runner and scorer.

Three-valued on purpose (see endorsement/README.md):

* ``ACCEPT``      -- the channel cleared the value; carries a level and the adopted value.
* ``REJECT``      -- the channel refused; carries a *reason code*, because a refusal for the
                     wrong reason is a latent bug that a boolean assertion hides.
* ``UNSUPPORTED`` -- the mechanism the case asks for does not exist yet. Distinct from REJECT so
                     that spec-first cases (most of them) read as "not built" rather than "failing".
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Verdict(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    UNSUPPORTED = "UNSUPPORTED"


class Level(str, Enum):
    """Mirror of lattice.IntegrityLabel.Level, as strings so cases stay YAML-readable."""
    TRUSTED = "TRUSTED"
    SELF_AUTHORED = "SELF_AUTHORED"
    ENDORSED = "ENDORSED"
    COMPUTED_PLATFORM_VALUE = "COMPUTED_PLATFORM_VALUE"
    FREE_TEXT_THIRD_PARTY = "FREE_TEXT_THIRD_PARTY"
    UNTRUSTED = "UNTRUSTED"

    @property
    def rank(self) -> int:
        """Lower is more trusted, matching IntegrityLabel.Level.value."""
        return _LEVEL_ORDER.index(self)


_LEVEL_ORDER = [
    Level.TRUSTED,
    Level.SELF_AUTHORED,
    Level.ENDORSED,
    Level.COMPUTED_PLATFORM_VALUE,
    Level.FREE_TEXT_THIRD_PARTY,
    Level.UNTRUSTED,
]


class Reason(str, Enum):
    """Why an endorsement was refused. Extend freely; the runner only compares strings."""
    ACCEPTOR_FAILED = "ACCEPTOR_FAILED"
    ACCEPTOR_FAILED_DERIVED = "ACCEPTOR_FAILED_DERIVED"
    NOT_FOUND = "NOT_FOUND"
    CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"

    NOT_ALLOWLISTED = "NOT_ALLOWLISTED"
    URL_IDENTITY_UNVERIFIED = "URL_IDENTITY_UNVERIFIED"
    URL_UNSAFE = "URL_UNSAFE"
    QUERY_NOT_TASK_DERIVED = "QUERY_NOT_TASK_DERIVED"
    FETCH_FAILED = "FETCH_FAILED"
    NO_MATCHES = "NO_MATCHES"
    SOURCE_INCONCLUSIVE = "SOURCE_INCONCLUSIVE"
    CHECKER_DISAGREEMENT = "CHECKER_DISAGREEMENT"
    CROSSCHECK_DIVERGED = "CROSSCHECK_DIVERGED"
    VALUE_NOT_IN_TASK = "VALUE_NOT_IN_TASK"
    TASK_TEXT_UNAVAILABLE = "TASK_TEXT_UNAVAILABLE"
    TASK_TOO_UNSPECIFIC = "TASK_TOO_UNSPECIFIC"
    NOT_IN_REGISTRY = "NOT_IN_REGISTRY"
    REGISTRY_INSUFFICIENT = "REGISTRY_INSUFFICIENT"
    NOT_IN_CANDIDATES = "NOT_IN_CANDIDATES"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"

    DOMAIN_MISMATCH = "DOMAIN_MISMATCH"
    NO_SELF_IDENTITY = "NO_SELF_IDENTITY"
    NO_DIRECTORY = "NO_DIRECTORY"
    NOT_IN_DIRECTORY = "NOT_IN_DIRECTORY"
    AMBIGUOUS_DIRECTORY = "AMBIGUOUS_DIRECTORY"
    NAME_NOT_IN_TASK = "NAME_NOT_IN_TASK"
    NAME_TOO_SHORT = "NAME_TOO_SHORT"
    DIRECTORY_ENTRY_REFUSED = "DIRECTORY_ENTRY_REFUSED"

    RECALL_DIVERGED = "RECALL_DIVERGED"
    QUESTION_ECHOES_VALUE = "QUESTION_ECHOES_VALUE"
    NO_PRIOR = "NO_PRIOR"
    PRIOR_NOT_ENDORSED = "PRIOR_NOT_ENDORSED"

    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    NOT_ON_SCREEN = "NOT_ON_SCREEN"
    NEEDLE_REJECTED = "NEEDLE_REJECTED"
    CLAIM_NOT_ATTESTED = "CLAIM_NOT_ATTESTED"
    CLAIM_NOT_ATTESTABLE = "CLAIM_NOT_ATTESTABLE"

    PARSE_FAILED = "PARSE_FAILED"
    CAPABILITY_NOT_AUTHORIZED = "CAPABILITY_NOT_AUTHORIZED"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    CARDINALITY_EXCEEDED = "CARDINALITY_EXCEEDED"
    NO_TRUSTED_DERIVATION = "NO_TRUSTED_DERIVATION"
    STRUCTURAL_MISMATCH = "STRUCTURAL_MISMATCH"

    PRECONDITION = "PRECONDITION"
    OTHER = "OTHER"


@dataclass
class Outcome:
    """What the channel actually did with one variant."""
    verdict: Verdict
    level: Optional[Level] = None
    value: Optional[str] = None
    reason: Optional[Reason] = None
    bits: Optional[float] = None
    stage: str = ""
    message: str = ""
    detail: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "level": self.level.value if self.level else None,
            "value": self.value,
            "reason": self.reason.value if self.reason else None,
            "bits": self.bits,
            "stage": self.stage,
            "message": self.message,
            "detail": self.detail,
        }


class Unsupported(Exception):
    """Raised by the adapter when a case asks for machinery that isn't built yet."""

    def __init__(self, what: str, stage: str = ""):
        super().__init__(what)
        self.what = what
        self.stage = stage
