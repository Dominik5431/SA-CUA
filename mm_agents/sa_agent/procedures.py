"""
procedures.py -- Layer 3, step 1: a proposed procedure becomes an AST, never a string.
=====================================================================================

Plan §5.1. When untrusted content proposes *a thing to run* rather than *a value*, Layer 1 has
nothing to say: there is no charset that both admits ``sudo apt-get install -y htop`` and refuses
``rm -f ~/BankStatement.pdf``. Capacity is the wrong question for a procedure, because a procedure
is *supposed* to carry instructions.

So the mechanism changes shape. The command string is parsed host-side into a typed AST -- argv
arrays, pipeline topology, ``&&``/``;`` sequencing, redirections -- and three things follow that
do not follow from handling it as text:

1. **Every leaf is a Layer-1 typed value with a known type**, because the command's own grammar
   says so: the operands of ``apt-get install`` are package names, the operands of ``rm`` are file
   paths, the operand of ``git clone`` is a URL. Leaf verification (plan §5.4 Tier B) is only
   meaningful once the leaves are individually addressable and typed.
2. **Every node carries a capability**, so containment (§5.3) can ask the only question that
   generalizes: does this procedure's effect set fit inside what the *user's own task* authorized?
   See ``capabilities.classify_procedure``.
3. **The string the untrusted source wrote is never what executes.** ``render()`` re-emits an
   argv/pipeline from the verified AST, so parameter metacharacters are inert by construction --
   a leaf that survived verification as the literal five characters ``htop`` cannot become
   ``htop; rm -rf ~`` on the way to the shell, because the rendering path never concatenates.

**Parse failure is a refusal, not a fallback.** A procedure this module cannot model is a
procedure whose effects it cannot enumerate, and admitting one on the grounds that nothing
recognized it as bad is precisely the inverted burden of proof the rest of the design exists to
avoid. Control flow (``for``/``while``/``if``), command substitution (``$(...)``, backticks),
process substitution and background execution are therefore all parse errors -- which is also,
incidentally, what stops the ``for ((i=1;i<=10000;i++)); do truncate ...; done`` payload from
plan §6.1 without any rule that mentions loops or truncation.

Nothing here consults a blocklist. The tables below are a *grammar* -- how each program's argv
splits into subcommand, flags and operands, and what an operand of that program means -- not a
list of dangerous things.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Optional

MAX_PROCEDURE_CHARS = 2000
MAX_TOKENS = 200

_SEQ_OPS = ("&&", "||", ";", "\n")
_PIPE_OPS = ("|",)
_REDIR_OPS = (">", ">>", "<", ">|", "&>")

_SHELL_KEYWORDS = frozenset({
    "for", "while", "until", "do", "done", "if", "then", "elif", "fi", "case", "esac",
    "select", "function", "in", "{", "}", "((", "))", "((;", "));", "[[", "]]", "!",
    "trap", "eval", "exec", "source", ".",
})

_SUBSTITUTION_RE = re.compile(r"\$\(|`|\$\{|\$\[|<\(|>\(")


class ProcedureParseError(ValueError):
    """The text is not a procedure this module can model. Always a refusal (plan §5.1)."""


@dataclass(frozen=True)
class Redirect:
    op: str
    target: str


@dataclass(frozen=True)
class Command:
    """One simple command: ``[env assignments] [privilege prefix] argv [redirections]``.

    ``argv`` is the command proper, with any ``sudo``/``env``/``nohup`` wrapper lifted into
    ``prefixes``. Splitting them matters twice over: the wrapper contributes its own capability
    (privileged execution), and the operand grammar of the *wrapped* program is what types the
    leaves -- ``sudo apt-get install htop`` has a package-name operand, not a ``sudo`` operand.
    """
    argv: tuple[str, ...]
    prefixes: tuple[str, ...] = ()
    assignments: tuple[str, ...] = ()
    redirects: tuple[Redirect, ...] = ()

    @property
    def program(self) -> str:
        return os.path.basename(self.argv[0]) if self.argv else ""

    @property
    def privileged(self) -> bool:
        return any(os.path.basename(p) == "sudo" for p in self.prefixes)

    @property
    def prefix_programs(self) -> tuple[str, ...]:
        return tuple(os.path.basename(p) for p in self.prefixes if not p.startswith("-"))


@dataclass(frozen=True)
class Stage:
    """A pipeline: one or more commands joined by ``|``."""
    commands: tuple[Command, ...]


@dataclass(frozen=True)
class Procedure:
    stages: tuple[Stage, ...]
    operators: tuple[str, ...] = ()
    source: str = ""

    @property
    def commands(self) -> tuple[Command, ...]:
        return tuple(c for s in self.stages for c in s.commands)

    @property
    def is_single_command(self) -> bool:
        """One stage, one command -- a redirection is allowed.

        This is the shape Tier B may admit, and the property is worth naming rather than
        inlining: Tier B's warrant is *per parameter* -- each leaf independently cleared a
        verifier -- and a per-parameter warrant says nothing whatsoever about composition. Who
        vouches for the fact that these three commands belong together, in this order, with this
        data flowing between them? Tier A does (the host owns the shape) and Tier C does (a
        trusted source states the shape). Tier B does not, so it may not admit a shape.

        A redirection is *not* composition in that sense: it adds one more typed leaf (the target
        path) and one more capability (write, or overwrite for ``>``), both of which the
        per-parameter warrant covers. A pipeline or an ``&&`` sequence is composition, because
        what makes it a single procedure is the relationship *between* the stages, and nothing
        Tier B checks looks at that relationship.
        """
        return len(self.stages) == 1 and len(self.stages[0].commands) == 1

    @property
    def is_simple(self) -> bool:
        """One command with no redirection -- the shape a canonical playbook may match.

        Stricter than :attr:`is_single_command` on purpose: a playbook renders a *fixed* argv, so
        a redirection the template does not contain is structure the host did not author.
        """
        return self.is_single_command and not self.stages[0].commands[0].redirects



UNKNOWN_CAPABILITY = "unknown"


@dataclass(frozen=True)
class CommandSpec:
    """How one program's argv decomposes, and what its nodes do."""
    program: str
    caps: dict = field(default_factory=dict)
    operands: dict = field(default_factory=dict)
    operand_params: dict = field(default_factory=dict)
    n_subcommands: int = 0
    value_flags: frozenset = frozenset()
    flag_types: dict = field(default_factory=dict)
    output_flags: frozenset = frozenset()
    recursive_flags: frozenset = frozenset()

    def caps_for(self, sub: str) -> Optional[frozenset]:
        if sub in self.caps:
            return self.caps[sub]
        return self.caps.get("")

    def flag_operand_type(self, flag: str) -> tuple[str, dict]:
        """The value-type a flag's argument is checked as. Default SHORT_TEXT: fail-closed, so a
        flag whose value has side effects but was never typed here still gets held to task_match."""
        t = self.flag_types.get(flag)
        if isinstance(t, tuple):
            name, params = t[0], (t[1] if len(t) > 1 else {})
        else:
            name, params = (t or "SHORT_TEXT"), {}
        return name, dict(params)

    def operand_type(self, sub: str, index: int) -> tuple[str, dict]:
        types = self.operands.get(sub) or self.operands.get("") or ("SHORT_TEXT",)
        name = types[index] if index < len(types) else types[-1]
        return name, dict(self.operand_params.get(name, {}))


_FS_READ = frozenset({"os.fs.read"})
_RUN = frozenset({"os.terminal.run"})
_PKG_INSTALL = frozenset({"os.fs.write", "os.terminal.run"})
_PKG_REMOVE = frozenset({"os.fs.delete", "os.terminal.run"})


def _spec(program, caps, operands=None, **kw) -> CommandSpec:
    return CommandSpec(program=program, caps=caps, operands=operands or {}, **kw)


COMMAND_TABLE: dict[str, CommandSpec] = {
    "rm":        _spec("rm", {"": frozenset({"os.fs.delete"})}, {"": ("FILE_PATH",)},
                       recursive_flags=frozenset({"-r", "-R", "--recursive", "-rf", "-fr"})),
    "rmdir":     _spec("rmdir", {"": frozenset({"os.fs.delete"})}, {"": ("FILE_PATH",)}),
    "unlink":    _spec("unlink", {"": frozenset({"os.fs.delete"})}, {"": ("FILE_PATH",)}),
    "shred":     _spec("shred", {"": frozenset({"os.fs.delete"})}, {"": ("FILE_PATH",)}),
    "truncate":  _spec("truncate", {"": frozenset({"os.fs.overwrite"})}, {"": ("FILE_PATH",)},
                       value_flags=frozenset({"-s", "--size", "-r", "--reference"}),
                       flag_types={"-s": "OPTION_LABEL", "--size": "OPTION_LABEL",
                                   "-r": "FILE_PATH", "--reference": "FILE_PATH"}),
    "dd":        _spec("dd", {"": frozenset({"os.fs.overwrite"})}, {"": ("SHORT_TEXT",)}),
    "mv":        _spec("mv", {"": frozenset({"os.fs.write"})}, {"": ("FILE_PATH",)}),
    "cp":        _spec("cp", {"": frozenset({"os.fs.write"})}, {"": ("FILE_PATH",)},
                       recursive_flags=frozenset({"-r", "-R", "--recursive"})),
    "mkdir":     _spec("mkdir", {"": frozenset({"os.fs.write"})}, {"": ("FILE_PATH",)}),
    "touch":     _spec("touch", {"": frozenset({"os.fs.write"})}, {"": ("FILE_PATH",)}),
    "ln":        _spec("ln", {"": frozenset({"os.fs.write"})}, {"": ("FILE_PATH",)}),
    "tee":       _spec("tee", {"": frozenset({"os.fs.write"})}, {"": ("FILE_PATH",)}),
    "tar":       _spec("tar", {"": frozenset({"os.fs.write"})}, {"": ("FILE_PATH",)},
                       value_flags=frozenset({"-f", "--file", "-C", "--directory",
                                              "--strip-components"}),
                       flag_types={"-f": "FILE_PATH", "--file": "FILE_PATH",
                                   "-C": "FILE_PATH", "--directory": "FILE_PATH",
                                   "--strip-components": "OPTION_LABEL"}),
    "unzip":     _spec("unzip", {"": frozenset({"os.fs.write"})}, {"": ("FILE_PATH",)},
                       value_flags=frozenset({"-d"}), flag_types={"-d": "FILE_PATH"}),
    "gzip":      _spec("gzip", {"": frozenset({"os.fs.write"})}, {"": ("FILE_PATH",)}),
    "gunzip":    _spec("gunzip", {"": frozenset({"os.fs.write"})}, {"": ("FILE_PATH",)}),
    "bunzip2":   _spec("bunzip2", {"": frozenset({"os.fs.write"})}, {"": ("FILE_PATH",)}),
    "xz":        _spec("xz", {"": frozenset({"os.fs.write"})}, {"": ("FILE_PATH",)}),
    "unxz":      _spec("unxz", {"": frozenset({"os.fs.write"})}, {"": ("FILE_PATH",)}),
    "make":      _spec("make", {"": frozenset({"os.terminal.run", "os.fs.write"})},
                       {"": ("OPTION_LABEL",)},
                       value_flags=frozenset({"-C", "-f", "-j"}),
                       flag_types={"-C": "FILE_PATH", "-f": "FILE_PATH", "-j": "OPTION_LABEL"}),
    "sed":       _spec("sed", {"": frozenset({"os.fs.write"})},
                       {"": ("SHORT_TEXT", "FILE_PATH")},
                       value_flags=frozenset({"-e", "-f"}),
                       flag_types={"-e": "SHORT_TEXT", "-f": "FILE_PATH"}),
    "chmod":     _spec("chmod", {"": _RUN}, {"": ("SHORT_TEXT", "FILE_PATH")},
                       recursive_flags=frozenset({"-R", "--recursive"})),
    "chown":     _spec("chown", {"": _RUN}, {"": ("SHORT_TEXT", "FILE_PATH")},
                       recursive_flags=frozenset({"-R", "--recursive"})),
    "cat":       _spec("cat", {"": _FS_READ}, {"": ("FILE_PATH",)}),
    "ls":        _spec("ls", {"": _FS_READ}, {"": ("FILE_PATH",)}),
    "head":      _spec("head", {"": _FS_READ}, {"": ("FILE_PATH",)}, value_flags=frozenset({"-n", "-c"})),
    "tail":      _spec("tail", {"": _FS_READ}, {"": ("FILE_PATH",)}, value_flags=frozenset({"-n", "-c"})),
    "wc":        _spec("wc", {"": _FS_READ}, {"": ("FILE_PATH",)}),
    "grep":      _spec("grep", {"": _FS_READ}, {"": ("SHORT_TEXT", "FILE_PATH")},
                       value_flags=frozenset({"-e", "-m", "--include", "--exclude"}),
                       recursive_flags=frozenset({"-r", "-R", "--recursive"})),
    "find":      _spec("find", {"": _FS_READ}, {"": ("FILE_PATH",)},
                       value_flags=frozenset({"-name", "-type", "-maxdepth", "-exec"})),
    "which":     _spec("which", {"": _FS_READ}, {"": ("IDENTIFIER",)}),
    "file":      _spec("file", {"": _FS_READ}, {"": ("FILE_PATH",)}),
    "stat":      _spec("stat", {"": _FS_READ}, {"": ("FILE_PATH",)}),
    "pwd":       _spec("pwd", {"": _FS_READ}, {"": ("SHORT_TEXT",)}),
    "echo":      _spec("echo", {"": frozenset()}, {"": ("SHORT_TEXT",)}),
    "printf":    _spec("printf", {"": frozenset()}, {"": ("SHORT_TEXT",)}),
    "apt-get":   _spec("apt-get",
                       {"install": _PKG_INSTALL, "update": _PKG_INSTALL, "upgrade": _PKG_INSTALL,
                        "remove": _PKG_REMOVE, "purge": _PKG_REMOVE, "autoremove": _PKG_REMOVE},
                       {"install": ("PACKAGE_NAME",), "remove": ("PACKAGE_NAME",),
                        "purge": ("PACKAGE_NAME",), "": ("PACKAGE_NAME",)},
                       operand_params={"PACKAGE_NAME": {"manager": "apt"}},
                       n_subcommands=1, value_flags=frozenset({"-o", "-t"}),
                       flag_types={"-o": "SHORT_TEXT", "-t": "OPTION_LABEL"}),
    "apt":       _spec("apt",
                       {"install": _PKG_INSTALL, "update": _PKG_INSTALL, "upgrade": _PKG_INSTALL,
                        "remove": _PKG_REMOVE, "purge": _PKG_REMOVE, "autoremove": _PKG_REMOVE},
                       {"install": ("PACKAGE_NAME",), "remove": ("PACKAGE_NAME",),
                        "purge": ("PACKAGE_NAME",), "": ("PACKAGE_NAME",)},
                       operand_params={"PACKAGE_NAME": {"manager": "apt"}},
                       n_subcommands=1, value_flags=frozenset({"-o", "-t"}),
                       flag_types={"-o": "SHORT_TEXT", "-t": "OPTION_LABEL"}),
    "nvm":       _spec("nvm",
                       {"install": frozenset({"web.download", "os.fs.write", "os.terminal.run"}),
                        "use": _RUN, "alias": _RUN, "": _RUN},
                       {"install": ("SEMVER",), "use": ("SEMVER",), "": ("OPTION_LABEL",)},
                       n_subcommands=1),
    "pip":       _spec("pip",
                       {"install": _PKG_INSTALL, "uninstall": _PKG_REMOVE, "download": frozenset({"web.download", "os.fs.write"})},
                       {"": ("PACKAGE_NAME",)},
                       operand_params={"PACKAGE_NAME": {"manager": "pip"}},
                       n_subcommands=1,
                       value_flags=frozenset({"-r", "--requirement", "-i", "--index-url", "-t", "--target"}),
                       flag_types={"-i": "URL", "--index-url": "URL", "-r": "FILE_PATH",
                                   "--requirement": "FILE_PATH", "-t": "FILE_PATH",
                                   "--target": "FILE_PATH"}),
    "npm":       _spec("npm",
                       {"install": _PKG_INSTALL, "i": _PKG_INSTALL,
                        "uninstall": _PKG_REMOVE, "remove": _PKG_REMOVE},
                       {"": ("PACKAGE_NAME",)},
                       operand_params={"PACKAGE_NAME": {"manager": "npm"}},
                       n_subcommands=1, value_flags=frozenset({"--prefix", "--registry"}),
                       flag_types={"--registry": "URL", "--prefix": "FILE_PATH"}),
    "docker":    _spec("docker",
                       {"pull": frozenset({"web.download", "os.fs.write"}), "run": _RUN,
                        "build": _PKG_INSTALL, "rm": frozenset({"os.fs.delete"}),
                        "rmi": frozenset({"os.fs.delete"})},
                       {"pull": ("PACKAGE_NAME",), "": ("SHORT_TEXT",)},
                       operand_params={"PACKAGE_NAME": {"manager": "dockerhub"}},
                       n_subcommands=1),
    "git":       _spec("git",
                       {"clone": frozenset({"os.fs.write", "web.download"}),
                        "config": frozenset({"os.fs.write"}),
                        "pull": frozenset({"os.fs.write", "web.download"}),
                        "fetch": frozenset({"web.download"}),
                        "status": _FS_READ, "log": _FS_READ, "diff": _FS_READ,
                        "checkout": frozenset({"os.fs.write"}),
                        "add": frozenset({"os.fs.write"}),
                        "commit": frozenset({"os.fs.write"}),
                        "push": frozenset({"web.submit_form"})},
                       {"clone": ("URL", "FILE_PATH"), "config": ("SHORT_TEXT",),
                        "": ("SHORT_TEXT",)},
                       n_subcommands=1,
                       value_flags=frozenset({"-b", "--branch", "-C", "--depth"}),
                       flag_types={"-C": "FILE_PATH", "-b": "OPTION_LABEL",
                                   "--branch": "OPTION_LABEL", "--depth": "OPTION_LABEL"}),
    "curl":      _spec("curl", {"": frozenset()}, {"": ("URL",)},
                       value_flags=frozenset({"-o", "--output", "-H", "--header", "-d",
                                              "--data", "-X", "-u", "--user", "-A"}),
                       flag_types={"-o": "FILE_PATH", "--output": "FILE_PATH"},
                       output_flags=frozenset({"-o", "-O", "--output", "--remote-name"})),
    "wget":      _spec("wget", {"": frozenset()}, {"": ("URL",)},
                       value_flags=frozenset({"-O", "--output-document", "-P", "--directory-prefix"}),
                       flag_types={"-O": "FILE_PATH", "--output-document": "FILE_PATH",
                                   "-P": "FILE_PATH", "--directory-prefix": "FILE_PATH"},
                       output_flags=frozenset({"-O", "--output-document", "-P", "--directory-prefix"})),
    "bash":      _spec("bash", {"": _RUN}, {"": ("SHORT_TEXT",)}),
    "sh":        _spec("sh", {"": _RUN}, {"": ("SHORT_TEXT",)}),
    "dash":      _spec("dash", {"": _RUN}, {"": ("SHORT_TEXT",)}),
    "zsh":       _spec("zsh", {"": _RUN}, {"": ("SHORT_TEXT",)}),
    "python":    _spec("python", {"": _RUN}, {"": ("FILE_PATH",)}),
    "python3":   _spec("python3", {"": _RUN}, {"": ("FILE_PATH",)}),
    "node":      _spec("node", {"": _RUN}, {"": ("FILE_PATH",)}),
    "tmux":      _spec("tmux",
                       {"new-session": _RUN, "new": _RUN, "attach-session": _RUN,
                        "attach": _RUN, "a": _RUN, "kill-session": _RUN, "send-keys": _RUN,
                        "has-session": _FS_READ, "list-sessions": _FS_READ, "ls": _FS_READ},
                       {"": ("SHORT_TEXT",)},
                       n_subcommands=1, value_flags=frozenset({"-s", "-t", "-n", "-c", "-F"}),
                       flag_types={"-s": "OPTION_LABEL", "-t": "OPTION_LABEL", "-n": "OPTION_LABEL",
                                   "-c": "FILE_PATH", "-F": "SHORT_TEXT"}),
    "systemctl": _spec("systemctl", {"": _RUN}, {"": ("IDENTIFIER",)}, n_subcommands=1),
    "service":   _spec("service", {"": _RUN}, {"": ("IDENTIFIER",)}),
}

_PREFIX_PROGRAMS = frozenset({"sudo", "env", "nohup", "nice", "time", "command"})
_PREFIX_VALUE_FLAGS = frozenset({"-u", "--user", "-g", "--group", "-n"})
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _tokenize(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError as e:
        raise ProcedureParseError(f"the text is not a well-formed command line ({e})") from e


def _split_command(tokens: list[str]) -> Command:
    """One simple command's tokens -> Command, lifting assignments/wrappers off the front."""
    assignments: list[str] = []
    prefixes: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _ASSIGNMENT_RE.match(tok):
            assignments.append(tok)
            i += 1
            continue
        base = os.path.basename(tok)
        if base in _PREFIX_PROGRAMS:
            prefixes.append(base)
            i += 1
            while i < len(tokens) and tokens[i].startswith("-") and tokens[i] != "-":
                flag = tokens[i]
                prefixes.append(flag)
                i += 1
                if flag in _PREFIX_VALUE_FLAGS and i < len(tokens):
                    prefixes.append(tokens[i])
                    i += 1
            continue
        break
    argv = tuple(tokens[i:])
    if not argv:
        raise ProcedureParseError(
            "the command has no program to run (only environment assignments or wrappers)")
    return Command(argv=argv, prefixes=tuple(prefixes), assignments=tuple(assignments))


def parse_procedure(text: str) -> Procedure:
    """Parse *text* into a :class:`Procedure`, or raise :class:`ProcedureParseError`.

    Deliberately narrow. Everything it refuses, it refuses because the resulting AST would not
    let ``capabilities.classify_procedure`` enumerate the effects -- not because the construct
    looked dangerous.
    """
    raw = (text or "").strip()
    if not raw:
        raise ProcedureParseError("empty procedure")
    if len(raw) > MAX_PROCEDURE_CHARS:
        raise ProcedureParseError(
            f"procedure is {len(raw)} characters, over the {MAX_PROCEDURE_CHARS}-character cap")
    m = _SUBSTITUTION_RE.search(raw)
    if m:
        raise ProcedureParseError(
            f"the procedure uses substitution ({m.group(0)!r}), so what its leaves will actually "
            f"be is not decidable until it runs -- there is nothing here to verify")
    tokens = _tokenize(raw.replace("\n", " ; "))
    if not tokens:
        raise ProcedureParseError("empty procedure")
    if len(tokens) > MAX_TOKENS:
        raise ProcedureParseError(f"procedure has {len(tokens)} tokens, over the {MAX_TOKENS} cap")

    stages: list[Stage] = []
    operators: list[str] = []
    pipeline: list[Command] = []
    current: list[str] = []
    redirects: list[Redirect] = []

    def flush_command() -> None:
        if not current:
            raise ProcedureParseError("an operator has no command on one side of it")
        cmd = _split_command(current)
        pipeline.append(Command(argv=cmd.argv, prefixes=cmd.prefixes,
                                assignments=cmd.assignments, redirects=tuple(redirects)))
        current.clear()
        redirects.clear()

    def flush_stage() -> None:
        flush_command()
        stages.append(Stage(commands=tuple(pipeline)))
        pipeline.clear()

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _SEQ_OPS:
            flush_stage()
            operators.append(";" if tok == "\n" else tok)
            i += 1
            continue
        if tok in _PIPE_OPS:
            flush_command()
            i += 1
            continue
        if tok in _REDIR_OPS:
            if i + 1 >= len(tokens):
                raise ProcedureParseError(f"redirection {tok!r} has no target")
            redirects.append(Redirect(op=tok, target=tokens[i + 1]))
            i += 2
            continue
        if tok in _SHELL_KEYWORDS or tok in ("&", "(", ")", ";;"):
            raise ProcedureParseError(
                f"the procedure uses the shell construct {tok!r}; control flow and grouping are "
                f"not modelled, so the set of effects cannot be enumerated")
        current.append(tok)
        i += 1
    flush_stage()

    if len(operators) != len(stages) - 1:
        raise ProcedureParseError("trailing sequencing operator")
    return Procedure(stages=tuple(stages), operators=tuple(operators), source=raw)



def render_command(cmd: Command) -> str:
    parts = list(cmd.assignments) + list(cmd.prefixes) + list(cmd.argv)
    out = " ".join(shlex.quote(p) if _needs_quoting(p) else p for p in parts)
    for r in cmd.redirects:
        out += f" {r.op} {shlex.quote(r.target) if _needs_quoting(r.target) else r.target}"
    return out


_TILDE_SAFE_RE = re.compile(r"^~[A-Za-z0-9_.-]*/[A-Za-z0-9_@%+=:,./-]*$")


def _needs_quoting(tok: str) -> bool:
    if _TILDE_SAFE_RE.match(tok):
        return False
    return shlex.quote(tok) != tok


def render(proc: Procedure) -> str:
    """Re-emit the procedure from its AST.

    This -- not the untrusted source's string -- is what a verified procedure hands to the sink.
    The distinction is the whole of plan §5.1's third property: a leaf that cleared verification
    as five characters is re-quoted from those five characters, so it cannot have acquired a
    metacharacter along the way, and no ``template``-splicing step exists for one to be smuggled
    through.
    """
    out: list[str] = []
    for idx, stage in enumerate(proc.stages):
        if idx:
            out.append(proc.operators[idx - 1])
        out.append(" | ".join(render_command(c) for c in stage.commands))
    return " ".join(out)



_SAFE_FLAG_RE = re.compile(r"^(-[A-Za-z]{1,8}|--[A-Za-z][A-Za-z0-9][A-Za-z0-9-]{0,28})$")


def _mask_leaf(type_name: str) -> str:
    return f"‹unverified {type_name.lower()}›" if type_name else "‹unverified›"


def _skeleton_command(cmd: Command, revealed: frozenset) -> str:
    parts = decompose(cmd)
    spec = parts.spec
    op_by_val: dict = {}
    for o in parts.operands:
        op_by_val.setdefault(o.value, o)
    recognised_subs = (set(spec.operands) | set(spec.caps)) - {""} if spec else set()
    value_flags = spec.value_flags if spec else frozenset()
    n_sub = spec.n_subcommands if spec else 0

    def leaf(tok: str) -> str:
        if tok in revealed:
            return tok
        o = op_by_val.get(tok)
        return _mask_leaf(o.type_name if o else "value")

    out: list[str] = []
    for a in cmd.assignments:
        name, _, val = a.partition("=")
        out.append(f"{name}={val if val in revealed else '‹unverified›'}")
    for p in cmd.prefixes:
        out.append(p if (p in _PREFIX_PROGRAMS or _SAFE_FLAG_RE.match(p)) else "‹unverified›")
    out.append(cmd.program if spec is not None else "‹program›")

    i, subs_shown = 1, 0
    while i < len(cmd.argv):
        tok = cmd.argv[i]
        if tok == "--":
            out.append("--")
            out.extend(leaf(t) for t in cmd.argv[i + 1:])
            break
        if tok.startswith("-") and len(tok) > 1:
            name, sep, inline = tok.partition("=")
            shown = name if _SAFE_FLAG_RE.match(name) else "‹option›"
            if sep:
                out.append(f"{shown}={leaf(inline)}")
            elif name in value_flags and i + 1 < len(cmd.argv):
                out.append(shown)
                out.append(leaf(cmd.argv[i + 1]))
                i += 2
                continue
            else:
                out.append(shown)
            i += 1
            continue
        if n_sub and subs_shown < n_sub:
            out.append(tok if tok in recognised_subs else "‹subcommand›")
            subs_shown += 1
        else:
            out.append(leaf(tok))
        i += 1

    for r in cmd.redirects:
        out.append(r.op)
        out.append(r.target if r.target in revealed else "‹unverified path›")
    return " ".join(out)


def render_skeleton(proc: Procedure, revealed: frozenset) -> str:
    """A partial view of *proc*: cleared parts (in *revealed*, a set of operand values the caller
    verified) plus structural tokens shown; everything unverified masked. For planner feedback on a
    refused procedure -- never a runnable string, and never written back as a cleared value."""
    out: list[str] = []
    for idx, stage in enumerate(proc.stages):
        if idx:
            out.append(proc.operators[idx - 1])
        out.append(" | ".join(_skeleton_command(c, revealed) for c in stage.commands))
    return " ".join(out)



@dataclass(frozen=True)
class Operand:
    """One typed leaf of the AST, with enough provenance to report which node it came from."""
    value: str
    type_name: str
    type_params: dict
    program: str
    subcommand: str
    flag: str = ""


@dataclass(frozen=True)
class CommandParts:
    spec: Optional[CommandSpec]
    subcommand: str
    flags: tuple[str, ...]
    operands: tuple[Operand, ...]
    writes_output: bool
    recursive: bool


_GLOB_RE = re.compile(r"[*?\[]")


def decompose(cmd: Command) -> CommandParts:
    """Split one command's argv into subcommand, flags and typed operands.

    Unknown program -> ``spec is None``; the caller (``capabilities.classify_procedure``) turns
    that into UNKNOWN and containment refuses. The operands are still collected, untyped, so an
    audit record of a refused procedure still says what it was about.
    """
    spec = COMMAND_TABLE.get(cmd.program)
    flags: list[str] = []
    positionals: list[str] = []
    flag_values: list[tuple[str, str]] = []
    writes_output = False
    recursive = False
    value_flags = spec.value_flags if spec else frozenset()
    output_flags = spec.output_flags if spec else frozenset()
    recursive_flags = spec.recursive_flags if spec else frozenset()

    i = 1
    while i < len(cmd.argv):
        tok = cmd.argv[i]
        if tok == "--":
            positionals.extend(cmd.argv[i + 1:])
            break
        if tok.startswith("-") and len(tok) > 1:
            name, sep, inline = tok.partition("=")
            flags.append(name)
            if name in recursive_flags or tok in recursive_flags:
                recursive = True
            if not tok.startswith("--") and len(name) > 2:
                for ch in name[1:]:
                    if f"-{ch}" in recursive_flags:
                        recursive = True
                    if f"-{ch}" in output_flags:
                        writes_output = True
            if name in output_flags:
                writes_output = True
            if sep and inline:
                flag_values.append((name, inline))
                i += 1
                continue
            if name in value_flags and i + 1 < len(cmd.argv):
                flag_values.append((name, cmd.argv[i + 1]))
                i += 2
                continue
            i += 1
            continue
        positionals.append(tok)
        i += 1

    n_sub = spec.n_subcommands if spec else 0
    subcommand = positionals[0] if (n_sub and positionals) else ""
    rest = positionals[n_sub:] if n_sub else positionals

    operands: list[Operand] = []
    for fname, val in flag_values:
        if spec is None:
            operands.append(Operand(val, "SHORT_TEXT", {}, cmd.program, subcommand, flag=fname))
        else:
            tname, tparams = spec.flag_operand_type(fname)
            operands.append(Operand(val, tname, tparams, cmd.program, subcommand, flag=fname))
    for idx, val in enumerate(rest):
        if spec is None:
            operands.append(Operand(val, "SHORT_TEXT", {}, cmd.program, subcommand))
            continue
        tname, tparams = spec.operand_type(subcommand, idx)
        operands.append(Operand(val, tname, tparams, cmd.program, subcommand))
    for r in cmd.redirects:
        if r.op in (">", ">>", ">|", "&>"):
            writes_output = True
            operands.append(Operand(r.target, "FILE_PATH", {}, cmd.program, subcommand))
    return CommandParts(spec=spec, subcommand=subcommand, flags=tuple(flags),
                        operands=tuple(operands), writes_output=writes_output,
                        recursive=recursive)


def has_glob(parts: CommandParts) -> bool:
    """Whether any operand carries a glob metacharacter.

    Separate from ``recursive`` because they are separate authorizations in plan §5.4 Tier B:
    ``-r`` says "descend", ``*`` says "however many of these there turn out to be". Both are ways
    of turning a bounded operand list into an unbounded one, which is why Tier B wants the task
    to have asked for either explicitly.
    """
    return any(_GLOB_RE.search(o.value) for o in parts.operands)
