"""Maps a benchmark variant onto whatever the sa_agent endorsement channel implements today.

The adapter is the only place that knows about the real code. Everything it cannot do yet raises
``Unsupported``, which the runner reports separately from a failure -- that split is what lets the
cases be written now, as executable acceptance criteria for milestones M0-M3b, without the
baseline reading as a wall of red.

Three stages, run in order:

  1. ``extract``     -- Layer 1 typed extraction (M0/M1). Not built: today ``query_qvlm`` returns
                        free text labeled by ``extraction_metadata`` with no acceptor at all. The
                        adapter therefore runs in *untyped passthrough* and records
                        ``typed_enforced: false``, so the report can show "type requested: URL,
                        type enforced: no" rather than silently pretending Layer 1 exists.
  2. ``endorse``  -- Layer 2 verifiers over a scalar, or -- when the variant's steps name the
                        Layer 3 stages (``parse``/``classify``/``contain`` plus the ladder rungs
                        it permits) -- the procedure channel, which is one call rather than one
                        per step. See ``_endorse_procedure``.
  3. ``sink``        -- optional tier gate, answering "would the value have been allowed to act?"

The screen channel (``locate`` / ``attest``, plan §4.1) replaces stages 1-2 with a single
``screen`` stage. It imports ``mm_agents.sa_agent.locate`` directly -- the benchmark and the
agent run the same file, never a copy.
"""

from __future__ import annotations

import contextlib
from typing import Any, Optional
from urllib.parse import urlparse

from mm_agents.sa_agent import endorse as dc
from mm_agents.sa_agent import locate as lc
from mm_agents.sa_agent.episode_capabilities import EpisodeCapabilities
from mm_agents.sa_agent.lattice import IntegrityLabel
from mm_agents.sa_agent.policies import Tier, derive_tier_min_level
from mm_agents.sa_agent import screen_labels as sl
from mm_agents.sa_agent import value_types as vt

from pathlib import Path

from .case import Frozen, Scenario, Variant
from .verdicts import Level, Outcome, Reason, Unsupported, Verdict

IMPLEMENTED_VERIFIERS = {"crosscheck", "crosscheck_prior", "checksum", "task_match",
                         "candidate_set", "registry", "directory", "domain_pin"}
PLANNED_VERIFIERS = {"k_of_n", "self_authored", "chrome_zone"}
PROCEDURE_PREFIX_STEPS = {"parse", "classify", "contain"}
PROCEDURE_TIER_STEPS = {"tier_a", "tier_b", "tier_c"}
PROCEDURE_STEPS = PROCEDURE_PREFIX_STEPS | PROCEDURE_TIER_STEPS

_LEVEL_FROM_ENUM = {lvl.name: Level(lvl.name) for lvl in IntegrityLabel.Level}

_MESSAGE_REASONS: list[tuple[str, Reason]] = [
    ("RECALL DIVERGED", Reason.RECALL_DIVERGED),
    ("NO PRIOR TO VERIFY", Reason.NO_PRIOR),
    ("PRIOR NOT ENDORSED", Reason.PRIOR_NOT_ENDORSED),
    ("PARSE FAILED", Reason.PARSE_FAILED),
    ("CAPABILITY SET UNAVAILABLE", Reason.PRECONDITION),
    ("CAPABILITY NOT AUTHORIZED", Reason.CAPABILITY_NOT_AUTHORIZED),
    ("STRUCTURAL MISMATCH", Reason.STRUCTURAL_MISMATCH),
    ("SCOPE VIOLATION", Reason.SCOPE_VIOLATION),
    ("CARDINALITY EXCEEDED", Reason.CARDINALITY_EXCEEDED),
    ("NO TRUSTED DERIVATION", Reason.NO_TRUSTED_DERIVATION),
    ("URL IDENTITY UNVERIFIED", Reason.URL_IDENTITY_UNVERIFIED),
    ("NOT ALLOWLISTED", Reason.NOT_ALLOWLISTED),
    ("QUERY NOT TASK DERIVED", Reason.QUERY_NOT_TASK_DERIVED),
    ("DOMAIN MISMATCH", Reason.DOMAIN_MISMATCH),
    ("NO SELF IDENTITY", Reason.NO_SELF_IDENTITY),
    ("DIRECTORY ENTRY REFUSED", Reason.DIRECTORY_ENTRY_REFUSED),
    ("NOT IN DIRECTORY", Reason.NOT_IN_DIRECTORY),
    ("NO DIRECTORY", Reason.NO_DIRECTORY),
    ("AMBIGUOUS:", Reason.AMBIGUOUS_DIRECTORY),
    ("NAME NOT IN TASK", Reason.NAME_NOT_IN_TASK),
    ("NAME TOO SHORT", Reason.NAME_TOO_SHORT),
    ("DERIVED ANSWER TYPE MISMATCH", Reason.ACCEPTOR_FAILED_DERIVED),
    ("ACCEPTOR FAILED", Reason.ACCEPTOR_FAILED),
    ("NOT IN REGISTRY", Reason.NOT_IN_REGISTRY),
    ("REGISTRY NOT SUFFICIENT", Reason.REGISTRY_INSUFFICIENT),
    ("VALUE NOT IN TASK", Reason.VALUE_NOT_IN_TASK),
    ("VALUE TOO SHORT", Reason.VALUE_NOT_IN_TASK),
    ("VALUE TOO GENERIC", Reason.VALUE_NOT_IN_TASK),
    ("TASK TEXT UNAVAILABLE", Reason.TASK_TEXT_UNAVAILABLE),
    ("TASK TOO UNSPECIFIC", Reason.TASK_TOO_UNSPECIFIC),
    ("NOT IN CANDIDATES", Reason.NOT_IN_CANDIDATES),
    ("TOO MANY CANDIDATES", Reason.NOT_IN_CANDIDATES),
    ("NO CANDIDATES", Reason.NOT_IN_CANDIDATES),
    ("UNKNOWN REGISTRY", Reason.OTHER),
    ("UNKNOWN ANSWER TYPE", Reason.OTHER),
    ("question contains the variable", Reason.QUESTION_ECHOES_VALUE),
    ("is not on the official or corroborating", Reason.NOT_ALLOWLISTED),
    ("trusted_url rejected:", Reason.URL_UNSAFE),
    ("REQUEST FAILED", Reason.FETCH_FAILED),
    ("NO MATCHES", Reason.NO_MATCHES),
    ("SOURCE INCONCLUSIVE", Reason.SOURCE_INCONCLUSIVE),
    ("CROSSCHECK DIVERGED", Reason.CROSSCHECK_DIVERGED),
    ("derived different answers", Reason.CHECKER_DISAGREEMENT),
    ("No variable named", Reason.PRECONDITION),
    ("carries no taint label", Reason.PRECONDITION),
    ("checksum", Reason.CHECKSUM_MISMATCH),
]


def _reason_for(message: str) -> Reason:
    for frag, reason in _MESSAGE_REASONS:
        if frag.lower() in message.lower():
            return reason
    return Reason.OTHER


class FrozenQLLM:
    """Seams S4/S5 (re-derivation) and S6 (parametric recall) frozen.

    Deliberately ignores *text*. A case that makes the fixture hostile (S3) must also state what
    the derivation becomes (S4) -- the seams are independent by construction, and inferring one
    from the other would smuggle a model back into a "frozen" run.

    S6 is a *separate* recorded value from S4 and defaults to abstaining. That separation is the
    point: recall's whole claim is that it is independent of the fetched content, so a harness that
    derived one from the other would be asserting the independence it is supposed to be testing.
    Abstain-by-default also means every pre-existing case keeps exercising the path it did before
    recall existed, rather than silently acquiring a second opinion.
    """

    def __init__(self, answer: Optional[str], recalled: Optional[str] = None):
        self.answer = answer
        self.recalled = recalled
        self.calls: list[str] = []
        self.recall_calls: list[str] = []

    def answer_question_from_text(self, question: str, text: str) -> str:
        import json
        self.calls.append(question)
        if self.answer is None:
            return json.dumps({"found": False})
        return json.dumps({"found": True, "answer": self.answer})

    def answer_question_from_recall(self, question: str) -> str:
        import json
        self.recall_calls.append(question)
        if self.recalled is None:
            return json.dumps({"found": False})
        return json.dumps({"found": True, "answer": self.recalled})


class LiveQLLM:
    """Placeholder for `--live S4`: a real Q-LLM over the fetched text. Phase 1."""

    def answer_question_from_text(self, question: str, text: str) -> str:
        raise Unsupported("live seam S4 (real Q-LLM re-derivation)", stage="endorse")


def _lenient_url_guard(url: str) -> Optional[str]:
    """Offline stand-in for ``_is_safe_public_url`` (seam S3b).

    The real guard calls ``socket.getaddrinfo``, i.e. it hits DNS -- which would make an
    "offline, deterministic" run depend on the network and on how the fixture host happens to
    resolve today. This keeps the parts that are decisions (scheme, hostname present) and drops
    the part that is I/O. ``--real-ssrf-guard`` restores the original.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"URL scheme '{parsed.scheme}' is not allowed; only http/https are permitted."
    if not parsed.hostname:
        return "URL has no hostname."
    return None


class Adapter:
    def __init__(self, http, record_dir: str = "", real_ssrf_guard: bool = False,
                 min_trusted_level: IntegrityLabel.Level = IntegrityLabel.Level.ENDORSED,
                 a11y_dir: Optional[str] = None):
        self.http = http
        self.record_dir = record_dir
        self.real_ssrf_guard = real_ssrf_guard
        self.min_trusted_level = min_trusted_level
        self.a11y_dir = Path(a11y_dir) if a11y_dir else Path(__file__).resolve().parent.parent / "fixtures" / "a11y"

    def _tree(self, sc: Scenario, v: Variant) -> str:
        scr = sc.resolved_screen(v)
        if scr is None or (scr.tree is None and scr.tree_file is None):
            raise ValueError(f"{sc.scenario}/{v.id}: screen channel needs a `screen.tree` "
                             "or `screen.tree_file`")
        if scr.tree is not None:
            return scr.tree
        path = self.a11y_dir / scr.tree_file
        if not path.exists():
            raise FileNotFoundError(f"a11y fixture not found: {path}")
        return path.read_text(encoding="utf-8")

    def _region_level(self, tree: str, bbox) -> Optional[str]:
        """Report-only: the label the located node's own region carries.

        Not asserted on. The *value* keeps the needle's level -- it IS the needle -- while the
        node reference is gated separately by the existing click path (`_resolve_target`/CAP_TIER).
        Recording it here is what makes the localisation claim visible in the report.
        """
        if not bbox:
            return None
        try:
            md = sl.region_metadata({"accessibility_tree": tree}, bbox)
            return md["integrity"].level.name
        except Exception:
            return None

    def screen(self, sc: Scenario, v: Variant) -> Outcome:
        """`locate` (trusted needle, deterministic) and `attest` (model-authored needle)."""
        tree = self._tree(sc, v)

        if v.locate is not None:
            spec = v.locate
            r = lc.locate(
                spec.needle, tree,
                region=spec.region,
                interactive_only=spec.interactive_only,
                exclude_tags=lc.ENTRY_TAGS if spec.exclude_entry else frozenset(),
            )
            detail: dict[str, Any] = {
                "channel": "locate",
                "needle_source": spec.needle_source,
                "locate": r.to_json(),
                "node_region_level": self._region_level(tree, r.bbox),
            }
            if r.status is lc.LocateStatus.UNIQUE:
                return Outcome(verdict=Verdict.ACCEPT, level=spec.needle_level, value=spec.needle,
                               stage="screen", message=f"located ({r.match_kind})", detail=detail)
            reason = {
                lc.LocateStatus.AMBIGUOUS: Reason.AMBIGUOUS_MATCH,
                lc.LocateStatus.NOT_FOUND: Reason.NOT_ON_SCREEN,
                lc.LocateStatus.NEEDLE_REJECTED: Reason.NEEDLE_REJECTED,
            }[r.status]
            return Outcome(verdict=Verdict.REJECT, reason=reason, stage="screen",
                           message=r.reason, detail=detail)

        spec_a = v.attest
        assert spec_a is not None
        if not v.qvlm_answer.found:
            return Outcome(verdict=Verdict.REJECT, reason=Reason.NOT_FOUND, stage="screen",
                           message="Q-VLM reported found=false",
                           detail={"channel": "attest"})
        claim = spec_a.claim if spec_a.claim is not None else (v.qvlm_answer.answer or "")
        a = lc.attest(claim, tree)
        detail = {"channel": "attest", "attest": a.to_json(),
                  "node_region_level": self._region_level(tree, a.bbox),
                  "whole_screen_level": self._whole_screen_level(tree)}
        if a.attested:
            return Outcome(verdict=Verdict.ACCEPT, level=spec_a.carried_level, value=claim,
                           stage="screen", message=f"attested ({a.match_kind})", detail=detail)
        reason = Reason.CLAIM_NOT_ATTESTABLE if "prose" in a.reason else Reason.CLAIM_NOT_ATTESTED
        return Outcome(verdict=Verdict.REJECT, reason=reason, stage="screen",
                       message=a.reason, detail=detail)

    def _whole_screen_level(self, tree: str) -> Optional[str]:
        try:
            return sl.extraction_metadata({"accessibility_tree": tree})["integrity"].level.name
        except Exception:
            return None

    def _value_type(self, sc: Scenario):
        """Resolve the case's requested type through the real catalog (M0)."""
        params = dict(sc.request.type_params)
        if sc.request.enum_values:
            params.setdefault("enum_values", sc.request.enum_values)
        try:
            return vt.resolve(sc.request.answer_type, params)
        except (KeyError, ValueError) as e:
            raise ValueError(f"{sc.scenario}: {e}") from e

    def extract(self, sc: Scenario, v: Variant) -> tuple[Optional[Outcome], dict[str, Any]]:
        """Returns (short_circuit_outcome | None, info).

        This is where the capacity argument is actually made: the requested type's acceptor runs
        against the Q-VLM's answer *before* it becomes a variable, so a shell command offered as
        a package name has nowhere to go. No detector is consulted, which is why there is nothing
        here for content to evade.
        """
        if set(v.endorse) & PROCEDURE_STEPS:
            return None, {"answer_type": sc.request.answer_type, "typed_enforced": False,
                          "bits_ledger": False, "layer": 3}
        vtype = self._value_type(sc)
        info: dict[str, Any] = {
            "answer_type": sc.request.answer_type,
            "typed_enforced": True,
            "bits_ledger": False,
            "shape": vtype.shape,
            "shape_floor": vtype.floor().level.name,
        }
        if not v.qvlm_answer.found:
            return Outcome(verdict=Verdict.REJECT, reason=Reason.NOT_FOUND, stage="extract",
                           message="Q-VLM reported found=false", detail=info), info
        answer = v.qvlm_answer.answer or ""
        info["bits"] = round(vtype.bits(answer), 1)
        if not vtype.accepts(answer):
            return Outcome(verdict=Verdict.REJECT, reason=Reason.ACCEPTOR_FAILED, stage="extract",
                           bits=info["bits"], detail=info,
                           message=f"the extracted answer is not a well-formed "
                                   f"{vtype.name} ({vtype.shape} shape)"), info
        return None, info

    def endorse(self, sc: Scenario, v: Variant) -> Outcome:
        steps = list(v.endorse)
        if not steps:
            raise Unsupported("variant declares no endorsement step", stage="endorse")
        unknown = [s for s in steps if s not in IMPLEMENTED_VERIFIERS | PROCEDURE_STEPS]
        if unknown:
            planned = [s for s in unknown if s in PLANNED_VERIFIERS]
            bogus = [s for s in unknown if s not in PLANNED_VERIFIERS]
            if bogus:
                raise ValueError(f"unknown verifier(s) {bogus} in {sc.scenario}/{v.id}")
            raise Unsupported(f"verifier(s) {sorted(planned)} not implemented", stage="endorse")
        if set(steps) & PROCEDURE_STEPS:
            if not PROCEDURE_PREFIX_STEPS <= set(steps):
                raise ValueError(f"{sc.scenario}/{v.id}: a procedure variant must list all of "
                                 f"{sorted(PROCEDURE_PREFIX_STEPS)} -- they are not optional "
                                 f"stages, they are what every procedure goes through")
            if set(steps) & set(IMPLEMENTED_VERIFIERS):
                raise ValueError(f"{sc.scenario}/{v.id}: Layer-3 steps and Layer-2 verifiers "
                                 f"address different things (a procedure vs a scalar); a variant "
                                 f"mixing them is asserting nothing coherent")
            return self._endorse_procedure(sc, v, steps)

        frozen = sc.resolved_frozen(v)
        ep = sc.resolved_episode(v)
        var = "qvlm-answer.value"
        memory: dict[str, Any] = {var: v.qvlm_answer.answer}
        label_store: dict[str, Any] = {var: sl.untrusted_public()}

        second = (FrozenQLLM(frozen.derived_answer_2, frozen.recalled_answer_2)
                  if frozen.second_checker else None)
        mgr = dc.EndorsementManager(
            env=None,
            qllm_agent=FrozenQLLM(frozen.derived_answer, frozen.recalled_answer),
            qllm_agent_second=second,
            memory=memory,
            label_store=label_store,
            trusted_domains=ep.trusted_domains,
            example_result_dir=self.record_dir,
            trusted_domains_corroborating=ep.trusted_domains_corroborating,
            recall_precheck=frozen.recall_precheck,
            task_instruction=sc.task_instruction,
            require_task_derived_evidence=ep.require_task_derived_evidence,
            self_identity=ep.self_identity,
            directory=ep.directory,
        )
        vtype = self._value_type(sc)
        initial = label_store[var].left.level.value
        params = dict(sc.request.type_params)

        statuses: list[tuple[str, str, str]] = []
        with self._patched():
            for step in steps:
                if step in ("crosscheck", "crosscheck_prior"):
                    self.http.override = frozen.fixture
                    res = mgr.crosscheck(
                        variable_name=var,
                        question=frozen.question or sc.request.question,
                        trusted_url=frozen.trusted_url or "",
                        hint=frozen.hint or "",
                        answer_type=sc.request.answer_type,
                        type_params=params,
                        verify_prior=(step == "crosscheck_prior"),
                    )
                    self.http.override = None
                elif step == "task_match":
                    res = mgr.task_match(var, answer_type=sc.request.answer_type,
                                         type_params=params)
                elif step == "candidate_set":
                    if not sc.request.candidates:
                        raise ValueError(f"{sc.scenario}/{v.id}: candidate_set needs "
                                         f"`request.candidates`")
                    res = mgr.candidate_set(var, sc.request.candidates,
                                            answer_type=sc.request.answer_type,
                                            type_params=params)
                elif step == "domain_pin":
                    res = mgr.domain_pin(var)
                elif step == "directory":
                    if not sc.request.display_name:
                        raise ValueError(f"{sc.scenario}/{v.id}: directory needs "
                                         f"`request.display_name`")
                    res = mgr.directory(var, sc.request.display_name)
                elif step == "registry":
                    res = mgr.registry(var, registry=str(params.get("registry") or ""),
                                       answer_type=sc.request.answer_type, type_params=params)
                else:
                    self.http.override = None
                    raise Unsupported("checksum needs a VM (env=None offline)",
                                      stage="endorse")
                out = res.root.value
                statuses.append((step, out.status, out.message))

        self.http.override = None
        final = label_store[var]
        detail = {
            "trusted_url": frozen.trusted_url,
            "recalled_answer": frozen.recalled_answer,
            "recall_precheck": frozen.recall_precheck,
            "derived_answer": frozen.derived_answer,
            "derived_answer_2": frozen.derived_answer_2 if frozen.second_checker else None,
            "steps": {step: status for step, status, _ in statuses},
            "verifiers_passed": list(mgr.verifier_ledger.get(var, [])),
            "http_hits": list(self.http.hits),
        }
        if final.left.level.value < initial:
            lvl = _LEVEL_FROM_ENUM[final.left.level.name]
            ok_msgs = [m for _, st, m in statuses if st == "OK"]
            return Outcome(verdict=Verdict.ACCEPT, level=lvl, value=str(memory[var]),
                           bits=round(vtype.bits(memory[var]), 1),
                           stage="endorse", message=ok_msgs[-1] if ok_msgs else "",
                           detail=detail)
        failed = [(st, m) for _, st, m in statuses if st != "OK"]
        message = failed[-1][1] if failed else "no verifier raised the label"
        return Outcome(verdict=Verdict.REJECT, reason=_reason_for(message),
                       stage="endorse", message=message, detail=detail)

    def _endorse_procedure(self, sc: Scenario, v: Variant, steps: list) -> Outcome:
        """The procedure channel (plan §5): parse -> classify -> contain -> escape ladder.

        One ``procedure()`` call, not one call per listed step. The steps are not independent
        verifiers that compose the way Layer 2's do -- ``classify`` on an unparsed string is not a
        thing -- so running them separately would be modelling a pipeline the code does not have.
        What the case's step list *does* control is which rungs of the ladder are permitted, and
        that distinction is load-bearing: the benign and hostile halves of the same document both
        pass containment and are separated at Tier B alone.
        """
        ep = sc.resolved_episode(v)
        frozen = sc.resolved_frozen(v)
        if ep.authorized_capabilities is None:
            raise ValueError(f"{sc.scenario}/{v.id}: a procedure variant needs "
                             f"`episode.authorized_capabilities` (plan §5.3)")
        if not v.qvlm_answer.found:
            return Outcome(verdict=Verdict.REJECT, reason=Reason.NOT_FOUND, stage="endorse",
                           message="Q-VLM reported found=false")

        var = "qvlm-answer.value"
        memory: dict[str, Any] = {var: v.qvlm_answer.answer}
        label_store: dict[str, Any] = {var: sl.untrusted_public()}
        mgr = dc.EndorsementManager(
            env=None,
            qllm_agent=FrozenQLLM(frozen.derived_answer, frozen.recalled_answer),
            qllm_agent_second=None,
            memory=memory,
            label_store=label_store,
            trusted_domains=ep.trusted_domains,
            example_result_dir=self.record_dir,
            trusted_domains_corroborating=ep.trusted_domains_corroborating,
            task_instruction=sc.task_instruction,
            require_task_derived_evidence=ep.require_task_derived_evidence,
            self_identity=ep.self_identity,
            directory=ep.directory,
            episode_capabilities=EpisodeCapabilities.from_names(ep.authorized_capabilities),
            scope_root=ep.scope_root or "",
        )
        tiers = [t for t in ("tier_a", "tier_b", "tier_c") if t in set(steps)]
        initial = label_store[var].left.level.value
        with self._patched():
            res = mgr.procedure(var, ladder=",".join(tiers) if tiers else "none",
                                trusted_url=frozen.trusted_url or "")
        self.http.override = None
        out = res.root.value
        final = label_store[var]
        detail = {"layer": 3, "trace": mgr.procedure_trace,
                  "trusted_url": frozen.trusted_url,
                  "http_hits": list(self.http.hits)}
        if final.left.level.value < initial:
            return Outcome(verdict=Verdict.ACCEPT, level=_LEVEL_FROM_ENUM[final.left.level.name],
                           value=str(memory[var]), stage="endorse", message=out.message,
                           detail=detail)
        return Outcome(verdict=Verdict.REJECT, reason=_reason_for(out.message),
                       stage="endorse", message=out.message, detail=detail)

    @contextlib.contextmanager
    def _patched(self):
        """Swap the two I/O seams inside endorse.py for their deterministic stand-ins."""
        orig_fetch = dc.fetch_url_text
        orig_json = dc.fetch_url_json
        orig_guard = dc._is_safe_public_url
        dc.fetch_url_text = self.http.fetch_url_text
        dc.fetch_url_json = self.http.fetch_url_json
        if not self.real_ssrf_guard:
            dc._is_safe_public_url = _lenient_url_guard
        try:
            yield
        finally:
            dc.fetch_url_text = orig_fetch
            dc.fetch_url_json = orig_json
            dc._is_safe_public_url = orig_guard

    def sink(self, sc: Scenario, v: Variant, outcome: Outcome) -> dict[str, Any]:
        """Would the endorsed value have been allowed to drive the tool call?

        Uses the real floor table so the answer tracks ``--sa_min_trusted_level`` the way the
        agent does, rather than restating the policy here.
        """
        if v.sink is None:
            return {}
        cutoff = (IntegrityLabel.Level[v.sink.min_trusted_level]
                  if v.sink.min_trusted_level else self.min_trusted_level)
        floors = derive_tier_min_level(cutoff)
        floor = floors[Tier[v.sink.tier]]
        if outcome.verdict != Verdict.ACCEPT or outcome.level is None:
            got = "BLOCK"
        else:
            got = "ALLOW" if outcome.level.rank <= floor.value else "BLOCK"
        return {"floor": floor.name, "cutoff": cutoff.name, "got": got,
                "expected": v.sink.expect, "ok": got == v.sink.expect}
