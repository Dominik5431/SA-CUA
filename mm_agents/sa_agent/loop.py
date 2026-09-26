import logging
from typing import Literal, Optional

from openai.types.chat import ChatCompletionUserMessageParam

from mm_agents import token_usage
from mm_agents.utils.loop import Loop
from mm_agents.sa_agent.tool_scheme import Tool
from mm_agents.sa_agent.lattice import IntegrityLabel, InverseLattice, PowersetLattice, ProductLabel
from mm_agents.sa_agent.qllm import QLLMAgent
from mm_agents.sa_agent.prompts import (
    SYST_PROMPT_PLLM_FULL_EXPOSURE,
    SYST_PROMPT_PLLM_NO_EXPOSURE,
    build_pllm_prompt,
    build_sa_pllm_prompt,
    render_initial_state_note,
    SYST_DEF_1,
    SYST_DEF_2,
    SYST_DEF_3,
)
from mm_agents.sa_agent.policies import SAPolicy, derive_tier_min_level
from mm_agents.sa_agent.screen_labels import (
    FileOwnershipProbe,
    FileProvenanceMap,
    configure_min_trusted_level,
    configure_prompt_trusted_domains,
    configure_prompt_trusted_senders,
    configure_self_identity,
    configure_visit_domains,
    trusted_public,
    untrusted_public,
)
from mm_agents.sa_agent.endorse import EndorsementManager
from mm_agents.sa_agent.episode_capabilities import resolve_episode_capabilities
from mm_agents.sa_agent.trust_detection import (
    resolve_episode_self_identity,
    resolve_episode_trusted_domains,
    resolve_episode_trusted_senders,
    resolve_specification,
)

logger = logging.getLogger("desktopenv.experiment")

_MIN_TRUSTED_LEVEL_CHOICES: dict[str, "IntegrityLabel.Level"] = {
    "trusted": IntegrityLabel.Level.TRUSTED,
    "self_authored": IntegrityLabel.Level.SELF_AUTHORED,
    "endorsed": IntegrityLabel.Level.ENDORSED,
    "computed": IntegrityLabel.Level.COMPUTED_PLATFORM_VALUE,
    "free_text": IntegrityLabel.Level.FREE_TEXT_THIRD_PARTY,
}
from mm_agents.sa_agent.tools import (
    run_vlm,
    find,
    summarize_screenshot_content,
    checkdone,
    move_to,
    click,
    mouse_down,
    mouse_up,
    right_click,
    hscroll,
    vscroll,
    type_text,
    key_down,
    key_up,
    hotkey,
    get_page_elements,
    get_page_text,
    find_element_by_text,
    verify_hypothesis,
)


def _as_planner_model(model_str: str) -> str:
    """
    Normalize a model string to the '<provider>:<model_name>' format expected by
    the Fides P-LLM (see get_tool_response in loop_and_planner*.py).

    --model (and other agents' *_model args) follow this repo's own convention of
    '<provider> | <model_name>' instead, so when --fides_p_llm isn't given and we
    fall back to --model, translate the delimiter rather than passing it through.
    """
    if ":" in model_str:
        return model_str
    if "|" in model_str:
        provider, _, name = model_str.partition("|")
        return f"{provider.strip()}:{name.strip()}"
    return model_str


class FidesOpenCUALoop(Loop):
    """
    Wraps FidesOpenCUAAgent in a real Fides tool-calling LabeledPlanningLoop
    (mm_agents/fides/loop_and_planner.py for "full_exposure", or
    mm_agents/fides/loop_and_planner_no_exposure.py for "no_exposure"),
    mirroring the construction recipe in mm_agents/fides/run.py's test().

    reset()/loop() translate between the generic Loop interface (instruction,
    obs) driven by lib_run_single.run_single_example and the Fides-specific
    LabeledPlanningLoop.loop(msg, label) tool-calling loop.
    """

    def __init__(
        self,
        agent,
        env,
        example_result_dir,
        max_steps,
        args,
        variant: Literal["full_exposure", "no_exposure"] = "no_exposure",
        p_llm: Optional[str] = None,
        q_llm: Optional[str] = None,
        q_llm_second_check: Optional[str] = None,
        system_defense_level: int = 1,
        model_defense_level: int = 0,
        token_log_path: Optional[str] = None,
    ):
        self.agent = agent
        self.env = env
        self.example_result_dir = example_result_dir
        self.max_steps = max_steps
        self.args = args
        self.variant = variant
        self.p_llm = _as_planner_model(p_llm or args.model)
        self.q_llm = _as_planner_model(q_llm or self.p_llm)
        q_llm_second_check = _as_planner_model(q_llm_second_check) if q_llm_second_check else None
        token_usage.register_role("q_vlm", args.model)
        token_usage.register_role("planner", self.p_llm)
        token_usage.register_role("q_llm", self.q_llm)
        token_usage.register_role("q_llm_second", q_llm_second_check)

        if variant == "no_exposure":
            from mm_agents.sa_agent.loop_and_planner_no_exposure import LabeledBasicPlanner, LabeledPlanningLoop
        else:
            from mm_agents.sa_agent.loop_and_planner import LabeledBasicPlanner, LabeledPlanningLoop

        python_tools = [
            run_vlm,
            find,
            summarize_screenshot_content,
            checkdone,
            move_to,
            click,
            mouse_down,
            mouse_up,
            right_click,
            hscroll,
            vscroll,
            type_text,
            key_down,
            key_up,
            hotkey,
            get_page_elements,
            get_page_text,
            find_element_by_text,
            verify_hypothesis,
        ]
        if system_defense_level >= 1:
            python_tools.remove(run_vlm)
        if system_defense_level >= 2:
            for t in (summarize_screenshot_content, get_page_elements, get_page_text, find_element_by_text, verify_hypothesis):
                python_tools.remove(t)
        tools = [Tool.from_callable(t) for t in python_tools]

        qllm_agent = QLLMAgent(
            action_space=args.action_space,
            observation_type="a11y_tree",
            max_trajectory_length=args.max_trajectory_length,
            model=self.q_llm,
            token_log_path=token_log_path,
        )

        functions_block = SYST_DEF_1 if system_defense_level == 0 else SYST_DEF_2 if system_defense_level == 1 else SYST_DEF_3
        exposure_block = SYST_PROMPT_PLLM_NO_EXPOSURE if variant == "no_exposure" else SYST_PROMPT_PLLM_FULL_EXPOSURE
        system_prompt = build_pllm_prompt(functions_block=functions_block, exposure_block=exposure_block)

        universe = frozenset({})
        self._initial_label = ProductLabel(
            IntegrityLabel.trusted(),
            InverseLattice(PowersetLattice(subset=universe, universe=universe)),
        )

        if variant == "no_exposure":
            qllm_agent_second = (
                QLLMAgent(
                    action_space=args.action_space,
                    observation_type="a11y_tree",
                    max_trajectory_length=args.max_trajectory_length,
                    model=q_llm_second_check,
                    token_log_path=token_log_path,
                )
                if q_llm_second_check else None
            )
            planner = LabeledBasicPlanner(
                tools=tools,
                state=[{"role": "system", "content": system_prompt}],
                model=self.p_llm,
            )
            self._fides_loop = LabeledPlanningLoop(
                planner=planner,
                model=self.p_llm,
                tools=tools,
                policy=[],
                qllm_agent=qllm_agent,
                qllm_agent_second=qllm_agent_second,
                agent=agent,
                env=env,
                example_result_dir=example_result_dir,
                step_idx=0,
                max_steps=max_steps,
                observation_type=args.observation_type,
                model_defense_level=model_defense_level,
                token_log_path=token_log_path,
            )
        else:
            planner = LabeledBasicPlanner(
                state=[{"role": "system", "content": system_prompt}],
                tools=tools,
            )
            self._fides_loop = LabeledPlanningLoop(
                planner=planner,
                model=self.p_llm,
                tools=tools,
                policy=[],
                qllm_agent=qllm_agent,
                agent=agent,
                env=env,
                example_result_dir=example_result_dir,
                step_idx=0,
                max_steps=max_steps,
                observation_type=args.observation_type,
                model_defense_level=model_defense_level,
                token_log_path=token_log_path,
            )

    def reset(self, tags, runtime_logger):
        self.agent.reset(tags, _logger=runtime_logger)
        self._fides_loop.example_result_dir = self.example_result_dir
        self._fides_loop.step_idx = 0
        self._fides_loop.turn = 0

    def loop(self, instruction: str, obs: dict) -> str:
        user_message = ChatCompletionUserMessageParam(role="user", content=instruction)
        response, _label = self._fides_loop.loop(user_message, label=self._initial_label)
        return response


class SAAgentLoop(Loop):
    """
    SA-Agent: FidesOpenCUALoop's no_exposure planning loop extended into a
    dual-LLM IFC/taint-tracking agent. On top of the fides variable-passing
    machinery it adds:

    * zone-based screen taint labels (screen_labels.label_screen) attached to
      every Q-LLM extraction / located coordinate (see tools.py);
    * a deterministic pre-execution policy gate (SAPolicy) that blocks
      consequential tool calls whose arguments/targets are UNTRUSTED;
    * two hybrid tools (host-side web fetch, VM terminal); and
    * two verified endorsement tools (web cross-check, sha256) — the only
      sanctioned way to clear integrity taint.

    Harness Loop signature: (agent, env, example_result_dir, max_steps, args).
    Always uses the no_exposure variant. Reads its configuration from the
    --sa_* args added in run.py.
    """

    def __init__(self, agent, env, example_result_dir, max_steps, args):
        self.agent = agent
        self.env = env
        self.example_result_dir = example_result_dir
        self.max_steps = max_steps
        self.args = args

        self.p_llm = _as_planner_model(getattr(args, "sa_p_llm", None) or args.model)
        self.q_llm = _as_planner_model(getattr(args, "sa_q_llm", None) or self.p_llm)
        _q_second_raw = getattr(args, "sa_q_llm_second_check", None)
        q_llm_second = _as_planner_model(_q_second_raw) if _q_second_raw else None
        token_usage.register_role("q_vlm", args.model)
        token_usage.register_role("planner", self.p_llm)
        token_usage.register_role("q_llm", self.q_llm)
        token_usage.register_role("q_llm_second", q_llm_second)

        system_defense_level = getattr(args, "sa_system_defense_level", 1)
        model_defense_level = getattr(args, "sa_model_defense_level", 0)
        token_log_path = getattr(args, "sa_token_log_path", None)
        trusted_domains = [
            d.strip() for d in (getattr(args, "sa_trusted_domains", "") or "").split(",") if d.strip()
        ]
        self.trusted_domains = trusted_domains
        trusted_domains_corroborating = [
            d.strip() for d in (getattr(args, "sa_trusted_domains_corroborating", "") or "").split(",") if d.strip()
        ]
        self.trusted_domains_corroborating = trusted_domains_corroborating
        min_trusted_level_choice = getattr(args, "sa_min_trusted_level", "endorsed")
        resolved_min_trusted_level = _MIN_TRUSTED_LEVEL_CHOICES.get(
            min_trusted_level_choice, IntegrityLabel.Level.ENDORSED
        )
        configure_min_trusted_level(resolved_min_trusted_level)

        if getattr(args, "sa_allow_corroborated_destructive", False):
            logger.warning(
                "--sa_allow_corroborated_destructive is deprecated and now a no-op: tier floors "
                "are derived automatically from --sa_min_trusted_level (see "
                "policies.derive_tier_min_level) so the redaction cutoff and the policy gate can "
                "never disagree about what's already visible to the planner. Use "
                "--sa_min_trusted_level=endorsed (or looser) if you want ENDORSED to "
                "suffice for DESTRUCTIVE actions too."
            )

        self.trust_confirmation_mode = getattr(args, "sa_trust_confirmation", "off")
        self.spec_classifier_mode = getattr(args, "sa_spec_classifier", "loose")
        self.layer3_mode = getattr(args, "sa_layer3", "off")
        self.operator_capabilities = getattr(args, "sa_authorized_capabilities", "") or ""
        self.scope_root = getattr(args, "sa_scope_root", "") or ""

        from mm_agents.sa_agent.loop_and_planner_no_exposure import (
            LabeledBasicPlanner,
            LabeledPlanningLoop,
        )
        from mm_agents.sa_agent.tool_registry import SA_TOOLS, SA_TOOL_NAMES

        DEFENSE_LEVEL_DISABLES: dict[int, frozenset[str]] = {
            0: frozenset(),
            1: frozenset(),
            2: frozenset({"query_qvlm"}),
        }
        assert getattr(args, "sa_mask_mode", "deny") != "off", (
            "--sa_mask_mode=off is no longer supported: the SA planner is masked-only. Its "
            "system prompt states flatly that the redacted screenshot is its sole observation, "
            "and no action_list is sent in any mode. Run the unmasked baseline with PromptAgent "
            "instead."
        )
        disabled_by_config: set[str] = set()
        disabled_tools = {
            t.strip() for t in (getattr(args, "sa_disabled_tools", "") or "").split(",") if t.strip()
        }
        unknown = disabled_tools - SA_TOOL_NAMES
        assert not unknown, (
            f"--sa_disabled_tools names unknown tool(s): {sorted(unknown)}. "
            f"Valid names: {sorted(SA_TOOL_NAMES)}"
        )
        effective_disabled = (DEFENSE_LEVEL_DISABLES[system_defense_level] | disabled_tools
                              | disabled_by_config)
        active_entries = [e for e in SA_TOOLS if e.name not in effective_disabled]
        self.active_tool_names = frozenset(e.name for e in active_entries)

        tools = [e.build(trusted_domains, trusted_domains_corroborating) for e in active_entries]

        qllm_agent = QLLMAgent(
            action_space=args.action_space,
            observation_type="a11y_tree",
            max_trajectory_length=args.max_trajectory_length,
            model=self.q_llm,
            token_log_path=token_log_path,
        )
        qllm_agent_second = (
            QLLMAgent(
                action_space=args.action_space,
                observation_type="a11y_tree",
                max_trajectory_length=args.max_trajectory_length,
                model=q_llm_second,
                token_log_path=token_log_path,
            )
            if q_llm_second else None
        )

        from mm_agents.sa_agent.masking import MaskConfig, RuleCache, normalize_model_str
        mask_mode = getattr(args, "sa_mask_mode", "off")
        _raw_propose = getattr(args, "sa_propose_model", None)
        if _raw_propose is None:
            propose_model = normalize_model_str(getattr(args, "model", None))
            if propose_model and mask_mode != "off":
                logger.info("[sa][mask] no --sa_propose_model given -- proposing rules with "
                            "--model (%s)", propose_model)
        else:
            propose_model = normalize_model_str(_raw_propose)
        if mask_mode != "off":
            token_usage.register_role("rule_proposer", propose_model)
        self.mask_config = MaskConfig(
            mode=mask_mode,
            style=getattr(args, "sa_mask_style", "fill"),
            click_policy=getattr(args, "sa_mask_click_policy", "block"),
            on_unknown_domain=getattr(args, "sa_mask_on_unknown_domain", "abort"),
            on_native_app=getattr(args, "sa_mask_on_native_app", "app_rules"),
            default_deny=True,
            rule_caching=getattr(args, "rule_caching", True),
            propose_model=propose_model,
            propose_vllm_port=getattr(args, "sa_propose_vllm_port", None),
            rules=RuleCache(rules_dir=getattr(args, "sa_rules_dir", None),
                            model=getattr(args, "sa_rules_model", None),
                            cache_dir=getattr(args, "sa_rules_cache_dir", "rules_cache"),
                            category_dirs={"ucm": getattr(args, "sa_ucm_rules_dir",
                                                          "llm_rules_container_v19")},
                            strict=True),
        ) if mask_mode != "off" else None

        query_screen_output = getattr(args, "sa_query_screen_output", "labeled") or "labeled"
        system_prompt = build_sa_pllm_prompt(
            active_entries,
            trusted_domains=trusted_domains,
            trusted_domains_corroborating=trusted_domains_corroborating,
            icl_pack=getattr(args, "sa_icl_pack", None),
            query_screen_output=query_screen_output,
        )
        self._base_system_prompt = system_prompt

        self._initial_label = trusted_public()

        planner = LabeledBasicPlanner(
            tools=tools,
            state=[{"role": "system", "content": system_prompt}],
            model=self.p_llm,
        )
        if query_screen_output == "clear":
            planner.clear_result_tools = frozenset({"query_screen"})
            logger.info("[sa][ablation] query_screen answers are shown to the planner unredacted")

        policy_tier_min_level = derive_tier_min_level(resolved_min_trusted_level)

        self.sa_policy = SAPolicy(
            mode=getattr(args, "sa_policy_mode", "enforce"),
            strict_targets=getattr(args, "sa_strict_targets", False),
            trusted_domains=trusted_domains,
            trusted_domains_corroborating=trusted_domains_corroborating,
            trace_path=None,
            tier_min_level=policy_tier_min_level,
            mask_click_policy=getattr(args, "sa_mask_click_policy", "block"),
        )

        self.endorse_manager = EndorsementManager(
            env=env,
            qllm_agent=qllm_agent,
            qllm_agent_second=qllm_agent_second,
            memory=planner.memory,
            label_store=planner.label_store,
            trusted_domains=trusted_domains,
            trusted_domains_corroborating=trusted_domains_corroborating,
            example_result_dir=example_result_dir,
            require_task_derived_evidence=getattr(args, "sa_task_derived_evidence", False),
            scope_root=getattr(args, "sa_scope_root", "") or "",
        )

        self.file_owner_probe = FileOwnershipProbe(
            env=env, enabled=getattr(args, "sa_file_owner_shortcut", "on") != "off")
        self.file_provenance_map = FileProvenanceMap(env=env, owner_probe=self.file_owner_probe)

        history_window = getattr(args, "sa_history_window", -1)
        if history_window is None or int(history_window) < 0:
            history_window = int(getattr(args, "max_trajectory_length", 10) or 0)
        history_window = int(history_window)
        logger.info("[sa] planner trajectory window: %s",
                    f"the last {history_window} tool-call elements (call + result + screenshot)"
                    if history_window else "off (append-only, one ephemeral screenshot per call)")

        self._fides_loop = LabeledPlanningLoop(
            planner=planner,
            model=self.p_llm,
            tools=tools,
            policy=[],
            qllm_agent=qllm_agent,
            qllm_agent_second=qllm_agent_second,
            agent=agent,
            env=env,
            example_result_dir=example_result_dir,
            step_idx=0,
            max_steps=max_steps,
            observation_type=args.observation_type,
            model_defense_level=model_defense_level,
            token_log_path=token_log_path,
            sa_policy=self.sa_policy,
            endorse_manager=self.endorse_manager,
            principal_map=None,
            file_provenance_map=self.file_provenance_map,
            mask_config=self.mask_config,
            history_window=history_window,
        )

    def set_episode_trusted_domains(self, official, corroborating) -> None:
        """Extend this episode's VERIFICATION allowlists with hosts the benign config declares.

        Not the same thing as `configure_prompt_trusted_domains`, and the two are worth keeping
        apart. That one is the ZONE side -- which pixels are shown and which controls are clickable
        -- and it is derived by a model from the instruction. This is the VERIFICATION side, read by
        `endorse` (crosscheck/listing/allowlist) and by the `fetch_web_content` host gate, and it
        is read verbatim from the operator-authored benign config, so no model stands between the
        operator's intent and the list.

        Why a task needs it at all: the CLI allowlist names documentation sources that generalise
        across every task (`docs.python.org`, `manpages.ubuntu.com`). A task whose subject IS a
        particular third party -- unsubscribe me from these newsletters -- cannot be served that
        way; the hosts are the task's own data, they differ per fixture, and putting them on the
        global list would vouch for them in every other episode too. Episode-scoped, reset in
        reset(), so exactly that does not happen.
        """
        official = [d.strip() for d in (official or []) if str(d).strip()]
        corroborating = [d.strip() for d in (corroborating or []) if str(d).strip()]
        merged = list(self.trusted_domains) + official
        merged_corr = list(self.trusted_domains_corroborating) + corroborating
        for consumer in (self.endorse_manager, self.sa_policy):
            if consumer is None:
                continue
            consumer.trusted_domains = list(merged)
            consumer.trusted_domains_corroborating = list(merged_corr)
        configure_visit_domains(list(merged) + list(merged_corr))
        if official or corroborating:
            logger.info("[sa] episode verification hosts: official=%s corroborating=%s",
                        official, corroborating)

    def set_episode_vm_hosted_domains(self, domains) -> None:
        """Hosts this episode serves from inside the VM, so a verifier fetches them there.

        Separate from the trust lists above and doing a different job: those decide WHETHER a
        host may be used as evidence, this decides WHERE the page is fetched from. A domain
        usually needs both -- the unsubscribe fixtures put each benign publisher on
        `trusted_domains_corroborating` (so crosscheck will accept it) and here (so the fetch
        reaches the guest's mock rather than the real internet).

        Deliberately NOT inferred from the trust lists. Other suites put genuinely public hosts
        on the corroborating list, and routing those through the guest would change what they
        resolve to. Reset per episode in reset(), like the trust lists.
        """
        domains = [d.strip() for d in (domains or []) if str(d).strip()]
        if self.endorse_manager is not None:
            self.endorse_manager.vm_hosted_domains = list(domains)
        from mm_agents.sa_agent.hybrid_tools import set_episode_vm_route
        set_episode_vm_route(
            getattr(getattr(self.endorse_manager, "env", None) or getattr(self, "env", None),
                    "controller", None),
            domains)
        if domains:
            logger.info("[sa] episode VM-hosted verification hosts: %s", domains)

    def reset(self, tags, runtime_logger):
        self.agent.reset(tags, _logger=runtime_logger)
        self._fides_loop.example_result_dir = self.example_result_dir
        self._fides_loop.step_idx = 0
        self._fides_loop.turn = 0
        planner = self._fides_loop.planner
        planner.history = [planner.history[0]]
        planner.memory.clear()
        planner.label_store.clear()
        planner.pending_result_label = trusted_public()
        planner.last_var_refs = []
        self._fides_loop._consecutive_blocks = 0
        self._fides_loop._stale_repeat_count = 0
        self._fides_loop._last_was_ui_action = False
        self._fides_loop._dropped_history = []
        self._fides_loop._preamble_len = 0
        self._fides_loop._windowed_mask = None
        self._fides_loop._mask_base = None
        self._fides_loop._last_mask = None
        self._fides_loop._mask_obs = None
        self._fides_loop._mask_rects_n = 0
        if getattr(self._fides_loop, "sa_policy", None) is not None:
            self._fides_loop.sa_policy.set_mask(None)
        self._fides_loop._clipboard_label = untrusted_public()
        self._fides_loop._last_clipboard_content = None
        self._fides_loop._xclip_available = None
        self.endorse_manager.example_result_dir = self.example_result_dir
        dm = self.endorse_manager
        dm.endorsed_rects.clear()
        dm.verifier_ledger.clear()
        dm._uncapped_grade.clear()
        dm._task_kw = None
        dm.procedure_trace = {}
        dm.seen_clear_identifiers = {}
        self.sa_policy.trace_path = (
            f"{self.example_result_dir}/policy_trace.jsonl" if self.example_result_dir else None
        )
        self.file_provenance_map.reset_episode()
        self.set_episode_trusted_domains([], [])
        self.set_episode_vm_hosted_domains([])
        configure_prompt_trusted_domains([])
        configure_prompt_trusted_senders([])
        configure_self_identity(None)
        self._fides_loop._cached_obs_raw = None
        if getattr(self, "env", None) is not None:
            self.env._last_settled_obs = None

    def loop(self, instruction: str, obs: dict) -> str:
        note = render_initial_state_note(obs.get("accessibility_tree", "") or "")
        decision, payload = resolve_specification(
            model=self.p_llm,
            instruction=instruction,
            initial_state_note=note,
            mode=self.spec_classifier_mode,
            auto_answer=getattr(self, "_spec_auto_answer", "") or "",
            loose_instruction=getattr(self, "_spec_loose_instruction", "") or "",
        )
        if decision == "halt":
            logger.info("SAAgentLoop: halting for specification -- %s", payload)
            return payload
        instruction = payload
        episode_trusted_domains = resolve_episode_trusted_domains(
            model=self.p_llm,
            instruction=instruction,
            initial_state_note=note,
            mode=self.trust_confirmation_mode,
        )
        configure_prompt_trusted_domains(episode_trusted_domains)
        episode_self_identity = resolve_episode_self_identity(
            model=self.p_llm,
            instruction=instruction,
            initial_state_note=note,
            mode=self.trust_confirmation_mode,
        )
        configure_self_identity(episode_self_identity)
        episode_trusted_senders = resolve_episode_trusted_senders(
            model=self.p_llm,
            instruction=instruction,
            initial_state_note=note,
            mode=self.trust_confirmation_mode,
        )
        configure_prompt_trusted_senders(episode_trusted_senders)
        self.endorse_manager.task_instruction = instruction
        episode_caps = resolve_episode_capabilities(
            model=self.p_llm,
            instruction=instruction,
            initial_state_note=note,
            mode=self.layer3_mode,
            operator_set=[c.strip() for c in self.operator_capabilities.split(",") if c.strip()],
        )
        self.endorse_manager.episode_capabilities = episode_caps
        starting_point = "## YOUR STARTING POINT\n" + note
        if episode_trusted_domains:
            starting_point += (
                "\n\n**Dynamic trust for this episode:** "
                + ", ".join(episode_trusted_domains)
                + " — the task instruction named or clearly implied these, so their own "
                "site-authored structural/navigational content is TRUSTED. Community/discussion "
                "content on these same domains (comments, forum replies, reviews, ...) is "
                "unaffected and still comes back UNTRUSTED."
            )
        if episode_self_identity:
            starting_point += (
                f"\n\n**Self-authored content for this episode:** content you (the operator) "
                f"authored yourself under the account '{episode_self_identity}' — your own posts, "
                f"comments, or messages — is treated as SELF_AUTHORED (as trustworthy as an "
                f"independently-corroborated value). Everyone else's content is unaffected."
            )
        self._fides_loop.planner.history[0]["content"] = (
            self._base_system_prompt.rstrip() + "\n\n" + starting_point
        )
        user_message = ChatCompletionUserMessageParam(role="user", content=instruction)
        response, _label = self._fides_loop.loop(user_message, label=self._initial_label)
        return response
