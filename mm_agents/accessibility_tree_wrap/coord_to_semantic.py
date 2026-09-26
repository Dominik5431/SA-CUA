"""
coord_to_semantic.py  —  Bidirectional GUI Action Bridge
=========================================================

Three environments supported:
  1. Desktop OS  — AT-SPI2 XML  (pyatspi / OSWorld format)
  2. Web browser — AXTree text dump  (WebArena / BrowserGym format)
                   or live Playwright page (see resolve_playwright)
  3. Android     — UIAutomator2 view hierarchy XML

Direction A  |  Coordinate → Semantic
  Given a raw (x, y) click from e.g. pyautogui and the structural
  snapshot taken *before* the action, return a human-readable
  semantic action string:
      click(target="File > Print")
      click(element="Submit")
      tap(element="Buy Now")

Direction B  |  Tree → Action List
  Given a structural snapshot, extract every interactable element
  and return a list of InteractableAction objects, each carrying
  BOTH representations:
      .semantic   = 'click(target="File > Print")'
      .coordinate = 'click(x=60, y=211)'
      .x_center, .y_center, .bbox, ...

Direction C  |  Semantic → Coordinate
  Given a semantic action string and the structural snapshot,
  look up the named element and return the coordinate form:
      semantic_to_coordinate('click(target="File > Print")', xml)
      → CoordinateAction(action='click', x=60, y=211)
      → 'click(x=60, y=211)'
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

from . import native_regions as _nr



class ActionType(str, Enum):
    CLICK       = "click"
    DOUBLE_CLICK= "doubleClick"
    RIGHT_CLICK = "rightClick"
    TYPE        = "type"
    TAP         = "tap"
    SWIPE       = "swipe"
    SCROLL      = "scroll"
    DRAG        = "drag"
    HOVER       = "hover"
    NAVIGATE    = "navigate"
    HOTKEY      = "hotkey"
    UNKNOWN     = "unknown"


_ATSPI_ROLE_ACTION: dict[str, ActionType] = {
    "push button":          ActionType.CLICK,
    "toggle button":        ActionType.CLICK,
    "radio button":         ActionType.CLICK,
    "check box":            ActionType.CLICK,
    "menu":                 ActionType.CLICK,
    "menu item":            ActionType.CLICK,
    "check menu item":      ActionType.CLICK,
    "radio menu item":      ActionType.CLICK,
    "tear off menu item":   ActionType.CLICK,
    "link":                 ActionType.CLICK,
    "list item":            ActionType.CLICK,
    "tree item":            ActionType.CLICK,
    "table cell":           ActionType.CLICK,
    "page tab":             ActionType.CLICK,
    "combo box":            ActionType.CLICK,
    "entry":                ActionType.TYPE,
    "text":                 ActionType.TYPE,
    "spin button":          ActionType.TYPE,
    "scroll bar":           ActionType.DRAG,
    "slider":               ActionType.DRAG,
}

_ANDROID_CLASS_ACTION: dict[str, ActionType] = {
    "Button":           ActionType.TAP,
    "ImageButton":      ActionType.TAP,
    "CheckBox":         ActionType.TAP,
    "RadioButton":      ActionType.TAP,
    "Switch":           ActionType.TAP,
    "ToggleButton":     ActionType.TAP,
    "TextView":         ActionType.TAP,
    "EditText":         ActionType.TYPE,
    "AutoCompleteTextView": ActionType.TYPE,
    "SearchView":       ActionType.TYPE,
    "SeekBar":          ActionType.SWIPE,
    "RatingBar":        ActionType.SWIPE,
    "ListView":         ActionType.SCROLL,
    "RecyclerView":     ActionType.SCROLL,
    "ScrollView":       ActionType.SCROLL,
    "ViewPager":        ActionType.SWIPE,
}

_PATH_ROLES = {
    "menu bar", "menu", "menu item", "check menu item",
    "radio menu item", "tear off menu item",
    "page tab list",
}

_INTERACTABLE_ROLES = set(_ATSPI_ROLE_ACTION.keys())



class Zone(str, Enum):
    CHROME   = "chrome"
    DOCUMENT = "document"
    OS       = "os"


class Capability(str, Enum):
    READ     = "read"
    NAVIGATE = "navigate"
    WRITE    = "write"
    SEND     = "send"
    EXECUTE  = "execute"
    UNKNOWN  = "unknown"


class LabelTaint(str, Enum):
    TRUSTED = "trusted"
    TAINTED = "tainted"


_CAP_RANK: dict[Capability, int] = {
    Capability.READ:     0,
    Capability.NAVIGATE: 1,
    Capability.WRITE:    2,
    Capability.SEND:     3,
    Capability.EXECUTE:  4,
    Capability.UNKNOWN:  5,
}

_BROWSER_APPS = frozenset({"Google Chrome", "Chromium", "chromium-browser"})

_TERMINAL_APPS = frozenset({"gnome-terminal-server", "GNOME Terminal", "xterm", "Terminal", "Konsole"})

_CURATED_HOTKEYS: dict[str, list[tuple[tuple[str, ...], str, Capability]]] = {
    "browser": [
        (("ctrl", "f"), "Find in page", Capability.READ),
        (("ctrl", "l"), "Focus address bar", Capability.READ),
        (("ctrl", "t"), "New tab", Capability.NAVIGATE),
        (("ctrl", "w"), "Close tab", Capability.WRITE),
    ],
    "terminal": [
        (("ctrl", "c"), "Interrupt running command", Capability.WRITE),
    ],
}

_DOC_ROLES = {
    "document web", "document frame", "document text", "document spreadsheet",
    "document presentation", "document email", "embedded",
}

_TEXT_CONTENT_ROLES = {
    "text", "label", "static", "paragraph", "heading", "caption", "list item",
    "table cell", "column header", "row header", "block quote", "description list",
    "description term", "description value", "code", "comment",
    "section",
}

_MIN_CONTENT_TEXT_LEN = 3

_REGION_ROLES = {"article", "section", "landmark", "list item", "blockquote", "group"}

CHROME_LIKE_ROLES = {
    "menu bar", "menu", "menu item", "check menu item", "radio menu item", "tear off menu item",
    "tool bar", "page tab list", "scroll bar", "status bar",
}

_TRUSTED_URL_SCHEMES = ("chrome://", "about:", "chrome-extension://", "devtools://")
_UNTRUSTED_CHROME_SCHEME_PREFIXES = ("chrome://new-tab-page", "chrome-search://")
_NAMEABLE_INTERNAL_SCHEMES = ("chrome://", "chrome-extension://", "devtools://")

_OMNIBOX_CLASS = "OmniboxViewViews"
_OMNIBOX_NAME = "Address and search bar"

_USER_HREF = re.compile(r"/(?:u|user)/([A-Za-z0-9_\-.]+)", re.IGNORECASE)

_GITLAB_AUTHOR_HREF = re.compile(
    r"^https?://[^/]+/([A-Za-z0-9][A-Za-z0-9._\-]*)/?(?:[?#].*)?$", re.IGNORECASE)

_VALUE_SHAPE_RE = re.compile(
    r"^[$€£]?\s*\d[\d,.]*\s*%?\s*[a-zA-Z]{0,4}\.?$"
    r"|^\d{1,4}[-/.]\d{1,2}([-/.]\d{1,4})?$"
    r"|^(true|false|yes|no)$",
    re.IGNORECASE,
)
_MIN_INSTANCES_FOR_VALUE_SHAPE = 3


def _classify_value_shape(label: str, group_labels: list[str]) -> str:
    """Classify a single region-slot *instance*'s value as "computed" (platform-derived, bounded
    output space) or "free_text" (unconstrained, likely human-typed), given every value observed
    for the same (region_role, region_slot_index) slot across the screen (*group_labels*,
    including this instance's own value).

    Deliberately per-instance rather than one verdict for the whole slot group: a slot where most
    instances are genuinely computed (repeated "Reply" controls, price badges, ...) but one
    instance is a lone, different value must not launder that instance along with its siblings --
    under a group-level verdict an attacker only needs two duplicate decoy values elsewhere in the
    same slot to drag a third, distinct injected value into "computed" too. Two independent
    signals, either is sufficient, checked against *this instance's own value* only:

    1. It alone matches the narrow value-shape regex (numeric/currency/percentage/date/bool) --
       authoritative regardless of siblings. A natural-language injected instruction has very
       little room to also satisfy this shape while remaining semantically meaningful, which is
       what makes this path hard to game with crafted content.
    2. This exact (stripped) value recurs at least once more among its slot-mates -- repetition
       across independently-authored instances is evidence of a small, platform-constrained
       vocabulary (e.g. "Approved"/"Rejected" status badges), but only for the value(s) that
       actually repeat, never for a value that merely sits in an otherwise-repetitive slot without
       repeating itself.
    """
    stripped = (label or "").strip()
    if not stripped:
        return "free_text"
    if _VALUE_SHAPE_RE.match(stripped):
        return "computed"
    group_stripped = [(s or "").strip() for s in group_labels]
    if group_stripped.count(stripped) >= 2:
        return "computed"
    return "free_text"


_NAV_ROLES = {
    "link", "page tab", "menu", "menu item",
    "check menu item", "radio menu item", "tear off menu item",
}

_OPAQUE_ROLES = {
    "push button", "toggle button", "radio button", "check box",
    "combo box", "list item", "tree item", "table cell",
}


_ATSPI_ACTION_NAME_MAP: dict[str, tuple[ActionType, Optional[Capability]]] = {
    "click":           (ActionType.CLICK, None),
    "press":           (ActionType.CLICK, None),
    "activate":        (ActionType.CLICK, None),
    "open":            (ActionType.CLICK, None),
    "doDefault":       (ActionType.CLICK, None),
    "jump":            (ActionType.CLICK, Capability.NAVIGATE),
    "toggle":          (ActionType.CLICK, Capability.WRITE),
    "check":           (ActionType.CLICK, Capability.WRITE),
    "uncheck":         (ActionType.CLICK, Capability.WRITE),
    "select":          (ActionType.CLICK, None),
    "choose":          (ActionType.CLICK, None),
    "edit":            (ActionType.TYPE,  Capability.WRITE),
    "expand":          (ActionType.CLICK, Capability.READ),
    "collapse":        (ActionType.CLICK, Capability.READ),
    "showContextMenu": (ActionType.RIGHT_CLICK, Capability.READ),
    "scrollUp":        (ActionType.SCROLL, Capability.READ),
    "scrollDown":      (ActionType.SCROLL, Capability.READ),
    "scrollLeft":      (ActionType.SCROLL, Capability.READ),
    "scrollRight":     (ActionType.SCROLL, Capability.READ),
    "scrollForward":   (ActionType.SCROLL, Capability.READ),
    "scrollBackward":  (ActionType.SCROLL, Capability.READ),
    "increment":       (ActionType.DRAG, Capability.WRITE),
    "decrement":       (ActionType.DRAG, Capability.WRITE),
}

_SCROLL_ACTION_DIRECTION: dict[str, str] = {
    "scrollUp": "up", "scrollDown": "down",
    "scrollLeft": "left", "scrollRight": "right",
    "scrollForward": "down", "scrollBackward": "up",
}


def _capability_of(
    role: str,
    act: ActionType,
    zone: Zone,
    app: str,
    url: Optional[str],
) -> Capability:
    """Conservative effect classification.

    Guiding rule: err toward the higher-sensitivity class, never the lower. READ
    is reserved for affordances that genuinely neither change state nor move the
    agent (scrolling). Any activatable control whose effect we cannot see returns
    UNKNOWN rather than a guessed class — the opacity is exactly what an attacker
    exploits, so it is surfaced for the authorization step to resolve.
    """
    if zone == Zone.OS and app in _TERMINAL_APPS:
        return Capability.EXECUTE
    if act == ActionType.TYPE:
        return Capability.WRITE
    if act == ActionType.SCROLL:
        return Capability.READ
    if act == ActionType.RIGHT_CLICK:
        return Capability.READ
    if act == ActionType.DRAG and role == "slider":
        return Capability.WRITE
    if role in _NAV_ROLES:
        return Capability.NAVIGATE
    if zone == Zone.CHROME:
        return Capability.NAVIGATE
    if role in _OPAQUE_ROLES:
        return Capability.UNKNOWN
    if zone == Zone.DOCUMENT:
        return Capability.UNKNOWN
    return Capability.READ


@dataclass
class BoundedElement:
    """Internal: a UI element with bounding box + metadata."""
    label: str
    role: str
    x: int
    y: int
    w: int
    h: int
    path: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def x_center(self) -> int:
        return self.x + self.w // 2

    @property
    def y_center(self) -> int:
        return self.y + self.h // 2

    @property
    def area(self) -> int:
        return self.w * self.h

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h

    def full_path(self) -> str:
        """e.g. 'File > Print' for nested menus, else just the label."""
        parts = [p for p in self.path if p]
        return " > ".join(parts) if parts else self.label

    def norm_path(self) -> str:
        return _normalise(self.full_path())

    def norm_label(self) -> str:
        return _normalise(self.label)


@dataclass
class InteractableAction:
    """
    A fully described GUI action that carries both representations.

    semantic   — world-model / human-readable form
                 e.g. 'click(target="File > Print")'

    coordinate — execution form, ready for pyautogui or OSWorld
                 e.g. 'click(x=60, y=211)'

    action_type — the ActionType enum value
    label       — the bare element label
    path        — full path string (same as what appears in semantic)
    x_center, y_center — center of the bounding box
    bbox        — (x, y, w, h) of the bounding box
    role        — AT-SPI role / Android class / AX role string
    """
    semantic:    str
    coordinate:  str
    action_type: ActionType
    label:       str
    path:        str
    x_center:    int
    y_center:    int
    bbox:        tuple[int, int, int, int]
    role:        str
    url:         Optional[str] = None
    app:         str = "os"
    frame_name:  Optional[str] = None

    zone:         Zone = Zone.OS
    region_id:    Optional[str] = None
    region_role:  Optional[str] = None
    region_index: Optional[int] = None
    region_slot_index: Optional[int] = None
    region_value_shape: Optional[str] = None
    cross_origin_embed: bool = False
    app_document: bool = False
    app_document_kind: Optional[str] = None
    doc_name:     Optional[str] = None
    author:       Optional[str] = None
    capability:   Capability = Capability.UNKNOWN
    label_taint:  LabelTaint = LabelTaint.TRUSTED

    supported_actions:   frozenset[ActionType] = field(default_factory=frozenset)
    action_capabilities: dict[ActionType, Capability] = field(default_factory=dict)
    direction:            Optional[str] = None
    value:                Optional[float] = None
    value_range:          Optional[tuple[float, float, float]] = None
    scroll_confidence:    Optional[str] = None
    description:          Optional[str] = None
    checked:               Optional[bool] = None
    selected:              Optional[bool] = None
    focused:                Optional[bool] = None
    has_popup:              Optional[str] = None
    role_description:       Optional[str] = None

    def __repr__(self) -> str:
        return (
            f"InteractableAction("
            f"semantic={self.semantic!r}, "
            f"coordinate={self.coordinate!r}, "
            f"center=({self.x_center}, {self.y_center}))"
        )

    def __eq__(self, o: InteractableAction) -> bool:
        return self.semantic == o.semantic and self.coordinate == o.coordinate


@dataclass
class GlobalAction:
    """A keyboard-only action with no target bounding box — a hotkey/shortcut.

    Kept separate from InteractableAction rather than forcing a fake bbox onto
    it: nothing here can be resolved by a pointer coordinate.
    """
    keys:         tuple[str, ...]
    description:  str
    action_type:  ActionType = ActionType.HOTKEY
    capability:   Capability = Capability.UNKNOWN
    zone:         Zone = Zone.OS
    scope:        str = "element"
    element_ref:  Optional[str] = None
    app:          str = "os"
    region_id:    Optional[str] = None
    label_taint:  LabelTaint = LabelTaint.TRUSTED

    def __repr__(self) -> str:
        return f"GlobalAction(keys={'+'.join(self.keys)!r}, {self.description!r}, cap={self.capability.value})"


@dataclass
class DocumentTextNode:
    """Inert text painted inside a Zone.DOCUMENT boundary.

    Not an action: nothing can be done to it, so it is not an ``InteractableAction`` and never
    reaches the planner's action_list. It exists for *labelling* -- a free-form extraction reads
    the text on a screen, and until this was collected the only evidence
    ``screen_labels.extraction_label()`` had was the affordances that happened to accompany that
    text. On a page that is prose with no controls (a mail message body is the canonical case) the
    join over affordances is empty and the read was labelled TRUSTED.

    Field names deliberately match ``InteractableAction``'s, so
    ``screen_labels._document_zone_integrity`` -- which reads them all through ``getattr`` -- grades
    a text node and a control by the identical ladder with no branch of its own.
    """
    label:        str
    role:         str
    bbox:         tuple[int, int, int, int]
    url:          Optional[str] = None
    zone:         Zone = Zone.OS
    app:          str = "os"
    frame_name:   Optional[str] = None
    region_id:    Optional[str] = None
    region_role:  Optional[str] = None
    cross_origin_embed: bool = False
    app_document: bool = False
    app_document_kind: Optional[str] = None
    doc_name:     Optional[str] = None
    author:       Optional[str] = None
    region_value_shape: Optional[str] = None

    @property
    def x_center(self) -> int:
        return self.bbox[0] + self.bbox[2] // 2

    @property
    def y_center(self) -> int:
        return self.bbox[1] + self.bbox[3] // 2


@dataclass
class CoordinateAction:
    """Result of resolving a semantic action back to coordinates."""
    action_type: ActionType
    x: int
    y: int
    label: str
    path: str
    bbox: tuple[int, int, int, int]

    @property
    def x_center(self) -> int:
        return self.x + self.bbox[2] // 2

    @property
    def y_center(self) -> int:
        return self.y + self.bbox[3] // 2

    def to_string(self, center: bool = True) -> str:
        """
        Return an executable action string.

        center=True  → uses bounding box center (default, more reliable)
        center=False → uses raw top-left corner
        """
        px = self.x if not center else self.x + (self.bbox[2] // 2)
        py = self.y if not center else self.y + (self.bbox[3] // 2)
        act = self.action_type.value
        return f"{act}(x={px}, y={py})"

    def __repr__(self) -> str:
        return f"CoordinateAction({self.to_string()} for {self.label!r})"



def _normalise(s: str) -> str:
    """Lowercase, strip, collapse whitespace, strip diacritics."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def _build_semantic(action_type: ActionType, path: str, text: str = "") -> str:
    act = action_type.value
    if action_type == ActionType.TYPE and text:
        return f'{act}(element="{path}", text={text!r})'
    kw = "target" if " > " in path else "element"
    return f'{act}({kw}="{path}")'


def _build_coordinate(action_type: ActionType, elem: BoundedElement) -> str:
    act = action_type.value
    return f"{act}(x={elem.x_center}, y={elem.y_center})"


def _best_candidate(
    candidates: list[BoundedElement],
    prefer_leaf_roles: set[str],
) -> BoundedElement:
    """Choose the most specific element: smallest area, leaf roles preferred."""
    candidates.sort(key=lambda e: (
        e.area,
        0 if e.role in prefer_leaf_roles else 1,
    ))
    return candidates[0]



_SEM_RE = re.compile(
    r"(?P<act>\w+)\("
    r"(?:"
    r'(?:element|target)="(?P<label>[^"]+)"'
    r"|"
    r"coord=\((?P<cx>\d+),\s*(?P<cy>\d+)\)"
    r")"
)
_COORD_RE = re.compile(r"[xy]=(\d+)")


def parse_semantic(action: str) -> dict:
    """
    Parse a semantic action string into a dict with keys:
      action_type, label (or None), x (or None), y (or None)

    Handles:
      click(element="Submit")
      click(target="File > Print")
      type(element="Search", text="hello")
      tap(element="Buy Now")
      click(coord=(120, 45))
      click(x=120, y=45)
    """
    m = _SEM_RE.search(action)
    if m:
        act = m.group("act")
        label = m.group("label")
        cx = m.group("cx")
        cy = m.group("cy")
        return {
            "action_type": ActionType(act) if act in ActionType._value2member_map_ else ActionType.UNKNOWN,
            "label": label,
            "x": int(cx) if cx else None,
            "y": int(cy) if cy else None,
        }

    coords = _COORD_RE.findall(action)
    if len(coords) >= 2:
        act_match = re.match(r"(\w+)\(", action)
        act = act_match.group(1) if act_match else "click"
        return {
            "action_type": ActionType(act) if act in ActionType._value2member_map_ else ActionType.UNKNOWN,
            "label": None,
            "x": int(coords[0]),
            "y": int(coords[1]),
        }

    return {"action_type": ActionType.UNKNOWN, "label": None, "x": None, "y": None}


def _find_by_label(
    label: str,
    elements: list[BoundedElement],
) -> Optional[BoundedElement]:
    """
    Match a label/path string to the best element.

    Priority:
      1. Exact path match
      2. Case-insensitive path match
      3. Exact label match (ignoring path)
      4. Case-insensitive label match
      5. Substring match on normalised label
    """
    target_norm = _normalise(label)

    exact = [e for e in elements if e.full_path() == label]
    if exact:
        return _best_candidate(exact, _INTERACTABLE_ROLES)

    ci_path = [e for e in elements if e.norm_path() == target_norm]
    if ci_path:
        return _best_candidate(ci_path, _INTERACTABLE_ROLES)

    exact_label = [e for e in elements if e.label == label]
    if exact_label:
        return _best_candidate(exact_label, _INTERACTABLE_ROLES)

    ci_label = [e for e in elements if e.norm_label() == target_norm]
    if ci_label:
        return _best_candidate(ci_label, _INTERACTABLE_ROLES)

    sub = [e for e in elements if target_norm in e.norm_path()
                                  or e.norm_label() in target_norm]
    if sub:
        return _best_candidate(sub, _INTERACTABLE_ROLES)

    return None



_ATSPI_STATE_NS     = "https://accessibility.ubuntu.example.org/ns/state"
_ATSPI_COMPONENT_NS = "https://accessibility.ubuntu.example.org/ns/component"
_ATSPI_ATTR_NS      = "https://accessibility.ubuntu.example.org/ns/attributes"
_ATSPI_ACTION_NS    = "https://accessibility.ubuntu.example.org/ns/action"
_ATSPI_VALUE_NS     = "https://accessibility.ubuntu.example.org/ns/value"

_SCROLL_CONTAINER_ROLES = {"document web", "document frame", "embedded", "scroll pane", "frame"}


def _parse_coord_str(s: str) -> Optional[tuple[int, int]]:
    m = re.match(r"\((\d+),\s*(\d+)\)", s.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def _parse_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _read_action_names(node: ET.Element, action_ns: str) -> set[str]:
    """Collect the distinct act:<name> action names a node exposes, from the
    act:<name>_desc / act:<name>_kb attribute pairs desktop_env/server/main.py
    emits for every AT-SPI action interface a node has."""
    prefix = f"{{{action_ns}}}"
    names: set[str] = set()
    for key in node.attrib:
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix):]
        if rest.endswith("_desc"):
            names.add(rest[: -len("_desc")])
        elif rest.endswith("_kb"):
            names.add(rest[: -len("_kb")])
    return names


_PRIMARY_ACTION_PRIORITY = (
    ActionType.CLICK, ActionType.TYPE, ActionType.RIGHT_CLICK,
    ActionType.DRAG, ActionType.SCROLL,
)


def _resolve_actions_from_names(
    act_names: set[str],
) -> tuple[Optional[ActionType], frozenset[ActionType], dict[ActionType, Optional[Capability]], bool]:
    """Map raw act:<name> names to (primary ActionType, all supported
    ActionTypes, per-type capability hints, skip_entirely).

    skip_entirely is True iff the node's only recognized signal is
    "clickAncestor" (AT-SPI's way of saying "clicking here routes to my
    ancestor's click handler") — such a node must not become a standalone
    CLICK target at its own (usually smaller, wrapper) bbox.
    """
    recognized: dict[ActionType, Optional[Capability]] = {}
    for n in act_names:
        mapped = _ATSPI_ACTION_NAME_MAP.get(n)
        if mapped is not None:
            atype, cap_hint = mapped
            if atype not in recognized or recognized[atype] is None:
                recognized[atype] = cap_hint
    if recognized:
        primary = next((a for a in _PRIMARY_ACTION_PRIORITY if a in recognized), next(iter(recognized)))
        return primary, frozenset(recognized.keys()), recognized, False
    if "clickAncestor" in act_names:
        return None, frozenset(), {}, True
    return None, frozenset(), {}, False


def _classify_atspi_node(
    role: str, editable: bool, act_names: set[str],
) -> tuple[Optional[ActionType], frozenset[ActionType], dict[ActionType, Optional[Capability]], bool]:
    """Combine act:* action-name evidence with the role-based fallback table
    into one (primary ActionType, supported ActionTypes, capability hints,
    skip_entirely) tuple.

    TYPE-role override: for entry/text/spin-button roles, AT-SPI's generic
    act:activate/click/doDefault names mean "focus this field", not "invoke
    like a button" — the ROLE, not the action name, disambiguates this (real
    captures show GTK entries expose only "activate", identical to what a push
    button exposes). TYPE stays primary regardless; any act-name-derived
    actions (e.g. showContextMenu) still contribute as secondary supported
    actions. Used both for the standalone element's classification and for
    hinting a contextual hotkey's (attr:keyshortcuts) capability, so the two
    stay consistent.
    """
    primary, supported, cap_hints, skip_entirely = _resolve_actions_from_names(act_names)
    if skip_entirely:
        return None, frozenset(), {}, True

    role_fallback_act = _ATSPI_ROLE_ACTION.get(role, ActionType.CLICK)
    if role == "text" and not editable:
        role_fallback_act = ActionType.CLICK

    if role_fallback_act == ActionType.TYPE:
        act = ActionType.TYPE
        supported = supported | {ActionType.TYPE}
        cap_hints = dict(cap_hints)
        cap_hints.setdefault(ActionType.TYPE, None)
    elif primary is not None:
        act = primary
    else:
        act = role_fallback_act
        supported = frozenset({act})
        cap_hints = {}
    return act, supported, cap_hints, False


def _descendant_union_bbox(
    node: ET.Element, state_ns: str, component_ns: str,
) -> Optional[tuple[int, int, int, int]]:
    """Union bbox of every showing descendant with valid geometry.

    Fallback signal for scroll-container synthesis, used only when a
    candidate container exposes no act:scroll* action of its own (a11y XML
    has no viewport/scrollTop API, so this can only reliably detect overflow
    past the bottom/right edge — up/left are not inferable this way).
    """
    sn, cn = state_ns, component_ns
    x0 = y0 = x1 = y1 = None
    for child in node.iter():
        if child is node:
            continue
        if child.get(f"{{{sn}}}showing", "false") != "true":
            continue
        coord = _parse_coord_str(child.get(f"{{{cn}}}screencoord", ""))
        size = _parse_coord_str(child.get(f"{{{cn}}}size", ""))
        if not coord or not size or size[0] <= 0 or size[1] <= 0:
            continue
        cx, cy = coord
        cw, ch = size
        x0 = cx if x0 is None else min(x0, cx)
        y0 = cy if y0 is None else min(y0, cy)
        x1 = cx + cw if x1 is None else max(x1, cx + cw)
        y1 = cy + ch if y1 is None else max(y1, cy + ch)
    if x0 is None:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def _atspi_tag_to_role(tag: str) -> str:
    """Convert a Clark-notation or plain lxml tag to a role string.

    Examples: '{ns}push-button' → 'push button',  'menu-item' → 'menu item'
    """
    if tag.startswith("{"):
        tag = tag.split("}", 1)[1]
    return tag.replace("-", " ")


def _first_child_name(node: ET.Element) -> str:
    """Return the first non-empty name found among descendants (depth-first)."""
    for child in node:
        n = (child.get("name") or "").strip()
        if n:
            return n
        deep = _first_child_name(child)
        if deep:
            return deep
    return ""


@dataclass
class _WalkCtx:
    """Provenance context threaded down the AT-SPI tree during collection."""
    app:          str = "os"
    zone:         Zone = Zone.OS
    frame_privileged: bool = False
    frame_name:   Optional[str] = None
    region_id:    Optional[str] = None
    region_role:  Optional[str] = None
    region_index: Optional[int] = None
    cross_origin_embed: bool = False
    app_document: bool = False
    doc_name:     Optional[str] = None
    in_file_chooser: bool = False
    app_document_kind: Optional[str] = None
    region_authors: dict = field(default_factory=dict)
    region_counts:  dict = field(default_factory=dict)
    region_slot_counts: dict = field(default_factory=dict)
    seen_apps:      set = field(default_factory=set)
    seen_zones:     set = field(default_factory=set)
    scroll_candidates: list = field(default_factory=list)
    global_actions:    list = field(default_factory=list)
    document_text:     list = field(default_factory=list)


def _frame_is_privileged(frame_node: ET.Element) -> bool:
    """True if *frame_node* (a top-level browser ``<frame>``) is currently showing an internal,
    non-network-reachable page rather than an arbitrary web page.

    Finds the frame's omnibox entry and reads its live text -- the same field
    desktop_env/evaluators/getters/chrome.py's get_active_url_from_accessTree already reads via
    lxml for a different purpose. An empty/missing omnibox (no browser frame, or a transient
    state) is conservatively untrusted (returns False), matching pre-existing behavior.
    """
    an = _ATSPI_ATTR_NS
    for node in frame_node.iter():
        if _atspi_tag_to_role(node.tag) != "entry":
            continue
        cls = node.get(f"{{{an}}}class")
        name = (node.get("name") or "").strip()
        if cls != _OMNIBOX_CLASS and name != _OMNIBOX_NAME:
            continue
        url = (node.text or "").strip()
        if not url or url.startswith(_UNTRUSTED_CHROME_SCHEME_PREFIXES):
            return False
        return url.startswith(_TRUSTED_URL_SCHEMES)
    return False


def get_active_tab_url(a11y_xml: str, include_internal: bool = False) -> Optional[str]:
    """The URL displayed in the Chrome omnibox anywhere in *a11y_xml*, exactly as shown.

    The omnibox belongs to the SELECTED tab, so this answers "which page is the agent looking at"
    without guessing: the tab strip also marks the active tab with a `selected` state, but the
    omnibox already reflects that choice and carries the address too. Note the URL lives in the
    element's TEXT, not its accessible name -- the name is the static, localisable label
    "Address and search bar", which is why the widget class `OmniboxViewViews` is the primary match.

    Deliberately NOT derived from any element's ``url`` (attr:url / InteractableAction.url -- a
    link's href *target*, e.g. an ad or outbound link on the page): that answers "where would this
    take me", and the question here is "what page is this content rendered on".

    Returned VERBATIM, so it may have no scheme: Chrome elides `https://` (and, on this repo's
    plain-HTTP replicas, `http://`) in the omnibox, and there is no way to tell which from the text
    alone. Callers that need to parse it should add a scheme for parsing only, never record the
    invented one as fact.

    Returns None for internal/privileged pages (chrome://, about:, devtools://, chrome-search://)
    and for a missing or empty omnibox -- there is no ordinary web page to report there.

    ``include_internal`` RELAXES ONLY THE FIRST HALF OF THAT, and exists because "there is no
    ordinary web page here" is the right answer to one question and the wrong answer to another.
    The default answers "which site's content is on screen", which is what a trusted-domain check
    wants (see get_active_tab_hostname): chrome://settings is not a site and must not be compared
    against an episode's allowlist. But the SA agent's masking asks a different question --
    "which stored rule set grades this surface" -- and there the same None is a hazard.
    masking.resolve_content_roots picks a document-web subtree STRUCTURALLY, with no omnibox check,
    so chrome://settings arrives as a web root; a None URL then makes its scope empty, and
    masking._resolve_rules default-denies a web root it cannot name. The result is that the WebUI
    of Settings / Extensions is masked black, while this module's own zone ladder says the opposite
    -- _frame_is_privileged puts that very content in Zone.CHROME, first-party, never masked. The
    two ladders disagreeing about the same pixels is the bug; naming the page is what lets a
    reviewed rule set settle it, rather than each ladder guessing.

    The chrome-search:// / chrome://new-tab-page carve-out is NOT relaxed by the flag. The New Tab
    Page reflects earlier-visited pages' titles in its Most Visited tiles, which is a real if narrow
    attacker-influence path, and default-denying it is the correct treatment -- the same reason
    _frame_is_privileged excludes it from the privileged set.
    """
    if not a11y_xml:
        return None
    try:
        root = ET.fromstring(a11y_xml)
    except ET.ParseError:
        return None
    return omnibox_url_in(root, include_internal=include_internal)


def omnibox_url_in(node: ET.Element, include_internal: bool = False) -> Optional[str]:
    """get_active_tab_url's body, over an already-parsed subtree rather than the whole screen.

    SAME ANSWER, NARROWER QUESTION. Passed the document root it is get_active_tab_url. Passed ONE
    browser window's `frame` element it answers "what is THIS window showing", which is what a
    screen with two browsers needs: the omnibox and the page it describes share that window's frame
    (verified on every corpus capture -- application > frame > ... > tool-bar > entry, and
    application > frame > ... > document-web), so scoping the walk to the frame attributes the URL
    to the right page instead of to whichever window came first in document order. See
    masking._web_root_urls.

    Every scheme rule of get_active_tab_url applies unchanged; see its docstring.
    """
    an = _ATSPI_ATTR_NS
    for el in node.iter():
        if _atspi_tag_to_role(el.tag) != "entry":
            continue
        cls = el.get(f"{{{an}}}class")
        name = (el.get("name") or "").strip()
        if cls != _OMNIBOX_CLASS and name != _OMNIBOX_NAME:
            continue
        url = (el.text or "").strip()
        if not url or url.startswith(_UNTRUSTED_CHROME_SCHEME_PREFIXES):
            return None
        if url.startswith(_TRUSTED_URL_SCHEMES) and not (
                include_internal and url.startswith(_NAMEABLE_INTERNAL_SCHEMES)):
            return None
        return url
    return None


def get_active_tab_hostname(a11y_xml: str) -> Optional[str]:
    """Best-effort current-tab hostname -- get_active_tab_url()'s host component.

    Used by screen_labels.py to decide whether DOCUMENT-zone content sits on an episode-confirmed
    trusted domain, and by the SA agent's masking to decide which rule set applies. Returns None
    wherever get_active_tab_url does, i.e. for internal/privileged pages and a missing omnibox.
    """
    url = get_active_tab_url(a11y_xml)
    if not url:
        return None
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.hostname.lower() if parsed.hostname else None


def _collect_atspi(
    node: ET.Element,
    ancestor_path: list[str],
    out: list[BoundedElement],
    ctx: Optional[_WalkCtx] = None,
) -> None:
    if ctx is None:
        ctx = _WalkCtx()

    role = _atspi_tag_to_role(node.tag)
    name = (node.get("name") or (node.text or "")).strip()

    sn = _ATSPI_STATE_NS
    cn = _ATSPI_COMPONENT_NS
    an = _ATSPI_ATTR_NS
    un = _ATSPI_ACTION_NS
    vn = _ATSPI_VALUE_NS
    url = node.get(f"{{{an}}}url")
    description = (node.get(f"{{{an}}}description") or "").strip() or None
    checked  = (node.get(f"{{{sn}}}checked")  == "true") if f"{{{sn}}}checked"  in node.attrib else None
    selected = (node.get(f"{{{sn}}}selected") == "true") if f"{{{sn}}}selected" in node.attrib else None
    focused  = (node.get(f"{{{sn}}}focused")  == "true") if f"{{{sn}}}focused"  in node.attrib else None
    has_popup = (node.get(f"{{{an}}}haspopup") or "").strip() or None
    if has_popup == "false":
        has_popup = None
    role_description = (node.get(f"{{{an}}}roledescription") or "").strip() or None

    child_ctx = ctx
    if role == "application":
        app = name or "os"
        zone = Zone.CHROME if app in _BROWSER_APPS else Zone.OS
        child_ctx = replace(ctx, app=app, zone=zone)
    elif role == "frame":
        frame_privileged = _frame_is_privileged(node) if ctx.zone == Zone.CHROME else ctx.frame_privileged
        child_ctx = replace(ctx, frame_name=name or None, frame_privileged=frame_privileged)
    elif role in _DOC_ROLES and ctx.zone in (Zone.CHROME, Zone.OS) and not ctx.frame_privileged:
        child_ctx = replace(ctx, zone=Zone.DOCUMENT,
                            app_document=(ctx.zone == Zone.OS),
                            doc_name=(name or None) if ctx.zone == Zone.OS else ctx.doc_name,
                            region_id=None, region_role=None, region_index=None)
    elif role in _DOC_ROLES and ctx.zone == Zone.DOCUMENT:
        child_ctx = replace(ctx, cross_origin_embed=True)
    elif role == _nr.FILE_CHOOSER_ROLE:
        child_ctx = replace(ctx, in_file_chooser=True)
    elif (ctx.zone == Zone.OS and not ctx.frame_privileged
          and (_cross_kind := _nr.content_crossing_kind(
              ctx.app, role, in_file_chooser=ctx.in_file_chooser))):
        child_ctx = replace(ctx, zone=Zone.DOCUMENT, app_document=True,
                            app_document_kind=_cross_kind,
                            region_id=None, region_role=None, region_index=None)
    elif role in _REGION_ROLES and ctx.zone == Zone.DOCUMENT:
        idx = ctx.region_counts.get(role, 0)
        ctx.region_counts[role] = idx + 1
        rid = f"{role}#{idx}@{node.get(f'{{{cn}}}screencoord', '')}"
        child_ctx = replace(ctx, region_id=rid, region_role=role, region_index=idx)

    child_ctx.seen_apps.add(child_ctx.app)
    child_ctx.seen_zones.add(child_ctx.zone)

    if child_ctx.zone == Zone.DOCUMENT and role == "link" and url:
        m = _USER_HREF.search(url)
        if not m:
            if "author-link" in (node.get(f"{{{an}}}class") or "").split():
                m = _GITLAB_AUTHOR_HREF.search(url)
        if m and child_ctx.region_id:
            child_ctx.region_authors.setdefault(child_ctx.region_id, set()).add(m.group(1).lower())

    showing  = node.get(f"{{{sn}}}showing",  "false") == "true"
    visible  = node.get(f"{{{sn}}}visible",  "false") == "true"
    enabled  = node.get(f"{{{sn}}}enabled",  "false") == "true"
    editable = node.get(f"{{{sn}}}editable", "false") == "true"

    new_path = ancestor_path.copy()
    if name and role in _PATH_ROLES:
        new_path = new_path + [name]

    act_names = _read_action_names(node, un)
    value = _parse_float(node.get(f"{{{vn}}}value"))
    value_min = _parse_float(node.get(f"{{{vn}}}min"))
    value_max = _parse_float(node.get(f"{{{vn}}}max"))
    value_step = _parse_float(node.get(f"{{{vn}}}step"))
    value_range = (
        (value_min, value_max, value_step)
        if value_min is not None and value_max is not None and value_step is not None
        else None
    )
    keyshortcut = node.get(f"{{{an}}}keyshortcuts")

    if showing and visible and keyshortcut:
        keys = tuple(k.strip().lower() for k in keyshortcut.split("+") if k.strip())
        if keys:
            elem_ref_path = new_path + ([name] if name and name not in new_path else [])
            hint_act, _hint_supported, hint_cap_hints, _hint_skip = _classify_atspi_node(
                role, editable, act_names)
            if hint_act is not None:
                hint_cap = hint_cap_hints.get(hint_act) or _capability_of(
                    role, hint_act, child_ctx.zone, child_ctx.app, url)
            else:
                hint_cap = _capability_of(role, ActionType.CLICK, child_ctx.zone, child_ctx.app, url)
            child_ctx.global_actions.append(GlobalAction(
                keys=keys,
                description=f"Activate {name or role}",
                capability=hint_cap,
                zone=child_ctx.zone,
                scope="element",
                element_ref=" > ".join(p for p in elem_ref_path if p) or (name or role),
                app=child_ctx.app,
                region_id=child_ctx.region_id,
                label_taint=(LabelTaint.TAINTED if child_ctx.zone == Zone.DOCUMENT else LabelTaint.TRUSTED),
            ))

    if showing and visible:
        coord = _parse_coord_str(node.get(f"{{{cn}}}screencoord", ""))
        size  = _parse_coord_str(node.get(f"{{{cn}}}size", ""))

        if coord and size and size[0] > 0 and size[1] > 0:
            if role in _SCROLL_CONTAINER_ROLES:
                scroll_dirs = {
                    _SCROLL_ACTION_DIRECTION[n] for n in act_names if n in _SCROLL_ACTION_DIRECTION
                }
                confidence = "atspi-action"
                if not scroll_dirs:
                    union = _descendant_union_bbox(node, sn, cn)
                    if union is not None:
                        ux, uy, uw, uh = union
                        margin = 8
                        if ux + uw > coord[0] + size[0] + margin:
                            scroll_dirs.add("right")
                        if uy + uh > coord[1] + size[1] + margin:
                            scroll_dirs.add("down")
                        confidence = "geometry-heuristic"
                if scroll_dirs:
                    child_ctx.scroll_candidates.append({
                        "bbox": (coord[0], coord[1], size[0], size[1]),
                        "role": role,
                        "path": new_path + ([name] if name and name not in new_path else []),
                        "label": name or role,
                        "zone": child_ctx.zone,
                        "app": child_ctx.app,
                        "frame_name": child_ctx.frame_name,
                        "region_id": child_ctx.region_id,
                        "region_role": child_ctx.region_role,
                        "region_index": child_ctx.region_index,
                        "cross_origin_embed": child_ctx.cross_origin_embed,
                        "app_document": child_ctx.app_document,
                        "app_document_kind": child_ctx.app_document_kind,
                        "doc_name": child_ctx.doc_name,
                        "label_taint": (LabelTaint.TAINTED if child_ctx.zone == Zone.DOCUMENT else LabelTaint.TRUSTED),
                        "directions": frozenset(scroll_dirs),
                        "confidence": confidence,
                    })

            if (child_ctx.zone == Zone.DOCUMENT
                    and role in _TEXT_CONTENT_ROLES
                    and len(name) >= _MIN_CONTENT_TEXT_LEN
                    and not ((enabled or editable) and role in _INTERACTABLE_ROLES)):
                child_ctx.document_text.append(DocumentTextNode(
                    label=name,
                    role=role,
                    bbox=(coord[0], coord[1], size[0], size[1]),
                    zone=child_ctx.zone,
                    app=child_ctx.app,
                    frame_name=child_ctx.frame_name,
                    region_id=child_ctx.region_id,
                    region_role=child_ctx.region_role,
                    cross_origin_embed=child_ctx.cross_origin_embed,
                    app_document=child_ctx.app_document,
                    app_document_kind=child_ctx.app_document_kind,
                    doc_name=child_ctx.doc_name,
                ))

            if (enabled or editable) and role in _INTERACTABLE_ROLES:
                act, supported, cap_hints, skip_entirely = _classify_atspi_node(role, editable, act_names)
                if not skip_entirely:
                    if not name:
                        name = _first_child_name(node)
                    if not name:
                        name = (node.get(f"{{{an}}}class") or node.get("class") or "").strip()
                    if not name and node.get(f"{{{an}}}toolkit") == "clutter":
                        for child in node:
                            _collect_atspi(child, new_path, out, child_ctx)
                        return

                    elem_path = new_path + ([name] if name and name not in new_path else [])

                    region_slot_index = None
                    if child_ctx.region_id:
                        region_slot_index = child_ctx.region_slot_counts.get(child_ctx.region_id, 0)
                        child_ctx.region_slot_counts[child_ctx.region_id] = region_slot_index + 1

                    resolved_caps: dict[ActionType, Capability] = {}
                    for atype in supported:
                        hint = cap_hints.get(atype)
                        resolved_caps[atype] = hint if hint is not None else _capability_of(
                            role, atype, child_ctx.zone, child_ctx.app, url)
                    primary_capability = resolved_caps.get(
                        act, _capability_of(role, act, child_ctx.zone, child_ctx.app, url))

                    out.append(BoundedElement(
                        label=name,
                        role=role,
                        x=coord[0],
                        y=coord[1],
                        w=size[0],
                        h=size[1],
                        path=elem_path,
                        extra={
                            "action_type": act,
                            "url": url,
                            "app": child_ctx.app,
                            "frame_name": child_ctx.frame_name,
                            "zone": child_ctx.zone,
                            "region_id": child_ctx.region_id,
                            "region_role": child_ctx.region_role,
                            "region_index": child_ctx.region_index,
                            "region_slot_index": region_slot_index,
                            "cross_origin_embed": child_ctx.cross_origin_embed,
                            "app_document": child_ctx.app_document,
                            "app_document_kind": child_ctx.app_document_kind,
                            "doc_name": child_ctx.doc_name,
                            "capability": primary_capability,
                            "label_taint": (LabelTaint.TAINTED
                                            if child_ctx.zone == Zone.DOCUMENT
                                            else LabelTaint.TRUSTED),
                            "supported_actions": supported,
                            "action_capabilities": resolved_caps,
                            "raw_action_names": tuple(sorted(act_names)),
                            "value": value,
                            "value_range": value_range,
                            "keyshortcut": keyshortcut,
                            "description": description,
                            "checked": checked,
                            "selected": selected,
                            "focused": focused,
                            "has_popup": has_popup,
                            "role_description": role_description,
                        },
                    ))

    for child in node:
        _collect_atspi(child, new_path, out, child_ctx)



def resolve_atspi(x: int, y: int, a11y_xml: str) -> str:
    """Coordinate → semantic string  (AT-SPI2)."""
    elements = _atspi_elements(a11y_xml)
    candidates = [e for e in elements if e.contains(x, y)]
    if not candidates:
        return f"click(coord=({x}, {y}))  # no element found"
    best = _best_candidate(candidates, _INTERACTABLE_ROLES)
    act: ActionType = best.extra.get("action_type", ActionType.CLICK)
    return _build_semantic(act, best.full_path())



def extract_actions_atspi(a11y_xml: str) -> list[InteractableAction]:
    """Extract all interactable actions from an AT-SPI2 XML snapshot."""
    elements = _atspi_elements(a11y_xml)
    return _elements_to_actions(elements)


def extract_all_atspi(a11y_xml: str) -> tuple[list[InteractableAction], list["GlobalAction"]]:
    """Extract both the interactable-action list and the bbox-free global
    (hotkey) action list from one AT-SPI2 XML snapshot in a single tree walk —
    for callers that want both without parsing the tree twice (unlike calling
    extract_actions_atspi + extract_global_actions_atspi separately)."""
    elements, global_actions, _text = _extract_atspi_full(a11y_xml)
    return _elements_to_actions(elements), global_actions


def extract_all_atspi_with_content(
    a11y_xml: str,
) -> tuple[list[InteractableAction], list["GlobalAction"], list[DocumentTextNode]]:
    """``extract_all_atspi`` plus the inert Zone.DOCUMENT text nodes, from the same single walk.

    Separate entry point rather than a third element on ``extract_all_atspi``: that function's
    two-tuple is unpacked by callers outside this module, and widening it would break them for the
    sake of a value only the labeller wants.
    """
    elements, global_actions, document_text = _extract_atspi_full(a11y_xml)
    return _elements_to_actions(elements), global_actions, document_text



def semantic_to_coordinate_atspi(
    semantic: str,
    a11y_xml: str,
) -> Optional[CoordinateAction]:
    """Semantic action → CoordinateAction  (AT-SPI2)."""
    parsed = parse_semantic(semantic)
    elements = _atspi_elements(a11y_xml)

    if parsed["x"] is not None and parsed["label"] is None:
        return CoordinateAction(
            action_type=parsed["action_type"],
            x=parsed["x"], y=parsed["y"],
            label="", path="",
            bbox=(parsed["x"], parsed["y"], 0, 0),
        )

    elem = _find_by_label(parsed["label"] or "", elements)
    if elem is None:
        return None

    act = elem.extra.get("action_type", parsed["action_type"])
    return CoordinateAction(
        action_type=act,
        x=elem.x, y=elem.y,
        label=elem.label,
        path=elem.full_path(),
        bbox=elem.bbox,
    )


def _dedupe_nested(elements: list[BoundedElement]) -> list[BoundedElement]:
    """Drop wrapper/leaf redundancy.

    A container and its sole interactive descendant can both be interactable
    roles exposing the same action at the same spot — e.g. Reddit wraps every
    "Reply" link in a same-named ``list-item``. Keep the most specific (smallest)
    element, which is the real affordance (the link, classified NAVIGATE), and
    drop the larger wrapper (the list-item, classified UNKNOWN).
    """
    kept: list[BoundedElement] = []
    for e in sorted(elements, key=lambda el: el.area):
        lbl = e.norm_label()
        act = e.extra.get("action_type")
        direction = e.extra.get("direction")
        if lbl and any(
            k.extra.get("action_type") == act
            and k.extra.get("direction") == direction
            and k.norm_label() == lbl
            and e.contains(k.x_center, k.y_center)
            for k in kept
        ):
            continue
        kept.append(e)
    order = {id(e): i for i, e in enumerate(elements)}
    kept.sort(key=lambda e: order[id(e)])
    return kept


def _dedupe_scroll_candidates(candidates: list[dict]) -> list[dict]:
    """Drop a geometry-heuristic candidate nested inside another candidate's
    bbox that already offers the same direction (outermost wins — scrolling
    the outer viewport is what an agent hovering anywhere in the region
    actually triggers). Two independent act:scroll* signals are never
    collapsed, regardless of nesting."""

    def _contains(outer: dict, inner: dict) -> bool:
        ox, oy, ow, oh = outer["bbox"]
        ix, iy, iw, ih = inner["bbox"]
        return ox <= ix and oy <= iy and ix + iw <= ox + ow and iy + ih <= oy + oh

    kept: list[dict] = []
    for c in sorted(candidates, key=lambda c: c["bbox"][2] * c["bbox"][3], reverse=True):
        if c["confidence"] == "geometry-heuristic" and any(
            k is not c and _contains(k, c) and (k["directions"] & c["directions"])
            for k in kept
        ):
            continue
        kept.append(c)
    order = {id(c): i for i, c in enumerate(candidates)}
    kept.sort(key=lambda c: order[id(c)])
    return kept


def _scroll_candidates_to_elements(candidates: list[dict]) -> list[BoundedElement]:
    out: list[BoundedElement] = []
    for c in _dedupe_scroll_candidates(candidates):
        x, y, w, h = c["bbox"]
        for direction in sorted(c["directions"]):
            out.append(BoundedElement(
                label=c["label"],
                role=c["role"],
                x=x, y=y, w=w, h=h,
                path=c["path"],
                extra={
                    "action_type": ActionType.SCROLL,
                    "direction": direction,
                    "url": None,
                    "app": c["app"],
                    "zone": c["zone"],
                    "region_id": c["region_id"],
                    "region_role": c["region_role"],
                    "region_index": c["region_index"],
                    "cross_origin_embed": c["cross_origin_embed"],
                    "app_document": c["app_document"],
                    "app_document_kind": c.get("app_document_kind"),
                    "doc_name": c.get("doc_name"),
                    "capability": Capability.READ,
                    "label_taint": c["label_taint"],
                    "supported_actions": frozenset({ActionType.SCROLL}),
                    "action_capabilities": {ActionType.SCROLL: Capability.READ},
                    "scroll_confidence": c["confidence"],
                },
            ))
    return out


def _extract_atspi_full(
    a11y_xml: str,
) -> tuple[list[BoundedElement], list["GlobalAction"], list[DocumentTextNode]]:
    """Single tree walk producing the element list (Direction B's usual output, now including
    synthesized SCROLL affordances), the bbox-free global/hotkey action list, and the inert
    Zone.DOCUMENT text nodes. extract_actions_atspi/extract_global_actions_atspi are thin
    wrappers over this so a caller wanting several of them doesn't re-parse."""
    try:
        root = ET.fromstring(a11y_xml)
    except ET.ParseError:
        return [], [], []
    out: list[BoundedElement] = []
    ctx = _WalkCtx()
    _collect_atspi(root, [], out, ctx)
    for e in out:
        rid = e.extra.get("region_id")
        if rid:
            authors = ctx.region_authors.get(rid)
            e.extra["author"] = next(iter(authors)) if authors and len(authors) == 1 else None
    for t in ctx.document_text:
        if t.region_id:
            authors = ctx.region_authors.get(t.region_id)
            t.author = next(iter(authors)) if authors and len(authors) == 1 else None

    slot_groups: dict[tuple[str, int], list[str]] = {}
    for e in out:
        role_r = e.extra.get("region_role")
        slot = e.extra.get("region_slot_index")
        if role_r is not None and slot is not None:
            slot_groups.setdefault((role_r, slot), []).append(e.label or "")
    for e in out:
        role_r = e.extra.get("region_role")
        slot = e.extra.get("region_slot_index")
        group = slot_groups.get((role_r, slot)) if role_r is not None and slot is not None else None
        e.extra["region_value_shape"] = (
            _classify_value_shape(e.label or "", group) if group and len(group) >= _MIN_INSTANCES_FOR_VALUE_SHAPE
            else None
        )

    out.extend(_scroll_candidates_to_elements(ctx.scroll_candidates))
    elements = _dedupe_nested(out)

    global_actions: list[GlobalAction] = list(ctx.global_actions)
    global_actions.append(GlobalAction(
        keys=("escape",), description="Cancel / close dialog",
        capability=Capability.READ, zone=Zone.OS, scope="global", app="os",
    ))
    for app_name in sorted(ctx.seen_apps):
        if app_name in _BROWSER_APPS:
            for keys, desc, cap in _CURATED_HOTKEYS["browser"]:
                global_actions.append(GlobalAction(
                    keys=keys, description=desc, capability=cap,
                    zone=Zone.CHROME, scope="app", app=app_name,
                ))
        if app_name in _TERMINAL_APPS:
            for keys, desc, cap in _CURATED_HOTKEYS["terminal"]:
                global_actions.append(GlobalAction(
                    keys=keys, description=desc, capability=cap,
                    zone=Zone.OS, scope="app", app=app_name,
                ))
    return elements, global_actions, ctx.document_text


def _atspi_elements(a11y_xml: str) -> list[BoundedElement]:
    elements, _globals, _text = _extract_atspi_full(a11y_xml)
    return elements


def extract_global_actions_atspi(a11y_xml: str) -> list["GlobalAction"]:
    """Extract every keyboard-only (bbox-free) action from an AT-SPI2 XML
    snapshot: per-element attr:keyshortcuts plus a small curated set of
    app-level hotkeys for apps actually observed in the snapshot."""
    _elements, global_actions, _text = _extract_atspi_full(a11y_xml)
    return global_actions



_AX_LINE_RE = re.compile(
    r"\[(?P<bid>\d+)\]\s+"
    r"(?P<role>\S+)\s+"
    r"'(?P<label>[^']*)'"
    r".*?coord=\((?P<x>\d+),\s*(?P<y>\d+)\)"
    r".*?size=\((?P<w>\d+),\s*(?P<h>\d+)\)",
    re.DOTALL,
)

_WEB_ROLE_ACTION: dict[str, ActionType] = {
    "link":       ActionType.CLICK,
    "button":     ActionType.CLICK,
    "checkbox":   ActionType.CLICK,
    "radio":      ActionType.CLICK,
    "combobox":   ActionType.CLICK,
    "option":     ActionType.CLICK,
    "menuitem":   ActionType.CLICK,
    "tab":        ActionType.CLICK,
    "textbox":    ActionType.TYPE,
    "searchbox":  ActionType.TYPE,
    "spinbutton": ActionType.TYPE,
    "slider":     ActionType.SCROLL,
    "scrollbar":  ActionType.SCROLL,
}

_WEB_INTERACTABLE = set(_WEB_ROLE_ACTION.keys())


def _axtree_elements(axtree_text: str) -> list[BoundedElement]:
    out = []
    for m in _AX_LINE_RE.finditer(axtree_text):
        role  = m.group("role").lower()
        label = m.group("label").strip()
        x, y  = int(m.group("x")), int(m.group("y"))
        w, h  = int(m.group("w")), int(m.group("h"))
        bid   = m.group("bid")

        if role not in _WEB_INTERACTABLE or not label:
            continue
        if w == 0 or h == 0:
            continue

        out.append(BoundedElement(
            label=label,
            role=role,
            x=x, y=y, w=w, h=h,
            path=[label],
            extra={"bid": bid, "action_type": _WEB_ROLE_ACTION[role]},
        ))
    return out



def resolve_axtree(x: int, y: int, axtree_text: str) -> str:
    elements = _axtree_elements(axtree_text)
    candidates = [e for e in elements if e.contains(x, y)]
    if not candidates:
        return f"click(coord=({x}, {y}))  # no element found"
    best = _best_candidate(candidates, _WEB_INTERACTABLE)
    act: ActionType = best.extra.get("action_type", ActionType.CLICK)
    return _build_semantic(act, best.full_path())



def extract_actions_axtree(axtree_text: str) -> list[InteractableAction]:
    return _elements_to_actions(_axtree_elements(axtree_text))



def semantic_to_coordinate_axtree(
    semantic: str,
    axtree_text: str,
) -> Optional[CoordinateAction]:
    parsed = parse_semantic(semantic)
    if parsed["x"] is not None and parsed["label"] is None:
        return CoordinateAction(
            action_type=parsed["action_type"],
            x=parsed["x"], y=parsed["y"],
            label="", path="",
            bbox=(parsed["x"], parsed["y"], 0, 0),
        )
    elements = _axtree_elements(axtree_text)
    elem = _find_by_label(parsed["label"] or "", elements)
    if elem is None:
        return None
    act = elem.extra.get("action_type", parsed["action_type"])
    return CoordinateAction(
        action_type=act,
        x=elem.x, y=elem.y,
        label=elem.label,
        path=elem.full_path(),
        bbox=elem.bbox,
    )



_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _android_elements(hierarchy_xml: str) -> list[BoundedElement]:
    try:
        root = ET.fromstring(hierarchy_xml)
    except ET.ParseError:
        return []

    out = []
    for node in root.iter("node"):
        bounds = node.get("bounds", "")
        m = _BOUNDS_RE.match(bounds)
        if not m:
            continue

        x1, y1, x2, y2 = (int(m.group(i)) for i in range(1, 5))
        w, h = x2 - x1, y2 - y1
        if w <= 0 or h <= 0:
            continue

        cls       = (node.get("class") or "").split(".")[-1]
        clickable = node.get("clickable", "false") == "true"
        enabled   = node.get("enabled", "true") == "true"
        text      = (node.get("text") or "").strip()
        desc      = (node.get("content-desc") or "").strip()
        res_id    = (node.get("resource-id") or "").split("/")[-1]
        label     = text or desc or res_id

        if not label or not enabled:
            continue

        act = _ANDROID_CLASS_ACTION.get(cls, ActionType.TAP)
        if cls not in _ANDROID_CLASS_ACTION and clickable:
            act = ActionType.TAP

        if not clickable and act == ActionType.TAP:
            continue

        out.append(BoundedElement(
            label=label,
            role=cls,
            x=x1, y=y1, w=w, h=h,
            path=[label],
            extra={"action_type": act, "resource_id": res_id},
        ))
    return out



def resolve_android(x: int, y: int, hierarchy_xml: str) -> str:
    elements = _android_elements(hierarchy_xml)
    candidates = [e for e in elements if e.contains(x, y)]
    if not candidates:
        return f"tap(coord=({x}, {y}))  # no element found"
    best = _best_candidate(candidates, set(_ANDROID_CLASS_ACTION.keys()))
    act: ActionType = best.extra.get("action_type", ActionType.TAP)
    return _build_semantic(act, best.full_path())



def extract_actions_android(hierarchy_xml: str) -> list[InteractableAction]:
    return _elements_to_actions(_android_elements(hierarchy_xml))



def semantic_to_coordinate_android(
    semantic: str,
    hierarchy_xml: str,
) -> Optional[CoordinateAction]:
    parsed = parse_semantic(semantic)
    if parsed["x"] is not None and parsed["label"] is None:
        return CoordinateAction(
            action_type=parsed["action_type"],
            x=parsed["x"], y=parsed["y"],
            label="", path="",
            bbox=(parsed["x"], parsed["y"], 0, 0),
        )
    elements = _android_elements(hierarchy_xml)
    elem = _find_by_label(parsed["label"] or "", elements)
    if elem is None:
        return None
    act = elem.extra.get("action_type", parsed["action_type"])
    return CoordinateAction(
        action_type=act,
        x=elem.x, y=elem.y,
        label=elem.label,
        path=elem.full_path(),
        bbox=elem.bbox,
    )



def _elements_to_actions(elements: list[BoundedElement]) -> list[InteractableAction]:
    """Convert a list of BoundedElements into InteractableAction objects."""
    actions = []
    for elem in elements:
        act: ActionType = elem.extra.get("action_type", ActionType.CLICK)
        path = elem.full_path()
        semantic   = _build_semantic(act, path)
        coordinate = _build_coordinate(act, elem)

        description = elem.extra.get("description")
        if description and description.strip().lower() == (elem.label or "").strip().lower():
            description = None

        role_description = elem.extra.get("role_description")
        if role_description and role_description.strip().lower() == (elem.role or "").strip().lower():
            role_description = None

        actions.append(InteractableAction(
            semantic=semantic,
            coordinate=coordinate,
            action_type=act,
            label=elem.label,
            path=path,
            x_center=elem.x_center,
            y_center=elem.y_center,
            bbox=elem.bbox,
            role=elem.role,
            url=elem.extra.get("url"),
            app=elem.extra.get("app", "os"),
            frame_name=elem.extra.get("frame_name"),
            zone=elem.extra.get("zone", Zone.OS),
            region_id=elem.extra.get("region_id"),
            region_role=elem.extra.get("region_role"),
            region_index=elem.extra.get("region_index"),
            region_slot_index=elem.extra.get("region_slot_index"),
            region_value_shape=elem.extra.get("region_value_shape"),
            cross_origin_embed=elem.extra.get("cross_origin_embed", False),
            app_document=elem.extra.get("app_document", False),
            app_document_kind=elem.extra.get("app_document_kind"),
            doc_name=elem.extra.get("doc_name"),
            author=elem.extra.get("author"),
            capability=elem.extra.get("capability", Capability.UNKNOWN),
            label_taint=elem.extra.get("label_taint", LabelTaint.TRUSTED),
            supported_actions=elem.extra.get("supported_actions") or frozenset({act}),
            action_capabilities=elem.extra.get("action_capabilities") or {},
            direction=elem.extra.get("direction"),
            value=elem.extra.get("value"),
            value_range=elem.extra.get("value_range"),
            scroll_confidence=elem.extra.get("scroll_confidence"),
            description=description,
            checked=elem.extra.get("checked"),
            selected=elem.extra.get("selected"),
            focused=elem.extra.get("focused"),
            has_popup=elem.extra.get("has_popup"),
            role_description=role_description,
        ))
    return actions



class ActionBridge:
    """
    Unified interface for all three environments.

    Usage:
        bridge = ActionBridge("atspi", a11y_xml)

        # Direction A: coordinate → semantic
        bridge.to_semantic(120, 45)
        → 'click(target="File > Print")'

        # Direction B: tree → full action list
        bridge.all_actions()
        → [InteractableAction(...), ...]

        # Direction C: semantic → coordinate
        bridge.to_coordinate('click(target="File > Print")')
        → CoordinateAction(action_type=<click>, x=60, y=211 ...)
        → .to_string()  →  'click(x=60, y=211)'
    """

    _ENVS = {"atspi", "axtree", "android"}

    def __init__(self, env: str, snapshot: str):
        if env not in self._ENVS:
            raise ValueError(f"env must be one of {self._ENVS!r}")
        self._env      = env
        self._snapshot = snapshot
        self._resolve   = {
            "atspi":   resolve_atspi,
            "axtree":  resolve_axtree,
            "android": resolve_android,
        }[env]
        self._extract   = {
            "atspi":   extract_actions_atspi,
            "axtree":  extract_actions_axtree,
            "android": extract_actions_android,
        }[env]
        self._sem2coord = {
            "atspi":   semantic_to_coordinate_atspi,
            "axtree":  semantic_to_coordinate_axtree,
            "android": semantic_to_coordinate_android,
        }[env]

    def to_semantic(self, x: int, y: int) -> str:
        """Direction A: coordinate → semantic string."""
        return self._resolve(x, y, self._snapshot)

    def global_actions(self) -> list["GlobalAction"]:
        """Bbox-free keyboard-only actions (hotkeys). Only implemented for the
        atspi environment this pass; axtree/android return an empty list."""
        if self._env != "atspi":
            return []
        return extract_global_actions_atspi(self._snapshot)

    def all_actions(self) -> list[InteractableAction]:
        """Direction B: snapshot → list of all interactable actions."""
        return self._extract(self._snapshot)

    def to_coordinate(self, semantic: str) -> Optional[CoordinateAction]:
        """Direction C: semantic string → CoordinateAction."""
        return self._sem2coord(semantic, self._snapshot)

    def round_trip(self, x: int, y: int) -> dict:
        """
        Full round-trip: coord → semantic → coord.
        Useful for validating that the conversion is lossless.
        """
        sem  = self.to_semantic(x, y)
        back = self.to_coordinate(sem)
        return {
            "input_coord": (x, y),
            "semantic":    sem,
            "resolved":    back.to_string() if back else None,
            "resolved_center": (back.x_center, back.y_center) if back else None,
        }

    def update_snapshot(self, new_snapshot: str) -> None:
        """Replace the snapshot (e.g. after a state transition)."""
        self._snapshot = new_snapshot



async def resolve_playwright(x: int, y: int, page) -> str:
    """Direction A for live browser via Playwright (async)."""
    info = await page.evaluate(
        """([px, py]) => {
            const el = document.elementFromPoint(px, py);
            if (!el) return null;
            let node = el;
            while (node && node !== document.body) {
                const label =
                    node.getAttribute('aria-label') ||
                    node.getAttribute('title') ||
                    node.getAttribute('alt') ||
                    node.getAttribute('placeholder') ||
                    (node.innerText || '').trim().slice(0, 60) ||
                    node.getAttribute('name') || node.id;
                if (label) return {
                    label, tag: node.tagName.toLowerCase(),
                    role: node.getAttribute('role') || '',
                    type: node.getAttribute('type') || '',
                };
                node = node.parentElement;
            }
            return {label: null, tag: el.tagName.toLowerCase(), role:'', type:''};
        }""",
        [x, y],
    )
    if not info or not info.get("label"):
        return f"click(coord=({x}, {y}))  # unlabelled element"
    label = re.sub(r"\s+", " ", info["label"]).strip()
    action = "type" if info.get("type") in ("text","email","password","search") else "click"
    return f'{action}(element="{label}")'


async def extract_actions_playwright(page) -> list[InteractableAction]:
    """Direction B for live browser — queries interactable elements via CDP."""
    rows = await page.evaluate("""
        () => {
            const roles = new Set(['button','link','checkbox','radio','combobox',
                                   'option','menuitem','tab','textbox','searchbox']);
            const out = [];
            document.querySelectorAll(
                'button, a, input, select, textarea, [role], [tabindex]'
            ).forEach(el => {
                const r = (el.getAttribute('role') ||
                           el.tagName.toLowerCase()).toLowerCase();
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return;
                const label =
                    el.getAttribute('aria-label') ||
                    el.getAttribute('title') ||
                    el.getAttribute('alt') ||
                    el.getAttribute('placeholder') ||
                    (el.innerText || '').trim().slice(0, 60) ||
                    el.getAttribute('name') || el.id;
                if (!label) return;
                out.push({
                    role: r, label,
                    x: Math.round(rect.left), y: Math.round(rect.top),
                    w: Math.round(rect.width), h: Math.round(rect.height),
                    type: el.getAttribute('type') || '',
                });
            });
            return out;
        }
    """)
    actions = []
    for row in rows:
        role  = row["role"]
        label = row["label"]
        x, y, w, h = row["x"], row["y"], row["w"], row["h"]
        act = ActionType.TYPE if row.get("type") in ("text","email","password","search") else ActionType.CLICK
        elem = BoundedElement(label=label, role=role, x=x, y=y, w=w, h=h,
                              path=[label], extra={"action_type": act})
        path     = elem.full_path()
        semantic = _build_semantic(act, path)
        coord    = _build_coordinate(act, elem)
        actions.append(InteractableAction(
            semantic=semantic, coordinate=coord, action_type=act,
            label=label, path=path,
            x_center=elem.x_center, y_center=elem.y_center,
            bbox=elem.bbox, role=role,
        ))
    return actions



SAMPLE_ATSPI = """\
<desktop-frame name="Desktop"
    xmlns:st="https://accessibility.ubuntu.example.org/ns/state"
    xmlns:cp="https://accessibility.ubuntu.example.org/ns/component"
    xmlns:attr="https://accessibility.ubuntu.example.org/ns/attributes"
    xmlns:act="https://accessibility.ubuntu.example.org/ns/action"
    xmlns:val="https://accessibility.ubuntu.example.org/ns/value">
  <frame name="LibreOffice Writer"
      st:enabled="true" st:visible="true" st:showing="true"
      cp:screencoord="(0, 0)" cp:size="(1920, 1080)">
    <menu-bar name=""
        st:enabled="true" st:visible="true" st:showing="true"
        cp:screencoord="(0, 24)" cp:size="(1920, 24)">
      <menu name="File"
          st:enabled="true" st:visible="true" st:showing="true"
          cp:screencoord="(0, 24)" cp:size="(40, 24)">
        <menu-item name="Print"
            st:enabled="true" st:visible="true" st:showing="true"
            cp:screencoord="(0, 200)" cp:size="(120, 22)"/>
        <menu-item name="Save"
            st:enabled="true" st:visible="true" st:showing="true"
            cp:screencoord="(0, 178)" cp:size="(120, 22)"/>
      </menu>
      <menu name="Edit"
          st:enabled="true" st:visible="true" st:showing="true"
          cp:screencoord="(42, 24)" cp:size="(40, 24)"/>
    </menu-bar>
    <push-button name="Close"
        st:enabled="true" st:visible="true" st:showing="true"
        cp:screencoord="(1880, 0)" cp:size="(40, 24)"/>
    <entry name="Search"
        st:enabled="true" st:visible="true" st:showing="true" st:editable="true"
        attr:keyshortcuts="Ctrl+K"
        act:activate_desc="" act:activate_kb=""
        act:showContextMenu_desc="" act:showContextMenu_kb=""
        cp:screencoord="(200, 600)" cp:size="(300, 28)"/>
    <document-web name="Untitled document"
        st:enabled="true" st:visible="true" st:showing="true"
        act:scrollUp_desc="" act:scrollUp_kb=""
        act:scrollDown_desc="" act:scrollDown_kb=""
        cp:screencoord="(0, 48)" cp:size="(1920, 1000)"/>
  </frame>
</desktop-frame>
"""

SAMPLE_ANDROID = """\
<hierarchy>
  <node class="android.widget.FrameLayout" bounds="[0,0][1080,2340]">
    <node class="android.widget.Button" text="Buy Now"
          clickable="true" enabled="true" bounds="[200,900][880,980]"/>
    <node class="android.widget.EditText" text=""
          content-desc="Search products"
          clickable="true" enabled="true" bounds="[20,60][1060,130]"/>
    <node class="android.widget.TextView" text="Product Title"
          clickable="false" enabled="true" bounds="[0,100][1080,200]"/>
  </node>
</hierarchy>
"""

SAMPLE_AXTREE = """\
[1] link 'Home'    focused: False coord=(10, 10) size=(50, 20)
[2] link 'Sign in' focused: False coord=(900, 10) size=(60, 20)
[3] textbox 'Search' focused: True  coord=(200, 50) size=(400, 30)
[4] button 'Go'    focused: False coord=(610, 50) size=(60, 30)
"""



if __name__ == "__main__":
    bridge = ActionBridge("atspi", SAMPLE_ATSPI)

    print("=== Direction A: Coordinate → Semantic ===")
    print(bridge.to_semantic(60, 210))
    print(bridge.to_semantic(1890, 10))
    print(bridge.to_semantic(250, 614))

    print("\n=== Direction B: Tree → Action List ===")
    for a in bridge.all_actions():
        print(f"  {a.semantic:<40}  {a.coordinate}")

    print("\n=== Direction C: Semantic → Coordinate ===")
    for sem in [
        'click(target="File > Print")',
        'click(element="Close")',
        'type(element="Search")',
        'click(element="NONEXISTENT")',
    ]:
        result = bridge.to_coordinate(sem)
        print(f"  {sem:<40} → {result.to_string() if result else 'NOT FOUND'}")

    print("\n=== Round-trip validation ===")
    for coord in [(60, 210), (1890, 10), (250, 614)]:
        print(" ", bridge.round_trip(*coord))

    print("\n=== Android ===")
    ab = ActionBridge("android", SAMPLE_ANDROID)

    print("Direction B (all actions):")
    for a in ab.all_actions():
        print(f"  {a.semantic:<42}  {a.coordinate}")

    print("Direction C:")
    print(" ", ab.to_coordinate('tap(element="Buy Now")'))
    print(" ", ab.to_coordinate('type(element="Search products")'))

    print("\n=== AXTree (Web) ===")
    wb = ActionBridge("axtree", SAMPLE_AXTREE)
    print("Direction B:")
    for a in wb.all_actions():
        print(f"  {a.semantic:<40}  {a.coordinate}")
    print("Direction C:")
    print(" ", wb.to_coordinate('click(element="Sign in")'))
    print(" ", wb.to_coordinate('type(element="Search")'))