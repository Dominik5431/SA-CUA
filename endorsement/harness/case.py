"""Case schema: a scenario is one piece of untrusted content plus N variants over it.

The seam model (README §2): every model/network step in the channel is a *seam* that can be
frozen (default, deterministic) or run live. The host's trust *decision* is never frozen --
it is the thing under test.

  S1 screen extraction   Q-VLM      -> `qvlm_answer`
  S2 query construction  P-LLM      -> `frozen.trusted_url` / `frozen.question` / `frozen.hint`
  S3 fetch               network    -> fixtures/http
  S4 re-derivation       Q-LLM      -> `frozen.derived_answer`
  S5 second checker      Q-LLM'     -> `frozen.derived_answer_2`
  S6 parametric recall   Q-LLM      -> `frozen.recalled_answer` (closed-book; no source text)
  D  decision            host       -> never frozen
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator

from .verdicts import Level, Reason, Verdict

VariantClass = Literal["utility", "security", "residual"]
SeamName = Literal["S0", "S1", "S2", "S3", "S4", "S5", "S6"]


class Episode(BaseModel):
    model_config = {"extra": "forbid"}
    """Trusted, pre-episode inputs. Everything here is TRUSTED by construction."""
    starting_state: str = ""
    trusted_domains: list[str] = Field(default_factory=list)
    trusted_domains_corroborating: list[str] = Field(default_factory=list)
    require_task_derived_evidence: bool = False
    authorized_capabilities: Optional[list[str]] = None
    scope_root: Optional[str] = None
    self_identity: str = ""
    directory: list[str] = Field(default_factory=list)


class Request(BaseModel):
    model_config = {"extra": "forbid"}
    """What the P-LLM asked the Q-VLM for."""
    question: str
    answer_type: str = "SHORT_TEXT"
    type_params: dict[str, Any] = Field(default_factory=dict)
    candidates: Optional[list[str]] = None
    display_name: Optional[str] = None
    enum_values: Optional[list[str]] = None


class QVLMAnswer(BaseModel):
    model_config = {"extra": "forbid"}
    found: bool = True
    answer: Optional[str] = None


class Frozen(BaseModel):
    model_config = {"extra": "forbid"}
    """Recorded values for seams S2/S4/S5. Absent field + seam not live => harness supplies a
    sensible default (S2 falls back to the scenario-level `crosscheck` block; S4 falls back to
    the scenario's expected trusted answer)."""
    question: Optional[str] = None
    trusted_url: Optional[str] = None
    hint: Optional[str] = None
    derived_answer: Optional[str] = None
    derived_answer_2: Optional[str] = None
    second_checker: bool = False
    recalled_answer: Optional[str] = None
    recalled_answer_2: Optional[str] = None
    recall_precheck: bool = True
    fixture: Optional[str] = None


class Screen(BaseModel):
    model_config = {"extra": "forbid"}
    """Seam S0: the AT-SPI tree the host reads. Inline XML keeps a case self-contained;
    `tree_file` points into fixtures/a11y/ for real captures."""
    tree: Optional[str] = None
    tree_file: Optional[str] = None


class LocateSpec(BaseModel):
    model_config = {"extra": "forbid"}
    """The selection direction: the needle is authored by a TRUSTED source, so nothing untrusted
    crosses and there is no model in the loop. See plan §4.1 / §6.8."""
    needle: str
    needle_source: Literal["task", "chrome", "starting_state", "cleared_variable"] = "task"
    needle_level: Level = Level.TRUSTED
    region: Optional[list[int]] = None
    interactive_only: bool = False
    exclude_entry: bool = True


class AttestSpec(BaseModel):
    model_config = {"extra": "forbid"}
    """The extraction direction: the MODEL authors the needle. Confirms faithful reporting only."""
    claim: Optional[str] = None
    carried_level: Level = Level.UNTRUSTED


class Expect(BaseModel):
    model_config = {"extra": "forbid"}
    verdict: Verdict
    level: Optional[Level] = None
    value: Optional[str] = None
    reason: Optional[Reason] = None
    bits_max: Optional[float] = None
    detail: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _coherent(self) -> "Expect":
        if self.verdict == Verdict.ACCEPT and self.level is None:
            raise ValueError("ACCEPT expectations must state a level (exact, not a floor)")
        if self.verdict == Verdict.REJECT and self.reason is None:
            raise ValueError("REJECT expectations must state a reason code")
        return self


class Sink(BaseModel):
    model_config = {"extra": "forbid"}
    """Optional end-to-end assertion: would the endorsed value have been allowed to *act*?

    The question that actually matters is not what level a value got, but whether the tool call
    it feeds would run. Several cases (e.g. a typosquat cleared to ENDORSED) are only
    stopped here, at the policy tier -- not by the endorser.
    """
    tool: str
    tier: Literal["READ", "REVERSIBLE_WRITE", "DESTRUCTIVE"]
    expect: Literal["ALLOW", "BLOCK"]
    min_trusted_level: Optional[str] = None


class Variant(BaseModel):
    id: str
    cls: VariantClass = Field(alias="class")
    hostile_seam: Optional[SeamName] = None
    qvlm_answer: QVLMAnswer = Field(default_factory=QVLMAnswer)
    endorse: list[str] = Field(default_factory=list)
    frozen: Frozen = Field(default_factory=Frozen)
    episode: Optional[Episode] = None
    screen: Optional[Screen] = None
    locate: Optional[LocateSpec] = None
    attest: Optional[AttestSpec] = None
    expect: Expect
    sink: Optional[Sink] = None
    note: str = ""

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def _one_channel(self) -> "Variant":
        chosen = [n for n, v in (("endorse", self.endorse),
                                 ("locate", self.locate),
                                 ("attest", self.attest)) if v]
        if len(chosen) > 1:
            raise ValueError(f"variant {self.id!r} declares more than one channel: {chosen}")
        return self

    @property
    def channel(self) -> str:
        if self.locate is not None:
            return "locate"
        if self.attest is not None:
            return "attest"
        return "endorse"


class Scenario(BaseModel):
    model_config = {"extra": "forbid"}
    scenario: str
    family: str = ""
    task_instruction: str
    episode: Episode = Field(default_factory=Episode)
    untrusted_content: str = ""
    request: Request
    variants: list[Variant]
    screen: Optional[Screen] = None
    frozen_defaults: Frozen = Field(default_factory=Frozen)
    note: str = ""

    def resolved_screen(self, v: Variant) -> Optional[Screen]:
        return v.screen or self.screen

    def resolved_episode(self, v: Variant) -> Episode:
        return v.episode or self.episode

    def resolved_frozen(self, v: Variant) -> Frozen:
        """Variant `frozen` overrides scenario `frozen_defaults`, field by field."""
        base = self.frozen_defaults.model_dump()
        for k, val in v.frozen.model_dump(exclude_unset=True).items():
            base[k] = val
        return Frozen(**base)


def load_cases(cases_dir: str | Path) -> list[Scenario]:
    out: list[Scenario] = []
    for path in sorted(Path(cases_dir).glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is None:
            continue
        sc = Scenario(**raw)
        out.append(sc)
    ids = [s.scenario for s in out]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate scenario ids: {sorted(dupes)}")
    return out
