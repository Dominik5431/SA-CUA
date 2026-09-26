"""Shared per-native-app content-region registry.

ONE source of truth for the Zone.OS(shell) / Zone.DOCUMENT(content) split inside NATIVE app
windows, used by BOTH live pipelines so they cannot disagree about where the trust boundary sits:

  * masking  (``mm_agents/sa_agent/masking.py::_verdict_rows``) -- which rows the P-LLM planner
    is shown un-redacted;
  * integrity/zones (``mm_agents/accessibility_tree_wrap/coord_to_semantic.py`` +
    ``screen_labels.py``) -- which ``Zone`` / trust / capability each affordance carries.

The two meet in ``loop_and_planner_no_exposure.py``: an action reaches the planner only if masking
did not redact it AND integrity trusts it, so a revealed element is usable only when both pipelines
agree. Defining the boundary in two places is how they drift; it is defined here once.

THE INVARIANT this module serves. For a native app window, masking's rules + default-deny must reach
ONLY the window's CONTENT BODY -- the surface an attacker can influence (a file listing, an opened
document, a message list/body). The SHELL -- the application's own menus, toolbars and navigation --
is first-party by construction and is never masked; only pixels the content body owns are blacked
out when not granted trust. The web already draws this line (an app's ``Zone.CHROME`` vs the
``Zone.DOCUMENT`` it displays); this module draws the same line for native apps so masking agrees
with the integrity pipeline, which already zones native chrome ``Zone.OS``/trusted.

Two structural handles establish "this is shell, not content body":

  1. UNIVERSAL CHROME ROLES (``NATIVE_CHROME_ROLES``). An application's own menus and toolbars are
     never attacker-influenceable -- an email or a document cannot inject a File-menu entry or a
     toolbar button; they are the program's fixed structure. So any row that IS one of those roles,
     or sits inside a menu / menu bar / tool bar, is shell in EVERY native app, with no per-app rule
     and no located-container gate. (In a browser the same widgets are ``Zone.CHROME`` and go down
     the web path; an Electron app that draws a "toolbar" inside ``document web`` keeps that document
     role, so it is not swept in here.)

  2. PER-APP CONTENT CONTAINERS. Several apps wrap their content body in a single, role-distinct
     container: a GTK file chooser's file ``table``, an archive manager's member list. Named
     STRUCTURALLY (by role, never by an attacker-controllable accessible name), so everything OUTSIDE
     the container in that window is shell (visible + trusted) and only the container is redacted +
     trust-gated. ``basename_trust`` then releases the user's own file rows inside it with no rule.

WHERE THE CONTENT BODY CANNOT BE SEPARATED BY STRUCTURE, masking FAILS CLOSED: a row that is neither
chrome nor inside a located content container is left to the ordinary rules + default-deny (i.e. it
is masked unless a reviewed rule trusts it). This is not a violation of the invariant but its
conservative edge -- the classic case is Thunderbird's ambiguous ``tree`` role, which is the shell
folder pane (``folderTree``) in one place and the attacker-authored message list (``threadTree``) in
another; role alone cannot tell them apart, so both stay default-denied rather than risk unmasking
the message list. Separating those needs the ``id`` (a later, id-level step), NOT a role rule here.

WHAT IS DELIBERATELY *NOT* HERE.

  * Doc-role apps (LibreOffice, Evince, VSCode, Impress) already cross into ``Zone.DOCUMENT`` via
    ``coord_to_semantic._DOC_ROLES`` on the integrity side, and on the masking side their shells
    already carry reviewed rules and their document bodies are handled by the trusted-file shortcut +
    default-deny -- so they need no masking shell-flip. Crucially, a *window-wide* doc-role masking
    flip would be UNSOUND: Thunderbird renders a mail body as ``document web`` (so it, too, has a
    ``_DOC_ROLES`` node) while its message LIST -- attacker-authored subject/sender text -- sits in a
    peer ``tree`` that is NOT a document container. Trusting "everything that is not the document"
    would unmask that list. ``NativeRegionScope.classify`` therefore never has a doc-role branch.

  * The TERMINAL is deliberately excluded FROM THIS ROLE-CHAIN SPLIT. It fits the shell/content shape,
    but its affordances earn ``Capability.EXECUTE`` only while they are ``Zone.OS``
    (``coord_to_semantic._capability_of``: ``zone == Zone.OS and app in _TERMINAL_APPS``). Crossing
    the terminal screen into ``Zone.DOCUMENT`` to gate its output would silently DOWNGRADE typing a
    command from EXECUTE to WRITE -- the lower-sensitivity direction the capability model forbids. So
    the terminal's masking is handled WITHOUT moving its zone: ``masking._verdict_rows`` trusts a
    terminal window by default (matching integrity's Zone.OS) and flips it to masked once a
    taint-producing command is visible in its scrollback (``sa_agent/terminal_taint.py``).
    That decision needs the terminal's TEXT, which this role-only module does
    not see, which is the other reason it lives in the masking verdict rather than here.

So beyond the universal chrome roles (handle 1 above), the located-container split here is confined
to a self-scoping container (the file chooser, matched only within its own ``file chooser`` subtree)
and two app-gated ones with no capability coupling and no second attacker surface (the archive
manager's member list, and GNOME Files' view -- see ``NAUTILUS_APPS``).

This module is pure data + tiny predicates and imports nothing from either pipeline, so both can
import it with no cycle (masking already bridges to the ``at-spi/`` package rather than importing
the zone module -- see ``at-spi/critical_path.py``).
"""
from __future__ import annotations

from typing import Optional, Sequence


NATIVE_DOC_ROLES = frozenset({
    "document web", "document frame", "document text", "document spreadsheet",
    "document presentation", "document email", "embedded",
})

FILE_ROLLER_APPS = frozenset({
    "file-roller", "File Roller", "Archive Manager", "org.gnome.FileRoller",
})

FILE_CHOOSER_ROLE = "file chooser"

FILE_CHOOSER_CONTENT_ROLES = frozenset({"table", "tree table"})

FILE_ROLLER_CONTENT_ROLES = frozenset({"tree table", "table"})

NAUTILUS_APPS = frozenset({"org.gnome.Nautilus", "nautilus", "Files"})
NAUTILUS_SPLIT_ROLE = "split pane"
NAUTILUS_VIEW_ROLE = "page tab list"
NAUTILUS_VIEW_CONTENT_ROLES = frozenset({"layered pane", "tree table", "table"})
NAUTILUS_SIDEBAR_ROLE = "list box"
_NAUTILUS_HEADER_PREFIX = ("frame", "panel", "filler", "panel")

KIND_FILE_LISTING = "file_listing"

NATIVE_CHROME_ROLES = frozenset({
    "menu bar", "menu", "menu item", "menu item check", "menu item radio", "tool bar",
})
_CHROME_CONTAINER_ROLES = frozenset({"menu bar", "menu", "tool bar"})

NATIVE_DIALOG_ROLES = frozenset({"dialog", "alert", "color chooser", "font chooser"})


def is_native_chrome(role: str, ancestor_roles: Sequence[str]) -> bool:
    """True if this native row is the application's own menu/toolbar chrome -- shell in every native
    app, independent of any located content container. ``ancestor_roles`` is the row's root-first
    ancestor role chain (own role excluded)."""
    if role in NATIVE_CHROME_ROLES:
        return True
    return any(r in _CHROME_CONTAINER_ROLES for r in ancestor_roles)


def _norm(app: Optional[str]) -> str:
    return (app or "").strip()


def app_is_file_roller(app: Optional[str]) -> bool:
    a = _norm(app)
    return a in FILE_ROLLER_APPS or a.lower() in {"file-roller", "file roller", "archive manager"}


def app_is_nautilus(app: Optional[str]) -> bool:
    a = _norm(app)
    return a in NAUTILUS_APPS or a.lower() in {"org.gnome.nautilus", "nautilus"}


def _nautilus_region(role: str, ancestor_roles: Sequence[str]) -> Optional[str]:
    """Nautilus shell/content split for one row (see NAUTILUS_APPS). Content is checked FIRST, so no
    row inside the view -- however it is nested -- can ever come back as shell."""
    chain = list(ancestor_roles) + [role]
    if NAUTILUS_VIEW_ROLE in chain or any(r in NAUTILUS_VIEW_CONTENT_ROLES for r in chain):
        return "content"
    if NAUTILUS_SPLIT_ROLE in chain:
        return "shell" if NAUTILUS_SIDEBAR_ROLE in chain else None
    if "frame" in chain:
        f = chain.index("frame")
        if tuple(chain[f:f + len(_NAUTILUS_HEADER_PREFIX)]) == _NAUTILUS_HEADER_PREFIX:
            return "shell"
    return None



def content_crossing_kind(app: Optional[str], role: str, *, in_file_chooser: bool) -> Optional[str]:
    """During a top-down walk that is currently in ``Zone.OS``, does ENTERING a node of this role
    cross into a NON-doc-role content container the application is merely displaying?

    Returns the content KIND to record on the crossing (``KIND_FILE_LISTING`` / ``KIND_TERMINAL``),
    or ``None`` to stay in ``Zone.OS``. Doc-role crossings (``document text``/``document web``/...)
    are handled by the caller's existing ``_DOC_ROLES`` branch and are NOT repeated here.

    ``in_file_chooser`` is the caller's per-branch flag, set once a ``file chooser`` ancestor is
    seen and threaded to descendants, so the file table crosses only inside a chooser.
    """
    if in_file_chooser and role in FILE_CHOOSER_CONTENT_ROLES:
        return KIND_FILE_LISTING
    if app_is_file_roller(app) and role in FILE_ROLLER_CONTENT_ROLES:
        return KIND_FILE_LISTING
    if app_is_nautilus(app) and (role == NAUTILUS_VIEW_ROLE or role in NAUTILUS_VIEW_CONTENT_ROLES):
        return KIND_FILE_LISTING
    return None



class NativeRegionScope:
    """Per-turn view of this window's native content containers, so the masking pipeline can split
    each row into shell / content / None (fail-closed). Universal chrome (menus, toolbars) is shell
    with no gate; the CONTAINER-based shell/content split is applied only where a content container
    was positively located, so a mis-detection over-masks (safe) rather than trusting an app whose
    structure we did not recognise.

    Build it once per turn with :func:`scope_from_role_chains`, then call :meth:`classify` per row.
    """

    __slots__ = ("app", "has_file_chooser_table", "has_file_roller_list", "has_nautilus_view")

    def __init__(self, app: Optional[str], *, has_file_chooser_table: bool,
                 has_file_roller_list: bool, has_nautilus_view: bool = False):
        self.app = _norm(app)
        self.has_file_chooser_table = has_file_chooser_table
        self.has_file_roller_list = has_file_roller_list
        self.has_nautilus_view = has_nautilus_view

    @property
    def active(self) -> bool:
        """True when at least one recognised container is present, i.e. some row may be reclassified."""
        return bool(self.has_file_chooser_table
                    or (self.has_file_roller_list and app_is_file_roller(self.app))
                    or (self.has_nautilus_view and app_is_nautilus(self.app)))

    def classify(self, role: str, ancestor_roles: Sequence[str]) -> Optional[str]:
        """Classify one native row as ``"shell"`` (first-party -- trust it, never masked),
        ``"content"`` (content body -- keep default-deny + rules + basename_trust), or ``None``
        (structure cannot separate shell from content here -- leave it to the ordinary rules +
        default-deny, i.e. fail closed). See the module docstring for the invariant.

        ``ancestor_roles`` is the row's root-first ancestor role chain, NOT including its own role
        (exactly what ``selector_propose.ancestor_path_rows`` yields, minus the ids/classes).
        """
        if is_native_chrome(role, ancestor_roles):
            return "shell"
        if self.has_file_chooser_table and FILE_CHOOSER_ROLE in ancestor_roles:
            fc = ancestor_roles.index(FILE_CHOOSER_ROLE)
            below = list(ancestor_roles[fc + 1:]) + [role]
            if any(r in FILE_CHOOSER_CONTENT_ROLES for r in below):
                return "content"
            return "shell"
        if self.has_file_roller_list and app_is_file_roller(self.app):
            if role in FILE_ROLLER_CONTENT_ROLES or any(r in FILE_ROLLER_CONTENT_ROLES for r in ancestor_roles):
                return "content"
            return "shell"
        if self.has_nautilus_view and app_is_nautilus(self.app):
            return _nautilus_region(role, ancestor_roles)
        if role in NATIVE_DIALOG_ROLES or any(r in NATIVE_DIALOG_ROLES for r in ancestor_roles):
            return "shell"
        return None


def scope_from_role_chains(app: Optional[str],
                           row_chains: Sequence[Sequence[str]]) -> NativeRegionScope:
    """Scan every content row's FULL role chain (root-first ancestors + own role) once to learn
    which containers this window has, then return a :class:`NativeRegionScope`.

    A file-chooser table is "located" only when a content-role node actually sits inside a
    ``file chooser`` -- a chooser with no listing does not license the shell flip.
    """
    has_fc_table = False
    has_roller = app_is_file_roller(app)
    roller_list = False
    is_nautilus = app_is_nautilus(app)
    nautilus_view = False
    for chain in row_chains:
        seen_fc = False
        seen_split = False
        for r in chain:
            if r == FILE_CHOOSER_ROLE:
                seen_fc = True
            elif seen_fc and r in FILE_CHOOSER_CONTENT_ROLES:
                has_fc_table = True
            if has_roller and r in FILE_ROLLER_CONTENT_ROLES:
                roller_list = True
            if r == NAUTILUS_SPLIT_ROLE:
                seen_split = True
            elif is_nautilus and seen_split and r == NAUTILUS_VIEW_ROLE:
                nautilus_view = True
    return NativeRegionScope(app,
                             has_file_chooser_table=has_fc_table,
                             has_file_roller_list=roller_list,
                             has_nautilus_view=nautilus_view)
