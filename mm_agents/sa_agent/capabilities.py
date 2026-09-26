from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, List, Optional, Tuple


class Tier(Enum):
    READ = "READ"
    REVERSIBLE_WRITE = "REVERSIBLE_WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"


@dataclass(frozen=True)
class Capability:
    name: str
    tier: Tier
    description: str


CAPABILITY_CATALOG: dict[str, Capability] = {
    "web.read_screen":    Capability("web.read_screen",    Tier.READ,             "Read rendered page text/visible UI; OCR; summarize. No interaction."),
    "web.read_dom":       Capability("web.read_dom",       Tier.READ,             "Read DOM / accessibility tree."),
    "web.scroll":         Capability("web.scroll",         Tier.READ,             "Scroll within the current page."),
    "web.search":         Capability("web.search",         Tier.REVERSIBLE_WRITE, "Type a query into a search engine / site search box."),
    "web.navigate_url":   Capability("web.navigate_url",   Tier.REVERSIBLE_WRITE, "Navigate to a URL from a planner-provided allowlist."),
    "web.click_link":     Capability("web.click_link",     Tier.REVERSIBLE_WRITE, "Click links/buttons."),
    "web.fill_form":      Capability("web.fill_form",      Tier.REVERSIBLE_WRITE, "Enter non-sensitive data into form fields."),
    "web.submit_form":    Capability("web.submit_form",    Tier.DESTRUCTIVE,      "Submit a form / click a confirm/publish/post control."),
    "web.download":       Capability("web.download",       Tier.DESTRUCTIVE,      "Download a file."),
    "web.accept_consent": Capability("web.accept_consent", Tier.REVERSIBLE_WRITE, "Dismiss cookie/consent banners."),
    "os.fs.read":         Capability("os.fs.read",         Tier.READ,             "Read file/dir listings and contents."),
    "os.app.launch":      Capability("os.app.launch",      Tier.REVERSIBLE_WRITE, "Launch an application."),
    "os.fs.write":        Capability("os.fs.write",        Tier.REVERSIBLE_WRITE, "Create/append/move files (no overwrite of existing, no delete)."),
    "os.fs.overwrite":    Capability("os.fs.overwrite",    Tier.DESTRUCTIVE,      "Overwrite existing files."),
    "os.fs.delete":       Capability("os.fs.delete",       Tier.DESTRUCTIVE,      "Delete files / empty trash."),
    "os.terminal.run":    Capability("os.terminal.run",    Tier.REVERSIBLE_WRITE, "Run a shell command (scoped by planner-provided allowlist)."),
    "email.read":         Capability("email.read",         Tier.READ,             "Read inbox/message bodies (untrusted content!)."),
    "email.draft":        Capability("email.draft",        Tier.REVERSIBLE_WRITE, "Compose a draft, saved not sent."),
    "email.send":         Capability("email.send",         Tier.DESTRUCTIVE,      "Send / reply / forward (gate shows recipients + subject)."),
    "chat.read":          Capability("chat.read",          Tier.READ,             "Read messages (untrusted content!)."),
    "chat.draft":         Capability("chat.draft",         Tier.REVERSIBLE_WRITE, "Compose without sending."),
    "chat.send":          Capability("chat.send",          Tier.REVERSIBLE_WRITE, "Send a message in an existing thread to existing participants."),
    "chat.add_participant": Capability("chat.add_participant", Tier.DESTRUCTIVE,  "Add someone to a conversation (exfiltration risk)."),
}


def parse_capability_set(names: list[str]) -> frozenset[Capability]:
    """Resolve a list of capability name strings to a frozenset of Capability objects."""
    result = set()
    for name in names:
        if name not in CAPABILITY_CATALOG:
            raise ValueError(f"Unknown capability: {name!r}. Valid capabilities: {list(CAPABILITY_CATALOG)}")
        result.add(CAPABILITY_CATALOG[name])
    return frozenset(result)


def format_capability_catalog_table() -> str:
    """Format the full catalog as a table for embedding in P-LLM system prompt."""
    lines = ["| Capability | Tier | Permits |", "|---|---|---|"]
    for cap in CAPABILITY_CATALOG.values():
        lines.append(f"| `{cap.name}` | {cap.tier.value} | {cap.description} |")
    return "\n".join(lines)



_TYPEWRITE_RE = re.compile(
    r"""pyautogui\.(?:typewrite|write)\s*\(\s*['"]([^'"\\]*)['"]""",
    re.IGNORECASE,
)

_CAPABILITY_SIGNALS: dict[str, list[tuple[re.Pattern, str, bool]]] = {
    "os.fs.delete": [
        (re.compile(r"os\.remove\b|shutil\.rmtree\b|os\.unlink\b|os\.rmdir\b"),
         "Python file-deletion API call", False),
        (re.compile(r"\brm\s+-[rfRF]|\brm\b.*--force|\brmdir\b|\bdel\s+/[sfq]", re.IGNORECASE),
         "shell rm/del command typed in terminal", True),
    ],
    "os.fs.write": [
        (re.compile(r"open\s*\([^)]*[\"\'](w|a|wb|ab)[\"\']\s*[,)]"),
         "Python file write/append API call", False),
    ],
    "os.terminal.run": [
        (re.compile(r"subprocess\b|os\.system\b|os\.popen\b|os\.exec\b"),
         "Python shell-execution API call", False),
        (re.compile(r"\bsudo\b|\bcurl\b.*\|\s*(?:ba)?sh\b|\bchmod\b|\bchown\b|\bmkfs\b|\bdd\b\s+if=",
                    re.IGNORECASE),
         "privileged shell command typed in terminal", True),
    ],
    "email.send": [
        (re.compile(r"smtplib\b|\.sendmail\b"),
         "Python SMTP/email-send API call", False),
    ],
    "web.navigate_url": [
        (re.compile(r"webbrowser\.open\b|requests\.(get|post|put|delete)\b"),
         "Python URL-navigation API call", False),
    ],
}


class CapabilityEnforcer:
    """
    Capability enforcer for a pyautogui-based executor.

    The P-LLM reasons about abstract capabilities (e.g. 'os.fs.delete').
    The enforcer maps each capability to concrete signals — Python API patterns
    or text typed via pyautogui — and blocks actions that exercise an
    ungrantd capability.
    """

    def __init__(self, allowed_caps: frozenset[Capability]):
        self.allowed_caps = allowed_caps
        self._allowed_names: frozenset[str] = frozenset(c.name for c in allowed_caps)
        self.blocked_actions: list[str] = []
        self.destructive_actions_taken: list[str] = []

    def _enforce(self, action_code: str, cap_name: str, signal_desc: str) -> tuple[bool, str] | None:
        """
        Check one capability signal.
        Returns (False, reason) if the capability is not granted.
        Returns None if granted (keep checking remaining signals).
        Logs a warning for DESTRUCTIVE capabilities that are granted.
        """
        if cap_name not in self._allowed_names:
            reason = (
                f"Capability not granted: `{cap_name}` ({signal_desc}). "
                f"Granted: {sorted(self._allowed_names) or ['(none)']}"
            )
            self.blocked_actions.append(action_code)
            return False, reason
        cap = CAPABILITY_CATALOG.get(cap_name)
        if cap and cap.tier == Tier.DESTRUCTIVE:
            msg = (
                f"\n[SA WARNING] DESTRUCTIVE action executing "
                f"(capability: {cap_name} — {signal_desc}):\n"
                f"  {action_code.strip()}\n"
                "Automated eval: proceeding without confirmation.\n"
            )
            print(msg, flush=True)
            self.destructive_actions_taken.append(action_code)
        return None

    def check(
        self,
        action_code: str,
    ) -> tuple[bool, str]:
        """
        Returns (allowed, reason).  False → action blocked, no env.step().

        Layers:
          1. Python API backstop (subprocess, os.remove, etc.)
          2. Typed-text inspection (pyautogui.typewrite content)
        """
        typed_texts = _TYPEWRITE_RE.findall(action_code)

        for cap_name, signals in _CAPABILITY_SIGNALS.items():
            for pattern, desc, is_text in signals:
                if is_text:
                    matched = any(pattern.search(t) for t in typed_texts)
                else:
                    matched = bool(pattern.search(action_code))
                if matched:
                    result = self._enforce(action_code, cap_name, desc)
                    if result is not None:
                        return result

        return True, "OK"


from mm_agents.sa_agent.procedures import (
    UNKNOWN_CAPABILITY,
    Operand,
    Procedure,
    decompose,
    has_glob,
)


@dataclass(frozen=True)
class ProcedureEffects:
    """What a parsed procedure does, node by node."""
    capabilities: frozenset
    unknown_programs: Tuple[str, ...]
    operands: Tuple[Operand, ...]
    recursive: bool
    glob: bool
    privileged: bool

    @property
    def effect_capabilities(self) -> frozenset:
        """The non-READ subset -- the only part containment compares.

        READ-tier capabilities are implicitly authorized for every episode, and that is a
        deliberate scoping decision rather than an oversight. Containment is an *integrity*
        mechanism: it asks what a procedure changes. A read changes nothing, so requiring the
        task to have authorized `os.fs.read` before `apt-get` may consult its package lists would
        refuse every real task while protecting nothing. What a read can do is leak, and that is
        the confidentiality axis' problem -- the reader-set half of the product label, which this
        function has no business second-guessing.
        """
        return frozenset(
            c for c in self.capabilities
            if c == UNKNOWN_CAPABILITY
            or c not in CAPABILITY_CATALOG
            or CAPABILITY_CATALOG[c].tier is not Tier.READ
        )

    @property
    def max_tier(self) -> Tier:
        tiers = [CAPABILITY_CATALOG[c].tier for c in self.capabilities if c in CAPABILITY_CATALOG]
        if any(c not in CAPABILITY_CATALOG for c in self.capabilities):
            return Tier.DESTRUCTIVE
        if not tiers:
            return Tier.READ
        for t in (Tier.DESTRUCTIVE, Tier.REVERSIBLE_WRITE, Tier.READ):
            if t in tiers:
                return t
        return Tier.READ


def classify_procedure(proc: Procedure) -> ProcedureEffects:
    """Walk the AST and collect the capability each node needs."""
    caps: set = set()
    unknown: list = []
    operands: list = []
    recursive = False
    glob = False
    privileged = False

    for cmd in proc.commands:
        parts = decompose(cmd)
        operands.extend(parts.operands)
        recursive = recursive or parts.recursive
        glob = glob or has_glob(parts)
        if cmd.privileged:
            privileged = True
            caps.add("os.terminal.run")
        node_caps = parts.spec.caps_for(parts.subcommand) if parts.spec else None
        if node_caps is None:
            unknown.append(f"{cmd.program} {parts.subcommand}".strip())
            caps.add(UNKNOWN_CAPABILITY)
        else:
            caps |= set(node_caps)
        if parts.writes_output:
            if cmd.program in ("curl", "wget"):
                caps |= {"web.download", "os.fs.write"}
        for r in cmd.redirects:
            if r.op in (">", ">|", "&>"):
                caps.add("os.fs.overwrite")
            elif r.op == ">>":
                caps.add("os.fs.write")

    return ProcedureEffects(capabilities=frozenset(caps), unknown_programs=tuple(unknown),
                            operands=tuple(operands), recursive=recursive, glob=glob,
                            privileged=privileged)


def contain(effects: ProcedureEffects, authorized) -> Optional[str]:
    """Plan §5.3. ``None`` if the effect set fits inside *authorized*; else why it does not.

    *authorized* is derived once per episode from trusted inputs only (see
    ``episode_capabilities``), so nothing read mid-episode can widen it. That invariant is what
    makes this a containment check rather than a negotiation.
    """
    allowed = frozenset(authorized or ())
    needed = effects.effect_capabilities
    if UNKNOWN_CAPABILITY in needed or effects.unknown_programs:
        return (f"the procedure runs {', '.join(repr(u) for u in effects.unknown_programs) or 'a program'} "
                f"whose effects are not modelled, so there is no effect set to contain")
    missing = sorted(needed - allowed)
    if missing:
        return (f"it needs {', '.join(missing)}; authorized here: "
                f"{', '.join(sorted(allowed)) or 'nothing'}")
    return None
