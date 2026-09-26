"""
screen_labels.py — taint source: labeled screen views on the fides lattice
===========================================================================

This module is the bridge between the provenance-annotated action list produced
by ``mm_agents.accessibility_tree_wrap.coord_to_semantic`` and the fides IFC
lattice used throughout sa_agent. It manufactures the "labeled tool outputs"
FIDES assumes: every screen affordance and content region becomes a value with
an integrity + confidentiality label. (Design ported from
``mm_agents/ifc/screen_variables.py`` onto ``lattice.FidesLattice``.)

Taint model
-----------
Label type: ``FidesLattice = ProductLabel[IntegrityLabel,
InverseLattice[PowersetLattice[str]]]``.

* integrity (``label.left``): TRUSTED joins UNTRUSTED to UNTRUSTED — taint is
  sticky under join.
* confidentiality (``label.right``): an inverse-ordered reader set over the
  ``PRINCIPALS`` universe. Join intersects reader sets, so combining data can
  only narrow who may see the result. ``public_conf()`` (everyone) is the
  lattice bottom; ``user_conf()`` ({"user"}) is more confidential.

Source rules (v1, a11y-tree only):

* integrity   — ``Zone.CHROME`` / ``Zone.OS`` (browser chrome, a native app's own
  menus and toolbars) are first-party UI → TRUSTED. ``Zone.DOCUMENT`` — content an
  application is *displaying* rather than authoring — is attacker-influenceable →
  UNTRUSTED. The boundary is the AT-SPI document role, not the application: a
  browser page, a mail message pane and an opened spreadsheet all cross it.
  Reading ``Zone.OS`` as "first-party" outright is what used to make every native
  app unconditionally trusted (see ``app_document`` in
  ``_integrity_of_with_provenance``, and the endorsement design).
* confidentiality — supplied by a per-task ``PrincipalMap``; default PUBLIC.
  ``url_principal_map`` builds one from (url-substring, readers) rules, e.g. to
  mark the user's own account pages confidential to {"user"}.

Propagation rules (enforced in loop_and_planner_no_exposure.py / tools.py):

* A region's label is the join over the affordances it contains.
* A Q-LLM extraction over a screen is labeled ``extraction_label()`` — the join
  of all DOCUMENT-zone labels on that screen (a page-text read may have pulled
  from any of them), over inert *text* as well as controls: a read returns text,
  so joining only over affordances made "untrusted content is on this screen" a
  proxy for "untrusted controls are on this screen", which fails for prose that
  carries no controls. Screens with no DOCUMENT-zone content at all still yield
  TRUSTED.
* Coordinate-returning tools (``find``, ``find_element_by_text``) refine to the
  label of the affordance actually located at (x, y): DOCUMENT-element
  coordinates are themselves attacker-influenceable data.
* A tool result's label = source label ⊔ join of the labels of every variable
  passed as an argument.

Blocking vs. endorsement (see policies.py / endorse.py): consequential
(write/destructive-tier) tool calls whose effective label is UNTRUSTED are
blocked by ``SAPolicy``; the only way to clear integrity taint is an explicit
verified endorsement (web cross-check against an allowlisted official
source, or checksum verification) — never trust-on-assertion, never a human
prompt. Confidentiality is never cleared.

Assumptions about the accessibility_tree_wrap schema:

* ``obs["accessibility_tree"]`` is the raw AT-SPI XML string from the VM server.
* ``InteractableAction.zone ∈ {chrome, document, os}``; DOCUMENT means "rendered
  inside a browser document" and is the only attacker-influenceable zone.
* ``InteractableAction.capability`` is conservative: opaque controls come back
  UNKNOWN (treated as the strongest sink tier by policies.py).
* ``region_id`` is optional; DOCUMENT affordances without a region still taint
  ``extraction_label()``.
* That layer provides no confidentiality information — reader sets come solely
  from the ``PrincipalMap`` here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, FrozenSet, Optional
from urllib.parse import unquote

from mm_agents.accessibility_tree_wrap.coord_to_semantic import (
    ActionType,
    GlobalAction,
    InteractableAction,
    Zone,
    CHROME_LIKE_ROLES,
    extract_all_atspi_with_content,
    get_active_tab_hostname,
)
from mm_agents.accessibility_tree_wrap import native_regions as _nr
from mm_agents.sa_agent.lattice import (
    FidesLattice,
    IntegrityLabel,
    InverseLattice,
    PowersetLattice,
    ProductLabel,
)

PRINCIPALS: FrozenSet[str] = frozenset({"user", "public"})

Confidentiality = InverseLattice


def public_conf() -> Confidentiality:
    """Everyone may read — bottom of the (inverse) confidentiality lattice."""
    return InverseLattice(PowersetLattice(subset=PRINCIPALS, universe=PRINCIPALS))


def user_conf() -> Confidentiality:
    """Only the user may read."""
    return InverseLattice(PowersetLattice(subset=frozenset({"user"}), universe=PRINCIPALS))


def trusted_public() -> FidesLattice:
    return ProductLabel(IntegrityLabel.trusted(), public_conf())


def endorsed_public() -> FidesLattice:
    return ProductLabel(IntegrityLabel.endorsed(), public_conf())


def untrusted_public() -> FidesLattice:
    return ProductLabel(IntegrityLabel.untrusted(), public_conf())


def make_metadata(label: FidesLattice) -> dict[str, Any]:
    """Shape expected by ``labels.metadata_to_label`` (lattice objects, not strings)."""
    return {"integrity": label.left, "confidentiality": label.right}


_MIN_TRUSTED_LEVEL: "IntegrityLabel.Level" = IntegrityLabel.Level.ENDORSED


def configure_min_trusted_level(level: "IntegrityLabel.Level") -> None:
    """Set once at startup from CLI config (mirrors how trusted_domains etc. are threaded through
    once from loop.py). The default (ENDORSED) is the ladder's intended everyday cutoff:
    redact/block unverified computed-platform-value content (see the module-level comment above
    for why COMPUTED_PLATFORM_VALUE is forgeable from attacker-controlled UGC and must not pass by
    default), free-text third-party content, and fully untrusted content (ads, cross-origin
    embeds, unclassifiable DOCUMENT content). Set to TRUSTED or SELF_AUTHORED for stricter
    behavior, or looser (COMPUTED_PLATFORM_VALUE/FREE_TEXT_THIRD_PARTY) to accept weaker evidence
    -- pair with SAPolicy.tier_min_level so the two never drift into an inconsistent
    combination."""
    global _MIN_TRUSTED_LEVEL
    _MIN_TRUSTED_LEVEL = level


_EPISODE_TRUSTED_DOMAINS: FrozenSet[str] = frozenset()


def configure_prompt_trusted_domains(domains) -> None:
    """Set once per episode (SAAgentLoop.loop(), after trust_detection.py resolves and -- in
    'ask' mode -- the operator confirms the candidate set). Empty/default means no dynamic
    elevation: every DOCUMENT-zone affordance keeps today's unconditional UNTRUSTED label."""
    global _EPISODE_TRUSTED_DOMAINS
    _EPISODE_TRUSTED_DOMAINS = frozenset(d.strip().lower().lstrip(".") for d in domains if d and d.strip())
    _label_screen_cached.cache_clear()


def _host_is_episode_trusted(hostname: Optional[str]) -> bool:
    if not hostname or not _EPISODE_TRUSTED_DOMAINS:
        return False
    hostname = hostname.lower()
    return any(hostname == d or hostname.endswith("." + d) for d in _EPISODE_TRUSTED_DOMAINS)


_EPISODE_VISIT_DOMAINS: FrozenSet[str] = frozenset()


def configure_visit_domains(domains) -> None:
    """Set once per episode from the effective operator allowlists (official + corroborating).
    Path-pinned entries (e.g. `anonymous.4open.science/api/repo`) are reduced to their host, since
    navigation is decided per host, not per path. Empty means no navigation elevation."""
    global _EPISODE_VISIT_DOMAINS
    hosts = set()
    for d in domains:
        s = str(d).strip().lower().lstrip(".") if d is not None else ""
        if not s:
            continue
        host = s.split("/", 1)[0]
        if host:
            hosts.add(host)
    _EPISODE_VISIT_DOMAINS = frozenset(hosts)
    _label_screen_cached.cache_clear()


def _host_is_visitable(hostname: Optional[str]) -> bool:
    if not hostname or not _EPISODE_VISIT_DOMAINS:
        return False
    hostname = hostname.lower()
    return any(hostname == d or hostname.endswith("." + d) for d in _EPISODE_VISIT_DOMAINS)


_EPISODE_SELF_IDENTITY: Optional[str] = None


def configure_self_identity(identity: Optional[str]) -> None:
    """Set once per episode (SAAgentLoop.loop(), after trust_detection.py resolves and -- in
    'ask' mode -- the operator confirms). None/empty means no elevation: every detected region
    author is compared to nothing and never matches."""
    global _EPISODE_SELF_IDENTITY
    _EPISODE_SELF_IDENTITY = identity.strip() if identity and identity.strip() else None
    _label_screen_cached.cache_clear()


def _is_self_authored(author: Optional[str]) -> bool:
    if not author or not _EPISODE_SELF_IDENTITY:
        return False
    return author.strip().lower().lstrip("@") == _EPISODE_SELF_IDENTITY.strip().lower().lstrip("@")




def configure_prompt_trusted_senders(_senders=()) -> None:
    """No-op. Kept so callers that still pass a detected sender list keep working; sender-name
    trust no longer exists (see the note above). Identity now flows through get_contacts ->
    endorse_element with exact identifier matching."""
    return None


def is_untrusted(label: FidesLattice) -> bool:
    return not meets_level(label, _MIN_TRUSTED_LEVEL)


def meets_level(label: FidesLattice, min_level: "IntegrityLabel.Level") -> bool:
    """True iff label's integrity is at least as trusted as min_level (lower Level.value = more
    trusted, see IntegrityLabel). Used by tier-gating code that needs the real level directly,
    independent of the is_untrusted() toggle above -- e.g. SAPolicy.tier_min_level."""
    return label.left.level.value <= min_level.value


def conf_permits(conf: Confidentiality, audience: FrozenSet[str]) -> bool:
    """True iff every principal in *audience* is in the label's reader set."""
    return audience.issubset(conf.inner.subset)



PrincipalMap = Callable[[InteractableAction], Confidentiality]


def _default_principal_map(_action: InteractableAction) -> Confidentiality:
    return public_conf()


def url_principal_map(rules: list[tuple[str, Confidentiality]]) -> PrincipalMap:
    """Build a principal map from (url-substring, readers) rules; first matching
    substring on the affordance's URL wins, default PUBLIC."""

    def _map(action: InteractableAction) -> Confidentiality:
        url = action.url or ""
        for needle, conf in rules:
            if needle in url:
                return conf
        return public_conf()

    return _map


def _document_zone_integrity(action: InteractableAction | GlobalAction, current_hostname: Optional[str]) -> IntegrityLabel:
    """Classify a DOCUMENT-zone *action* onto the full IntegrityLabel.Level ladder (see
    lattice.py). Shared by integrity_of() (GlobalAction) and _integrity_of_with_provenance()
    (every InteractableAction, see _label_screen_uncached) so the two never drift apart. Checked
    in priority order -- each rule short-circuits the ones below it:

    1. cross_origin_embed -- a nested document/embedded boundary crossed while already inside
       rendered page content (e.g. a third-party ad iframe). No origin/hostname is available for
       it, so it is always UNTRUSTED regardless of anything else below.
    2. Self-authored -- the detected region author (coord_to_semantic.py's best-effort
       _USER_HREF byline) matches the episode's resolved self-identity (see
       trust_detection.resolve_episode_self_identity / configure_self_identity). The operator's
       own content is as trustworthy as an independently-corroborated value.
    3. Episode-trusted-domain elevation, only when region_id is None AND the action's AT-SPI role
       is on the CHROME_LIKE_ROLES allowlist -- see the note below; narrower than the pre-ladder
       behavior (which elevated anything with region_id is None, regardless of role).
    4. Not elevated (no region_id at all, or a role outside the chrome-like allowlist) -- no
       structural signal to classify by; conservative UNTRUSTED fallback, same as today's flat
       behavior for non-elevated content.
    5. Inside a detected region: COMPUTED_PLATFORM_VALUE if Stage-1b's cross-instance value-shape
       classifier (coord_to_semantic._classify_value_shape) called this slot "computed" (bounded,
       platform-derived output, e.g. a price/rating/status badge); otherwise
       FREE_TEXT_THIRD_PARTY -- the general UGC bucket (forum posts, chat/email messages, listing
       descriptions), whether the slot was classified "free_text" or there weren't enough
       same-role instances to classify at all.
    """
    if getattr(action, "cross_origin_embed", False):
        return IntegrityLabel.untrusted()

    if _is_self_authored(getattr(action, "author", None)):
        return IntegrityLabel.self_authored()


    region_id = getattr(action, "region_id", None)
    role = getattr(action, "role", None)

    if (_host_is_episode_trusted(current_hostname) or _host_is_visitable(current_hostname)) and region_id is None and role in CHROME_LIKE_ROLES:
        return IntegrityLabel.trusted()

    if region_id is None:
        return IntegrityLabel.untrusted()

    if getattr(action, "region_value_shape", None) == "computed":
        return IntegrityLabel.computed_platform_value()

    return IntegrityLabel.free_text_third_party()


def integrity_of(action: InteractableAction | GlobalAction, current_hostname: Optional[str] = None) -> IntegrityLabel:
    if action.zone != Zone.DOCUMENT:
        return IntegrityLabel.trusted()
    return _document_zone_integrity(action, current_hostname)


logger = logging.getLogger("desktopenv.sa.screen_labels")

class FileProvenanceMap:
    """Reads the provenance manifest ``SetupController`` writes once, before the episode starts
    (see ``desktop_env/controllers/setup.py``'s ``PROVENANCE_MANIFEST_PATH`` /
    ``_write_provenance_manifest``), and answers per-window ownership/readers queries for
    non-browser app content.

    This is SA-Agent's analog of CaMeL's ``Tool(inner_sources=...)`` for files placed by the
    environment setup mechanism rather than fetched by a tracked tool call during the episode --
    the setup code that actually knows a file's provenance records it directly (mirroring how
    CaMeL's simulated Drive/email environment records realistic sender/sharing facts), rather than
    SA-Agent trying to infer it from OS-level signals that this benchmark's benign and adversarial
    file-placement paths do not actually distinguish.

    Lazily fetched and cached once per episode: the manifest is written once, before the episode
    starts, and never changes mid-episode.
    """

    MANIFEST_PATH = "/home/user/.rtc_provenance.json"

    def __init__(self, env: Any, owner_probe: "Optional[FileOwnershipProbe]" = None):
        self._env = env
        self.owner_probe = owner_probe
        self._entries: Optional[list[dict[str, Any]]] = None
        self._dynamic_entries: list[dict[str, Any]] = []
        self._trusted_files: set[str] = set()
        self._trusted_basenames: set[str] = set()

    def register_download(self, path: str, integrity: "IntegrityLabel", source: str = "") -> None:
        """Record provenance for a file/directory created live during the episode (currently
        only `git clone`, see hybrid_tools.extract_cloned_path). Stores the bare IntegrityLabel
        rather than a full FidesLattice/ProductLabel -- _integrity_of_with_provenance only ever
        needs the integrity half, and confidentiality is deliberately left untouched for these
        entries (see module docs / the design plan for this feature). *source* is a short
        human-readable string (e.g. the git command) kept only for debugging, never shown to the
        planner."""
        self._dynamic_entries.append({"path": path, "integrity": integrity, "source": source})

    def set_trusted_files(self, paths) -> None:
        """Register the episode's operator-vouched trusted files (the masking design). Paths are
        normalized; their basenames are also kept so a window titled by basename (native-app titles
        show the filename, not the full path) still resolves. Idempotent; call once per episode."""
        self._trusted_files = {os.path.normpath(str(p)) for p in (paths or []) if str(p).strip()}
        self._trusted_basenames = {os.path.basename(p) for p in self._trusted_files}

    def reset_episode(self) -> None:
        """Clear all provenance state (static manifest cache and dynamic registrations) for a
        fresh episode."""
        self._entries = None
        self._dynamic_entries = []
        self._trusted_files = set()
        self._trusted_basenames = set()
        if self.owner_probe is not None:
            self.owner_probe.reset_episode()

    def _ensure_loaded(self) -> list[dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        entries: list[dict[str, Any]] = []
        try:
            raw = self._env.controller.get_file(self.MANIFEST_PATH)
            if raw:
                entries = json.loads(raw.decode("utf-8", "replace")).get("files", [])
        except Exception:
            entries = []
        self._entries = entries
        return entries

    def lookup(self, frame_name: Optional[str]) -> Optional[dict[str, Any]]:
        """Match a window's title against known file paths (by basename -- native app window
        titles show the filename, not the full path; see coord_to_semantic.py's ``frame_name``
        capture). Checks dynamic (live-registered) entries first, most-recent-first, so a path
        collision prefers the latest registration; falls back to the static pre-episode manifest.
        Returns the matching entry, or None if no known file corresponds to this window."""
        if not frame_name:
            return None
        for entry in reversed(self._dynamic_entries):
            path = entry.get("path")
            if path and os.path.basename(path) in frame_name:
                return entry
        for entry in self._ensure_loaded():
            path = entry.get("path")
            if path and os.path.basename(path) in frame_name:
                return entry
        return None

    @staticmethod
    def _entry_is_trusted(entry: dict[str, Any]) -> bool:
        """Grade one provenance entry, exactly as _integrity_of_with_provenance does: a
        live-registered entry carries its IntegrityLabel outright (trusted iff not UNTRUSTED); a
        static manifest entry is trusted iff its recorded owner is the user."""
        if "integrity" in entry:
            return entry["integrity"].level != IntegrityLabel.Level.UNTRUSTED
        return entry.get("owner", "user") == "user"

    def window_trust(self, frame_name: Optional[str]) -> Optional[bool]:
        """The masking shortcut (the masking design). Does this window show a KNOWN file, and is
        it trusted? ``True`` = a trusted-file window (the caller may skip masking it); ``False`` =
        a known-untrusted file (mask it); ``None`` = there is no provenance answer here (an empty
        manifest, a title that does not look like an opened file, or a file this map has never heard
        of), so the caller falls through to the ordinary rule-based masking. ``None`` therefore
        never grants trust -- it only declines to short-circuit.

        Manifest/registration-scoped on purpose: it keys on the window TITLE, which is all a masking
        turn has. FileOwnershipProbe needs the file PATH and cannot run from a title alone, so a
        user-authored base-image file with no manifest entry returns None (masked by the rules)
        rather than a wrong answer -- fail-closed, exactly as _integrity_of_with_provenance does."""
        if frame_name and any(b and b in frame_name for b in self._trusted_basenames):
            return True
        gimp = self._gimp_trust(frame_name)
        if gimp is not None:
            return gimp
        lo = self._libreoffice_new_doc_trust(frame_name)
        if lo is not None:
            return lo
        if self.is_empty() or not _looks_like_file_title(frame_name):
            return None
        entry = self.lookup(frame_name)
        if entry is None:
            return None
        return self._entry_is_trusted(entry)

    def _gimp_trust(self, frame_name: Optional[str]) -> Optional[bool]:
        """GIMP-specific arm of :meth:`window_trust` (see ``_gimp_title_token``). Resolve a GIMP
        window title to its provenance manifest entry -- by basename STEM for an imported raster
        (GIMP dropped the extension), by full filename for a native ``.xcf`` -- and grade it.
        ``True`` iff every matching entry is user-owned; ``False`` if any match is known-untrusted
        (fail closed, mask); ``None`` when the title is not a GIMP title we parse or no manifest
        entry matches (fall through to the ordinary rule-based masking). Ambiguity therefore never
        widens: a stem shared with an adversarially-placed file masks rather than reveals."""
        tok = _gimp_title_token(frame_name)
        if tok is None:
            return None
        token, is_stem = tok
        trusted = untrusted = 0
        for entry in list(reversed(self._dynamic_entries)) + self._ensure_loaded():
            path = entry.get("path")
            if not path:
                continue
            base = os.path.basename(path)
            key = os.path.splitext(base)[0] if is_stem else base
            if key == token:
                if self._entry_is_trusted(entry):
                    trusted += 1
                else:
                    untrusted += 1
        if untrusted:
            return False
        if trusted:
            return True
        return None

    def _libreoffice_new_doc_trust(self, frame_name: Optional[str]) -> Optional[bool]:
        """LibreOffice-specific arm of :meth:`window_trust` (see ``_LO_NEW_DOC_RE``). A brand-new,
        never-saved doc ("Untitled N - LibreOffice Calc/Writer/...") has no file behind it and no
        pre-existing content: the agent authors every cell/word, so nothing is injectable -> trust.
        ``None`` if the title is not that shape (an opened file carries its filename+extension, so it
        never matches and keeps flowing to lookup()). Fail-closed: if the manifest carries a
        known-UNTRUSTED entry whose basename stem equals "Untitled N" (an adversarially-placed file
        literally named that, opened with no extension), mask instead -- ambiguity never widens."""
        if not frame_name:
            return None
        m = _LO_NEW_DOC_RE.match(frame_name)
        if m is None:
            return None
        title = m.group(1)
        for entry in list(reversed(self._dynamic_entries)) + self._ensure_loaded():
            path = entry.get("path")
            if path and os.path.splitext(os.path.basename(path))[0] == title \
                    and not self._entry_is_trusted(entry):
                return False
        return True

    def path_trust(self, path: Optional[str]) -> Optional[bool]:
        """Path-keyed sibling of :meth:`window_trust`, for the terminal rule (the masking design):
        grade a concrete filesystem path against the trusted-paths set. ``True`` if a known-trusted
        entry (live-registered, most-recent-first, then the static manifest) covers it exactly,
        ``False`` if a known-untrusted entry does, ``None`` if the map has never heard of this path
        -- so an unknown file's terminal output stays UNTRUSTED (masked), never granted by absence.
        Exact normalized-path match, not basename: a window title only shows a basename, but a
        command names the whole path, and two files can share a basename."""
        if not path:
            return None
        norm = os.path.normpath(path)
        if norm in self._trusted_files:
            return True
        for entry in reversed(self._dynamic_entries):
            ep = entry.get("path")
            if ep and os.path.normpath(ep) == norm:
                return self._entry_is_trusted(entry)
        for entry in self._ensure_loaded():
            ep = entry.get("path")
            if ep and os.path.normpath(ep) == norm:
                return self._entry_is_trusted(entry)
        return None

    def basename_trust(self, name: Optional[str]) -> Optional[bool]:
        """Is this bare filename, as a file manager lists it, a file the USER owns? Row-keyed
        sibling of :meth:`window_trust` and :meth:`path_trust`.

        WHY A THIRD KEY. The other two cannot answer for a file that is merely *listed*.
        ``window_trust`` keys on a window TITLE, so it only fires for a document that is open;
        ``path_trust`` needs a whole path, and a file manager row does not carry one -- in the
        corpus's Nautilus captures a file is a ``canvas`` row whose entire text is the basename
        (``Invoice_1042.pdf``), with no id, no class and no path attribute. So the agent that has
        been told to attach ``~/Documents/sales_Q3_2025.pdf`` arrives at the chooser and finds the
        row black, even though the setup manifest records that exact path with ``owner: user``.
        Its own file, provenance already settled, unreadable.

        WHY BASENAME MATCHING IS SAFE HERE AND NOT IN path_trust. ``path_trust`` refuses basenames
        outright, and rightly: two files can share one. This grants ONLY when the basename is
        unambiguous -- exactly one known file has it and the user owns that file. A collision of
        any kind, including with an adversarially-placed file (``_adv_upload_file_setup`` records a
        provenance entry too), returns None and the row stays masked. A file no manifest mentions
        also returns None. Absence never grants, ambiguity never grants; the only ``True`` is a
        basename that identifies one user-owned file and nothing else.

        NATIVE SURFACES ONLY -- the caller enforces this, and it is not a detail. A web page can
        render any string it likes, so a page that prints a filename it learned from the task would
        otherwise have that row un-masked for it. A file manager listing a name is evidence about
        the filesystem; a web page printing one is evidence about the page.
        """
        base = os.path.basename((name or "").strip())
        if not base or "/" in (name or "").strip():
            return None
        owners: list[bool] = []
        for path in self._trusted_files:
            if os.path.basename(path) == base:
                owners.append(True)
        for entry in list(self._dynamic_entries) + list(self._ensure_loaded()):
            ep = entry.get("path")
            if ep and os.path.basename(os.path.normpath(ep)) == base:
                owners.append(self._entry_is_trusted(entry))
        if len(owners) != 1:
            return None
        return True if owners[0] else False

    def is_empty(self) -> bool:
        """True if there is no file-provenance question to ask at all this episode -- neither a
        file placed via _download_setup/_adv_upload_* nor one registered live via
        register_download() -- so every native-app window (terminal, Settings, an app opened with
        nothing loaded, ...) should stay trusted rather than being run through a lookup that could
        only ever return None."""
        return not self._ensure_loaded() and not self._dynamic_entries


class FileOwnershipProbe:
    """Runtime answer to one question the pre-episode manifest cannot answer: *did the user of
    this machine author the file this window is showing?*

    WHY A SECOND MECHANISM. ``FileProvenanceMap`` is authoritative for files the harness placed,
    because the setup code that placed them knows where they came from. It says nothing about the
    files that were simply *there* -- the user's own documents in the base image, anything a task
    creates outside a tracked tool call -- and ``_integrity_of_with_provenance`` fails closed on
    those, so opening your own spreadsheet in LibreOffice yields UNTRUSTED content and every
    downstream tier gate refuses to act on it. That is a false positive, not a defense.

    WHY OWNERSHIP ALONE IS NOT THE ANSWER, even though it is the obvious one. On this VM every
    file under the home directory is owned by ``user``, including anything a browser or a shell
    just downloaded. ``uid == user`` therefore means "nobody else on this machine wrote it", which
    is a real fact and a much weaker one than "the user authored it". Granting trust on ownership
    alone would hand it to any file an injection can get written to disk -- so ownership is
    treated here as *necessary and not sufficient*, and three further conditions carry the rest:

      * **no network-origin marker.** Browsers and GIO stamp downloads with the ``user.xdg.origin.url``
        / ``user.xdg.referrer.url`` extended attributes. Their presence is positive evidence the
        bytes came off the network; their absence is not proof of anything, which is why it is one
        condition of four rather than the test.
      * **older than this episode.** A file the user authored predates the task. A file that
        appeared or changed *during* the episode is, by construction, something this run caused --
        possibly at the direction of content it read. Those are exactly the ones a tracked tool
        call should have registered (see ``FileProvenanceMap.register_download``), and if none did,
        the honest answer is that we do not know who wrote it.
      * **not in a download directory.** Cheap, and it catches the case where the xattr is absent
        because the fetch was a plain ``curl``.

    The manifest always wins where it has an entry: this probe is consulted only for files it has
    never heard of. Everything here is read-only ``stat``/``getfattr``, cached per path per
    episode, and the whole mechanism is disabled by ``--sa_file_owner_shortcut off``, which
    restores the previous fail-closed behaviour exactly.
    """

    _FOREIGN_DIRS = ("/Downloads", "/Desktop/Downloads", "/tmp", "/var/tmp")
    _NAME_TOKEN_RE = re.compile(r"[^\s/\\:*?\"<>|]+\.[A-Za-z0-9]{1,6}")
    _URI_RE = re.compile(r"file://(\S+)")

    def __init__(self, env: Any, enabled: bool = True, home: str = "/home/user"):
        self._env = env
        self.enabled = enabled
        self._home = home.rstrip("/")
        self._uid: Optional[str] = None
        self._epoch: Optional[int] = None
        self._xattr: bool = False
        self._verdicts: dict[str, tuple[bool, str]] = {}
        self._paths: dict[str, Optional[str]] = {}
        self.grants: list[str] = []


    def reset_episode(self) -> None:
        """Clear every cached fact and re-read the VM's clock.

        The clock is read from the VM rather than the host on purpose: mtimes are VM-side, and a
        guest whose clock differs from the host's by even a few minutes would otherwise make every
        pre-existing file look like it was written during the episode (or vice versa, which is the
        direction that matters)."""
        self._verdicts.clear()
        self._paths.clear()
        self._epoch = None
        if not self.enabled:
            return
        self._xattr = bool((self._run("command -v getfattr") or "").strip())
        if not self._xattr:
            logger.warning("[sa][owner] getfattr is not installed in the VM -- the "
                           "download-origin check cannot run; grants rest on ownership, "
                           "episode age and location only")
        out = self._run("date +%s")
        try:
            self._epoch = int((out or "").strip().splitlines()[0])
        except (ValueError, IndexError):
            self._epoch = None
            logger.warning("[sa][owner] could not read the VM clock -- the file-ownership "
                           "shortcut is inactive for this episode")


    def user_authored(self, doc_name: Optional[str], frame_name: Optional[str]) -> tuple[bool, str]:
        """True iff the file this window is displaying was authored by the agent's own user.

        Returns the verdict and a short reason, which is logged rather than shown to the planner.
        """
        if not self.enabled or self._env is None or self._epoch is None:
            return False, "disabled"
        path = self._resolve(doc_name, frame_name)
        if path is None:
            return False, "no file identified"
        if path in self._verdicts:
            return self._verdicts[path]
        verdict = self._classify(path)
        self._verdicts[path] = verdict
        if verdict[0]:
            self.grants.append(path)
            logger.info("[sa][owner] %s -- user-authored, granting the trust shortcut", path)
        else:
            logger.info("[sa][owner] %s -- no shortcut (%s)", path, verdict[1])
        return verdict


    def _resolve(self, doc_name: Optional[str], frame_name: Optional[str]) -> Optional[str]:
        """Which file is this window showing? Two sources, and a hard requirement of uniqueness.

        LibreOffice names its document node with the ``file://`` URI, which settles the question
        outright. Everything else states a filename in the window title and nothing more, so the
        path has to be found on disk -- and it is accepted only when exactly one file matches. An
        ambiguous name is not a near-miss to be broken by a heuristic: two files with one name are
        precisely the situation where guessing grants trust to the wrong one.
        """
        key = f"{doc_name}\x00{frame_name}"
        if key in self._paths:
            return self._paths[key]
        path = None
        uri = self._URI_RE.search(doc_name or "")
        if uri:
            path = unquote(uri.group(1))
        else:
            for token in self._NAME_TOKEN_RE.findall(frame_name or ""):
                if "@" in token:
                    continue
                path = self._find_unique(token)
                if path:
                    break
        self._paths[key] = path
        return path

    def _find_unique(self, basename: str) -> Optional[str]:
        out = self._run(
            f"find {shlex.quote(self._home)} -maxdepth 4 -type f -name {shlex.quote(basename)} "
            f"-not -path '*/.*' 2>/dev/null | head -n 3")
        hits = [line.strip() for line in (out or "").splitlines() if line.strip()]
        return hits[0] if len(hits) == 1 else None


    def _classify(self, path: str) -> tuple[bool, str]:
        out = self._run(
            f"p={shlex.quote(path)}; "
            f"stat -c 'UID=%u MTIME=%Y' \"$p\" 2>/dev/null; "
            f"getfattr -d -m '.' \"$p\" 2>/dev/null | tr '\\n' ';'")
        text = out or ""
        uid = _grep1(r"UID=(\d+)", text)
        mtime = _grep1(r"MTIME=(\d+)", text)
        if uid is None or mtime is None:
            return False, "file not found in the VM"
        if self._uid is None:
            self._uid = (self._run("id -u") or "").strip().splitlines()[0] if self._env else None
        if self._uid is None or uid != self._uid:
            return False, f"owned by uid {uid}, not the agent's user"
        if self._xattr and ("user.xdg.origin.url" in text or "user.xdg.referrer.url" in text):
            return False, "carries a download-origin xattr"
        if any(d in path for d in self._FOREIGN_DIRS):
            return False, "lives in a download directory"
        if int(mtime) >= self._epoch:
            return False, "written during this episode, by nothing that registered provenance"
        return True, ("owned by the agent's user, predates the episode, no network origin"
                      if self._xattr else
                      "owned by the agent's user, predates the episode (no getfattr: "
                      "download-origin unchecked)")


    def _run(self, command: str) -> Optional[str]:
        controller = getattr(self._env, "controller", None)
        if controller is None:
            return None
        try:
            result = controller.execute_command(command)
        except Exception:
            return None
        if not isinstance(result, dict):
            return None
        return result.get("output") or ""


def _grep1(pattern: str, text: str) -> Optional[str]:
    m = re.search(pattern, text)
    return m.group(1) if m else None


@dataclass
class SAScreenView:
    """The labeled, discretized view of one screen observation."""

    affordances: list[tuple[InteractableAction, FidesLattice]] = field(default_factory=list)
    global_actions: list[tuple[GlobalAction, FidesLattice]] = field(default_factory=list)
    regions: dict[str, FidesLattice] = field(default_factory=dict)
    document_content: list[tuple[Any, FidesLattice]] = field(default_factory=list)

    def region_label(self, region_id: Optional[str]) -> FidesLattice:
        if region_id is None:
            return trusted_public()
        return self.regions.get(region_id, trusted_public())

    def affordance_at(
        self,
        x: float,
        y: float,
        action_types: Optional[FrozenSet[ActionType]] = None,
    ) -> Optional[tuple[InteractableAction, FidesLattice]]:
        """Smallest-area affordance whose bbox contains (x, y), or None.

        *action_types*, when given, restricts the search to affordances whose
        primary action_type is one of these — e.g. pointer/click-family tools
        should not resolve to a large-bbox synthesized SCROLL affordance that
        happens to cover the same point. None (the default) considers every
        affordance, which is what a free-form label lookup (target_metadata)
        legitimately wants.
        """
        best: Optional[tuple[InteractableAction, FidesLattice]] = None
        best_area = None
        for action, label in self.affordances:
            if action_types is not None and action.action_type not in action_types:
                continue
            bx, by, bw, bh = action.bbox
            if bx <= x <= bx + bw and by <= y <= by + bh:
                area = max(bw, 1) * max(bh, 1)
                if best_area is None or area < best_area:
                    best, best_area = (action, label), area
        return best

    def extraction_label(self) -> FidesLattice:
        """Label for a free-form extraction over this whole screen: the join of every DOCUMENT-zone
        label present (trusted if the screen has none) -- over *content* as well as affordances.

        The affordance-only version of this was unsound, and not only for native apps. A read
        returns text, so the bound has to cover the text; joining only over controls made
        "untrusted content is on this screen" a proxy for "untrusted controls are on this screen".
        The proxy holds for a forum page (every comment carries a Reply link) and fails for inert
        prose -- a mail message body has no controls at all, so the join was empty and the read
        came back TRUSTED. See the endorsement design.
        """
        result = trusted_public()
        for action, label in self.affordances:
            if action.zone == Zone.DOCUMENT or is_untrusted(label):
                result = result.join(label)
        for _node, label in self.document_content:
            result = result.join(label)
        return result


_CLIP_MIN_CHARS = 16


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def apply_clipboard_taint(view: SAScreenView, clips) -> int:
    """Re-label any node now displaying clipboard content with the clipboard's own label.

    WHY THIS IS NOT A PASTE GATE. Recognizing the *copy* was deliberately abandoned -- there is no
    way to know from the tree which controls copy, so ``_poll_clipboard`` observes the clipboard
    instead and labels what is in it. The consuming side was never given the same treatment: it
    recognizes one mechanism (``hotkey`` ctrl+v), and a paste reaches the screen by many others --
    a held ``key_down('ctrl')`` then ``key_down('v')``, a middle-click of the X11 PRIMARY
    selection, a right-click context menu's own Paste item, a drag-and-drop. Enumerating those is
    the game the copy side already lost.

    So this asks the question the other way round, about the result rather than the mechanism: is
    text that the clipboard holds now being *rendered* by a node, and is that node labelled better
    than the clipboard is? Then the node inherits the clipboard's label. Nothing needs to know how
    it got there.

    The case that motivates it cannot be fixed by any paste gate at all. Zone is decided by where
    text is *displayed*, not by where it came from: browser chrome is TRUSTED by construction, so
    content copied out of a redacted region and pasted into the omnibox is read back at full trust
    from a zone that is never redacted. The copy is not the leak and neither is the paste -- the
    leak is the rendering, and this is the only place that sees it.

    Only ever lowers a label (``join`` keeps the less-trusted side), so it cannot manufacture
    trust; and a node that legitimately holds the text because it is where the copy came FROM is
    already at that label, so the join is a no-op there. Returns how many nodes were re-labelled,
    for the run log.
    """
    needles = [(_norm_text(text), label) for text, label in clips
               if len(_norm_text(text)) >= _CLIP_MIN_CHARS]
    if not needles:
        return 0
    changed = 0
    for bucket, text_of in ((view.affordances, lambda n: getattr(n, "label", "")),
                            (view.document_content, lambda n: getattr(n, "text", ""))):
        for i, (node, label) in enumerate(bucket):
            rendered = _norm_text(text_of(node))
            if len(rendered) < _CLIP_MIN_CHARS:
                continue
            for needle, clip_label in needles:
                if needle not in rendered and rendered not in needle:
                    continue
                joined = label.join(clip_label)
                if joined.left.level != label.left.level:
                    bucket[i] = (node, joined)
                    changed += 1
                break
    return changed


def apply_endorsed_regions(view: SAScreenView, rects) -> int:
    """Raise the integrity of affordances inside an endorse_element rectangle to the level the
    endorser awarded, so the ONE verified row's controls clear the click gate while the rest of
    the redacted list stays untrusted.

    The mirror image of apply_clipboard_taint: that one only ever *lowers* a label; this one only
    ever *raises* one, and only inside a rectangle an already-cleared value established (the tool
    refuses an uncleared variable). So it cannot manufacture trust for anything the planner has not
    independently verified. Confidentiality (``label.right``) is preserved. Returns how many nodes
    were re-labelled, for the run log.
    """
    if not rects:
        return 0
    changed = 0
    for i, (node, label) in enumerate(view.affordances):
        bb = getattr(node, "bbox", None)
        if not bb or len(bb) < 4:
            continue
        cx, cy = bb[0] + bb[2] / 2.0, bb[1] + bb[3] / 2.0
        for r in rects:
            rx, ry, rw, rh = r["bbox"]
            if not (rx <= cx <= rx + rw and ry <= cy <= ry + rh):
                continue
            lvl = r["level"]
            if label.left.level.value > lvl.value:
                view.affordances[i] = (node, ProductLabel(IntegrityLabel(lvl), label.right))
                changed += 1
            break
    return changed


def label_screen(
    a11y_xml: Optional[str],
    principal_map: Optional[PrincipalMap] = None,
    file_provenance_map: Optional[FileProvenanceMap] = None,
) -> SAScreenView:
    """Turn a raw AT-SPI XML observation into a labeled ``SAScreenView``.

    Tolerant of missing/empty XML (returns an empty, all-trusted view — the
    policy layer then falls back to its own conservative defaults).
    """
    if principal_map is None and file_provenance_map is None and isinstance(a11y_xml, (str, bytes)) and a11y_xml:
        return _label_screen_cached(a11y_xml if isinstance(a11y_xml, str) else a11y_xml.decode("utf-8", "replace"))
    return _label_screen_uncached(a11y_xml, principal_map, file_provenance_map)


@lru_cache(maxsize=4)
def _label_screen_cached(a11y_xml: str) -> SAScreenView:
    return _label_screen_uncached(a11y_xml, None, None)


_FILE_TITLE_RE = re.compile(r"\S\.[A-Za-z0-9]{1,6}\s+-\s+\S")


def _looks_like_file_title(frame_name: Optional[str]) -> bool:
    return bool(frame_name) and _FILE_TITLE_RE.search(frame_name) is not None


_GIMP_TITLE_SUFFIX_RE = re.compile(r"[-–]\s*GIMP\s*$")
_GIMP_BRACKET_STEM_RE = re.compile(r"\[([^\]]+)\]\s*\((?:imported|overwritten|exported)")
_GIMP_XCF_RE = re.compile(r"([^\s*\[\]][^\s]*\.xcf)\b")


def _gimp_title_token(frame_name: Optional[str]) -> Optional[tuple[str, bool]]:
    """For a GIMP window title, return ``(token, is_stem)``: the bracketed image STEM (GIMP has
    already stripped the extension, ``is_stem=True``) or the leading ``.xcf`` filename
    (``is_stem=False``). ``None`` when this is not a GIMP title we can parse -- which falls the
    caller through to the ordinary rule-based masking, exactly as before."""
    if not frame_name or _GIMP_TITLE_SUFFIX_RE.search(frame_name) is None:
        return None
    m = _GIMP_BRACKET_STEM_RE.search(frame_name)
    if m:
        return m.group(1).strip(), True
    m = _GIMP_XCF_RE.search(frame_name)
    if m:
        return m.group(1).strip(), False
    return None


_LO_NEW_DOC_RE = re.compile(
    r"^(Untitled \d+)\s*[-–—]\s*LibreOffice "
    r"(?:Calc|Writer|Impress|Draw|Math|Base)\s*$")


def _user_authored(file_provenance_map: Optional[FileProvenanceMap], action: Any) -> bool:
    """Runtime fallback for a file no manifest entry covers -- see FileOwnershipProbe.

    Returns False whenever the probe is absent or switched off, so the caller's behaviour with
    ``--sa_file_owner_shortcut off`` is byte-for-byte what it was before this existed.
    """
    probe = getattr(file_provenance_map, "owner_probe", None)
    if probe is None:
        return False
    granted, _reason = probe.user_authored(getattr(action, "doc_name", None),
                                           getattr(action, "frame_name", None))
    return granted


def _integrity_of_with_provenance(
    action: InteractableAction,
    file_provenance_map: Optional[FileProvenanceMap],
    current_hostname: Optional[str] = None,
) -> tuple[IntegrityLabel, Optional[dict[str, Any]]]:
    """integrity_of(), extended for non-browser app content: consult the file-provenance manifest
    (see FileProvenanceMap) instead of the unconditional Zone.OS-is-trusted default -- but only
    when there's an actual reason to ask the question. Everything else in Zone.OS -- app chrome
    (menus/toolbars), a terminal, Settings, an app with nothing loaded -- has no provenance
    question to answer and stays trusted, the same way Zone.CHROME's own UI (address bar, tabs)
    is never run through a manifest lookup either. Two guards restore that default:
      1. an empty manifest (no _download_setup/_adv_upload_* this episode) means there is no
         provenance question to ask at all, for any window;
      2. even with a non-empty manifest, a window whose title doesn't look like it's displaying
         an opened file (see _looks_like_file_title) was never a provenance question either.
    Returns the resolved integrity label and the matching manifest entry (if any, for the
    confidentiality override below)."""
    if action.zone == Zone.DOCUMENT and getattr(action, "app_document", False):
        if (getattr(action, "app_document_kind", None) == _nr.KIND_FILE_LISTING
                and file_provenance_map is not None):
            if file_provenance_map.basename_trust(getattr(action, "label", None)) is True:
                return IntegrityLabel.trusted(), None
            return IntegrityLabel.untrusted(), None
        if (file_provenance_map is not None
                and not file_provenance_map.is_empty()
                and _looks_like_file_title(action.frame_name)):
            entry = file_provenance_map.lookup(action.frame_name)
            if entry is not None:
                if "integrity" in entry:
                    return entry["integrity"], entry
                owner = entry.get("owner", "user")
                return (IntegrityLabel.trusted() if owner == "user"
                        else IntegrityLabel.untrusted()), entry
        if _user_authored(file_provenance_map, action):
            return IntegrityLabel.trusted(), None
        return IntegrityLabel.untrusted(), None
    if action.zone == Zone.DOCUMENT:
        return _document_zone_integrity(action, current_hostname), None
    if (
        action.zone == Zone.CHROME
        or file_provenance_map is None
        or file_provenance_map.is_empty()
        or not _looks_like_file_title(action.frame_name)
    ):
        return IntegrityLabel.trusted(), None
    entry = file_provenance_map.lookup(action.frame_name)
    if entry is None:
        if _user_authored(file_provenance_map, action):
            return IntegrityLabel.trusted(), None
        return IntegrityLabel.untrusted(), None
    if "integrity" in entry:
        return entry["integrity"], entry
    owner = entry.get("owner", "user")
    return (IntegrityLabel.trusted() if owner == "user" else IntegrityLabel.untrusted()), entry


def _label_screen_uncached(
    a11y_xml: Optional[str],
    principal_map: Optional[PrincipalMap],
    file_provenance_map: Optional[FileProvenanceMap] = None,
) -> SAScreenView:
    pmap = principal_map or _default_principal_map
    view = SAScreenView()
    if not a11y_xml:
        return view
    if isinstance(a11y_xml, bytes):
        a11y_xml = a11y_xml.decode("utf-8", "replace")
    try:
        actions, global_actions, document_text = extract_all_atspi_with_content(a11y_xml)
    except Exception:
        return view
    current_hostname = get_active_tab_hostname(a11y_xml) if _EPISODE_TRUSTED_DOMAINS else None
    for action in actions:
        integrity, provenance_entry = _integrity_of_with_provenance(action, file_provenance_map, current_hostname)
        conf = pmap(action)
        if provenance_entry is not None and provenance_entry.get("readers"):
            readers = frozenset(provenance_entry["readers"]) & PRINCIPALS
            conf = conf.join(InverseLattice(PowersetLattice(subset=readers, universe=PRINCIPALS)))
        label = ProductLabel(integrity, conf)
        view.affordances.append((action, label))
        if action.region_id is not None:
            prev = view.regions.get(action.region_id)
            view.regions[action.region_id] = label if prev is None else prev.join(label)
    for node in document_text:
        integrity, provenance_entry = _integrity_of_with_provenance(
            node, file_provenance_map, current_hostname)
        conf = pmap(node)
        if provenance_entry is not None and provenance_entry.get("readers"):
            readers = frozenset(provenance_entry["readers"]) & PRINCIPALS
            conf = conf.join(InverseLattice(PowersetLattice(subset=readers, universe=PRINCIPALS)))
        label = ProductLabel(integrity, conf)
        view.document_content.append((node, label))
        if node.region_id is not None:
            prev = view.regions.get(node.region_id)
            view.regions[node.region_id] = label if prev is None else prev.join(label)
    for global_action in global_actions:
        label = ProductLabel(integrity_of(global_action, current_hostname), public_conf())
        view.global_actions.append((global_action, label))
    return view


def select_action_list_items(
    view: SAScreenView,
    max_items: int = 50,
) -> tuple[list[tuple[InteractableAction, FidesLattice]], int]:
    """Bound the affordances shown to the planner each step: keep every
    CHROME/OS-zone item (first-party UI, usually a modest, bounded count),
    then DOCUMENT-zone items in document order up to the remaining budget.

    Returns (kept, omitted_count). Real captures can carry 100+ affordances
    (see test/xml_examples/*.xml) — this keeps the per-step render bounded
    without hiding first-party UI, which the planner needs to navigate chrome.
    """
    chrome_or_os = [(a, l) for a, l in view.affordances if a.zone != Zone.DOCUMENT]
    document = [(a, l) for a, l in view.affordances if a.zone == Zone.DOCUMENT]
    remaining = max(0, max_items - len(chrome_or_os))
    kept = chrome_or_os + document[:remaining]
    omitted = len(view.affordances) - len(kept)
    return kept, omitted



def _view_from_obs(obs_raw: Optional[dict]) -> SAScreenView:
    tree = obs_raw.get("accessibility_tree") if isinstance(obs_raw, dict) else None
    return label_screen(tree)


def extraction_metadata(obs_raw: Optional[dict]) -> dict[str, Any]:
    """Metadata for a Q-LLM extraction over the whole screen in *obs_raw*."""
    return make_metadata(_view_from_obs(obs_raw).extraction_label())


def region_metadata(obs_raw: Optional[dict], bbox) -> dict[str, Any]:
    """Metadata for a value extracted from ONE redacted region, labeled by that region's own
    contents rather than by the whole screen.

    extraction_metadata() joins every DOCUMENT-zone element on screen, which is right for a
    free-form question about the whole picture and wrong here: it would drag an answer about a
    ENDORSED region down to UNTRUSTED because of an unrelated comment somewhere else on the
    page, and the planner would then be unable to use a value that is in fact no less trustworthy
    than its source. The join over the affordances actually inside the region is exactly the
    provenance of what the Q-VLM was shown.

    Falls back to the whole-screen label when the region contains no affordance the tree knows
    about -- absence of a hit must never launder taint (same rule as target_metadata).
    """
    view = _view_from_obs(obs_raw)
    try:
        rx, ry, rw, rh = (int(v) for v in bbox)
    except (TypeError, ValueError):
        return make_metadata(view.extraction_label())
    label = None
    for action, action_label in view.affordances:
        ax, ay = action.x_center, action.y_center
        if rx <= ax < rx + rw and ry <= ay < ry + rh:
            label = action_label if label is None else label.join(action_label)
    for node, node_label in view.document_content:
        nx, ny = node.x_center, node.y_center
        if rx <= nx < rx + rw and ry <= ny < ry + rh:
            label = node_label if label is None else label.join(node_label)
    if label is None:
        return make_metadata(view.extraction_label())
    return make_metadata(label)


def target_metadata(obs_raw: Optional[dict], x: Any, y: Any) -> dict[str, Any]:
    """Metadata for a value tied to the element at (x, y) — e.g. coordinates
    returned by ``find``. Falls back to the whole-screen extraction label when
    no affordance covers the point (the a11y tree may be incomplete, so absence
    of a hit must not launder taint)."""
    view = _view_from_obs(obs_raw)
    try:
        hit = view.affordance_at(float(x), float(y))
    except (TypeError, ValueError):
        hit = None
    if hit is not None:
        return make_metadata(hit[1])
    return make_metadata(view.extraction_label())
