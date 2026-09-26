"""terminal_taint.py — which shell commands turn a terminal's content UNTRUSTED.

One source of truth for the "certain list" of commands that flip a terminal from first-party
(the user's own shell -- trusted) to attacker-reachable (masked). Two callers share it so they
cannot drift:

* the masking pipeline (``masking._verdict_rows``): a terminal WINDOW is shown to the planner by
  default, and masked once a taint-producing command is visible in its scrollback.
* the ``run_terminal_command`` tool-output label (``LabeledPlanningLoop._terminal_output_label``,
  kept for runs that still enable that tool).

The model is a DENYLIST over a default-trusted terminal (the masking design): the terminal is not
masked by default; a command taints only when it actually PRINTS external / attacker-reachable
content ONTO THE TERMINAL -- a network fetch that streams to the tty, a content-printing command
reading a URL or an UNTRUSTED file to the tty, or an interpreter running inline code. `cd`, `ls`,
`git clone`, `mkdir`, an empty prompt, or a read of a TRUSTED file do NOT taint.

Two refinements make the gate track what is actually *visible* on the terminal (2026-09-17):

* THE LIVE INPUT LINE IS EXEMPT. A command taints only once it is in scrollback HISTORY -- i.e. it
  was run (Enter pressed) and the shell returned a fresh prompt below it. A command still being
  typed on the current input line has produced nothing -- no output, no file, no variable -- so it
  cannot taint. This is what stopped the terminal from masking itself mid-type and then blocking
  the very Enter needed to run the command (`text_has_taint_command` skips the last prompt line).

* REDIRECT-TO-A-FILE IS CLEAR. `curl URL > out.log`, `curl -o out.log URL`, plain `wget URL` --
  the bytes land in a file, not on the terminal, so the terminal stays first-party. That file is
  untrusted by provenance (it is not a trusted-list name), so a later `cat out.log` re-taints at
  the point the content is actually printed. The ONE exception is command/process substitution
  (`VAR=$(curl URL)`, backticks): a shell variable is an UNTRACKED sink -- unlike a file, we have
  no oracle to re-catch it when it is later reused -- so a capture taints eagerly, fail-closed,
  regardless of redirection.

Nothing here is a blocklist of *dangerous* commands -- it is a statement about which commands put
content whose provenance is invisible onto the screen. `rm` is not here; `cat` is.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Callable, Optional

TERMINAL_APPS = frozenset({
    "gnome-terminal-server", "GNOME Terminal", "gnome-terminal", "xterm", "Terminal",
    "Konsole", "konsole", "xfce4-terminal", "mate-terminal", "tilix", "kgx", "org.gnome.Console",
})

_NETWORK_COMMANDS = frozenset({
    "curl", "http", "https", "xh", "lwp-request", "lynx", "w3m",
    "nc", "ncat", "netcat", "socat",
    "ssh", "telnet", "ftp", "sftp", "lftp", "tftp",
    "host", "dig", "nslookup",
})
_NETWORK_FILE_WRITERS = frozenset({"wget", "wget2", "aria2c", "rsync"})
_TTY_TARGETS = frozenset({"/dev/stdout", "/dev/tty", "/dev/console", "-"})
_FILE_READ_COMMANDS = frozenset({
    "cat", "tac", "nl", "rev", "head", "tail", "less", "more", "most", "pg", "bat", "batcat", "view",
    "zcat", "zless", "bzcat", "bzless", "xzcat", "xzless", "lzcat", "lzless",
    "strings", "xxd", "hexdump", "od", "hd",
    "nano", "pico", "vi", "vim", "nvim", "gvim", "emacs", "emacsclient", "ed", "ex",
    "micro", "joe", "mcedit", "ne", "kak", "hx", "gedit", "kate", "mousepad", "code", "subl",
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "ug", "ugrep",
    "awk", "gawk", "mawk", "nawk", "sed",
    "sort", "uniq", "cut", "tr", "paste", "join", "comm", "column", "fold", "fmt", "pr", "expand", "unexpand",
    "diff", "sdiff", "colordiff", "delta", "wdiff",
    "jq", "yq", "xq", "gron", "mlr", "csvtool", "csvlook",
    "pdftotext", "pandoc", "antiword", "catdoc", "unrtf", "docx2txt",
    "tar", "unzip", "7z", "7za", "unrar", "ar", "cpio", "zipinfo", "unar",
    "python", "python2", "python3", "ipython", "pypy",
    "bash", "sh", "zsh", "dash", "ksh", "fish",
    "node", "nodejs", "deno", "bun",
    "perl", "ruby", "php", "lua", "tclsh", "rscript",
    "sqlite3", "mysql", "psql", "mongo", "mongosh", "redis-cli",
})
_URL_OPERAND_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_SUBSTITUTION_RE = re.compile(r"\$\(|`|\$\{|<\(|>\(")
_INLINE_CODE_FLAGS = frozenset({"-c", "-e", "--command", "--eval", "-p", "-E"})
_INTERPRETERS = frozenset({
    "python", "python2", "python3", "ipython", "pypy", "bash", "sh", "zsh", "dash", "ksh",
    "fish", "node", "nodejs", "deno", "bun", "perl", "ruby", "php", "lua", "tclsh", "rscript",
})

_PROMPT_RE = re.compile(r"\S[$#]\s+(\S.*)$")

_PROMPT_LINE_RE = re.compile(r"\S[$#]\s*(\S.*)?$")

PathTrust = Callable[[str], Optional[bool]]


def is_terminal_scope(app_scope: Optional[str]) -> bool:
    """True when a masking app scope (``"app:gnome-terminal-server"`` or a bare app name) is a
    terminal application."""
    if not app_scope:
        return False
    name = app_scope[len("app:"):] if app_scope.startswith("app:") else app_scope
    return name in TERMINAL_APPS


_OUT_REDIR_RE = re.compile(r"^(?P<fd>\d*)(?P<amp>&?)>(?P<app>>?)$")


def _stdout_redirected_to_file(stage: str) -> bool:
    """True when this pipeline stage sends its STDOUT to a file (so its content never reaches the
    terminal). Recognises shell redirects (`> f`, `>> f`, `1> f`, `&> f`) to a real file -- NOT
    stderr-only (`2> f`) and NOT a redirect back to the tty (`> /dev/stdout`, `>&2`)."""
    for m in re.finditer(r"(?P<fd>\d*)(?P<amp>&?)>(?P<app>>?)\s*(?P<tgt>[^\s|;&<>]*)", stage):
        if not (m.group("amp") == "&" or m.group("fd") in ("", "1")):
            continue
        tgt = m.group("tgt").strip()
        if not tgt or tgt.startswith("&") or tgt in _TTY_TARGETS:
            continue
        return True
    return False


def _strip_out_redirections(toks: list) -> list:
    """Drop stdout/stderr OUTPUT redirect operators and their targets from a token list, so the
    remaining tokens are the command and its real operands. Input redirects (`<`) are kept -- they
    name an input file the command reads, which the operand scan needs."""
    out, i = [], 0
    while i < len(toks):
        t = toks[i]
        if _OUT_REDIR_RE.match(t):
            i += 2
            continue
        if re.match(r"^(?:\d*&?>>?)\S", t):
            i += 1
            continue
        out.append(t)
        i += 1
    return out


def _has_stdout_flag(args: list) -> bool:
    """True when a fetcher is explicitly told to stream its body to STDOUT: `-O -`, `--output -`,
    `--output-document=-`, or a combined short cluster ending in the stdout sink (`-qO-`)."""
    for i, a in enumerate(args):
        if a in ("-O", "-o", "--output", "--output-document") and i + 1 < len(args) \
                and args[i + 1] in _TTY_TARGETS:
            return True
        if a.startswith("--output-document=") and a.split("=", 1)[1] in _TTY_TARGETS:
            return True
        if re.match(r"^-[a-zA-Z]*[Oo]-$", a):
            return True
    return False


def _writes_to_file(args: list) -> bool:
    """True when a print-by-default fetcher (curl) is told to write to a FILE instead of the tty:
    `-o F`, `--output F`, `-O`/`--remote-name` (F not the stdout sink)."""
    for i, a in enumerate(args):
        if a in ("-O", "--remote-name", "--remote-name-all"):
            return True
        if a in ("-o", "--output") and i + 1 < len(args) and args[i + 1] not in _TTY_TARGETS:
            return True
        if a.startswith("--output=") and a.split("=", 1)[1] not in _TTY_TARGETS:
            return True
    return False


def _file_operands(args: list) -> list:
    """The non-flag operands a file-reader would read, treating `< F` as naming an input file F."""
    ops, i = [], 0
    while i < len(args):
        t = args[i]
        if t in ("<", "0<", "<<") and i + 1 < len(args):
            ops.append(args[i + 1]); i += 2; continue
        if not t.startswith("-"):
            ops.append(t)
        i += 1
    return ops


def _stage_surfaces_untrusted(stage: str, path_trust: Optional[PathTrust],
                              taint_on_unknown_file: bool) -> bool:
    """True when this single pipeline stage puts attacker-reachable content onto its STDOUT (before
    any redirect). The caller decides whether that stdout actually reaches the terminal."""
    try:
        toks = _strip_out_redirections(shlex.split(stage))
    except ValueError:
        return True
    if not toks:
        return False
    verb = os.path.basename(toks[0])
    args = toks[1:]
    if verb in _NETWORK_FILE_WRITERS:
        return _has_stdout_flag(args)
    if verb in _NETWORK_COMMANDS:
        return not _writes_to_file(args)
    if verb in _INTERPRETERS and any(t in _INLINE_CODE_FLAGS for t in args):
        return True
    if verb in _FILE_READ_COMMANDS:
        for op in _file_operands(args):
            if _URL_OPERAND_RE.match(op):
                return True
            if path_trust is None:
                return True
            trust = path_trust(op)
            if trust is False or (trust is None and taint_on_unknown_file):
                return True
    return False


def command_taints(command: str, path_trust: Optional[PathTrust] = None,
                   taint_on_unknown_file: bool = False) -> bool:
    """True when running *command* PRINTS attacker-reachable content onto the terminal (so a terminal
    showing it must be masked). Denylist over a default-trusted terminal; see the module docstring.

    *path_trust* grades a file operand: True (trusted/user-owned), False (known-untrusted), or None
    (no record). A file-read of a non-URL operand taints when the operand is known-untrusted. With NO
    ``path_trust`` supplied, any plain file operand taints (the caller has no oracle to prove the file
    is the user's own). ``taint_on_unknown_file`` additionally taints when ``path_trust`` returns None
    -- the masking side sets this so a read of a file the episode never registered is fail-closed,
    while the tool-output caller leaves it False (its historical behaviour: taint only on a *known*
    untrusted operand).

    Redirect-to-a-file does NOT taint (the content lands in a file, untrusted by provenance, not on
    the terminal); command/process substitution DOES, eagerly, because a captured shell variable is
    an untracked sink. See the module docstring."""
    if not command or not command.strip():
        return False
    if _SUBSTITUTION_RE.search(command):
        return True
    for statement in re.split(r"\|\||&&|[;&\n]", command):
        if not statement.strip():
            continue
        stages = statement.split("|")
        if _stdout_redirected_to_file(stages[-1]):
            continue
        for stage in stages:
            if _stage_surfaces_untrusted(stage, path_trust, taint_on_unknown_file):
                return True
    return False


def text_has_taint_command(terminal_text: str, path_trust: Optional[PathTrust] = None,
                          taint_on_unknown_file: bool = True) -> bool:
    """True when a terminal's visible scrollback contains an EXECUTED taint-producing COMMAND.

    Scans only COMMAND lines -- the text after a shell prompt (`user@host:~$ <command>`) -- so plain
    OUTPUT lines are never mistaken for commands (`git clone …done.` output does not taint just
    because a later line mentions `curl`). Each extracted command is judged by ``command_taints``.

    The LAST prompt-bearing line is the LIVE input line (the cursor sits there): whatever is typed on
    it has NOT been run -- no Enter, no output, no file, no variable -- so it is EXEMPT. A command
    taints only once it is history, i.e. a later prompt line follows it. This is what stops a
    half-typed taint command from masking the terminal before it runs and then blocking the Enter
    that would run it (the deadlock the gpt-5.6-luna run hit)."""
    if not terminal_text:
        return False
    lines = terminal_text.splitlines()
    prompt_line_idxs = [i for i, ln in enumerate(lines) if _PROMPT_LINE_RE.search(ln)]
    live_input = prompt_line_idxs[-1] if prompt_line_idxs else None
    for i, line in enumerate(lines):
        if i == live_input:
            continue
        m = _PROMPT_RE.search(line)
        if not m:
            continue
        if command_taints(m.group(1).strip(), path_trust, taint_on_unknown_file):
            return True
    return False
