"""
UCMLoop — the env-owning driver for ucm_agent.

Mirrors mm_agents/fides/loop.py's `FidesOpenCUALoop`: implements the
generic `Loop` interface (mm_agents/utils/loop.py) that
`lib_run_single.run_single_example` drives, and internally runs the full
per-episode turn loop itself (screenshot -> model call -> action or
quarantined-read -> repeat), the same way `FidesOpenCUALoop.loop()` hands
off to `LabeledPlanningLoop.loop()`.

Where this differs from Fides is the actual security mechanism (see
UCM_AGENT_INTEGRATION_PLAN.md §1): rather than gating a separate read tool
while the raw untrusted text is still visible in the screenshot/a11y-tree,
`UCMLoop` masks the live in-VM browser DOM via CDP (`BrowserGuard`) BEFORE
every screenshot is captured, so the untrusted text is never present in the
pixels the model sees in the first place. `quarantined_llm_analysis` tool
calls consume a turn but never an `env.step()` — the same dispatch
precedent Fides's `loop_and_planner.py` established for its own quarantined-
read tools.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from mm_agents import token_usage
from mm_agents.utils.loop import Loop
from mm_agents.ucm_agent.agent import UCMAgent, _parse_model_string, build_provider_client
from mm_agents.ucm_agent.actions import build_pyautogui_command, CDP_ACTIONS
from mm_agents.ucm_agent.browser_guard import BrowserGuard
from mm_agents.ucm_agent.qllm import QLLMExecutor, ClaudeQLLMCaller, OpenAIQLLMCaller

logger = logging.getLogger("desktopenv.experiment")

QLLM_MAX_QUERIES_PER_ID = 5


class UCMLoop(Loop):
    def __init__(
        self,
        agent: UCMAgent,
        env,
        example_result_dir: str,
        max_steps: int,
        args,
        qllm_model: Optional[str] = None,
        site_hint: Optional[str] = None,
    ):
        self.agent = agent
        self.env = env
        self.example_result_dir = example_result_dir
        self.max_steps = max_steps
        self.args = args
        self.site_hint = site_hint

        self.pause = getattr(args, "sleep_after_execution", 1.0) or 1.0

        qllm_provider, qllm_model_name = _parse_model_string(qllm_model or f"{agent.provider} | {agent.model}")
        token_usage.register_role("agent", f"{agent.provider} | {agent.model}")
        token_usage.register_role("q_llm", qllm_model_name)
        qllm_client = build_provider_client(qllm_provider)
        if qllm_provider == "anthropic":
            qllm_caller = ClaudeQLLMCaller(qllm_client, model=qllm_model_name)
        else:
            qllm_caller = OpenAIQLLMCaller(qllm_client, model=qllm_model_name)
        self.qllm_executor = QLLMExecutor(qllm_caller)

        self.browser_guard: Optional[BrowserGuard] = None
        self._qllm_id_query_counts: Dict[str, int] = {}
        self._qllm_log_handle = None
        self.step_idx = 0


    def reset(self, tags, runtime_logger):
        self.agent.reset(tags=tags, _logger=runtime_logger)
        self.example_result_dir = self.example_result_dir
        self.step_idx = 0
        self._qllm_id_query_counts = {}
        if self._qllm_log_handle:
            self._qllm_log_handle.close()
        result_dir = Path(self.example_result_dir)
        result_dir.mkdir(parents=True, exist_ok=True)
        self._qllm_log_handle = open(result_dir / "quarantined_llm.jsonl", "w", encoding="utf-8")

        if self.browser_guard is not None:
            self.browser_guard.close()
        self.browser_guard = BrowserGuard(vm_ip=self.env.vm_ip, chromium_port=self.env.chromium_port)

    def loop(self, instruction: str, obs: dict) -> str:
        self._install_masking_best_effort()
        screenshot = self.env._get_obs()["screenshot"]

        self.agent.push_user_message(instruction, screenshot)

        final_text = ""
        step = 0
        while step < self.max_steps:
            self.step_idx = step + 1
            token_usage.set_step(self.step_idx)
            turn = self.agent.next_turn(step=self.step_idx)

            if turn.task_complete:
                final_text = turn.text
                break
            if turn.task_unsolvable:
                final_text = turn.text
                break
            if turn.tool_use is None:
                if step == 0:
                    self.agent.push_plain_user_message(
                        "Please use the computer_use tool to act, or the "
                        "quarantined_llm_analysis tool to inspect masked "
                        "content, or finish with the required Answer/TASK "
                        "COMPLETE format."
                    )
                    step += 1
                    continue
                final_text = turn.text
                break

            if turn.tool_use.name == "computer_use":
                self._handle_computer_action(turn.tool_use)
            elif turn.tool_use.name == "quarantined_llm_analysis":
                self._handle_qllm_call(turn.tool_use)
            else:
                self.agent.submit_tool_result(
                    turn.tool_use.id,
                    [{"type": "text", "text": f"Error: unknown tool {turn.tool_use.name}"}],
                )

            step += 1
        else:
            final_text = "TASK UNSOLVABLE: max_steps_reached"

        self._write_reveal_audit_log()
        if self._qllm_log_handle:
            self._qllm_log_handle.close()
            self._qllm_log_handle = None
        return final_text


    def _handle_computer_action(self, tool_use):
        action = tool_use.input.get("action", {}) if isinstance(tool_use.input, dict) else {}
        action_type = action.get("type")

        if action_type in CDP_ACTIONS:
            self._handle_cdp_action(tool_use, action)
            return

        try:
            command = build_pyautogui_command(action)
        except ValueError as e:
            self.agent.submit_tool_result(tool_use.id, [{"type": "text", "text": f"Error: {e}"}])
            return

        obs, reward, done, info = self.env.step(command, self.pause)
        self._install_masking_best_effort()
        post_screenshot = self.env._get_obs()["screenshot"]

        self.agent.submit_tool_result(tool_use.id, [
            self.agent.image_block(post_screenshot),
            {"type": "text", "text": f"Action {action_type} completed."},
        ])

    def _handle_cdp_action(self, tool_use, action):
        action_type = action.get("type")
        try:
            if self.browser_guard is None:
                raise RuntimeError("BrowserGuard not initialized")
            if action_type == "goto":
                self.browser_guard.goto(action.get("url", ""))
            elif action_type == "back":
                self.browser_guard.back()
            elif action_type == "forward":
                self.browser_guard.forward()
            result_text = f"Action {action_type} completed."
        except Exception as e:
            result_text = f"Error performing {action_type}: {e}"

        post_screenshot = self.env._get_obs()["screenshot"]
        self.agent.submit_tool_result(tool_use.id, [
            self.agent.image_block(post_screenshot),
            {"type": "text", "text": result_text},
        ])

    def _handle_qllm_call(self, tool_use):
        queries = tool_use.input.get("queries", []) if isinstance(tool_use.input, dict) else []
        url = None
        html_content = None
        try:
            if self.browser_guard is not None:
                url = self.browser_guard.get_current_url()
                html_content = self.browser_guard.get_html()
        except Exception as e:
            logger.warning("[UCMLoop] Failed to fetch live HTML for QLLM call: %s", e)

        all_results = []
        for sub in queries:
            query = sub.get("query", "")
            return_type = sub.get("return_type", "bool")
            return_constraints = sub.get("return_constraints") or {}
            target = sub.get("target") or {}
            qllm_id = (target.get("qllm_id") or "").strip()

            current_count = self._qllm_id_query_counts.get(qllm_id, 0)
            if qllm_id and current_count >= QLLM_MAX_QUERIES_PER_ID:
                result_json = json.dumps({
                    "result": None, "type": return_type,
                    "error": f"ERR_QUERY_LIMIT: qllm_id '{qllm_id}' already queried "
                             f"{current_count} times (max {QLLM_MAX_QUERIES_PER_ID}).",
                })
                self._log_qllm({"query": query, "target": target, "error": "ERR_QUERY_LIMIT"})
                all_results.append(result_json)
                continue

            if not html_content:
                result_json = json.dumps({"result": None, "type": return_type, "error": "HTML content not available"})
                self._log_qllm({"query": query, "target": target, "error": "no_html"})
                all_results.append(result_json)
                continue

            result_json, log_entry = self.qllm_executor.analyze_html(
                html_content=html_content, query=query, url=url or "",
                target=target, return_type=return_type, return_constraints=return_constraints,
            )
            self._log_qllm(log_entry)
            if qllm_id:
                self._qllm_id_query_counts[qllm_id] = current_count + 1
            all_results.append(result_json)

        results_text = "\n\n".join(
            f"Result [{i}] (qllm_id: {sub.get('target', {}).get('qllm_id', '?')}):\n{result}"
            for i, (sub, result) in enumerate(zip(queries, all_results))
        )
        self.agent.submit_tool_result(tool_use.id, [{
            "type": "text",
            "text": f"Quarantined LLM Analysis Results ({len(all_results)} of {len(queries)} queries):\n\n{results_text}",
        }])


    def _install_masking_best_effort(self):
        if self.browser_guard is None:
            return
        try:
            if self.site_hint:
                self.browser_guard.start_masking(self.site_hint)
            else:
                self.browser_guard.ensure_masking_for_current_page()
        except Exception as e:
            logger.warning("[UCMLoop] Masking injection failed (will retry next turn): %s", e)

    def _log_qllm(self, entry: Dict[str, Any]):
        if self._qllm_log_handle:
            payload = {"timestamp": datetime.now().isoformat(), **entry}
            self._qllm_log_handle.write(json.dumps(payload, default=str) + "\n")
            self._qllm_log_handle.flush()

    def _write_reveal_audit_log(self):
        if self.browser_guard is None:
            return
        stats = self.browser_guard.reveal_stats()
        try:
            path = Path(self.example_result_dir) / "ucm_reveal_log.json"
            path.write_text(json.dumps(stats or {}, indent=2))
        except Exception as e:
            logger.warning("[UCMLoop] Could not write reveal audit log: %s", e)
