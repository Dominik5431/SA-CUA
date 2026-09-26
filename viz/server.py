#!/usr/bin/env python3
"""Static web viewer for RedTeamCUA run results.

Serves a single-page app that lets you drill down
action_space -> observation_type -> model -> domain -> task,
then step through the trajectory of the newest run of that task.

No third-party dependencies; stdlib only.
"""
import argparse
import json
import mimetypes
import os
import re
import shutil
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

if HERE not in sys.path:
    sys.path.insert(0, HERE)
import latex_creator_utils

STEP_PNG_RE = re.compile(r"^step_(\d+)_(\d{8}@\d{6})\.png$")
RUNTIME_STEP_RE = re.compile(r"^Step (\d+)\s*$", re.M)
ECHO_RE = re.compile(r"^Step \d+: ", re.M)
SA_REASON_RE = re.compile(r"^Model Reasoning \([^)]*\):[ \t]*\n", re.M)



def scan_results(root):
    """Walk the results tree and return one record per task directory.

    Layout: {action_space}/{observation_type}/{model...}/{domain}/{task_id}
    The model segment may itself contain '/' (e.g. "vllm | Qwen/Qwen3.5-9B"),
    so it is whatever sits between the observation type and the last two levels.
    """
    tasks = []
    for dirpath, dirnames, filenames in os.walk(root):
        if not ({"traj.jsonl", "config.json", "result.txt"} & set(filenames)):
            continue
        dirnames[:] = []
        rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
        parts = rel.split("/")
        if len(parts) < 5:
            continue
        tasks.append({
            "path": rel,
            "action_space": parts[0],
            "observation_type": parts[1],
            "model": "/".join(parts[2:-2]),
            "domain": parts[-2],
            "task_id": parts[-1],
        })
    tasks.sort(key=lambda t: (t["action_space"], t["observation_type"],
                              t["model"], t["domain"], t["task_id"]))
    return tasks


def index_examples(examples_root):
    """task_id -> [(domain, path)] over evaluation_examples/examples/*/*.json."""
    idx = {}
    if not examples_root or not os.path.isdir(examples_root):
        return idx
    for domain in sorted(os.listdir(examples_root)):
        ddir = os.path.join(examples_root, domain)
        if not os.path.isdir(ddir):
            continue
        for name in os.listdir(ddir):
            if name.endswith(".json"):
                idx.setdefault(name[:-5], []).append((domain, os.path.join(ddir, name)))
    return idx


def find_example_config(idx, domain, task_id):
    """Locate the canonical task definition for a run that never saved its own.

    Prefers the results directory's own domain, then a related suite folder
    (results say "reddit" while the definition lives in "reddit_extended").
    """
    cands = idx.get(task_id) or []
    if not cands:
        return None
    for d, path in cands:
        if d == domain:
            return path
    for d, path in cands:
        if d.startswith(domain) or domain.startswith(d):
            return path
    return cands[0][1]



def _read(path, default=""):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def _read_json(path, default=None):
    txt = _read(path, "")
    if not txt.strip():
        return default
    try:
        return json.loads(txt)
    except ValueError:
        return default


def _split_runs(items, key):
    """Split a flat sequence into runs wherever the step counter restarts."""
    runs, cur, prev = [], [], None
    for it in items:
        n = key(it)
        if prev is not None and n <= prev:
            runs.append(cur)
            cur = []
        cur.append(it)
        prev = n
    if cur:
        runs.append(cur)
    return runs


def parse_traj(task_dir):
    """Return (runs of step records, run-level error records)."""
    entries, errors = [], []
    for line in _read(os.path.join(task_dir, "traj.jsonl")).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if "step_num" in d:
            entries.append(d)
        elif d.get("Error"):
            errors.append({"error": str(d["Error"]),
                           "traceback": str(d.get("traceback", ""))})
    return _split_runs(entries, lambda d: d["step_num"]), errors


def parse_runtime_log(task_dir):
    """Return runs of {step_num: raw block text} parsed from runtime.log."""
    txt = _read(os.path.join(task_dir, "runtime.log"))
    if not txt:
        return []
    marks = list(RUNTIME_STEP_RE.finditer(txt))
    blocks = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(txt)
        blocks.append({"step_num": int(m.group(1)),
                       "text": txt[m.end():end].strip("\n")})
    return [{b["step_num"]: b["text"] for b in run}
            for run in _split_runs(blocks, lambda b: b["step_num"])]


def split_log_block(text):
    """Pull [THINKING] / [RESPONSE_FIRST] sections out of a runtime.log block."""
    if not text:
        return "", "", ""
    think = resp = ""
    mt = re.search(r"^\[THINKING\]:", text, re.M)
    mr = re.search(r"^\[RESPONSE_FIRST\]:", text, re.M)
    if mt:
        think = text[mt.end():(mr.start() if mr and mr.start() > mt.end() else len(text))].strip()
    if mr:
        resp = trim_echo(text[mr.end():])
    if mt or mr:
        return think, resp, ""
    return "\n\n".join(sa_reasoning(text)), "", text


def trim_echo(text):
    """Drop the runner's own "Step N: <action>" / Reward / Done epilogue."""
    m = ECHO_RE.search(text)
    if m:
        text = text[:m.start()]
    return re.sub(r"\n+(Reward|Done):[^\n]*$", "", text.strip()).strip()


def sa_reasoning(text):
    """Every 'Model Reasoning (reasoning_content):' section inside a block."""
    out, marks = [], list(SA_REASON_RE.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        chunk = text[m.end():end]
        stop = re.search(r"^(Tool call |Reward:|Done:)", chunk, re.M)
        if stop:
            chunk = chunk[:stop.start()]
        chunk = chunk.strip()
        if chunk:
            out.append(chunk)
    return out


def strip_code(text):
    """Reasoning that a model emitted inline, i.e. everything before the code fence."""
    if not text:
        return ""
    idx = text.find("```")
    return text[:idx].strip() if idx > 0 else ""


def group_screenshots(task_dir):
    """step number -> {timestamp: filename} for every screenshot on disk."""
    shots = {}
    try:
        names = os.listdir(task_dir)
    except OSError:
        return shots
    for name in names:
        m = STEP_PNG_RE.match(name)
        if m:
            shots.setdefault(int(m.group(1)), {})[m.group(2)] = name
    return shots



SA_FILES = ("action_list_trace.jsonl", "planner_thinking.jsonl", "mask_stats.jsonl")
TURN_PNG_RE = re.compile(r"^turn_(\d+)\.png$")


def _read_jsonl(path):
    out = []
    for line in _read(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def is_sa_dir(task_dir):
    if any(os.path.exists(os.path.join(task_dir, f)) for f in SA_FILES):
        return True
    return os.path.isdir(os.path.join(task_dir, "masked"))


def _short(v, n=600):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = s.strip()
    return s if len(s) <= n else s[:n] + " …[+%d chars]" % (len(s) - n)


def _fmt_mask(m):
    pct = m.get("masked_pct")
    tcp = m.get("text_covered_pct")
    return (
        "masked %s%%   (%s regions masked of %s untrusted / %s rows total)\n"
        "text covered: %s   rules: %s (%s)   domain: %s   scope: %s\n"
        "url: %s"
    ) % (
        "%.1f" % pct if isinstance(pct, (int, float)) else "?",
        m.get("n_masked"), m.get("n_untrusted"), m.get("n_rows"),
        "%.1f%%" % tcp if isinstance(tcp, (int, float)) else "n/a",
        m.get("n_rules"), m.get("rules_from") or m.get("rules_capture") or "?",
        m.get("domain"), m.get("scope"), m.get("url"),
    )


def _fmt_action_list(al_obj):
    from collections import Counter
    inter = al_obj.get("interactable") or []
    trust = Counter(e.get("trust", "?") for e in inter)
    zone = Counter(e.get("zone", "?") for e in inter)
    cap = Counter(e.get("capability", "?") for e in inter)
    head = ["%d interactable elements" % len(inter),
            "  trust:      " + ", ".join("%s=%d" % kv for kv in trust.items()),
            "  zone:       " + ", ".join("%s=%d" % kv for kv in zone.items()),
            "  capability: " + ", ".join("%s=%d" % kv for kv in cap.items())]
    omitted = al_obj.get("omitted")
    if omitted:
        head.append("  omitted:    %s" % (len(omitted) if isinstance(omitted, list) else omitted))
    return "\n".join(head)


def _fmt_policy(pol):
    out = []
    for e in pol:
        ts = (e.get("ts") or "")[11:19]
        refs = e.get("var_refs")
        out.append("%s  %-22s %-6s  %s%s%s" % (
            ts, e.get("tool", ""), (e.get("decision") or "").upper(),
            e.get("reason", ""),
            "  refs=%s" % refs if refs else "",
            "  [args UNTRUSTED]" if e.get("args_untrusted") else "",
        ))
    return "\n".join(out)


def _fmt_endorse(dec):
    out = []
    for e in dec:
        ts = (e.get("ts") or "")[11:19]
        out.append("%s  %-18s %s: %s -> %s  [%s]%s" % (
            ts, e.get("tool", ""), e.get("variable", ""),
            e.get("old_label", "?"), e.get("new_label", "?"),
            e.get("status") or e.get("resolution") or "",
            "  %s" % e["message"] if e.get("message") else "",
        ))
    return "\n".join(out)


def _resolve_ref(content, memory):
    """A redacted tool response is {'root': {'output': '<key>'}, 'integrity': …};
    swap the key for the value memory.json recorded under it."""
    obj = content
    if isinstance(content, str):
        try:
            obj = json.loads(content)
        except ValueError:
            return content
    if isinstance(obj, dict):
        root = obj.get("root")
        ref = root.get("output") if isinstance(root, dict) else None
        if ref and ref in memory:
            tag = obj.get("integrity")
            return ("[%s] " % tag if tag else "") + str(memory[ref])
    return content if isinstance(content, str) else json.dumps(obj, ensure_ascii=False)


def _fmt_planner_history(ph, memory):
    lines = []
    for m in ph:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            lines.append("── system prompt (%d chars, hidden) ──" % len(content or ""))
        elif role == "user":
            lines.append("USER: " + _short(content, 1200))
        elif role == "assistant":
            c = (content or "").strip()
            if c:
                lines.append("ASSISTANT: " + _short(c, 1200))
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                lines.append("   → call %s(%s)" % (fn.get("name"), _short(fn.get("arguments"), 400)))
        elif role == "tool":
            lines.append("TOOL   ← " + _short(_resolve_ref(content, memory), 800))
        lines.append("")
    return "\n".join(lines).strip()


def _fmt_memory(mem):
    return "\n\n".join("%s:\n  %s" % (k, _short(v, 800)) for k, v in mem.items())


def _fmt_env_traj(entries):
    out = []
    for e in entries:
        info = e.get("info") or {}
        err = info.get("error") if isinstance(info, dict) else ""
        act = (e.get("action") or "").replace("\n", " ⏎ ")
        out.append("step %s: %s%s%s%s" % (
            e.get("step_num"), act[:200] or "(no action)",
            "   reward=%s" % e["reward"] if e.get("reward") is not None else "",
            "   done" if e.get("done") else "",
            "   ERROR: %s" % err if err else "",
        ))
    return "\n".join(out)


def build_sa_runs(task_dir, traj_runs):
    """Reconstruct a SA-Agent run at planner-turn granularity, or [] if the
    turn-keyed artifacts are absent (then the caller keeps the env-step view)."""
    join = lambda *a: os.path.join(task_dir, *a)
    al = _read_jsonl(join("action_list_trace.jsonl"))
    pt = _read_jsonl(join("planner_thinking.jsonl"))
    ms = _read_jsonl(join("mask_stats.jsonl"))
    pol = _read_jsonl(join("policy_trace.jsonl"))
    dec = _read_jsonl(join("endorse_log.jsonl"))
    ph = _read_json(join("planner_history.json"), []) or []
    mem = _read_json(join("memory.json"), {}) or {}

    spine = al or ms or [{"turn": i} for i in range(len(pt))]
    if not spine:
        return []
    spine_runs = _split_runs(spine, lambda d: int(d.get("turn", 0)))
    ms_runs = _split_runs(ms, lambda d: int(d.get("turn", 0))) if ms else []

    def count_imgs(sub):
        p = join(sub)
        if not os.path.isdir(p):
            return 0
        return sum(1 for n in os.listdir(p) if TURN_PNG_RE.match(n))
    have_imgs = count_imgs("masked") or count_imgs("unmasked")

    runs = []
    pt_cursor = 0
    for i, entries in enumerate(spine_runs):
        is_last = i == len(spine_runs) - 1
        off = i - len(spine_runs)
        ms_run = ms_runs[off] if -len(ms_runs) <= off < 0 else []
        ms_by_turn = {int(m.get("turn", -1)): m for m in ms_run}

        steps = []
        for e in entries:
            turn = int(e.get("turn", 0))
            think = pt[pt_cursor] if pt_cursor < len(pt) else {}
            pt_cursor += 1

            masked_fn = unmasked_fn = ""
            if is_last and have_imgs:
                src = int((ms_by_turn.get(turn) or {}).get("reused_from_turn", turn))
                mf, uf = "masked/turn_%03d.png" % src, "unmasked/turn_%03d.png" % src
                if os.path.exists(join(mf)):
                    masked_fn = mf
                if os.path.exists(join(uf)):
                    unmasked_fn = uf
            primary = masked_fn or unmasked_fn

            extra = []
            m = ms_by_turn.get(turn)
            if m:
                extra.append({"title": "Masking — what was hidden from the agent",
                              "body": _fmt_mask(m), "cls": "mask"})
            al_obj = e.get("action_list")
            if isinstance(al_obj, dict):
                extra.append({"title": "Action list — env state offered to the planner",
                              "body": _fmt_action_list(al_obj), "cls": ""})
                extra.append({"title": "Action list — raw",
                              "body": json.dumps(al_obj, indent=1),
                              "cls": "", "collapsed": True})

            steps.append({
                "step_num": turn, "timestamp": "",
                "screenshot": primary,
                "screenshot_unmasked": unmasked_fn if (masked_fn and unmasked_fn) else "",
                "action": "", "error": "", "reward": None, "done": None,
                "reasoning": (think.get("reasoning") or "").strip(),
                "response": (think.get("content") or "").strip(),
                "log": "", "label": "planner turn", "extra": extra,
            })

        run_panels = []
        toff = i - len(spine_runs)
        tr = traj_runs[toff] if traj_runs and -len(traj_runs) <= toff < 0 else []
        if tr:
            run_panels.append({"title": "Environment actions (traj.jsonl — executed pyautogui + reward)",
                               "body": _fmt_env_traj(tr), "cls": "action"})
        if ms_run:
            pcts = [x.get("masked_pct") or 0 for x in ms_run]
            run_panels.append({"title": "Masking summary (this run)",
                               "body": "masked screens: %d\navg %.1f%%   min %.1f%%   max %.1f%%"
                               % (len(pcts), sum(pcts) / len(pcts), min(pcts), max(pcts)),
                               "cls": "mask"})
        if is_last:
            if pol:
                run_panels.append({"title": "Trust-gate decisions (policy_trace — all reruns in this dir)",
                                   "body": _fmt_policy(pol), "cls": ""})
            if dec:
                run_panels.append({"title": "Endorsement events (endorse_log)",
                                   "body": _fmt_endorse(dec), "cls": ""})
            if ph:
                run_panels.append({"title": "Planner conversation (planner_history — newest run)",
                                   "body": _fmt_planner_history(ph, mem), "cls": "", "collapsed": True})
            if mem:
                run_panels.append({"title": "Resolved tool outputs (memory.json)",
                                   "body": _fmt_memory(mem), "cls": "", "collapsed": True})

        runs.append({"index": i, "started": "", "num_steps": len(entries),
                     "kind": "sa", "step_word": "Turn",
                     "steps": steps, "run_panels": run_panels})
    return runs


def build_task(root, rel, examples_idx=None):
    task_dir = os.path.join(root, rel)
    if not os.path.isdir(task_dir):
        return None

    parts = rel.replace(os.sep, "/").split("/")
    domain, task_id = parts[-2], parts[-1]

    config = _read_json(os.path.join(task_dir, "config.json"), {}) or {}
    config_source = ""
    if not config and examples_idx:
        alt = find_example_config(examples_idx, domain, task_id)
        if alt:
            config = _read_json(alt, {}) or {}
            if config:
                config_source = os.path.relpath(alt, os.path.dirname(root))
    check = _read_json(os.path.join(task_dir, "check.json"), {}) or {}
    responses = _read_json(os.path.join(task_dir, "response.json"), []) or []
    resp_by_step = {}
    for e in responses:
        if isinstance(e, dict) and "step_idx" in e:
            rounds = e.get("rounds_to_call") or {}
            if isinstance(rounds, dict):
                resp_by_step[int(e["step_idx"])] = "\n\n".join(
                    str(v) for v in rounds.values() if v)

    traj_runs, traj_errors = parse_traj(task_dir)
    log_runs = parse_runtime_log(task_dir)
    shots = group_screenshots(task_dir)

    if not traj_runs and shots:
        traj_runs = [[{"step_num": n,
                       "action_timestamp": max(shots[n]),
                       "screenshot_file": shots[n][max(shots[n])],
                       "action": "", "info": {}}
                      for n in sorted(shots) if n > 0]]

    runs = []
    for i, entries in enumerate(traj_runs):
        off = i - len(traj_runs)
        log = log_runs[off] if -len(log_runs) <= off < 0 else {}
        is_last = i == len(traj_runs) - 1

        steps = []
        first_ts = entries[0].get("action_timestamp", "") if entries else ""

        zero = shots.get(0) or {}
        cands = [t for t in zero if not first_ts or t <= first_ts]
        ts0 = max(cands) if cands else (max(zero) if zero else None)
        if ts0:
            steps.append({
                "step_num": 0, "timestamp": ts0,
                "screenshot": zero[ts0], "action": "", "error": "",
                "reasoning": "", "response": "", "log": "",
                "label": "initial state",
            })

        for e in entries:
            n = int(e["step_num"])
            block = log.get(n, "")
            think, resp, rest = split_log_block(block)
            model_resp = resp_by_step.get(n, "") if is_last else ""
            if not model_resp:
                model_resp = resp
            if not think:
                think = strip_code(model_resp)
            info = e.get("info") or {}
            steps.append({
                "step_num": n,
                "timestamp": e.get("action_timestamp", ""),
                "screenshot": e.get("screenshot_file") or "",
                "action": e.get("action", ""),
                "error": (info.get("error") or "") if isinstance(info, dict) else "",
                "reward": e.get("reward"),
                "done": e.get("done"),
                "reasoning": think,
                "response": model_resp,
                "log": rest,
                "label": "",
            })

        runs.append({
            "index": i,
            "started": first_ts,
            "num_steps": len(entries),
            "steps": steps,
        })

    if is_sa_dir(task_dir):
        sa_runs = build_sa_runs(task_dir, traj_runs)
        if sa_runs:
            runs = sa_runs

    adversary = extract_adversary(config)

    return {
        "path": rel,
        "config_source": config_source,
        "complete": os.path.exists(os.path.join(task_dir, "result.txt")),
        "instruction": config.get("instruction", ""),
        "task_id": config.get("id") or task_id,
        "meta_data": config.get("meta_data") or {},
        "tags": config.get("tags") or {},
        "adversary": adversary,
        "result": _read(os.path.join(task_dir, "result.txt")).strip(),
        "adversary_result": _read(os.path.join(task_dir, "adversary_result.txt")).strip(),
        "check": check,
        "errors": traj_errors,
        "has_recording": os.path.exists(os.path.join(task_dir, "recording.mp4")),
        "runs": runs,
    }


MAX_INJECTED_CHARS = 40000
REPO_ROOT = os.path.dirname(HERE)


def read_injected_file(rel_path):
    """Text of a file that gets uploaded into the environment (ownCloud tasks)."""
    full = os.path.join(REPO_ROOT, rel_path)
    if not os.path.isfile(full):
        return "", "file not found under %s" % REPO_ROOT
    if full.lower().endswith(".pdf"):
        try:
            import fitz
        except ImportError:
            return "", "PDF — install pymupdf to read its text here"
        try:
            with fitz.open(full) as doc:
                pages = [pg.get_text() for pg in doc]
            return "\n\n".join(pages).strip()[:MAX_INJECTED_CHARS], "extracted from PDF"
        except Exception as exc:
            return "", "could not read PDF: %s" % exc
    try:
        with open(full, "r", errors="replace") as f:
            return f.read()[:MAX_INJECTED_CHARS], ""
    except OSError as exc:
        return "", str(exc)


def extract_adversary(config):
    """Whatever the injection actually puts in front of the agent.

    Each platform stores it differently: reddit/gitlab inline the text,
    rocketchat carries NPC scenario prompts, ownCloud uploads a file.
    """
    out = []
    for item in (config.get("adversary_config") or []):
        typ = (item or {}).get("type", "")
        params = (item or {}).get("parameters") or {}

        inline = next((k for k in ("new_content", "content", "message", "text")
                       if params.get(k)), None)
        if inline:
            out.append({"type": typ, "label": inline,
                        "content": str(params[inline]), "note": ""})
            continue

        scenarios = params.get("scenarios_json_data")
        if isinstance(scenarios, dict) and scenarios:
            for npc, spec in scenarios.items():
                if isinstance(spec, dict):
                    body = spec.get("strategy_hint") or json.dumps(spec, indent=2)
                    note = spec.get("extra_info", "")
                else:
                    body, note = str(spec), ""
                out.append({"type": typ, "label": "NPC: %s" % npc,
                            "content": body, "note": note})
            continue

        local = params.get("local_file_path")
        if local:
            body, note = read_injected_file(local)
            dest = params.get("docker_path") or ""
            out.append({"type": typ, "label": local, "content": body,
                        "note": " → ".join(x for x in (note, dest) if x)})
    return out



class Handler(BaseHTTPRequestHandler):
    server_version = "RTCViz/1.0"
    root = ""
    examples_root = ""
    index_cache = None
    examples_cache = None
    lock = threading.Lock()

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _safe(self, rel):
        """Resolve a client-supplied path inside the results root, or None."""
        rel = unquote(rel or "").lstrip("/")
        full = os.path.realpath(os.path.join(self.root, rel))
        if full == self.root or full.startswith(self.root + os.sep):
            return full
        return None

    def _get_index(self):
        with Handler.lock:
            if Handler.index_cache is None:
                Handler.index_cache = scan_results(self.root)
            return Handler.index_cache

    def _get_examples(self):
        with Handler.lock:
            if Handler.examples_cache is None:
                Handler.examples_cache = index_examples(self.examples_root)
            return Handler.examples_cache

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path

        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path in ("/save.html", "/save"):
            return self._serve_static("save.html")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])

        if path == "/api/index":
            return self._json({"root": self.root, "tasks": self._get_index()})

        if path == "/api/rescan":
            with Handler.lock:
                Handler.index_cache = None
                Handler.examples_cache = None
            return self._json({"tasks": len(self._get_index())})

        if path == "/api/task":
            rel = (q.get("p") or [""])[0]
            full = self._safe(rel)
            if not full or not os.path.isdir(full):
                return self._json({"error": "unknown task"}, 404)
            data = build_task(self.root, os.path.relpath(full, self.root),
                               self._get_examples())
            return self._json(data or {"error": "unreadable"}, 200 if data else 500)

        if path == "/img":
            rel = (q.get("p") or [""])[0]
            full = self._safe(rel)
            if not full or not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
            with open(full, "rb") as f:
                blob = f.read()
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            return self._send(200, blob, ctype, {"Cache-Control": "max-age=3600"})

        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/save-images":
            return self._save_images()
        return self._send(404, b"not found", "text/plain")

    def _save_images(self):
        """Copy selected screenshots into viz/<subdir>/ and write a LaTeX snippet.

        Body: {
          "dir": "name",
          "images": [
            {"path": "task/path/step_1_....png", "step_num": 1, "label": ""}
            ... or plain strings ...
          ],
          "instruction": "...",
          "width": "0.8\\textwidth"   # optional LaTeX includegraphics width
        }
        Each image path must resolve inside the results root (same rule as /img).
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._json({"error": "invalid JSON body"}, 400)

        subdir = str(req.get("dir") or "").strip()
        images = req.get("images")
        if not isinstance(images, list):
            return self._json({"error": "images must be a list"}, 400)

        if (not subdir or subdir in (".", "..", "static")
                or "/" in subdir or "\\" in subdir
                or any(ord(c) < 32 for c in subdir)):
            return self._json({"error": "invalid directory name"}, 400)
        target = os.path.realpath(os.path.join(HERE, subdir))
        if target == HERE or not target.startswith(HERE + os.sep):
            return self._json({"error": "invalid directory name"}, 400)

        sources = []
        for item in images:
            if isinstance(item, str):
                rel, step_num, label = item, None, ""
            elif isinstance(item, dict):
                rel = str(item.get("path") or "").strip()
                step_num = item.get("step_num")
                label = str(item.get("label") or "")
            else:
                return self._json({"error": "each image must be a string or an object"}, 400)
            if not rel:
                return self._json({"error": "image entry has no path"}, 400)
            full = self._safe(rel)
            if not full or not os.path.isfile(full):
                return self._json({"error": "invalid image path: %s" % rel}, 400)
            sources.append((full, rel, step_num, label))
        if not sources:
            return self._json({"error": "no images selected"}, 400)

        try:
            os.makedirs(target, exist_ok=True)
        except OSError as exc:
            return self._json({"error": "cannot create directory: %s" % exc}, 500)

        saved = []
        latex_images = []
        try:
            for full, rel, step_num, label in sources:
                name = os.path.basename(full)
                dest = os.path.join(target, name)
                base, ext = os.path.splitext(name)
                n = 1
                while os.path.exists(dest):
                    dest = os.path.join(target, "%s_%d%s" % (base, n, ext))
                    n += 1
                shutil.copy2(full, dest)
                saved_name = os.path.basename(dest)
                saved.append(os.path.relpath(dest, HERE).replace(os.sep, "/"))
                latex_images.append({"filename": saved_name,
                                     "step_num": step_num, "label": label})
        except OSError as exc:
            return self._json({"error": "copy failed: %s" % exc, "saved": saved}, 500)

        try:
            model = str(req.get("model") or "")
            if not model:
                parts = sources[0][1].replace(os.sep, "/").split("/")
                model = "/".join(parts[2:-3]) if len(parts) >= 6 else "unknown"
            instruction = str(req.get("instruction") or "")
            width = str(req.get("width") or r"0.8\textwidth")
            tex_path = latex_creator_utils.write_trajectory_snippet(
                model=model,
                instruction=instruction,
                images=latex_images,
                output_path=os.path.join(target, "trajectory.tex"),
                image_width=width,
            )
            latex_rel = os.path.relpath(tex_path, HERE).replace(os.sep, "/")
        except (OSError, ValueError) as exc:
            return self._json({"ok": True, "dir": subdir, "count": len(saved),
                               "saved": saved, "latex": "",
                               "latex_error": "LaTeX snippet failed: %s" % exc})

        return self._json({"ok": True, "dir": subdir, "count": len(saved),
                           "saved": saved, "latex": latex_rel})

    def _serve_static(self, name):
        full = os.path.realpath(os.path.join(STATIC, name))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        with open(full, "rb") as f:
            blob = f.read()
        ctype = mimetypes.guess_type(full)[0] or "text/plain"
        if ctype.startswith("text/") or "javascript" in ctype:
            ctype += "; charset=utf-8"
        return self._send(200, blob, ctype, {"Cache-Control": "no-cache"})


def port_is_free(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(host, preferred, tries=200):
    for p in range(preferred, preferred + tries):
        if port_is_free(host, p) and port_is_free("127.0.0.1", p):
            return p
    raise SystemExit("no free port found near %d" % preferred)


def main():
    ap = argparse.ArgumentParser(description="RedTeamCUA results viewer")
    ap.add_argument("--results", default=os.path.join(os.path.dirname(HERE), "results"),
                    help="path to the results/ directory")
    ap.add_argument("--examples",
                    default=os.path.join(os.path.dirname(HERE),
                                         "evaluation_examples", "examples"),
                    help="task definitions, used when a crashed run saved no config.json")
    ap.add_argument("--port", type=int, default=8500,
                    help="preferred port; the next free one is used if taken")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--strict-port", action="store_true",
                    help="fail instead of falling forward to another port")
    args = ap.parse_args()

    root = os.path.realpath(args.results)
    if not os.path.isdir(root):
        raise SystemExit("results directory not found: %s" % root)

    if args.strict_port:
        if not port_is_free(args.host, args.port):
            raise SystemExit("port %d is already in use" % args.port)
        port = args.port
    else:
        port = pick_port(args.host, args.port)
        if port != args.port:
            print("port %d busy, using %d instead" % (args.port, port))

    Handler.root = root
    Handler.examples_root = os.path.realpath(args.examples)
    tasks = scan_results(root)
    Handler.index_cache = tasks
    Handler.examples_cache = index_examples(Handler.examples_root)
    httpd = ThreadingHTTPServer((args.host, port), Handler)
    print("results root : %s" % root)
    print("tasks indexed: %d" % len(tasks))
    print("task defs    : %d (%s)" % (len(Handler.examples_cache), Handler.examples_root))
    print("serving on   : http://localhost:%d/" % port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
