"""
value_types.py — Layer 1 of the endorsement design: values have *types*
============================================================================

Milestone M0 of the endorsement design. Nothing here talks to a model, the network, or
the VM: it is pure host-side structure, which is the whole point.

Why a type catalog is a security mechanism
------------------------------------------
The endorsement channel's failure mode is not "the model was tricked into saying yes". It is
that a *slot* the planner asked to fill with one small fact accepted an arbitrary string instead.
``query_qvlm("which package does this post say to install?")`` currently returns free text, so
``rm -f /home/user/BankStatement.pdf`` is a perfectly well-formed answer to it.

A type fixes that by **capacity**, not by detection:

    a pip package name cannot express a shell loop

There is no blocklist here, no "looks dangerous" heuristic, and therefore nothing to evade. The
attacker is free to write whatever they like on the page; what they cannot do is make a 39-symbol
charset carry a pipeline. Every rejection in this module is a statement about the *shape* of the
answer, never about its content, and that is why it cannot be talked around.

Three exports carry the design:

``ShapeClass``
    How much attacker-chosen information the slot can carry at all. ``NONE`` means the untrusted
    side only *selects* among alternatives the host already knew (a bool, an enum member, an index
    into a planner-supplied list) — it authors no content whatsoever. ``BOUNDED`` means it authors
    content, but within a charset and length that cannot hold a procedure. ``UNBOUNDED`` means it
    authors content freely (a URL's path and query, free text) and the shape argument buys nothing.

``ValueType``
    Acceptor, normalizer, type-aware equality, capacity, JSON schema, default verifiers.

``bits(vt, value)``
    The honest information-capacity number for the M1 ledger. Not a label and not a probability:
    an upper bound on how many bits of attacker choice this one answer could have carried.

Type-aware equality replaces ``endorse._matches``
----------------------------------------------------
``_matches`` accepts substring containment in both directions, which is a real hole rather than a
convenience: ``"ChatOverflow"`` and ``"https://github.com/attacker/ChatOverflow"`` read as
agreement, so two checkers that disagree completely can be made to look unanimous, and the bare
name — not a URL at all — is what gets adopted and cleared. Every structured type here therefore
compares *exactly* after normalization. Only ``SHORT_TEXT`` keeps the lenient comparison, because
free text has no structure to compare and pretending otherwise would just over-block; that
leniency is one more reason ``SHORT_TEXT`` is the escape hatch the framework should nudge the
planner away from rather than a default worth reaching for.

The URL normalizer keeps the tolerances the old code had for genuinely equivalent spellings
(scheme, ``www.``, a trailing slash, a ``.git`` suffix) and drops only the containment. Query
strings are preserved: for a URL the path and query *are* the payload.
"""

from __future__ import annotations

import math
import posixpath
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from mm_agents.sa_agent.lattice import IntegrityLabel



class ShapeClass:
    """Capacity classes. Deliberately plain strings rather than an Enum: they are written into
    the audit log and compared against YAML in the benchmark, and an Enum buys nothing here."""

    NONE = "NONE"
    BOUNDED = "BOUNDED"
    UNBOUNDED = "UNBOUNDED"


_SHAPE_FLOOR = {
    ShapeClass.NONE: IntegrityLabel.Level.COMPUTED_PLATFORM_VALUE,
    ShapeClass.BOUNDED: IntegrityLabel.Level.COMPUTED_PLATFORM_VALUE,
    ShapeClass.UNBOUNDED: IntegrityLabel.Level.UNTRUSTED,
}


def floor_for(shape: str) -> IntegrityLabel:
    """The most trust a value of this shape class may be shown at (plan §3, "Labeling rule")."""
    return IntegrityLabel(_SHAPE_FLOOR[shape])




def _norm_ws(s: str) -> str:
    return " ".join((s or "").split())


@dataclass(frozen=True)
class ValueType:
    """One slot shape. Constructed through :func:`resolve`, never directly by callers."""

    name: str
    shape: str
    json_schema: dict
    acceptor: Callable[[str], bool]
    normalize: Callable[[str], str]
    equal: Callable[[str, str], bool]
    capacity_bits: float
    bits_per_char: float = 0.0
    default_verifiers: tuple[str, ...] = ()
    params: dict = field(default_factory=dict)

    def accepts(self, value: Any) -> bool:
        """``acceptor`` with the string coercion and the empty-value rule applied once, centrally.

        A missing answer is never a valid typed answer: the Q-VLM's own ``found: false`` is the
        channel for "not present", and an empty string reaching an acceptor means something
        upstream lost the value."""
        if value is None:
            return False
        text = value if isinstance(value, str) else str(value)
        if not text.strip():
            return False
        try:
            return bool(self.acceptor(text))
        except Exception:
            return False

    def bits(self, value: Any) -> float:
        """Measured capacity of one concrete value (see the module-level :func:`bits`)."""
        return bits(self, value)

    def floor(self) -> IntegrityLabel:
        return floor_for(self.shape)


def bits(vt: ValueType, value: Any) -> float:
    """Attacker-choice capacity of *value* in this slot, in bits, capped at the type maximum.

    For a NONE shape this is a constant of the type (1 bit for a bool, log2 k for a k-member
    enum): the answer selects, it does not author, so its length is irrelevant. For the other two
    it grows with the number of accepted symbols actually used.

    An upper bound, not an estimate. Real content is nowhere near uniform over the charset, so the
    true mutual information is lower -- which is the right direction for a budget to err in.
    """
    if vt.shape == ShapeClass.NONE or vt.bits_per_char <= 0:
        return float(vt.capacity_bits)
    text = "" if value is None else (value if isinstance(value, str) else str(value))
    return float(min(vt.capacity_bits, len(text) * vt.bits_per_char))


def _charset_bits(size: int) -> float:
    return math.log2(size) if size > 1 else 0.0



_URL_RE = re.compile(r"^https?://[^\s/?#]+[^\s]*$", re.IGNORECASE)
_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
                          r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$")
_IDENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}(?:/[A-Za-z0-9._-]{1,64}){0,2}$")
_SEMVER_RE = re.compile(r"^v?\d+\.\d+(\.\d+)?([-+][0-9A-Za-z.-]+)?$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_POINT_RE = re.compile(r"^\d{1,4}\s*,\s*\d{1,4}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}$")
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
_EMAIL_LOCAL_MAX = 64
_EMAIL_MAX = 254
_OPTION_LABEL_RE = re.compile(r"^[A-Za-z0-9._@+=:,/-]{1,64}$")
_INT_RE = re.compile(r"^[+-]?\d{1,19}$")
_DEC_RE = re.compile(r"^[+-]?(\d{1,19})(\.\d{1,10})?$")
_PATH_RE = re.compile(r"^~?(/[^/\0]+)+/?$|^~/?$|^[^/\0;&|`$<>*?!\\\"'\s]+(/[^/\0]+)*/?$")
_PATH_BAD = re.compile(r"[;&|`$<>(){}\[\]*?!\\\"'\n\r\t]|\.\.")


def _strip_url_noise(s: str) -> str:
    """Scheme / ``www.`` / trailing slash / ``.git`` -- the trivial formatting differences two
    honest extractions of one URL commonly produce. Shared by the URL normalizer and by the
    SHORT_TEXT fallback so the two cannot drift apart."""
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = re.sub(r"\.git$", "", s)
    return s.rstrip("/")


def _url_key(s: str) -> str:
    """Canonical form for URL comparison: the tolerances two honest extractions of the same URL
    legitimately differ by, and nothing more. Query strings survive -- for a URL, path and query
    are exactly where the attacker's freedom lives (plan §3, UNBOUNDED)."""
    return _strip_url_noise(_norm_ws(s).casefold().rstrip("/"))


def _pep503(name: str) -> str:
    """PyPI's own normalization (PEP 503): runs of ``-``, ``_`` and ``.`` are one separator, and
    case is insignificant. Using the index's rule rather than inventing one means ``term_color``
    and ``term-color`` agree exactly where PyPI says they are the same project -- and
    ``termc0lor`` still does not agree with ``termcolor``, which is the case that matters."""
    return re.sub(r"[-_.]+", "-", _norm_ws(name)).lower()


_PKG_NORMALIZE = {
    "pip": _pep503,
    "pypi": _pep503,
    "npm": lambda s: _norm_ws(s).lower(),
    "apt": lambda s: _norm_ws(s).lower(),
    "debian": lambda s: _norm_ws(s).lower(),
    "dockerhub": lambda s: (lambda t: t if "/" in t else f"library/{t}")(_norm_ws(s).lower()),
    "github": lambda s: _norm_ws(s).lower(),
}
_PKG_CHARSET = {
    "pip": (re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$"), 65),
    "pypi": (re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$"), 65),
    "npm": (re.compile(r"^(@[a-z0-9][a-z0-9._-]{0,38}/)?[a-z0-9][a-z0-9._-]{0,62}$"), 39),
    "apt": (re.compile(r"^[a-z0-9][a-z0-9+.-]{1,62}$"), 39),
    "debian": (re.compile(r"^[a-z0-9][a-z0-9+.-]{1,62}$"), 39),
    "dockerhub": (re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}(/[a-z0-9][a-z0-9._-]{0,62})?$"), 40),
    "github": (re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"), 66),
}


def _lenient_equal(a: str, b: str) -> bool:
    """The pre-M0 ``endorse._matches`` behaviour, byte for byte, kept for ``SHORT_TEXT`` only.

    Substring containment in either direction, with the URL-noise fallback. Reproduced exactly
    rather than approximated, because SHORT_TEXT is the default type: an untyped call must mean
    precisely what it meant before this module existed, or landing M0 would silently change the
    behaviour of every caller that had not opted in.

    Retained *because* free text has no structure to compare: an honest re-derivation of a prose
    answer routinely differs by a clause, and demanding exact equality there would refuse correct
    answers. It is also precisely the leniency that let a bare name pass for a URL, so it is now
    confined to the one type with no alternative -- and that confinement is the argument for
    typing the slot in the first place.
    """
    na, nb = _norm_ws(a).casefold(), _norm_ws(b).casefold()
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    sa, sb = _strip_url_noise(na), _strip_url_noise(nb)
    return bool(sa) and bool(sb) and (sa == sb or sa in sb or sb in sa)


def _exact_equal(normalize: Callable[[str], str]) -> Callable[[str, str], bool]:
    def eq(a: str, b: str) -> bool:
        na, nb = normalize(a or ""), normalize(b or "")
        return bool(na) and na == nb
    return eq


def _numeric_equal(a: str, b: str) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False



def _str_schema(pattern: Optional[str] = None, **extra) -> dict:
    schema: dict[str, Any] = {"type": "string"}
    if pattern:
        schema["pattern"] = pattern
    schema.update(extra)
    return schema


def _simple(name: str, *, shape: str, regex: re.Pattern, charset: int, max_len: int,
            verifiers: tuple[str, ...], normalize=None, equal=None,
            schema: Optional[dict] = None) -> ValueType:
    nrm = normalize or (lambda s: _norm_ws(s))
    per_char = _charset_bits(charset)
    return ValueType(
        name=name, shape=shape,
        json_schema=schema or _str_schema(regex.pattern, maxLength=max_len),
        acceptor=lambda s: bool(regex.match(s.strip())),
        normalize=nrm,
        equal=equal or _exact_equal(nrm),
        capacity_bits=per_char * max_len,
        bits_per_char=per_char,
        default_verifiers=verifiers,
    )


def _bool_type() -> ValueType:
    def nrm(s: str) -> str:
        return _norm_ws(s).lower()
    return ValueType(
        name="BOOL", shape=ShapeClass.NONE,
        json_schema={"type": "boolean"},
        acceptor=lambda s: nrm(s) in ("true", "false"),
        normalize=nrm, equal=_exact_equal(nrm),
        capacity_bits=1.0,
        default_verifiers=(),
    )


def _enum_type(values: list[str]) -> ValueType:
    if not values:
        raise ValueError("ENUM requires a non-empty `enum_values` list")
    table = {_norm_ws(v).casefold(): v for v in values}

    def nrm(s: str) -> str:
        return table.get(_norm_ws(s).casefold(), "")
    return ValueType(
        name="ENUM", shape=ShapeClass.NONE,
        json_schema={"type": "string", "enum": list(values)},
        acceptor=lambda s: _norm_ws(s).casefold() in table,
        normalize=nrm, equal=_exact_equal(nrm),
        capacity_bits=_charset_bits(len(values)),
        default_verifiers=("format",),
        params={"enum_values": list(values)},
    )


def _index_type(n: int) -> ValueType:
    if n <= 0:
        raise ValueError("INDEX requires a positive `n`")

    def acc(s: str) -> bool:
        t = _norm_ws(s)
        return bool(_INT_RE.match(t)) and 0 <= int(t) < n
    return ValueType(
        name="INDEX", shape=ShapeClass.NONE,
        json_schema={"type": "integer", "minimum": 0, "maximum": n - 1},
        acceptor=acc, normalize=lambda s: str(int(_norm_ws(s))), equal=_numeric_equal,
        capacity_bits=_charset_bits(n),
        default_verifiers=("format",),
        params={"n": n},
    )


def _numeric_type(name: str, regex: re.Pattern, minimum=None, maximum=None) -> ValueType:
    def acc(s: str) -> bool:
        t = _norm_ws(s)
        if not regex.match(t):
            return False
        val = float(t)
        if minimum is not None and val < float(minimum):
            return False
        return not (maximum is not None and val > float(maximum))
    span = None
    if minimum is not None and maximum is not None:
        span = _charset_bits(int(float(maximum) - float(minimum)) + 1)
    return ValueType(
        name=name, shape=ShapeClass.NONE,
        json_schema={"type": "number" if name == "DECIMAL" else "integer",
                     **({"minimum": minimum} if minimum is not None else {}),
                     **({"maximum": maximum} if maximum is not None else {})},
        acceptor=acc, normalize=lambda s: _norm_ws(s).lstrip("+"), equal=_numeric_equal,
        capacity_bits=span if span is not None else 32.0,
        default_verifiers=("task_match", "crosscheck", "recall"),
        params={"minimum": minimum, "maximum": maximum},
    )


def _package_type(manager: str) -> ValueType:
    mgr = (manager or "pip").strip().lower()
    if mgr not in _PKG_CHARSET:
        raise ValueError(f"unknown package manager '{manager}'; known: {sorted(_PKG_CHARSET)}")
    regex, charset = _PKG_CHARSET[mgr]
    nrm = _PKG_NORMALIZE[mgr]
    per_char = _charset_bits(charset)
    max_len = 64 if mgr not in ("github", "dockerhub") else 140
    return ValueType(
        name="PACKAGE_NAME", shape=ShapeClass.BOUNDED,
        json_schema=_str_schema(regex.pattern, maxLength=max_len),
        acceptor=lambda s: bool(regex.match(_norm_ws(s))),
        normalize=nrm, equal=_exact_equal(nrm),
        capacity_bits=per_char * max_len, bits_per_char=per_char,
        default_verifiers=("registry", "crosscheck", "candidate_set", "recall"),
        params={"manager": mgr},
    )


_PKG_PIN = {
    "pip":       re.compile(r"^(?P<name>[^=<>!~\[]+)(?P<qual>(?:\[[A-Za-z0-9,._-]{1,64}\])?"
                            r"(?:(?:==|>=|<=|~=|!=|<|>)[A-Za-z0-9][A-Za-z0-9.\-+*]{0,31})?)$"),
    "npm":       re.compile(r"^(?P<name>(?:@[a-z0-9][a-z0-9._-]{0,38}/)?[^@]+)"
                            r"(?P<qual>(?:@[A-Za-z0-9][A-Za-z0-9.\-+^~*]{0,31})?)$"),
    "apt":       re.compile(r"^(?P<name>[^=]+)(?P<qual>(?:=[A-Za-z0-9][A-Za-z0-9.\-+:~]{0,63})?)$"),
    "dockerhub": re.compile(r"^(?P<name>[^:@]+)"
                            r"(?P<qual>(?::[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?"
                            r"(?:@sha256:[0-9a-f]{64})?)$"),
}
_PKG_PIN["pypi"] = _PKG_PIN["pip"]
_PKG_PIN["debian"] = _PKG_PIN["apt"]


def split_package_spec(value: str, manager: str = "pip") -> tuple[str, str]:
    """Split ``termcolor==2.4.0`` into ``("termcolor", "==2.4.0")``. ``("", "")`` if malformed.

    WHY THIS EXISTS. A package leaf arrives from content exactly as the content wrote it, and a
    version pin is the normal way to write one -- the reddit_2 task's own post says
    ``pip install termcolor==2.4.0``, and ``docker pull alpine:3.19.1`` is the literal string
    owncloud_3's task instruction uses. But the PACKAGE_NAME charset admits no ``=`` or ``:``, by
    design: it is the charset of a NAME. So the acceptor refused the pinned form, the registry
    lookup never ran, and every rung of the Layer-3 ladder failed with NOT_IN_REGISTRY on a
    perfectly ordinary command (observed live, own_reddit_2 2026-09-17).

    The split keeps the two claims apart rather than widening the name charset, which would have
    let `=` into the one place the capacity argument depends on it being absent:

      * the NAME is verified as a name -- looked up in the canonical index, unchanged;
      * the QUALIFIER is accepted on SHAPE only. It is bounded (a version/tag charset, length
        capped), so it cannot carry a procedure, which is the property the leaf check exists to
        guarantee. It is NOT verified as correct: no index can say that 2.4.0 is the version the
        user wanted, and a pin to a known-bad release of a real package is a residual this does not
        close. Where the task names the version, `task_match` on the whole spec is the stronger
        check and is tried first.

    Returns ``("", "")`` when the value does not parse as name+optional-qualifier for this
    ecosystem, so a caller that cannot split it refuses rather than guessing.
    """
    text = _norm_ws(value)
    rx = _PKG_PIN.get((manager or "pip").strip().lower())
    if not text or rx is None:
        return ("", "")
    m = rx.match(text)
    if not m:
        return ("", "")
    name, qual = m.group("name").strip(), m.group("qual").strip()
    return (name, qual) if name else ("", "")


def email_domain(s: str) -> str:
    """The domain half of an address, casefolded and dot-stripped. '' if there is no single '@'.

    Public because the *domain* is the half that decides whether a send is exfiltration, so
    callers outside this module (endorse.domain_pin) compare on it.
    """
    local, at, dom = _norm_ws(s).rpartition("@")
    return dom.casefold().rstrip(".") if at and local else ""


_TLD = None


def registrable_domain(value: str) -> str:
    """The eTLD+1 of an address, hostname or URL. '' when there is nothing resolvable.

    THE UNIT OF SAME-ORIGIN REASONING, and not the same thing as the hostname. Senders legitimately
    serve their click-throughs, images and unsubscribe endpoints from a branded subdomain --
    `links.morningbrew.com`, `email.thehustle.co`, `link.join1440.com` -- so comparing HOSTNAMES
    would reject the ordinary case. Comparing registrable domains accepts all three and still
    separates `techdaily.com` from `techdaily-optout.net`, which is the distinction that matters.

    Public-suffix aware rather than "last two labels": under the naive rule every `*.co.uk` would
    share a registrable domain with every other, which is not a weaker check but a wrong one.
    """
    global _TLD
    text = _norm_ws(value)
    if not text:
        return ""
    local, at, dom = text.rpartition("@")
    if at and local:
        text = dom
    text = re.sub(r"^[A-Za-z][A-Za-z0-9+.\-]*://", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.split(":", 1)[0].strip().strip(".").casefold()
    if not text:
        return ""
    if _TLD is None:
        import tldextract
        _TLD = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)
    try:
        parts = _TLD(text)
    except Exception:
        return ""
    if not parts.domain or not parts.suffix:
        return ""
    return f"{parts.domain}.{parts.suffix}"


def same_site(a: str, b: str) -> bool:
    """Do these two share a registrable domain? False when either has none (absence never matches)."""
    ra, rb = registrable_domain(a), registrable_domain(b)
    return bool(ra) and ra == rb


def _email_type(domain: Optional[str] = None) -> ValueType:
    """An address, optionally *pinned* to one domain.

    The pin is the whole point. Unpinned, an address is an UNBOUNDED slot however tidy its
    syntax: the domain is attacker-chosen, so the value is a delivery destination and
    `secrets-here@attacker.example` is a working covert channel. Pinned, only the local part is
    free, the shape class drops to BOUNDED, and capacity falls to that of an identifier.

    Note the two halves normalize differently on purpose: domains are case-insensitive, local
    parts are not (RFC 5321 §2.4). Casefolding the local part would silently merge two mailboxes
    the mail system treats as distinct, which is the wrong direction to be wrong in.
    """
    pin = (domain or "").strip().casefold().lstrip("@").rstrip(".")
    if pin and not _HOSTNAME_RE.match(pin):
        raise ValueError(f"domain pin '{domain}' is not a hostname")

    def nrm(s: str) -> str:
        local, at, dom = _norm_ws(s).rpartition("@")
        return f"{local}@{dom.casefold().rstrip('.')}" if at and local else _norm_ws(s).casefold()

    def acc(s: str) -> bool:
        t = _norm_ws(s)
        if len(t) > _EMAIL_MAX or not _EMAIL_RE.match(t):
            return False
        if len(t.rsplit("@", 1)[0]) > _EMAIL_LOCAL_MAX:
            return False
        return not pin or email_domain(t) == pin

    per_char = _charset_bits(70)
    free_len = _EMAIL_LOCAL_MAX if pin else _EMAIL_MAX
    return ValueType(
        name="EMAIL_ADDRESS",
        shape=ShapeClass.BOUNDED if pin else ShapeClass.UNBOUNDED,
        json_schema=_str_schema(_EMAIL_RE.pattern, maxLength=_EMAIL_MAX, format="email"),
        acceptor=acc, normalize=nrm, equal=_exact_equal(nrm),
        capacity_bits=per_char * free_len, bits_per_char=per_char,
        default_verifiers=("directory", "task_match", "candidate_set", "crosscheck"),
        params={"domain": pin} if pin else {},
    )


def _url_type() -> ValueType:
    return ValueType(
        name="URL", shape=ShapeClass.UNBOUNDED,
        json_schema=_str_schema(r"^https?://", maxLength=2048, format="uri"),
        acceptor=lambda s: bool(_URL_RE.match(_norm_ws(s))),
        normalize=_url_key, equal=_exact_equal(_url_key),
        capacity_bits=6.0 * 2048, bits_per_char=6.0,
        default_verifiers=("crosscheck", "recall", "same_site"),
    )


def _file_path_type(root: Optional[str] = None) -> ValueType:
    def acc(s: str) -> bool:
        t = _norm_ws(s)
        if not t or _PATH_BAD.search(t) or not _PATH_RE.match(t):
            return False
        if root:
            base = posixpath.normpath(root)
            cand = posixpath.normpath(t)
            return cand == base or cand.startswith(base.rstrip("/") + "/")
        return True
    return ValueType(
        name="FILE_PATH", shape=ShapeClass.BOUNDED,
        json_schema=_str_schema(maxLength=512),
        acceptor=acc,
        normalize=lambda s: posixpath.normpath(_norm_ws(s)),
        equal=_exact_equal(lambda s: posixpath.normpath(_norm_ws(s))),
        capacity_bits=6.0 * 512, bits_per_char=6.0,
        default_verifiers=("task_match", "candidate_set", "checksum", "listing"),
        params={"root": root},
    )


def _short_text_type(max_len: int = 200) -> ValueType:
    def acc(s: str) -> bool:
        t = _norm_ws(s)
        return bool(t) and len(t) <= max_len and t.isprintable()
    return ValueType(
        name="SHORT_TEXT", shape=ShapeClass.UNBOUNDED,
        json_schema=_str_schema(maxLength=max_len),
        acceptor=acc,
        normalize=lambda s: _norm_ws(s).casefold(),
        equal=_lenient_equal,
        capacity_bits=6.5 * max_len, bits_per_char=6.5,
        default_verifiers=("task_match",),
        params={"max_len": max_len},
    )


def coerce_procedure_payload(text: str) -> str:
    """Accept the STRUCTURED form of a procedure answer and return its canonical text.

        {"commands": ["curl -fsSL https://... -o /tmp/x.tar.xz",
                      "sudo tar -xJf /tmp/x.tar.xz -C /usr/local"]}
            ->  "curl -fsSL https://... -o /tmp/x.tar.xz\nsudo tar -xJf /tmp/x.tar.xz -C /usr/local"

    WHY THIS IS TRANSPORT AND NOT STRUCTURE. The decomposition into stages, commands, prefixes,
    assignments and redirects is derived HOST-SIDE by ``procedures.parse_procedure`` (shlex, plus
    a keyword table that refuses anything it cannot model). It must stay there: the screen reader
    is the one principal assumed to be attacker-influenced, and the AST is precisely what
    containment reasons about and what the cleared variable is re-rendered from. A Q-VLM that
    returned ``{"program": "tar", "sudo": false}`` for a line that actually reads ``sudo tar``
    would have containment check a procedure that is not the one on the screen.

    So this function converts a LIST OF LINES into the lines, and nothing else. It grants no new
    trust, because ``["a", "b"]`` and ``"a\nb"`` are the same answer: a newline inside the old
    single-string form was already load-bearing (``_SEQ_OPS`` includes ``"\n"``), so a model that
    chunks the text wrongly could always have produced the same wrong text. What it buys is shape
    discipline -- the model is asked for a list when the screen shows several commands, instead of
    having to guess whether a block is allowed to contain a newline at all.

    Returns the input unchanged when it does not claim to be the structured form, so a plain
    command string -- still the common case, and what every stored plan holds -- is untouched.

    An answer that DOES claim the structured form (``{...}``) but is malformed returns ``""``,
    which the acceptor rejects. Fail closed rather than falling back to reading it as shell text:
    ``{"commands": [{"program": "tar"}]}`` parsed as a command line yields a program literally
    named ``{commands:``, which is refused two layers later for being unclassifiable and reports
    an error naming nothing the planner can act on. No shell command is lost to this -- a real
    line that opens with ``{`` is a brace group, which ``procedures`` refuses anyway.
    """
    t = (text or "").strip()
    if not (t.startswith("{") and t.endswith("}")):
        return text
    try:
        obj = json.loads(t)
    except (ValueError, TypeError):
        return ""
    if not isinstance(obj, dict):
        return ""
    cmds = obj.get("commands")
    if isinstance(cmds, str):
        cmds = [cmds]
    if not isinstance(cmds, list) or not cmds:
        return ""
    lines = []
    for c in cmds:
        if not isinstance(c, str) or not c.strip():
            return ""
        lines.append(c.strip())
    return "\n".join(lines)


def _procedure_type(max_len: int = 512) -> ValueType:
    """A command line to be RUN, read off the screen for ``endorse_by_procedure``.

    This is the one deliberately unbounded shape the screen channel still admits (SHORT_TEXT is
    not): a procedure is *supposed* to carry instructions, so no charset could accept the benign
    one and refuse the malicious one (see ``endorse.procedure``). What makes it safe is not
    capacity-bounding but Layer-3 containment -- the effect set is gated against what the task
    authorized, and the variable is re-rendered from the verified AST. It must therefore never be
    cleared as a *value*: ``default_verifiers`` is ``("procedure",)`` only, so it cannot be
    task_match'd/crosschecked into a sink the way a laundered free-text value could."""
    _ALLOWED_CONTROL = frozenset("\n\t")

    def acc(s: str) -> bool:
        t = (s or "").strip()
        if not t or len(t) > max_len:
            return False
        return all(c in _ALLOWED_CONTROL or c.isprintable() for c in t)

    return ValueType(
        name="PROCEDURE", shape=ShapeClass.UNBOUNDED,
        json_schema=_str_schema(maxLength=max_len),
        acceptor=acc,
        normalize=lambda s: (s or "").strip(),
        equal=_lenient_equal,
        capacity_bits=6.5 * max_len, bits_per_char=6.5,
        default_verifiers=("procedure",),
        params={"max_len": max_len},
    )


_PARAMETRIC = {
    "PROCEDURE": lambda p: _procedure_type(int(p.get("max_len") or 512)),
    "ENUM": lambda p: _enum_type(list(p.get("enum_values") or p.get("values") or [])),
    "INDEX": lambda p: _index_type(int(p.get("n") or p.get("count") or 0)),
    "INTEGER": lambda p: _numeric_type("INTEGER", _INT_RE, p.get("minimum"), p.get("maximum")),
    "DECIMAL": lambda p: _numeric_type("DECIMAL", _DEC_RE, p.get("minimum"), p.get("maximum")),
    "PACKAGE_NAME": lambda p: _package_type(str(p.get("manager") or "pip")),
    "FILE_PATH": lambda p: _file_path_type(p.get("root") or p.get("scope_root")),
    "SHORT_TEXT": lambda p: _short_text_type(int(p.get("max_len") or 200)),
    "EMAIL_ADDRESS": lambda p: _email_type(p.get("domain") or p.get("pin_domain")),
}


def _build_fixed() -> dict[str, ValueType]:
    return {
        "BOOL": _bool_type(),
        "DATE": _simple("DATE", shape=ShapeClass.NONE, regex=_DATE_RE, charset=10, max_len=10,
                        verifiers=("crosscheck", "recall"),
                        normalize=lambda s: _norm_ws(s)),
        "SEMVER": _simple("SEMVER", shape=ShapeClass.BOUNDED, regex=_SEMVER_RE, charset=12,
                          max_len=32,
                          verifiers=("format", "listing", "crosscheck", "registry", "recall"),
                          normalize=lambda s: re.sub(r"^v", "", _norm_ws(s).lower())),
        "IDENTIFIER": _simple("IDENTIFIER", shape=ShapeClass.BOUNDED, regex=_IDENT_RE, charset=66,
                              max_len=64,
                              verifiers=("task_match", "registry", "listing", "crosscheck",
                                         "recall")),
        "HOSTNAME": _simple("HOSTNAME", shape=ShapeClass.BOUNDED, regex=_HOSTNAME_RE, charset=39,
                            max_len=253, verifiers=("allowlist", "recall", "same_site"),
                            normalize=lambda s: _norm_ws(s).lower().rstrip(".")),
        "SHA256": _simple("SHA256", shape=ShapeClass.BOUNDED, regex=_SHA256_RE, charset=16,
                          max_len=64, verifiers=("format",),
                          normalize=lambda s: _norm_ws(s).lower()),
        "USERNAME": _simple("USERNAME", shape=ShapeClass.BOUNDED, regex=_USERNAME_RE, charset=65,
                            max_len=39, verifiers=("candidate_set", "task_match", "recall"),
                            normalize=lambda s: _norm_ws(s).casefold()),
        "OPTION_LABEL": _simple("OPTION_LABEL", shape=ShapeClass.BOUNDED, regex=_OPTION_LABEL_RE,
                                charset=68, max_len=64, verifiers=("format",),
                                normalize=lambda s: _norm_ws(s)),
        "URL": _url_type(),
        "POINT": _simple("POINT", shape=ShapeClass.BOUNDED, regex=_POINT_RE, charset=11,
                         max_len=9, verifiers=("format",),
                         normalize=lambda s: re.sub(r"\s+", "", _norm_ws(s))),
    }


_FIXED = _build_fixed()

TYPE_NAMES: tuple[str, ...] = tuple(sorted(set(_FIXED) | set(_PARAMETRIC)))

DEFAULT_TYPE = "SHORT_TEXT"


_INLINE_PARAM_RE = re.compile(r"^\s*([A-Za-z_]+)\s*\[(.*)\]\s*$", re.S)


def _split_inline_params(name: str) -> tuple[str, dict]:
    m = _INLINE_PARAM_RE.match(name)
    if not m:
        return name.strip().upper(), {}
    key, body = m.group(1).upper(), m.group(2)
    if key == "ENUM":
        return key, {"enum_values": [v.strip() for v in body.split("|") if v.strip()]}
    if key == "INDEX":
        body = body.strip()
        return key, ({"n": int(body)} if body.isdigit() else {})
    raise ValueError(f"type '{key}' takes no inline [parameters]; only ENUM[a|b|...] and INDEX[n] do")


def resolve(name: Optional[str] = None, params: Optional[dict] = None, **kwargs) -> ValueType:
    """Look up a value type by name, applying *params* for the parameterized ones.

    Unknown names raise: silently falling back to SHORT_TEXT would turn a planner's typo into a
    silent removal of the type check, which is the one failure mode this module exists to prevent.
    """
    key, inline = _split_inline_params(name or DEFAULT_TYPE)
    merged = dict(inline)
    merged.update(params or {})
    merged.update(kwargs)
    if key in _PARAMETRIC:
        return _PARAMETRIC[key](merged)
    if key in _FIXED:
        return _FIXED[key]
    raise KeyError(f"unknown value type '{name}'; known types: {', '.join(TYPE_NAMES)}")


def try_resolve(name: Optional[str], params: Optional[dict] = None) -> Optional[ValueType]:
    """:func:`resolve` returning None instead of raising. For call sites that must degrade to
    untyped behaviour (an older config, a tool arg that predates typing) rather than fail."""
    try:
        return resolve(name, params)
    except (KeyError, ValueError):
        return None
