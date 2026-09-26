#!/usr/bin/env python3
"""Visualize a single agent trajectory from results/ (thinking, tool calls, screenshots).

Locates a task's result directory under --result_dir by fuzzy matching on
--model / --domain / --task_id, reconstructs the most recent run (task
directories are reused/overwritten in place across reruns, see
get_unfinished() in run.py), and renders it either to the terminal (rich) or
to a self-contained HTML report (--html).

Supports two result shapes:
  - PromptAgent: traj.jsonl + response.json (full raw thinking text per step).
  - SA-Agent / FidesOpenCUAAgent: traj.jsonl + planner_history.json (P-LLM
    tool calls, redacted refs resolved via memory.json) + optionally
    policy_trace.jsonl / runtime.log. See CLAUDE.md and the P-LLM/Q-VLM
    architecture in mm_agents/sa_agent/ for background.

Usage:
  python show_trajectory.py --list --model "Qwen3.5-9B" --domain chrome
  python show_trajectory.py --model "Qwen3.5-9B+sa" --domain gimp --task_id 7a4deb26
  python show_trajectory.py --model "Qwen3.5-9B" --task_id own_reddit_1_specific_delete_txt --html out.html
"""

import argparse
import ast
import base64
import html
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Optional



@dataclass
class Candidate:
    path: str
    action_space: str
    observation_type: str
    run_label: str
    domain: str
    task_id: str
    mtime: float


def _relparts(leaf_dir: str, result_dir: str):
    rel = os.path.relpath(leaf_dir, result_dir)
    return rel.split(os.sep)


def _dir_mtime(path: str) -> float:
    """Latest mtime of any file directly inside path (used to rank/pick 'most recent')."""
    best = 0.0
    try:
        for entry in os.scandir(path):
            if entry.is_file():
                best = max(best, entry.stat().st_mtime)
    except OSError:
        pass
    return best


def find_candidates(result_dir: str) -> list:
    """Walk result_dir, returning a Candidate for every leaf task directory
    (identified by containing traj.jsonl or planner_history.json)."""
    candidates = []
    for root, _dirs, files in os.walk(result_dir):
        if "traj.jsonl" not in files and "planner_history.json" not in files:
            continue
        parts = _relparts(root, result_dir)
        if len(parts) < 4:
            continue
        action_space, observation_type = parts[0], parts[1]
        run_label = "/".join(parts[2:-2])
        domain, task_id = parts[-2], parts[-1]
        candidates.append(Candidate(
            path=root,
            action_space=action_space,
            observation_type=observation_type,
            run_label=run_label,
            domain=domain,
            task_id=task_id,
            mtime=_dir_mtime(root),
        ))
    return candidates


def filter_candidates(candidates, action_space=None, observation_type=None,
                       model=None, domain=None, task_id=None, agent=None):
    out = []
    for c in candidates:
        if action_space and c.action_space != action_space:
            continue
        if observation_type and c.observation_type != observation_type:
            continue
        if model and model not in c.run_label:
            continue
        if agent and agent not in c.run_label:
            continue
        if domain and domain != c.domain:
            continue
        if task_id and task_id not in c.task_id:
            continue
        out.append(c)
    return out


def resolve_one(candidates) -> Candidate:
    """Pick the most-recently-modified candidate, warning about the rest."""
    chosen = max(candidates, key=lambda c: c.mtime)
    others = [c for c in candidates if c is not chosen]
    if others:
        sys.stderr.write(
            f"warning: {len(candidates)} runs matched; using the most recently modified one:\n"
            f"  {chosen.path}\n"
            f"Other matches (use --domain/--task_id/--agent to disambiguate):\n"
        )
        for c in sorted(others, key=lambda c: -c.mtime):
            sys.stderr.write(f"  {c.path}\n")
    return chosen



def _read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_jsonl(path):
    entries = []
    if not os.path.exists(path):
        return entries
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _read_text_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return [l.rstrip("\n") for l in f if l.strip()]


def latest_run_suffix(traj_entries):
    """traj.jsonl is append-only across in-place reruns (task dirs are reused,
    see get_unfinished() in run.py) so step_num can restart partway through the
    file. Return (entries_from_the_last_run, num_discarded_earlier_lines)."""
    step_indices = [i for i, e in enumerate(traj_entries) if "step_num" in e]
    cutoff = 0
    prev = None
    for idx in step_indices:
        sn = traj_entries[idx]["step_num"]
        if prev is not None and sn < prev:
            cutoff = idx
        prev = sn
    return traj_entries[cutoff:], cutoff


def group_by_step_num(entries):
    groups: dict = {}
    order = []
    for e in entries:
        sn = e.get("step_num")
        if sn is None:
            continue
        if sn not in groups:
            groups[sn] = []
            order.append(sn)
        groups[sn].append(e)
    return [(sn, groups[sn]) for sn in order]



CONSEQUENTIAL_TOOLS = {
    "click", "right_click", "mouse_down", "mouse_up", "drag_to", "move_to",
    "hscroll", "vscroll", "type_text", "key_down", "key_up", "hotkey", "press",
}
RAW_RESPONSE_PREFIXES = {
    "find": "FIND RAW RESPONSE: ",
    "checkdone": "RESPONSE: ",
}


def _parse_tool_content(content):
    """tool-role message content is a Python dict repr (single-quoted), not JSON."""
    if content is None:
        return None
    if not isinstance(content, str):
        return content
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        return ast.literal_eval(content)
    except (ValueError, SyntaxError):
        return content


def _resolve_refs(obj, memory: dict):
    """Replace 'tool-result-N.field' reference strings with their real value
    from memory.json (the no_exposure IFC loop never shows raw tool output to
    the planner directly -- see mm_agents/sa_agent/loop_and_planner_no_exposure.py)."""
    if isinstance(obj, str):
        return memory.get(obj, obj)
    if isinstance(obj, dict):
        return {k: _resolve_refs(v, memory) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_refs(v, memory) for v in obj]
    return obj


def build_planner_calls(planner_history: list, memory: dict, runtime_log_lines: list, planner_thinking: list):
    """Flatten planner_history.json into one entry per tool call, resolved
    against memory.json, with find()/checkdone() raw responses attached.

    planner_thinking is planner_thinking.jsonl (see
    LabeledPlanningLoop._log_planner_thinking in loop_and_planner_no_exposure.py):
    one entry is written per get_tool_response() call, which -- traced through
    LabeledBasicPlanner.next_action() -- is exactly one assistant turn appended to
    planner_history.json's history, in the same order. So zipping planner_history's
    assistant turns against planner_thinking by position (not tool name) is an exact
    pairing, unlike the find()/checkdone() runtime.log pairing below."""
    tool_responses = {}
    for turn in planner_history:
        if turn.get("role") == "tool":
            tool_responses[turn.get("tool_call_id")] = _parse_tool_content(turn.get("content"))

    raw_response_cursors = {name: iter(
        [l[len(prefix):] for l in runtime_log_lines if l.startswith(prefix)]
    ) for name, prefix in RAW_RESPONSE_PREFIXES.items()}
    thinking_cursor = iter(planner_thinking)

    calls = []
    call_index = 0
    for turn in planner_history:
        if turn.get("role") != "assistant":
            continue
        thinking = next(thinking_cursor, None)
        tool_calls = (turn.get("tool_calls") or None) or []
        if not tool_calls:
            call_index += 1
            calls.append({
                "index": call_index,
                "tool_name": "(no tool call)",
                "args": None,
                "result": None,
                "raw_response": None,
                "assistant_content": turn.get("content"),
                "planner_reasoning": (thinking or {}).get("reasoning"),
                "planner_content": (thinking or {}).get("content"),
            })
            continue
        for tc in tool_calls:
            call_index += 1
            name = tc.get("function", {}).get("name", "?")
            raw_args = tc.get("function", {}).get("arguments", "")
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                args = raw_args
            args = _resolve_refs(args, memory)
            result = _resolve_refs(tool_responses.get(tc.get("id")), memory)
            raw_response = None
            cursor = raw_response_cursors.get(name)
            if cursor is not None:
                raw_response = next(cursor, None)
            calls.append({
                "index": call_index,
                "tool_name": name,
                "args": args,
                "result": result,
                "raw_response": raw_response,
                "assistant_content": turn.get("content"),
                "planner_reasoning": (thinking or {}).get("reasoning"),
                "planner_content": (thinking or {}).get("content"),
            })
    return calls



def build_promptagent_steps(traj_latest, response_json):
    by_step_idx = {e["step_idx"]: e for e in (response_json or [])}
    steps = []
    for step_num, rows in group_by_step_num(traj_latest):
        resp = by_step_idx.get(step_num, {})
        rounds = resp.get("rounds_to_call", {})
        steps.append({
            "step_num": step_num,
            "thinking_first": rounds.get("first"),
            "thinking_second": rounds.get("second"),
            "actions": rows,
        })
    return steps



@dataclass
class Trajectory:
    task_dir: str
    candidate: Candidate
    instruction: Optional[str]
    result: Optional[str]
    adversary_result: Optional[str]
    check: Optional[dict]
    agent_kind: str
    discarded_traj_lines: int
    promptagent_steps: list = field(default_factory=list)
    planner_calls: list = field(default_factory=list)
    env_steps: list = field(default_factory=list)
    policy_trace: list = field(default_factory=list)
    action_list_turns: int = 0
    warnings: list = field(default_factory=list)


def load_trajectory(candidate: Candidate) -> Trajectory:
    d = candidate.path

    def p(name):
        return os.path.join(d, name)

    config = _read_json(p("config.json"))
    instruction = config.get("instruction") if config else None
    result = None
    if os.path.exists(p("result.txt")):
        result = open(p("result.txt"), encoding="utf-8").read().strip()
    adversary_result = None
    if os.path.exists(p("adversary_result.txt")):
        adversary_result = open(p("adversary_result.txt"), encoding="utf-8").read().strip()
    check = _read_json(p("check.json"))

    traj_all = _read_jsonl(p("traj.jsonl"))
    traj_latest, discarded = latest_run_suffix(traj_all)

    warnings = []
    if discarded:
        warnings.append(
            f"traj.jsonl contains data from an earlier, superseded run "
            f"({discarded} earlier line(s) discarded) -- this task directory was rerun in place."
        )
    if result is None and adversary_result is None:
        warnings.append("No result.txt / adversary_result.txt found -- this run did not complete evaluation.")

    response_json = _read_json(p("response.json"))
    planner_history = _read_json(p("planner_history.json"))

    traj = Trajectory(
        task_dir=d,
        candidate=candidate,
        instruction=instruction,
        result=result,
        adversary_result=adversary_result,
        check=check,
        agent_kind="promptagent" if response_json is not None else "planner",
        discarded_traj_lines=discarded,
        warnings=warnings,
    )

    if traj.agent_kind == "promptagent":
        traj.promptagent_steps = build_promptagent_steps(traj_latest, response_json)
        return traj

    memory = _read_json(p("memory.json"), default={}) or {}
    runtime_log_lines = _read_text_lines(p("runtime.log"))
    if not runtime_log_lines:
        traj.warnings.append(
            "runtime.log is empty -- no raw executor (find/checkdone) commentary was persisted "
            "for this run (either it predates the logging fix, or the run crashed before any find() call)."
        )
    planner_thinking = _read_jsonl(p("planner_thinking.jsonl"))
    if not planner_thinking:
        traj.warnings.append(
            "planner_thinking.jsonl is missing or empty -- this run predates the P-LLM "
            "reasoning-capture fix (it was previously only print()'d to stdout, never persisted)."
        )
    traj.planner_calls = build_planner_calls(planner_history or [], memory, runtime_log_lines, planner_thinking)
    traj.env_steps = [(sn, rows) for sn, rows in group_by_step_num(traj_latest)]
    traj.policy_trace = _read_jsonl(p("policy_trace.jsonl"))
    traj.action_list_turns = sum(1 for _ in _read_jsonl(p("action_list_trace.jsonl")))
    return traj



def render_terminal(traj: Trajectory):
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.text import Text

    console = Console()
    c = traj.candidate

    header = Text()
    header.append(f"{c.task_id}\n", style="bold")
    header.append(f"model/run_label: {c.run_label}\n")
    header.append(f"domain: {c.domain}    action_space: {c.action_space}    observation_type: {c.observation_type}\n")
    if traj.instruction:
        header.append(f"instruction: {traj.instruction}\n", style="italic")
    header.append(f"result: {traj.result}    adversary_result: {traj.adversary_result}\n")
    if traj.check:
        header.append(f"check: {traj.check}\n")
    console.print(Panel(header, title="Run", border_style="cyan"))

    for w in traj.warnings:
        console.print(f"[yellow]warning:[/yellow] {w}")

    if traj.agent_kind == "promptagent":
        for step in traj.promptagent_steps:
            body = Text()
            if step["thinking_first"]:
                body.append(step["thinking_first"] + "\n", style="dim")
            if step["thinking_second"]:
                body.append("--- second call ---\n", style="dim italic")
                body.append(step["thinking_second"] + "\n", style="dim")
            console.print(Panel(body, title=f"Step {step['step_num']}", border_style="blue"))
            for row in step["actions"]:
                console.print(Syntax(str(row.get("action", "")), "python", theme="ansi_dark", word_wrap=True))
                meta = f"screenshot: {row.get('screenshot_file')}    reward: {row.get('reward')}    done: {row.get('done')}"
                console.print(Text(meta, style="dim"))
            console.print()
        return

    console.print(Panel(
        f"planner tool calls: {len(traj.planner_calls)}    "
        f"env actions: {sum(len(rows) for _, rows in traj.env_steps)}    "
        f"policy decisions: {len(traj.policy_trace)}    "
        f"action_list_trace turns: {traj.action_list_turns} (element lists in file, not rendered inline)",
        title="Planner run summary", border_style="magenta",
    ))

    console.rule("Planner timeline")
    for call in traj.planner_calls:
        title = f"#{call['index']} {call['tool_name']}"
        body = Text()
        if call.get("planner_reasoning"):
            body.append("P-LLM thinking: ", style="bold yellow")
            body.append(f"{call['planner_reasoning']}\n", style="dim italic")
        if call.get("planner_content"):
            body.append("P-LLM content: ", style="bold yellow")
            body.append(f"{call['planner_content']}\n", style="dim italic")
        body.append("args: ", style="bold")
        body.append(f"{call['args']}\n")
        body.append("result: ", style="bold")
        body.append(f"{call['result']}\n")
        if call["raw_response"] is not None:
            body.append("raw executor response: ", style="bold green")
            body.append(f"{call['raw_response']}\n", style="dim")
        console.print(Panel(body, title=title, border_style="blue"))

    if traj.env_steps:
        console.rule("Environment actions (traj.jsonl)")
        for step_num, rows in traj.env_steps:
            for row in rows:
                console.print(Syntax(str(row.get("action", "")), "python", theme="ansi_dark", word_wrap=True))
                meta = f"step {step_num}    screenshot: {row.get('screenshot_file')}    reward: {row.get('reward')}    done: {row.get('done')}"
                console.print(Text(meta, style="dim"))

    if traj.policy_trace:
        console.rule("Policy decisions (policy_trace.jsonl)")
        for entry in traj.policy_trace:
            style = "green" if entry.get("decision") == "allow" else "red"
            console.print(Text(
                f"[{entry.get('decision')}] {entry.get('tool')} -> {entry.get('target')}: {entry.get('reason')}",
                style=style,
            ))



def _img_data_uri(task_dir, filename):
    if not filename:
        return None
    path = os.path.join(task_dir, filename)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _esc(x) -> str:
    return html.escape(str(x))


def render_html(traj: Trajectory) -> str:
    c = traj.candidate
    parts = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Trajectory: {_esc(c.task_id)}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 1000px; margin: 2rem auto; padding: 0 1rem; background:#fff; color:#111; }}
  @media (prefers-color-scheme: dark) {{ body {{ background:#1a1a1a; color:#eee; }} }}
  h1, h2 {{ border-bottom: 1px solid #8888; padding-bottom: .3rem; }}
  .meta {{ font-size: .9rem; opacity: .85; white-space: pre-wrap; }}
  .warning {{ background:#fff3cd; color:#664d03; padding:.5rem 1rem; border-radius:6px; margin:.5rem 0; }}
  @media (prefers-color-scheme: dark) {{ .warning {{ background:#4d3b00; color:#ffdf7e; }} }}
  .step {{ border: 1px solid #8884; border-radius: 8px; padding: 1rem; margin: 1rem 0; }}
  .thinking {{ white-space: pre-wrap; font-size: .9rem; opacity: .9; background:#8881; padding:.5rem; border-radius:6px; }}
  .action {{ white-space: pre-wrap; font-family: ui-monospace, monospace; background:#8882; padding:.5rem; border-radius:6px; }}
  img {{ max-width: 100%; border-radius: 6px; margin-top: .5rem; }}
  .row-meta {{ font-size: .8rem; opacity: .7; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .85rem; }}
  td, th {{ border: 1px solid #8884; padding: .3rem .5rem; text-align: left; }}
  .allow {{ color: #1a7f37; }}
  .block {{ color: #cf222e; }}
</style></head><body>
<h1>{_esc(c.task_id)}</h1>
<div class="meta">model/run_label: {_esc(c.run_label)}
domain: {_esc(c.domain)}    action_space: {_esc(c.action_space)}    observation_type: {_esc(c.observation_type)}
instruction: {_esc(traj.instruction)}
result: {_esc(traj.result)}    adversary_result: {_esc(traj.adversary_result)}
check: {_esc(traj.check)}</div>
""")
    for w in traj.warnings:
        parts.append(f'<div class="warning">warning: {_esc(w)}</div>\n')

    if traj.agent_kind == "promptagent":
        parts.append("<h2>Steps</h2>\n")
        for step in traj.promptagent_steps:
            parts.append(f'<div class="step"><h3>Step {step["step_num"]}</h3>\n')
            if step["thinking_first"]:
                parts.append(f'<div class="thinking">{_esc(step["thinking_first"])}</div>\n')
            if step["thinking_second"]:
                parts.append('<p><em>second call</em></p>\n')
                parts.append(f'<div class="thinking">{_esc(step["thinking_second"])}</div>\n')
            for row in step["actions"]:
                parts.append(f'<div class="action">{_esc(row.get("action", ""))}</div>\n')
                parts.append(f'<div class="row-meta">reward: {_esc(row.get("reward"))}    done: {_esc(row.get("done"))}</div>\n')
                uri = _img_data_uri(traj.task_dir, row.get("screenshot_file"))
                if uri:
                    parts.append(f'<img src="{uri}" alt="{_esc(row.get("screenshot_file"))}">\n')
            parts.append("</div>\n")
    else:
        parts.append(
            f'<p class="meta">planner tool calls: {len(traj.planner_calls)} &nbsp; '
            f'env actions: {sum(len(rows) for _, rows in traj.env_steps)} &nbsp; '
            f'policy decisions: {len(traj.policy_trace)} &nbsp; '
            f'action_list_trace turns: {traj.action_list_turns} (element lists in file, not rendered inline)</p>\n'
        )
        parts.append("<h2>Planner timeline</h2>\n")
        for call in traj.planner_calls:
            parts.append(f'<div class="step"><h3>#{call["index"]} {_esc(call["tool_name"])}</h3>\n')
            if call.get("planner_reasoning"):
                parts.append(f'<div class="thinking">P-LLM thinking: {_esc(call["planner_reasoning"])}</div>\n')
            if call.get("planner_content"):
                parts.append(f'<div class="thinking">P-LLM content: {_esc(call["planner_content"])}</div>\n')
            parts.append(f'<div class="action">args: {_esc(call["args"])}</div>\n')
            parts.append(f'<div class="action">result: {_esc(call["result"])}</div>\n')
            if call["raw_response"] is not None:
                parts.append(f'<div class="thinking">raw executor response: {_esc(call["raw_response"])}</div>\n')
            parts.append("</div>\n")

        if traj.env_steps:
            parts.append("<h2>Environment actions (traj.jsonl)</h2>\n")
            for step_num, rows in traj.env_steps:
                for row in rows:
                    parts.append(f'<div class="step"><div class="row-meta">step {step_num}</div>\n')
                    parts.append(f'<div class="action">{_esc(row.get("action", ""))}</div>\n')
                    parts.append(f'<div class="row-meta">reward: {_esc(row.get("reward"))}    done: {_esc(row.get("done"))}</div>\n')
                    uri = _img_data_uri(traj.task_dir, row.get("screenshot_file"))
                    if uri:
                        parts.append(f'<img src="{uri}" alt="{_esc(row.get("screenshot_file"))}">\n')
                    parts.append("</div>\n")

        if traj.policy_trace:
            parts.append("<h2>Policy decisions (policy_trace.jsonl)</h2>\n<table>\n")
            parts.append("<tr><th>decision</th><th>tool</th><th>target</th><th>reason</th><th>ts</th></tr>\n")
            for entry in traj.policy_trace:
                cls = "allow" if entry.get("decision") == "allow" else "block"
                parts.append(
                    f'<tr class="{cls}"><td>{_esc(entry.get("decision"))}</td><td>{_esc(entry.get("tool"))}</td>'
                    f'<td>{_esc(entry.get("target"))}</td><td>{_esc(entry.get("reason"))}</td>'
                    f'<td>{_esc(entry.get("ts"))}</td></tr>\n'
                )
            parts.append("</table>\n")

    parts.append("</body></html>\n")
    return "".join(parts)



def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--result_dir", default="./results")
    ap.add_argument("--action_space", default="pyautogui")
    ap.add_argument("--observation_type", default="screenshot_a11y_tree")
    ap.add_argument("--model", default="Qwen3.5-9B+sa", help="substring match against the run_label path segment(s)")
    ap.add_argument("--domain", default="chrome")
    ap.add_argument("--task_id", default="7b6c", help="substring/prefix match against the task id")
    ap.add_argument("--agent", default="sa", help="substring filter, e.g. sa / fides / hybrid")
    ap.add_argument("--list", action="store_true", help="list matching runs and exit")
    ap.add_argument("--html", nargs="?", const="", default=None, metavar="PATH",
                     help="write a self-contained HTML report. If the filter matches exactly one task "
                          "suite, PATH is the output file (default ./trajectory_<task_id>.html). If it "
                          "matches several task suites (partial filtering), PATH is instead treated as an "
                          "output directory (default ./html/) and one <task_id>.html is written per suite "
                          "-- each suite resolved to its own most-recently-modified run, same as the "
                          "single-run case.")
    args = ap.parse_args()

    result_dir = os.path.abspath(args.result_dir)
    if not os.path.isdir(result_dir):
        sys.exit(f"error: result_dir not found: {result_dir}")

    all_candidates = find_candidates(result_dir)
    matches = filter_candidates(
        all_candidates,
        action_space=args.action_space,
        observation_type=args.observation_type,
        model=args.model,
        domain=args.domain,
        task_id=args.task_id,
        agent=args.agent,
    )

    if args.list:
        if not matches:
            sys.exit("no matching runs found")
        for c in sorted(matches, key=lambda c: -c.mtime):
            print(f"{c.path}")
        return

    if not matches:
        sys.exit("no matching runs found (try --list to see what's available)")

    groups: dict = {}
    for c in matches:
        groups.setdefault(c.task_id, []).append(c)

    if len(groups) == 1 or args.html is None:
        candidate = resolve_one(matches)
        traj = load_trajectory(candidate)
        render_terminal(traj)
        if args.html is not None:
            out_path = args.html or f"./trajectory_{candidate.task_id}.html"
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(render_html(traj))
            print(f"\nHTML report written to {out_path}")
        return

    out_dir = args.html or "./html"
    os.makedirs(out_dir, exist_ok=True)
    print(f"{len(groups)} matching task suite(s); writing HTML reports to {out_dir}/\n")
    for task_id in sorted(groups):
        group = groups[task_id]
        candidate = resolve_one(group) if len(group) > 1 else group[0]
        traj = load_trajectory(candidate)
        out_path = os.path.join(out_dir, f"{task_id}.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(render_html(traj))
        print(f"  {task_id}: result={traj.result} adversary_result={traj.adversary_result} -> {out_path}")


if __name__ == "__main__":
    main()
