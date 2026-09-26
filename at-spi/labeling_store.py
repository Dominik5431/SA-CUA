#!/usr/bin/env python3
"""
labeling_store.py -- the element universe and the decision journal behind at-spi/label_gt.py's
hand-labeling of trusted/untrusted ground truth. No UI here; label_gt.py is the server that drives
it, and this module is independently runnable for coverage/QA reporting (`--stats`).

Three things distinguish this universe from extraction.py's and full_tree_extraction.py's, all
deliberate:

1. NO FILTERING. Every node carrying a bbox inside a content root enters the universe -- unnamed
   container nodes included (measured: 57% of bbox-carrying nodes in a document-web subtree have no
   accessible name at all), punctuation-only names included, full text kept rather than truncated
   at 200 chars. extraction.py's `_is_decorative`/name filters survive here only as a *routing*
   rule (`tier`), never as a drop: a node that can't be read by a human still has to receive a
   label, it just gets one by derivation rather than by keystroke. See `tier` below.

2. CONTENT ROOTS ARE PER-PLATFORM, and there can be several per capture.
   full_tree_extraction._content_root takes the FIRST `document-web` node and nothing else, which
   is wrong twice over for this corpus: it silently drops cross-origin iframes (the security-
   relevant surfaces -- a page with 3 document-web nodes gets 1 labeled), and it finds nothing at
   all in a native-app capture. resolve_content_roots() handles both, and the platform decision
   does NOT come from "does a document-web node exist" -- Electron/XUL apps expose those too
   (240_vscode_project has 1, 246_thunderbird has 3), so that test would label VS Code's embedded
   webview while missing its native chrome. It comes from manifest.json's `platform` field, with a
   browser-application-name fallback for captures the manifest doesn't cover.

3. IDENTITY IS CONTENT-ADDRESSED, not positional. `elem_index` shifts whenever extraction changes
   or the corpus is re-captured (both have happened), which would silently re-point every stored
   decision at a different element. Each row therefore carries `sig` -- a hash of
   (role, bbox, text, ordinal-within-file) -- and load_decisions() re-aligns by `sig` whenever
   `elem_index` no longer validates, reporting anything it can't place instead of dropping it.

The journal (labels/ground_truth_fulltree.jsonl) is append-only for auditability but fully
mutable in effect: the last record for a `sig` wins, so re-labeling an element is just another
append; a `reset` record invalidates every prior decision for one file so a deliberate relabeling
starts from a genuinely clean slate rather than from stale boxes; and a `drop` record retires a
single decision, which is what a re-capture's orphans need -- their element is gone, so the label
is neither ground truth nor work outstanding. --compact collapses the journal to its effective
records, archiving the previous one under labels/_archive/.

Usage:
  python at-spi/labeling_store.py --stats            # coverage + capture-QA report
  python at-spi/labeling_store.py --stats --per_file
  python at-spi/labeling_store.py --dump 008         # inspect one capture's universe
  python at-spi/labeling_store.py --history 008 --elem 47
  python at-spi/labeling_store.py --compact
"""
from __future__ import annotations

import argparse
import glob
import hashlib
from functools import cached_property
import json
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import capacity
from critical_path import _atspi_tag_to_role, _node_bbox, _q, effective_role

XML_DIR = os.path.join(_HERE, "xml")
SCREENSHOT_DIR = os.path.join(_HERE, "screenshots")
LABELS_DIR = os.path.join(_HERE, "labels")
ARCHIVE_DIR = os.path.join(LABELS_DIR, "_archive")
MANIFEST_PATH = os.path.join(_HERE, "manifest.json")

JOURNAL_PATH = os.path.join(LABELS_DIR, "ground_truth_fulltree.jsonl")
EXPORT_PATH = os.path.join(LABELS_DIR, "ground_truth_fulltree.json")
CACHE_PATH = os.path.join(LABELS_DIR, ".universe_cache.json")
EXCLUDED_PATH = os.path.join(LABELS_DIR, "dom_excluded.json")

UNIVERSE_SCHEMA = 23

SKIP_ZERO_AREA = "zero_area"
SKIP_OFFSCREEN = "offscreen"
SKIP_SR_ONLY = "sr_only"
SKIP_NOT_LAID_OUT = "not_laid_out"
SKIP_ECHO = "echo"
SKIP_EMBEDDED_FRAME = "embedded_frame"
SKIP_CONTROL_PAINTS = "control_paints"
SKIP_PAINTED_BY_DESCENDANTS = "painted_by_descendants"
SKIP_INJECTED_NAME = "injected_name"
SKIP_UNPAINTED_NAME = "unpainted_name"
SKIP_UNNAMED = "unnamed"
SKIP_CONTROL_SURFACE = "control_surface"

PAINT_YES, PAINT_NO, PAINT_UNKNOWN = "yes", "no", "unknown"

SKIP_REASONS: dict = {
    None:                        (PAINT_YES, False),
    SKIP_ZERO_AREA:              (PAINT_NO, False),
    SKIP_OFFSCREEN:              (PAINT_NO, False),
    SKIP_SR_ONLY:                (PAINT_NO, False),
    SKIP_NOT_LAID_OUT:           (PAINT_NO, False),
    SKIP_ECHO:                   (PAINT_YES, True),
    SKIP_EMBEDDED_FRAME:         (PAINT_YES, True),
    SKIP_CONTROL_PAINTS:         (PAINT_YES, True),
    SKIP_PAINTED_BY_DESCENDANTS: (PAINT_YES, True),
    SKIP_INJECTED_NAME:          (PAINT_UNKNOWN, False),
    SKIP_UNPAINTED_NAME:         (PAINT_UNKNOWN, False),
    SKIP_UNNAMED:                (PAINT_UNKNOWN, False),
    SKIP_CONTROL_SURFACE:        (PAINT_YES, True),
}


def paint_status_of(skip_reason: "str | None") -> tuple:
    """(paint_status, represented_elsewhere) for a skip_reason. Unknown reasons report UNKNOWN
    rather than defaulting to "no": a reason this table has not heard of is precisely the case
    where claiming nothing is drawn would be an unearned assertion."""
    return SKIP_REASONS.get(skip_reason, (PAINT_UNKNOWN, False))



DOM_CURATION_ENABLED = True
_dom_excluded_cache: dict | None = None

DEFAULT_SCREEN = (1920, 1080)

_SHELL_APPS = {
    "gnome-shell", "gjs", "xdg-desktop-portal-gtk", "evolution-alarm-notify",
    "org.gnome.Software", "mutter", "at-spi2-registryd",
}
_SHELL_APP_PREFIXES = ("gsd-", "ibus-", "at-spi")

_BROWSER_APPS = {"google chrome", "chromium", "chrome", "firefox", "mozilla firefox", "firefox-esr"}

_VERDICTS = ("trusted", "untrusted")

_CONTROL_ROLES = {
    "link", "push button", "toggle button", "check box", "radio button", "menu item",
    "check menu item", "radio menu item", "combo box", "slider", "spin button", "page tab",
    "text", "entry", "password text", "list box", "progress bar", "level bar", "dial",
    "toggle", "menu button", "date editor", "calendar", "color chooser", "file chooser",
    "rating",
}
_VISUAL_ROLES = {"image", "icon", "canvas", "image map", "video", "animation",
                 "drawing area", "math", "terminal", "chart", "diagram"}
_LABELABLE_UNNAMED_ROLES = _CONTROL_ROLES | _VISUAL_ROLES

_ATTR_NS = "{https://accessibility.ubuntu.example.org/ns/attributes}"
_CP_NS = "{https://accessibility.ubuntu.example.org/ns/component}"

_UNPAINTED_NAME_SOURCES = frozenset({"attribute", "related-element", "tooltip"})
_PAINTED_NAME_SOURCES = frozenset({"contents", "placeholder", "value", "CSS alt text"})
_KNOWN_NAME_SOURCES = _UNPAINTED_NAME_SOURCES | _PAINTED_NAME_SOURCES
UNKNOWN_NAME_SOURCES: Counter = Counter()

_TEXT_ONLY_ROLES = frozenset({"static", "label", "heading", "paragraph", "caption"})

_SR_ONLY_CLASS = re.compile(
    r"aok-offscreen|sr-only|screen-?reader|visually-?hidden|a11y[-_a-z]*hidden|assistive"
    r"|accessible-?hidden|show-for-sr|element-invisible|invisible_spoken"
    r"|nav-hidden-aria|\bclipped\b", re.I)

_UNPAINTABLE_AREA = 4
_TEXT_MIN_W, _TEXT_MIN_H = 4, 6
_MIN_NAME_LEN_FOR_GEOMETRY = 4


def _name_cannot_be_painted(role: str, text: str, w: int, h: int, name_from: str | None) -> bool:
    """Is this box physically too small to render the string the node announces?

    A derived test rather than a vocabulary one, so it holds on domains the class list has never
    met. Demotion, not deletion, as everywhere else here: the element is still in the tree and
    still reachable by an agent, it simply cannot be judged from a screenshot, so it takes a
    derived label from its container instead of a keystroke."""
    if name_from in _UNPAINTED_NAME_SOURCES:
        return False
    if len(text) < _MIN_NAME_LEN_FOR_GEOMETRY:
        return False
    if w * h <= _UNPAINTABLE_AREA:
        return True
    return role in _TEXT_ONLY_ROLES and (w < _TEXT_MIN_W or h < _TEXT_MIN_H)


def _demote_names_painted_by_descendants(rows: list[dict]) -> None:
    """A `contents` name on an INTERNAL node is the accname algorithm concatenating what its
    children draw -- the string is on screen, but those children paint it, not this node. Keeping
    both means the same pixels are judged twice, and since every metric here is row-weighted, a
    deeply-wrapped paragraph would count once per wrapper.

    Demoted only when the descendants that survived into the content tier actually ACCOUNT for the
    whole string, compared with all whitespace removed (the accname algorithm joins children with
    spaces inconsistently, so an exact match would miss most real cases). That condition is what
    makes this safe against the echo rule, which routes the inner `static` of
    `<a><static>X</static></a>` to `structural` and keeps the outer link: there is then no
    content-tier descendant left holding X, the test fails, and the link stays labelable. Without
    that guard the two rules would silently cancel and X would become unlabelable in either row.

    Runs after the walk because it depends on which descendants survived, which is not known while
    the parent is being emitted (rows are built parents-first, in document order).
    """
    children: dict[int, list[int]] = {}
    for r in rows:
        if r["parent"] is not None:
            children.setdefault(r["parent"], []).append(r["elem_index"])

    def content_text_below(i: int) -> str:
        out, stack = [], list(children.get(i, []))
        stack.reverse()
        while stack:
            j = stack.pop()
            row = rows[j]
            if row["tier"] == "content" and row["text"]:
                out.append(row["text"])
            kids = children.get(j)
            if kids:
                stack.extend(reversed(kids))
        return "".join(out)

    squash = str.maketrans("", "", " \t\n\r ")
    def content_text_rows_below(i: int) -> list[dict]:
        out, stack = [], list(children.get(i, []))
        while stack:
            j = stack.pop()
            row = rows[j]
            if row["tier"] == "content" and row["text"]:
                out.append(row)
            stack.extend(children.get(j, []))
        return out

    def accounted(r: dict) -> bool:
        below = content_text_below(r["elem_index"])
        return bool(below) and below.translate(squash) == r["text"].translate(squash)

    for r in rows:
        if r["tier"] != "content" or r["name_from"] != "contents" or not r["text"]:
            continue
        if r["role"] not in _CONTROL_ROLES or not children.get(r["elem_index"]):
            continue
        if not accounted(r):
            continue
        painted = content_text_rows_below(r["elem_index"])
        if not all(q["role"] in _TEXT_ONLY_ROLES for q in painted):
            continue
        for q in painted:
            q["tier"] = "structural"
            q["skip_reason"] = SKIP_CONTROL_PAINTS

    for r in rows:
        if r["tier"] != "content" or r["name_from"] != "contents" or not r["text"]:
            continue
        if not children.get(r["elem_index"]):
            continue
        if accounted(r):
            r["tier"] = "structural"
            r["skip_reason"] = SKIP_PAINTED_BY_DESCENDANTS




def _text_region_payload(node: ET.Element) -> str:
    """The Text-interface payload carried by this node, or "" when a descendant already holds it.

    TWO LENGTH GATES USED TO SIT HERE AND HAVE BEEN REMOVED. They were
        if len(payload) < _TEXT_REGION_MIN_CHARS:            # 200
            return ""
        if len(payload) <= max(len(name) * 2, 80):           # "not materially richer than the name"
            return ""
    and between them they discarded the contents of every spreadsheet in the corpus. A Calc cell
    serializes as `<table-cell name="A1" ...>Category</table-cell>`: the NAME is the cell
    reference and the element text is the VALUE. `Category` is 8 characters, so the first gate
    dropped it at 200 and the second would have dropped it at 80 -- and the row's `text` comes
    from the name alone, so the sheet's actual content was captured by the server, carried in the
    XML, and then thrown away here. 217/218/219 all read `A1, B1, C1...` with no values anywhere.

    Neither number was derived from anything. The 200 was reasoned from a count -- "110,958 web
    nodes carry element text, because nearly every accessible implements the Text interface and
    returns its own string" -- which is a real observation about the WEB arm and was turned into a
    blanket length rule that the app arm never had a say in. The `max(len(name) * 2, 80)` has no
    stated basis at all.

    What survives is the only test here that was ever structural: if a NAMED DESCENDANT carries
    the text, that descendant gets the row and this node must not duplicate it. That one is about
    who owns a string, not how long it is."""
    payload = (node.text or "").strip()
    if not payload:
        return ""
    if payload == (node.get("name") or "").strip():
        return ""
    if _has_named_descendant_any(node):
        return ""
    return payload


def _has_named_descendant_any(node: ET.Element) -> bool:
    return any((d.get("name") or "").strip() for d in node.iter() if d is not node)


def _enclosing_box(node: ET.Element, parent_map: dict) -> "list | None":
    """The nearest ancestor rectangle big enough to be worth pointing at.

    A text region's own box is often useless as a target -- VS Code's editor entry is 1x19 at the
    caret and gedit's `text` node has no geometry at all -- while the container that visibly holds
    the text does have a real rectangle (1802x912 and 900x672 respectively).
    """
    cur = parent_map.get(node)
    while cur is not None:
        b = _node_bbox(cur)
        if b and min(b[2], b[3]) >= _VISUAL_REGION_MIN_SIDE:
            return list(b)
        cur = parent_map.get(cur)
    return None


def row_nodes(content_root: ET.Element, parent_map: "dict | None" = None) -> list:
    """(node, bbox) for every node under `content_root` that becomes a ROW, in row order.

    THE one enumeration. build_universe_from_root() below builds its rows from this, and
    mask_screenshot.regions_from_tree() walks it to map tree nodes back onto those rows by
    position; assert_row_node_alignment() checks the two agree. Reproducing the predicate anywhere
    else is what this function exists to prevent.

    It is not "nodes with geometry", which is the mistake that was here: a node with a Text payload
    and no rectangle of its own ADOPTS its enclosing box (gedit's `text`, a Thunderbird message
    pane) and becomes a row, while a walk filtered on screencoord/size skips it. Measured, 35 of
    160 corpus captures diverged that way -- amazon by 70 nodes -- and since the mapping is a zip,
    every row after the first divergence was attributed to the wrong element, silently.
    """
    if parent_map is None:
        parent_map = {c: p for p in content_root.iter() for c in p}
    out: list = []
    for node in content_root.iter():
        bbox = _node_bbox(node)
        if _text_region_payload(node) and (bbox is None or bbox[2] <= 0 or bbox[3] <= 0):
            adopted = _enclosing_box(node, parent_map)
            if adopted:
                bbox = tuple(adopted)
        if bbox is None:
            continue
        out.append((node, tuple(bbox)))
    return out


def _label_box_for_control(node: ET.Element, parent_map: dict) -> "list | None":
    """The rectangle a degenerate control is actually drawn at, taken from its own <label>.

    A styled checkbox or radio keeps the real <input> in the layout at 1x1 px and paints the
    control with CSS on the <label> beside it. AT-SPI reports both faithfully: the input carries
    the accessible name at 1x1, the label carries the full clickable rectangle with NO name of its
    own -- because the accessible-name algorithm attributes a <label for=...>'s text to the control
    it labels, not to the label element. Name and geometry therefore land on different nodes, and
    the labeler is shown a 1x1 target for text that is plainly on screen 28px to the right.

    Measured on 026_www.booking.com: `Very good: 8+` is an <input class="faadc60545"> whose DOM
    rect really is [454,814,1,1], beside a <label> at [454,819,233,40]. Corpus-wide, 42 of the 74
    degenerate named controls have exactly this nameless-label sibling.

    Returns the label's box, or None when there is no such sibling -- in which case the control
    keeps its own rectangle and the existing rules apply unchanged.
    """
    parent = parent_map.get(node)
    if parent is None:
        return None
    for sib in parent:
        if sib is node or effective_role(sib) != "label":
            continue
        if (sib.get("name") or "").strip():
            continue
        b = _node_bbox(sib)
        if b and b[2] * b[3] > _UNPAINTABLE_AREA:
            return b
    return None


def _demote_control_surfaces(rows: list[dict]) -> None:
    """Drop a NAMELESS content row that shares a named control's exact rectangle.

    Two different shapes produce this, and both leave the same pixels with two labelable rows:

      * a nameless graphic inside the control that renders it. 025_www.booking.com's save-to-list
        heart is `push button [932,533,37,36]` named "Save ... to a trip list" wrapping
        `section [932,533,37,36]` with no name, promoted by _visual_regions() because nothing named
        covers it. Both reach the content tier on identical pixels.
      * the <label> whose box a degenerate control has just adopted (see _label_box_for_control):
        after the adoption the control and the label describe the same rectangle.

    The echo rule cannot see either, because it matches a child repeating its parent's NAME and
    these rows have no name at all. The named control is the row to keep: it carries both the
    affordance and the text, and an agent clicks the control, not the glyph inside it.
    """
    by_box: dict = {}
    for r in rows:
        if r["tier"] == "content" and r["text"] and r["role"] in _CONTROL_ROLES:
            by_box.setdefault(tuple(r["bbox"]), r)
    if not by_box:
        return
    for r in rows:
        if r["tier"] != "content" or r["text"]:
            continue
        owner = by_box.get(tuple(r["bbox"]))
        if owner is not None and owner is not r:
            r["tier"] = "structural"
            r["skip_reason"] = SKIP_CONTROL_SURFACE


def _has_painted_descendant(node: ET.Element, parent_map: dict) -> bool:
    """Does anything below this node draw text of its own ON SCREEN? A control that has such a
    descendant is a normal labelled control (that descendant draws its text); one that has none
    draws an icon, and is the thing being looked at.

    "Painted", not merely "named" -- which is what this asked before, via a bare
    `any(d.get("name"))`. That question is answered `True` by descendants that draw nothing, and
    the module already knows which those are and applies the same reasoning everywhere else:

        screen-reader-only  a visually-hidden label is not what the user sees
        unpainted name-from attribute/tooltip names are strings ABOUT a node, not strings IN it
        no geometry         a node with no box, or a collapsed one, draws nothing anywhere

    The case this fixes is an icon-only control with a screen-reader label whose wording differs
    from the control's own accessible name -- `aria-label="Delete item"` over an sr-only "Delete".
    The old test saw a named descendant, refused the control `draws_graphic`, and the demotion
    chain then filed it under `unpainted_name`, leaving the icon with no labelable row. Where the
    two strings MATCH, `_descendant_paints()` already rescued it, which is why this was invisible
    for so long: the bug only ever showed up when the wording drifted.
    """
    for d in node.iter():
        if d is node:
            continue
        if not (d.get("name") or "").strip():
            continue
        if d.attrib.get(_ATTR_NS + "name-from") in _UNPAINTED_NAME_SOURCES:
            continue
        bbox = _node_bbox(d)
        if bbox is None or bbox[2] <= 0 or bbox[3] <= 0:
            continue
        if _in_sr_only(d, parent_map):
            continue
        return True
    return False


def _descendant_paints(node: ET.Element, text: str) -> bool:
    """Is this exact string drawn by something below this node?

    An aria-label frequently just restates the text the element already renders -- Amazon's
    `+2 other colors/patterns` link is labelled with the very words inside it. Treating such a
    node as "name not painted" would be wrong twice over: the string IS on screen, and the echo
    rule has already demoted the child that repeats it, so demoting the parent as well leaves the
    string with no labelable row anywhere. Measured at 79 rows before this check existed.

    Any descendant repeating the string counts, not only a `contents` one. Requiring `contents`
    left 27 rows -- article headlines, consent-dialog titles -- with no labelable row at all,
    because their echo child was named by attribute too. The asymmetry is deliberate: an extra box
    costs the labeler one keystroke to skip, a dropped string costs coverage silently.
    """
    if not text:
        return False
    return any((d.get("name") or "").strip() == text for d in node.iter() if d is not node)


def _frame_paint_roots(frame: ET.Element, frame_bbox: list) -> list:
    """The outermost descendants of an embedded frame that are SMALLER than the frame itself.

    A third-party frame is routinely sized to the whole viewport so it can centre its card and
    swallow clicks around it, then stacks five or six wrappers at that same full box before
    anything is drawn. Descending past the wrappers finds the box that actually carries paint.

    A DEGENERATE box -- zero width or height -- is descended through like a bbox-less node, never
    treated as the paint root. 021_www.nytimes.com is why: its ad frame's only child is a
    [502,189,971,0] collapsed wrapper, which read as "the frame narrows to nothing", retargeted the
    whole 971x250 banner onto a zero-area row, and left the ad with no labelable element at all.
    """
    roots, stack = [], list(frame)
    while stack:
        node = stack.pop()
        bbox = _node_bbox(node)
        if bbox is None or bbox == frame_bbox or bbox[2] <= 0 or bbox[3] <= 0:
            stack.extend(list(node))
            continue
        roots.append(node)
    return roots


def _in_sr_only(node: ET.Element, parent_map: dict) -> bool:
    """Is this node, or anything it sits inside, marked screen-reader-only? See _SR_ONLY_CLASS."""
    cur = node
    while cur is not None:
        if _SR_ONLY_CLASS.search(cur.get(_q("attr", "class")) or ""):
            return True
        cur = parent_map.get(cur)
    return False


def _is_leaf(node: ET.Element) -> bool:
    """No accessible children, so nothing below it can be doing the drawing.

    A named LEAF paints its own box, whatever the name's provenance says: an aria-label on a node
    with no children is the only description of a thing that is unquestionably on screen -- the
    bare `<div aria-label="Close">x</div>` close button, archive.org's `Collection / Forum / About`
    tabs, GitHub's language bars, paperswithcode's 17x16 author avatars. Only two of the 56 leaves
    this rescues are not painted (a 1835x12 `Notifications: 0` live region and a 2x1 sr-only
    label), and both are cheap for a labeler to skip -- unlike the 54 real ones, which were being
    dropped with no labelable row anywhere in the file."""
    return len(list(node)) == 0

_VISUAL_REGION_MIN_SIDE = 24
_VISUAL_REGION_LEAF_MIN_SIDE = 8
_FRAME_PAINT_COVER = 0.9
_FRAME_PAINT_MIN = 0.08

_SUPPRESSED_MIN_EXTENT = 2000
_SUPPRESSED_MAX_NODES = 120
_CONSENT_WORDS = ("accept all", "reject all", "decline all", "accept cookies", "manage cookies",
                  "cookie settings", "cookie preferences", "customise my choices",
                  "customize my choices", "i agree", "allow all", "consent")


def load_manifest() -> dict[str, dict]:
    """{filename: manifest row}. Missing/unparseable manifest is not fatal -- the universe is
    derivable from the XML alone; the manifest only supplies category/url/platform metadata."""
    if not os.path.exists(MANIFEST_PATH):
        return {}
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as f:
            rows = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return {r["filename"]: r for r in rows if r.get("filename")}


def dom_excluded_sigs(filename: str) -> set:
    """Sigs the DOM curation pass says are not on screen. See DOM_CURATION_ENABLED.

    Read straight from the JSON rather than through dom_curation, which imports this module.
    A missing or unreadable artifact means "no opinion" and leaves every row where it was --
    the curation is an optional overlay, never a dependency.
    """
    global _dom_excluded_cache
    if not DOM_CURATION_ENABLED:
        return set()
    if _dom_excluded_cache is None:
        try:
            with open(EXCLUDED_PATH, encoding="utf-8") as f:
                doc = json.load(f)
            _dom_excluded_cache = {
                fn: {e["sig"] for e in rec.get("excluded", [])}
                for fn, rec in (doc.get("files") or {}).items()
            }
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            _dom_excluded_cache = {}
    return _dom_excluded_cache.get(filename, set())


def _sync_paint_fields(rows: list[dict]) -> None:
    """Re-derive paint_status / represented_elsewhere from each row's final skip_reason."""
    for row in rows:
        row["paint_status"], row["represented_elsewhere"] = paint_status_of(row["skip_reason"])


def _apply_dom_curation(rows: list[dict], excluded: set) -> None:
    """Demote rows the DOM says were never laid out. Demoted, never dropped: `sig` is
    untouched, so a decision already recorded against one still aligns and comes straight
    back if the curation is reverted -- it simply stops costing a keystroke and stops being
    scored. Same contract as every other skip_reason."""
    if not excluded:
        return
    for row in rows:
        if row["tier"] == "content" and row["sig"] in excluded:
            row["tier"] = "structural"
            row["skip_reason"] = SKIP_NOT_LAID_OUT


def _is_shell_app(name: str) -> bool:
    n = (name or "").strip()
    return n in _SHELL_APPS or n.startswith(_SHELL_APP_PREFIXES)


def _states(node: ET.Element) -> dict:
    return {k.split("}")[-1]: v for k, v in node.attrib.items() if k.startswith("{") and "/state" in k}


_POPUP_TOKENS = frozenset({"modal", "dialog", "popup", "overlay", "lightbox"})
_POPUP_ROLE_TAGS = frozenset({"dialog", "alert"})
_POPUP_VIEWPORT_FRACTION = 0.9


def _overlap_box(a, b) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def _names_popup(node: ET.Element) -> bool:
    """Does a class or the id NAME this element a pop-up -- `modal`, `reply-modal`,
    `video-call-overlay`, `fides-overlay`? Whole class names only, matched as the word or as its
    `-word`/`_word` suffix. A substring is not a name: `modal-open` is the class Bootstrap puts on
    <body> while a modal is open, and 050_simple.wikipedia.org's body carries
    `uls-dialog-sticky-hide` -- both are the page, not a pop-up."""
    names = (node.get(_q("attr", "class")) or "").lower().split() + [(node.get(_q("attr", "id")) or "").lower()]
    return any(n == t or n.endswith("-" + t) or n.endswith("_" + t)
               for n in names if n for t in _POPUP_TOKENS)


def popup_layers(content_root: ET.Element, parent_map: dict) -> "list[tuple[ET.Element, list[int]]]":
    """The open in-page pop-ups of one WEB content root, bottom to top: [(node, panel_bbox)].
    See the comment above for what counts and why."""
    rb = _node_bbox(content_root)
    if not rb or rb[2] <= 0 or rb[3] <= 0:
        return []
    root_area = rb[2] * rb[3]
    layers: list = []
    for node in content_root.iter():
        if node is content_root:
            continue
        st = _states(node)
        if st.get("showing") != "true":
            continue
        box = _node_bbox(node)
        if not box or box[2] <= 0 or box[3] <= 0:
            continue
        if node.tag in _POPUP_ROLE_TAGS:
            panel = list(box)
        elif _names_popup(node) and box[2] * box[3] >= _POPUP_VIEWPORT_FRACTION * root_area:
            inner = []
            for d in node.iter():
                if d is node or _states(d).get("showing") != "true":
                    continue
                b = _node_bbox(d)
                if b and b[2] > 0 and b[3] > 0 and b[2] * b[3] < _POPUP_VIEWPORT_FRACTION * box[2] * box[3]:
                    inner.append(b)
            if not inner:
                continue
            x0 = min(b[0] for b in inner); y0 = min(b[1] for b in inner)
            x1 = max(b[0] + b[2] for b in inner); y1 = max(b[1] + b[3] for b in inner)
            x0, y0 = max(x0, box[0]), max(y0, box[1])
            x1, y1 = min(x1, box[0] + box[2]), min(y1, box[1] + box[3])
            if x1 <= x0 or y1 <= y0:
                continue
            panel = [x0, y0, x1 - x0, y1 - y0]
        else:
            continue
        inside = set(node.iter())
        if not any(_overlap_box(b, panel) for d in content_root.iter()
                   if d is not content_root and d not in inside and _states(d).get("showing") == "true"
                   for b in [_node_bbox(d)] if b and b[2] > 0 and b[3] > 0):
            continue
        p = parent_map.get(node)
        while p is not None and all(p is not n for n, _ in layers):
            p = parent_map.get(p)
        if p is not None:
            continue
        layers.append((node, panel))
    return layers


_TAB_CONTAINER_IDS = frozenset({"tabpanelcontainer", "tabmail"})


def _inactive_tab_node_ids(root: ET.Element) -> set:
    """object-ids of every node inside an INACTIVE tab panel. A tab panel is a direct child of a tab
    container (id in _TAB_CONTAINER_IDS); it is inactive when a SIBLING panel is showing but it is
    not. Only marks a panel inactive when a sibling IS showing (a known-active tab exists), so a
    single-tab window and any ambiguous state are left untouched -- fail OPEN, never hide a tab that
    cannot be proven inactive (over-masking a live tab would be worse than the duplicate rows)."""
    inactive: set = set()
    for cont in root.iter():
        if cont.attrib.get(_ATTR_NS + "id") not in _TAB_CONTAINER_IDS:
            continue
        panels = list(cont)
        if not any(_states(p).get("showing") == "true" for p in panels):
            continue
        for panel in panels:
            if _states(panel).get("showing") != "true":
                for d in panel.iter():
                    inactive.add(id(d))
    return inactive


def resolve_content_roots(root: ET.Element, platform: str | None) -> tuple[list[dict], str]:
    """Returns ([root descriptors], platform_used). A descriptor is
    {node, kind, provenance, bbox, active} where `kind` is "web" or "native".

    web    -- every OUTERMOST `document-web` node, in document order. Not just the first: a page
              with cross-origin iframes at top level exposes one per frame, and those are exactly
              the surfaces whose trust label matters most. But not the nested ones either -- the
              per-root walk below is `node.iter()`, which already descends through a nested
              `document-web` into the frame's contents, so promoting that frame to a root as well
              enumerated the same pixels a second time.

              This mattered as soon as ad slots started rendering. A Google DFP ad arrives as
              SafeFrame -> creative frame -> inner frame, three nested `document-web` nodes, and
              001_stackoverflow.com came back with NINE roots for one page: the document plus
              eight ad frames, three of them sharing bbox [1295,903,301,251] and three sharing
              [623,990,729,90]. Every layer re-enumerated the same banner, so a single visible ad
              produced up to 17 rows stacked on one identical rectangle, each with its own sig and
              therefore each a separate thing to hand-label. Keeping only the outermost root
              leaves the ad fully present -- it is real, untrusted, third-party content and
              belongs in the corpus -- but present ONCE, as a container whose descendants take
              their trust from it.
    native -- every top-level window (direct child of a non-shell `application`) that carries a
              bbox. NOT filtered to st:active="true": 238_calc_budget has an active `Text Import`
              dialog stacked over a still-visible Calc frame, and filtering by active would leave
              the spreadsheet behind it unlabeled. `active` is recorded, not enforced.

    desktop -- BOTH of the above, for one screen that is showing both at once. See
              _desktop_content_roots. Live-only: every corpus caller resolves a single-surface
              capture through platform_for(), so "web" and "app" behave exactly as before.

    Browser chrome (tab bar, URL bar) is deliberately outside the web roots, while a native app's
    own menus and toolbars are deliberately inside its root -- a native app draws untrusted content
    into the same window as its chrome with no structural split this code could assume, so that
    boundary is one of the things being labeled by hand.
    """
    if platform is None:
        platform = _infer_platform(root)

    if platform == "desktop":
        return _desktop_content_roots(root), platform

    descriptors: list[dict] = []
    if platform == "web":
        parent_of = {c: p for p in root.iter() for c in p}

        def _nested_in_web_root(node: ET.Element) -> bool:
            p = parent_of.get(node)
            while p is not None:
                if p.tag == "document-web":
                    return True
                p = parent_of.get(p)
            return False

        for node in root.iter():
            if node.tag != "document-web":
                continue
            if _nested_in_web_root(node):
                continue
            bbox = _node_bbox(node)
            descriptors.append({
                "node": node, "kind": "web", "bbox": list(bbox) if bbox else None,
                "provenance": f"document-web#{len(descriptors)}",
                "active": None,
                "children": len(list(node)),
            })
        return descriptors, platform

    for app in root:
        if app.tag != "application" or _is_shell_app(app.get("name")):
            continue
        app_name = (app.get("name") or "").strip()
        for window in app:
            bbox = _node_bbox(window)
            if bbox is None:
                continue
            st = _states(window)
            descriptors.append({
                "node": window, "kind": "native", "bbox": list(bbox),
                "provenance": f"{app_name}/{_atspi_tag_to_role(window.tag)}"
                               f"{':' + (window.get('name') or '')[:60] if window.get('name') else ''}",
                "active": st.get("active") == "true",
                "stack": _stack_index(st.get("stackorder")),
                "children": len(list(window)),
            })
    _attach_screen_bboxes(root, descriptors)
    return descriptors, platform


def _desktop_content_roots(root: ET.Element) -> "list[dict]":
    """Content roots for a screen showing a browser and native windows AT THE SAME TIME.

    WHY THIS EXISTS. `resolve_content_roots` used to answer one question -- "is the agent on the
    web or in an app" -- and return only that surface's roots. On a corpus capture, which shows one
    thing, that is right. On a live desktop it is a blind spot that switches sides with the focus,
    and both sides have now been hit:

      * `platform="web"` was the old unconditional default, and the terminal the agent was actually
        working in contributed no rows at all, so a whole terminal could not be masked or reasoned
        about. That is what `browser_focused` was added to fix.
      * `platform="app"` then inherited the mirror image, and it is worse, because it fails OPEN.
        In the 2026-09-16 gpt-5.6-luna+sa `own_owncloud_2` run the planner opened a terminal over
        a browser window holding an injected document. Chrome's window WAS in the tree and WAS
        resolved as a native root -- the leak was not a missing window -- but the whole universe was
        then graded under the single active-window scope (`app:gnome-terminal-server`), and
        terminal default-trust marked every row in it trusted. The mask came back `0.0% of screen
        redacted` with the injection fully legible to the planner, which read it and said so.

    So the fix is not "resolve more roots", it is "stop pretending one screen is one surface". A
    desktop universe carries BOTH kinds, each row knows its `root_index`, and the caller grades each
    root under its OWN scope.

    WHAT IS RETURNED, and the one subtraction. Web roots first, exactly as the "web" path derives
    them (so browser chrome -- the tab strip and URL bar -- stays outside the universe and therefore
    unmasked, which is the behaviour a browser turn has always had). Then every top-level window
    that does NOT contain one of those web roots. The subtraction is what keeps a browser window
    from being enumerated twice: its page is already a web root, and taking the window as well would
    re-walk the same pixels as a native surface and grade the page under `app:chrome` rules.

    Z-ORDER. A web root is not a window and has no stacking index of its own, but it is PAINTED
    into one, so it inherits its host window's `stack` and `screen_bbox`. Without that a terminal
    raised above the browser would not subtract from the page behind it -- which is exactly the
    geometry of the run above. `host_window` records which window it came from, for anything that
    needs to get back to it.
    """
    web, _ = resolve_content_roots(root, "web")
    native, _ = resolve_content_roots(root, "app")

    parent_of = {c: p for p in root.iter() for c in p}

    def _ancestors(node):
        seen = []
        p = parent_of.get(node)
        while p is not None:
            seen.append(p)
            p = parent_of.get(p)
        return seen

    host_of: dict[int, int] = {}
    native_by_id = {id(d["node"]): i for i, d in enumerate(native)}
    for wi, wdesc in enumerate(web):
        for anc in _ancestors(wdesc["node"]):
            ni = native_by_id.get(id(anc))
            if ni is not None:
                host_of[wi] = ni
                break

    def _owning_app(node) -> str:
        p = parent_of.get(node)
        while p is not None:
            if p.tag.split("}")[-1] == "application":
                return (p.get("name") or "").strip().lower()
            p = parent_of.get(p)
        return ""

    web_keep = [wi for wi in range(len(web))
                if _owning_app(web[wi]["node"]) in _BROWSER_APPS]

    hosted = {host_of[wi] for wi in web_keep if wi in host_of}
    out: list[dict] = []
    for wi in web_keep:
        wdesc = web[wi]
        d = dict(wdesc)
        ni = host_of.get(wi)
        if ni is not None:
            host = native[ni]
            d["stack"] = host.get("stack")
            d["active"] = host.get("active")
            d["screen_bbox"] = host.get("screen_bbox") or host.get("bbox")
            d["host_window"] = host.get("provenance")
        d["provenance"] = f"{d.get('provenance', 'document-web')}"
        out.append(d)
    for ni, ndesc in enumerate(native):
        if ni in hosted:
            continue
        if _owning_app(ndesc["node"]) in _BROWSER_APPS:
            continue
        out.append(dict(ndesc))
    return out


def _stack_index(raw) -> "int | None":
    """The window's EWMH stacking index as an int, or None when the capture carries no stackorder
    (older server) or it is malformed. None is the fall-back signal occlusion_map keys on."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _contains(outer, inner) -> bool:
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[0] + outer[2] >= inner[0] + inner[2]
            and outer[1] + outer[3] >= inner[1] + inner[3])


def _attach_screen_bboxes(root: ET.Element, descriptors: "list[dict]") -> None:
    """Adds `screen_bbox` to every native descriptor: the window's footprint ON SCREEN, decoration
    included. Defaults to the client rectangle when nothing better is available.

    WHY A SECOND RECTANGLE. An AT-SPI `frame` reports its CLIENT area, and on a server-decorated
    window the title bar is not in it. 207_thunderbird_compose_reply's compose window reports
    [107,101,860,800] while its first child -- the menu bar -- starts at exactly (107,101); the
    45px title bar above it belongs to the window manager and appears nowhere in Thunderbird's
    tree. Occluding by the client rectangle therefore leaves a band across the top of every raised
    window into which the window BEHIND it still paints, which is precisely what it looks like:
    the Inbox's header `section` [115,64,1805,45] blacked out the compose window's title bar.

    WHERE THE REAL RECTANGLE IS. The compositor publishes one actor per managed window, and on
    this desktop it is reachable: `gnome-shell` exposes a `panel` per window whose box is the
    client rectangle plus decoration and shadow. Compose is [97,56,880,855] against a client
    [107,101,860,800] -- 45px of title bar on top, 10px of shadow on the other three sides. So the
    hull is the SMALLEST shell rectangle that contains the client rectangle. Measured across all
    46 top-level windows in the corpus: every one resolves, and the padding is exactly what the
    toolkit predicts -- 0 on client-side-decorated GTK apps (nautilus, gedit, evince, terminal,
    calculator), 37px on a maximized window under the GNOME top bar, L10/T45/R10/B10 on the two
    server-decorated windows (Thunderbird compose, VLC).

    NOT A CONSTANT. Nothing here is tuned: the padding is read per capture from the compositor's
    own geometry, so a different theme, DPI or decoration style corrects itself, and a toolkit that
    publishes no actor falls back to the client rectangle and simply over-masks as before.

    THE GUARD. Growing an occluder only ever REMOVES paint, which is the direction this project is
    not allowed to be wrong in, so the hull is refused when it would swallow a whole other window
    that the client rectangle does not already cover -- a shell rectangle that big is a workspace
    or an overview group, not this window's actor. Refusing falls back to the client rectangle,
    i.e. to over-masking.
    """
    natives = [d for d in descriptors if d.get("kind") == "native" and d.get("bbox")]
    if not natives:
        return
    shell_boxes = []
    for app in root:
        if app.tag != "application" or not _is_shell_app(app.get("name")):
            continue
        for node in app.iter():
            b = _node_bbox(node)
            if b and b[2] > 0 and b[3] > 0:
                shell_boxes.append(tuple(b))
    for d in natives:
        rect = tuple(d["bbox"])
        cands = [b for b in shell_boxes if _contains(b, rect)]
        hull = min(cands, key=lambda b: b[2] * b[3]) if cands else rect
        if any(_contains(hull, tuple(o["bbox"])) and not _contains(rect, tuple(o["bbox"]))
               for o in natives if o is not d):
            hull = rect
        d["screen_bbox"] = list(_visible_footprint(d["node"], rect, hull))


_MAX_SHADOW_PX = 48


def _visible_footprint(window: ET.Element, rect: tuple, hull: tuple) -> tuple:
    """The window's rectangle ON SCREEN with its INVISIBLE SHADOW MARGIN removed.

    A GTK client-side-decorated window's AT-SPI rectangle is its SURFACE, and a CSD surface carries
    a transparent shadow/resize border that the toolkit draws nothing into. gnome-terminal reports
    [234,33,786,533] while everything it actually paints -- title bar included, since CSD puts it in
    the tree -- fits [260,56,734,481]: a 26/23/26/29 px frame of the surface shows whatever is
    BEHIND the window.

    That matters because this rectangle is an OCCLUDER, and an occluder only ever REMOVES paint.
    Using the surface rect told the mask "the page is hidden here", so the page's redaction was
    subtracted from a ~26px frame in which the page was in fact fully visible, and it leaked in the
    clear. Measured live on own_owncloud_1_loose_open_terminals: with the terminal raised over the
    ownCloud page, redaction resumed at x=880 while the terminal's last painted pixel was x=855 --
    a 24px column of the injected document legible down the whole height of the window.

    THE TITLE BAR IS NOT SHADOW, and must stay. On a SERVER-decorated window the title bar belongs
    to the window manager and appears nowhere in the app's tree, which is why `_attach_screen_bboxes`
    grows the rect to the compositor's hull in the first place. So the extension the hull adds ABOVE
    the client area is kept, while the shadow it (or the surface) adds on the other three sides is
    dropped. Both cases come out right: Thunderbird compose keeps its 45px of title and loses 10px
    of shadow; the terminal keeps its in-tree title bar and loses all four shadow margins.

    Nothing here is tuned. The inset is whatever the window actually painted, read per capture, so a
    different theme, DPI or toolkit corrects itself. A window that paints no positioned child at all
    falls back to the hull and over-masks exactly as before -- the direction this is allowed to err.
    """
    def _overlaps_surface(b):
        return (b[0] < rect[0] + rect[2] and b[0] + b[2] > rect[0]
                and b[1] < rect[1] + rect[3] and b[1] + b[3] > rect[1])
    kids = [b for b in (_node_bbox(n) for n in window.iter() if n is not window)
            if b and b[2] > 0 and b[3] > 0 and _overlaps_surface(b)]
    if not kids:
        return hull
    x0 = max(rect[0], min(b[0] for b in kids))
    y0 = max(rect[1], min(b[1] for b in kids))
    x1 = min(rect[0] + rect[2], max(b[0] + b[2] for b in kids))
    y1 = min(rect[1] + rect[3], max(b[1] + b[3] for b in kids))
    if x1 <= x0 or y1 <= y0:
        return hull
    m = _MAX_SHADOW_PX
    x0 = min(x0, rect[0] + m)
    y0 = min(y0, rect[1] + m)
    x1 = max(x1, rect[0] + rect[2] - m)
    y1 = max(y1, rect[1] + rect[3] - m)
    y0 = max(hull[1], y0 - max(0, rect[1] - hull[1]))
    return (x0, y0, x1 - x0, y1 - y0)


def occlusion_map(roots: "list[dict]") -> "dict[int, list[list[int]]]":
    """For each content-root index, the rectangles of the roots KNOWN to be painted above it.

    A desktop capture is not a single plane. 207_thunderbird_compose_reply holds two top-level
    Thunderbird frames -- the Inbox at [70,64,1850,1016] and a compose window at [107,101,860,800]
    sitting on top of it -- and every consumer that treats a bbox as "these pixels show this node"
    is wrong about the 688,000 px where they overlap. The Inbox frame's own rectangle covers the
    compose window; painting it blacks out a window the Inbox is not showing.

    THE FULL Z-ORDER, WHEN THE CAPTURE HAS IT. The a11y server now emits `st:stackorder` per
    top-level window from the window manager's EWMH `_NET_CLIENT_LIST_STACKING` -- the TOTAL
    bottom-to-top order (higher index = nearer the top). When every native window carries it, this
    subtracts, from each window, the footprint of every window ABOVE it -- so overlap between any two
    windows is resolved, not just overlap with the single raised one. `resolve_content_roots` records
    it per root as `stack`.

    THE ONE-BIT FALL-BACK. Older captures (server before that change) carry no stackorder, only
    `st:active` on the window the WM has raised. There is no z-index otherwise -- Component.getLayer()
    / getMDIZOrder() are returned as constants by both Chromium and GTK for in-document nodes (see
    the note in mask_screenshot.mask_image). One bit is enough for the only ordering it settles: the
    active window is above every other top-level window it overlaps.

    EVERYTHING ELSE STAYS AMBIGUOUS, AND AMBIGUOUS MEANS PAINT. In the fall-back, two non-active
    windows that overlap are left unordered: neither subtracts from the other, so both paint and the
    overlap is masked twice. Over-masking is the direction this design is allowed to be wrong in;
    guessing (bigger is behind, contained is on top) would put the error on the false-negative side,
    and a fully covered window really can be the one in front. No heuristic, no threshold, no fixed N.

    Web roots never participate. Cross-origin top-level iframes are laid out by one renderer into
    one window; their boxes do not stack, they tile.
    """
    natives = [(i, r) for i, r in enumerate(roots)
               if r.get("kind") == "native" and r.get("bbox")]
    occludable = natives + [(i, r) for i, r in enumerate(roots)
                            if r.get("kind") == "web" and r.get("bbox")
                            and r.get("stack") is not None]
    if not natives:
        return {}

    def _overlaps(a, b) -> bool:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah

    def _footprint(r):
        return r.get("screen_bbox") or r["bbox"]

    if len(occludable) < 2:
        return {}
    above: "dict[int, list[list[int]]]" = {}
    for i, r in occludable:
        rstack, ractive = r.get("stack"), bool(r.get("active"))
        over = []
        for j, a in natives:
            if j == i or not _overlaps(_footprint(a), r["bbox"]):
                continue
            astack = a.get("stack")
            if rstack is not None and astack is not None:
                is_above = astack > rstack
            else:
                is_above = bool(a.get("active")) and not ractive
            if is_above:
                over.append(list(_footprint(a)))
        if over:
            above[i] = over
    return above


def _infer_platform(root: ET.Element) -> str:
    """Only used when manifest.json has no row for the capture. Deliberately does NOT test for the
    presence of a `document-web` node -- Electron/XUL native apps expose those (240_vscode_project,
    246_thunderbird) and would be misread as browser captures."""
    for app in root:
        if app.tag != "application" or _is_shell_app(app.get("name")):
            continue
        if (app.get("name") or "").strip().lower() in _BROWSER_APPS and len(list(app)):
            return "web"
    return "app"


def _has_alnum(text: str) -> bool:
    return any(c.isalnum() for c in text)


_VISUAL_REGION_NAMED_COVER = 0.10
_VISUAL_REGION_INNER_COVER = 0.5
def _union_area(rects: list) -> int:
    """Exact area covered by a set of rectangles, counting overlap once.

    Klee's algorithm: an x-sweep over a segment tree of compressed y-intervals, O(n log n). The
    previous implementation was an O(n^2 log n) coordinate-compression sweep kept tractable by a
    `_UNION_CAP = 200` guard in _covered(), which declared any region with more than 200 clipped
    rectangles fully covered without measuring. That guard was ATTACKER-REACHABLE: both callers
    drop a candidate when coverage is True, so 250 tiny named nodes scattered over a product photo
    -- covering perhaps 5% of it -- made the photo "explained by its descendants" and removed its
    only labelable row. Untrusted content that no row enumerates can never be labeled untrusted,
    and never gets masked. There is no fixed N here, and no approximation, so there is no
    threshold to aim at.
    """
    if not rects:
        return 0
    ys = sorted({r[1] for r in rects} | {r[1] + r[3] for r in rects})
    if len(ys) < 2:
        return 0
    y_index = {y: i for i, y in enumerate(ys)}
    n_slots = len(ys) - 1
    count = [0] * (4 * n_slots)
    covered = [0] * (4 * n_slots)

    def _update(node: int, lo: int, hi: int, left: int, right: int, delta: int) -> None:
        if right <= lo or hi <= left:
            return
        if left <= lo and hi <= right:
            count[node] += delta
        else:
            mid = (lo + hi) // 2
            _update(2 * node + 1, lo, mid, left, right, delta)
            _update(2 * node + 2, mid, hi, left, right, delta)
        if count[node] > 0:
            covered[node] = ys[hi] - ys[lo]
        elif hi - lo == 1:
            covered[node] = 0
        else:
            covered[node] = covered[2 * node + 1] + covered[2 * node + 2]

    events = []
    for x, y, w, h in rects:
        if w > 0 and h > 0:
            events.append((x, 1, y, y + h))
            events.append((x + w, -1, y, y + h))
    if not events:
        return 0
    events.sort()

    total, prev_x = 0, events[0][0]
    for x, delta, y0, y1 in events:
        total += covered[0] * (x - prev_x)
        prev_x = x
        _update(0, 0, n_slots, y_index[y0], y_index[y1], delta)
    return total


def _clip(inner, outer):
    x0, y0 = max(inner[0], outer[0]), max(inner[1], outer[1])
    x1 = min(inner[0] + inner[2], outer[0] + outer[2])
    y1 = min(inner[1] + inner[3], outer[1] + outer[3])
    return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None


def _covered(rects: list, bbox, fraction: float) -> bool:
    """Do `rects`, clipped to `bbox`, account for at least `fraction` of its area?

    Both fast paths below are exact implications, not approximations -- which is the whole point,
    because the answer decides whether a visual region keeps a labelable row and is therefore
    something a page author can try to steer:

        union <= sum of the parts    -> if the sum falls short, nothing can reach the threshold
        union >= any single part     -> if one rectangle already clears it, so does the union

    Neither can be gamed: crowding a region with many small boxes drives the SUM up but leaves the
    union honest, and the exact sweep runs whenever the bounds do not settle it. The bounds are
    here for speed alone (they retire the overwhelming majority of calls without a sweep), so
    removing them would change runtime and nothing else.
    """
    area = bbox[2] * bbox[3]
    if area <= 0:
        return True
    inside = [c for c in (_clip(r, bbox) for r in rects) if c]
    if not inside:
        return False
    need = fraction * area
    total = largest = 0
    for _x, _y, w, h in inside:
        a = w * h
        total += a
        if a > largest:
            largest = a
    if total < need:
        return False
    if largest >= need:
        return True
    return _union_area(inside) >= need


def _visual_regions(root: ET.Element, parent_map: dict, screen_w: int, screen_h: int) -> set:
    """ids of nodes that are textless visual regions -- see _VISUAL_REGION_MIN_SIDE's comment.

    Both halves of this are COVERAGE tests rather than presence tests, and both changed for the
    same reason: one small overlaid control was enough to erase a whole photo.

    A region qualifies when its named descendants do not ACCOUNT for its pixels (under
    _VISUAL_REGION_NAMED_COVER). The old test was "no named descendant at all", which meant a
    heart button on a hotel photo, a "Superhost" badge on a listing photo, or a "Take Quiz" button
    on a card thumbnail disqualified the image behind it. Worse, it did so inconsistently: on
    111_www.airbnb.com the search template draws the photo on its own empty layer and the photo
    got a box, while on 110 the carousel template puts image and overlays in one layer and it got
    nothing -- same site, same photo, opposite outcome, decided by an incidental extra <div>.

    An enclosing region is discarded only when its inner regions COVER it (under
    _VISUAL_REGION_INNER_COVER), not merely because one exists. "Keep the deepest" is right for
    finding the tightest box around a single drawn thing -- Amazon's `aok-relative` rather than
    the padded `s-product-image-container` -- and exactly wrong for a photo with a 37x36 control
    on top, where the photo IS the outer box. Both rules now agree: the region that survives is
    the one whose own pixels nothing else explains.
    """
    order = list(root.iter())
    kids = {id(p): list(p) for p in order}

    named_boxes: dict[int, list] = {}
    for node in reversed(order):
        acc: list = []
        for c in kids[id(node)]:
            acc.extend(named_boxes[id(c)])
        if (node.get("name") or "").strip():
            b = _node_bbox(node)
            if b is not None:
                acc.append(b)
        named_boxes[id(node)] = acc

    def _on_screen_box(b):
        """A candidate is judged on the part of it that is ON SCREEN.

        Coverage used to be computed against the raw box, and for a tall container that is the
        wrong denominator: 038_www.bbc.com's `article` is 1835x6837 -- 12.5M px, most of it below
        the fold -- so its named descendants covered under 10% of it and it was promoted to a
        textless visual region spanning the whole page. A labeler cannot judge pixels that are not
        on the screenshot, and a region that large is useless as a mask target either way.
        """
        x, y, w, h = b
        x0, y0 = max(x, 0), max(y, 0)
        x1, y1 = min(x + w, screen_w), min(y + h, screen_h)
        return None if x1 <= x0 or y1 <= y0 else (x0, y0, x1 - x0, y1 - y0)

    candidates = []
    for node in order:
        raw = _node_bbox(node)
        bbox = _on_screen_box(raw) if raw else None
        if bbox is None:
            continue
        x, y, w, h = bbox
        floor = _VISUAL_REGION_LEAF_MIN_SIDE if not kids[id(node)] else _VISUAL_REGION_MIN_SIDE
        if w < floor or h < floor:
            continue
        if x >= screen_w or y >= screen_h or x + w <= 0 or y + h <= 0:
            continue
        if _covered(named_boxes[id(node)], bbox, _VISUAL_REGION_NAMED_COVER):
            continue
        candidates.append(node)

    ids = {id(n) for n in candidates}
    below: dict[int, list] = {}
    for node in reversed(order):
        acc = []
        for c in kids[id(node)]:
            acc.extend(below[id(c)])
        own = _on_screen_box(_node_bbox(node)) if _node_bbox(node) else None
        below[id(node)] = acc + ([own] if (id(node) in ids and own) else [])

    keep = set()
    for node in candidates:
        bbox = _on_screen_box(_node_bbox(node))
        if bbox is None:
            continue
        inner: list = []
        for c in kids[id(node)]:
            inner.extend(below[id(c)])
        if _covered(inner, bbox, _VISUAL_REGION_INNER_COVER):
            continue
        keep.add(id(node))
    return keep




class NodeEvidence:
    """Stage 1 -- what is true of one node. Nothing here decides anything."""

    def __init__(self, node, *, role: str, text: str, bbox: list, screen: tuple,
                 parent_map: dict, is_web: bool, visual_region: bool, embedded_frame: bool,
                 parent_row: "dict | None", text_region: bool = False):
        self.node = node
        self.role = role
        self.text = text
        self.bbox = bbox
        self.parent_map = parent_map
        self.is_web = is_web
        self.visual_region = visual_region
        self.text_region = text_region
        self.embedded_frame = embedded_frame
        self.parent_row = parent_row
        x, y, w, h = bbox
        screen_w, screen_h = screen
        self.area_ok = w > 0 and h > 0
        self.on_screen = (self.area_ok and x < screen_w and y < screen_h
                          and x + w > 0 and y + h > 0)
        self.name_from = node.attrib.get(_ATTR_NS + "name-from") if is_web else None

    @cached_property
    def is_leaf(self) -> bool:
        return _is_leaf(self.node)

    @cached_property
    def in_sr_only(self) -> bool:
        return _in_sr_only(self.node, self.parent_map)

    @cached_property
    def name_unpaintable(self) -> bool:
        _x, _y, w, h = self.bbox
        return _name_cannot_be_painted(self.role, self.text, w, h, self.name_from)

    @cached_property
    def has_painted_descendant(self) -> bool:
        return _has_painted_descendant(self.node, self.parent_map)

    @cached_property
    def descendant_paints_name(self) -> bool:
        return _descendant_paints(self.node, self.text)

    @cached_property
    def is_echo(self) -> bool:
        """This node repeats its parent's name at its parent's exact bounds -- the same pixels
        described twice. The parent must SURVIVE for the outer row to be the one kept; where it
        does not, both rows vanish and the thing they describe becomes unlabelable."""
        p = self.parent_row
        return bool(p is not None and self.text and p["tier"] == "content"
                    and p["text"] == self.text and p["bbox"] == self.bbox)

    @cached_property
    def draws_graphic(self) -> bool:
        """Stage 2 -- visual ownership. An unpainted NAME does not mean an invisible ELEMENT: the
        logo link, the vote arrows and the bookmark button all name themselves by aria-label and
        are plainly on screen as icons. What decides is whether this node renders something of its
        own -- a visual role, a detected textless visual region, or a control with no painted-text
        descendant (an icon button). Containers named by aria-label -- landmarks ("Topbar",
        "sidebar"), sections, the document's own title -- draw nothing and go."""
        return (self.role in _VISUAL_ROLES or self.visual_region
                or (self.role in _CONTROL_ROLES and not self.has_painted_descendant))


def _label_target(ev: NodeEvidence) -> "str | None":
    """Stage 3 -- the skip_reason for this node, or None if a human should judge it.

    Order is load-bearing and is the original chain's, unchanged: cheapest and most absolute first
    (no pixels at all), then representation (another row owns these pixels), then the name-
    provenance rules that need subtree walks.
    """
    if not ev.area_ok:
        return SKIP_ZERO_AREA
    if not ev.on_screen:
        return SKIP_OFFSCREEN
    if ev.in_sr_only or ev.name_unpaintable:
        return SKIP_SR_ONLY
    if ev.is_echo:
        return SKIP_ECHO
    if ev.embedded_frame:
        return SKIP_EMBEDDED_FRAME
    if ev.is_web and ev.text and ev.name_from is None and not ev.draws_graphic and not ev.text_region:
        return SKIP_INJECTED_NAME
    if (ev.text and ev.name_from in _UNPAINTED_NAME_SOURCES and not ev.draws_graphic
            and not ev.is_leaf and not ev.descendant_paints_name and not ev.text_region):
        return SKIP_UNPAINTED_NAME
    if (not ev.text and ev.role not in _LABELABLE_UNNAMED_ROLES
            and not ev.visual_region and not ev.text_region):
        return SKIP_UNNAMED
    return None


def _screen_size(filename: str) -> tuple[int, int]:
    png = os.path.join(SCREENSHOT_DIR, os.path.splitext(filename)[0] + ".png")
    if not os.path.exists(png):
        return DEFAULT_SCREEN
    try:
        from PIL import Image
        with Image.open(png) as im:
            return im.size
    except Exception:
        return DEFAULT_SCREEN


def _sig(role: str, bbox: list, text: str, ordinal: int) -> str:
    x, y, w, h = bbox
    raw = f"{role}|{x},{y},{w},{h}|{text}|{ordinal}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def platform_for(filename: str, manifest: "dict | None" = None) -> "str | None":
    """"app", "web" or None for a capture -- which content roots resolve_content_roots() looks for.

    Split out of build_file_universe because it is not only the universe that needs it. The rule
    PROPOSER resolves the same tree independently, and it used to do so through
    full_tree_extraction._content_root() -- "the first document-web node" -- which is a web-only
    question. On a native capture that returned either nothing (33 captures: GTK apps expose no
    document-web at all) or the wrong subtree: on 207_thunderbird it found the quoted HTML inside
    the compose window and handed the model a 61-character skeleton of a mail quote, while the
    universe was resolving two native frames and 199 rows. The model answered `[]`, an empty
    allowlist means default-deny, and the mask came out as a black window. 39 of 160 captures had
    no rules for this reason and every one of them was a native capture.

    None means "let resolve_content_roots decide", which it does correctly from the tree; the
    manifest is consulted first only so that a capture the corpus knows to be web does not get
    re-derived on every call.
    """
    manifest = load_manifest() if manifest is None else manifest
    mrow = manifest.get(filename, {})
    platform = mrow.get("platform")
    return "app" if platform == "app" else ("web" if mrow else None)


def build_file_universe(filename: str, xml_dir: str = XML_DIR,
                        manifest: dict | None = None) -> dict:
    """Every bbox-carrying node inside every content root of one capture, in document order.

    Per-row fields beyond the obvious: `tier` ("content" -- named, non-empty, on-screen, so a human
    can actually look at it and judge; "structural" -- everything else, which receives a derived
    label instead of a keystroke), `skip_reason` (why a row is structural, or None), `visible`,
    `parent` (elem_index of the nearest ancestor that is itself in this universe, or None at a
    root), and `sig` (see module docstring).

    THIS is the corpus entry point: it resolves a capture off disk (XML, manifest row, screenshot
    size, DOM curation) and hands the parsed tree to build_universe_from_root(), which holds the
    actual walk. The split exists so a LIVE caller -- mm_agents/sa_agent/masking.py, which has an
    a11y tree and a screenshot in memory and no corpus at all -- runs the identical walk instead of
    a second copy of it that drifts. Nothing about the offline behaviour changes: every argument
    this function used to derive from disk is still derived from disk, here.
    """
    manifest = load_manifest() if manifest is None else manifest
    mrow = manifest.get(filename, {})
    path = os.path.join(xml_dir, filename)
    with open(path, encoding="utf-8", errors="replace") as f:
        root = ET.fromstring(f.read())

    return build_universe_from_root(
        root,
        platform=platform_for(filename, manifest),
        screen_size=_screen_size(filename),
        filename=filename,
        meta=mrow,
        dom_excluded=dom_excluded_sigs(filename),
        screenshot_name=os.path.splitext(filename)[0] + ".png",
        has_screenshot=os.path.exists(
            os.path.join(SCREENSHOT_DIR, os.path.splitext(filename)[0] + ".png")),
    )


def correct_menu_bar_offset(root: ET.Element) -> "tuple[int, int]":
    """LibreOffice reports every widget BELOW the menu bar too high, by the menu bar's own height.
    Rewrites `cp:screencoord` in place and returns (offset, nodes_moved); (0, 0) when it does not
    apply.

    THE BUG. VCL's accessibility layer reports the menu bar and the window content in the same
    coordinate frame and never offsets the content by the menu bar. The signature is exact and
    needs no threshold -- the first tool bar starts at the menu bar's OWN y instead of at its
    bottom, i.e. two siblings of a vertical stack claiming the same origin:

        menu bar      [70, 64, 1826, 25]
        1st tool bar  [70, 64, 1262, 39]      <- must be at y=89

    Everything below inherits it: on 219 the sheet's first data row is reported at y=201 and paints
    at 226, and 226 - 201 is the menu bar's 25. Verified on all seven LibreOffice captures in the
    corpus (Writer 214-216, Calc 217-219, Impress 244), menu bar 25px in every one. VS Code, whose
    menu bar and toolbars nest correctly, does not match the signature and is untouched.

    WHY THE CORRECTION IS NOT A CONSTANT. It is the menu bar's own reported height, read from the
    tree per capture, so a different theme, font scale or DPI corrects itself. Nothing here is
    tuned; the detector is an equality and the offset is a measurement.

    WHAT IS NOT MOVED. The menu bar and its items are correct already. So are the menu bar's
    ANCESTORS -- a container holding both the menu bar and the content has the right origin and
    only the wrong height, and moving its top would break the one edge it gets right. Everything
    else at or below the menu bar's y moves down.
    """
    menu_bars = [n for n in root.iter()
                 if _atspi_tag_to_role(n.tag) == "menu bar" and (_node_bbox(n) or (0, 0, 0, 0))[3] > 0]
    tool_bars = [n for n in root.iter()
                 if _atspi_tag_to_role(n.tag) == "tool bar" and (_node_bbox(n) or (0, 0, 0, 0))[3] > 0]
    if not menu_bars or not tool_bars:
        return 0, 0
    mb = min(menu_bars, key=lambda n: _node_bbox(n)[1])
    tb = min(tool_bars, key=lambda n: _node_bbox(n)[1])
    mb_box, tb_box = _node_bbox(mb), _node_bbox(tb)
    if tb_box[1] != mb_box[1]:
        return 0, 0
    offset = mb_box[3]
    if offset <= 0:
        return 0, 0

    parent_map = {c: p for p in root.iter() for c in p}
    exempt = {id(n) for n in mb.iter()}
    cur = parent_map.get(mb)
    while cur is not None:
        exempt.add(id(cur))
        cur = parent_map.get(cur)

    moved = 0
    for node in root.iter():
        if id(node) in exempt:
            continue
        box = _node_bbox(node)
        if box is None or box[1] < mb_box[1]:
            continue
        node.set(_q("cp", "screencoord"), str((box[0], box[1] + offset)))
        moved += 1
    return offset, moved


def build_universe_from_root(root, *, platform: "str | None", screen_size: tuple,
                             filename: str = "<live>", meta: "dict | None" = None,
                             dom_excluded: "set | None" = None,
                             screenshot_name: "str | None" = None,
                             has_screenshot: bool = False) -> dict:
    """The universe walk itself, over an already-parsed tree. No disk access whatsoever.

    Callers: build_file_universe() above (corpus), and the SA-Agent runtime adapter (live). Keeping
    ONE walk is the point -- `query_regions()` zips its rows against a re-walk of the same tree by
    position, so a live builder that enumerated nodes even slightly differently would mis-attribute
    region ownership silently, with no error anywhere. See assert_row_node_alignment().

    `meta` is the manifest row offline and whatever the runtime knows (url/title) live; every field
    read from it is optional and absent-tolerant.
    """
    meta = meta or {}
    dom_excluded = dom_excluded or set()
    _mb_offset, _mb_moved = correct_menu_bar_offset(root)
    descriptors, platform_used = resolve_content_roots(root, platform)

    screen_w, screen_h = screen_size
    rows: list[dict] = []
    ordinals: Counter = Counter()

    inactive_tab_ids = _inactive_tab_node_ids(root)

    for root_index, desc in enumerate(descriptors):
        is_web = desc["kind"] == "web"
        parent_map = {c: p for p in desc["node"].iter() for c in p}
        _layers = popup_layers(desc["node"], parent_map) if is_web else []
        desc["layers"] = [panel for _n, panel in _layers]
        _layer_of = {d: k for k, (n, _p) in enumerate(_layers, start=1) for d in n.iter()}
        visual_regions = _visual_regions(desc["node"], parent_map, screen_w, screen_h)

        embedded: set = set()
        for sub in (desc["node"].iter() if desc["kind"] == "web" else ()):
            if sub.tag != "document-web" or sub is desc["node"]:
                continue
            for inner in sub.iter():
                if inner is not sub:
                    embedded.add(id(inner))
            frame_bbox = _node_bbox(sub)
            holder = parent_map.get(sub)
            while holder is not None and _node_bbox(holder) is None:
                holder = parent_map.get(holder)
            if frame_bbox is not None and holder is not None and _node_bbox(holder) == frame_bbox:
                embedded.add(id(sub))

        for sub in (desc["node"].iter() if desc["kind"] == "web" else ()):
            if sub.tag != "document-web" or sub is desc["node"]:
                continue
            frame_bbox = _node_bbox(sub)
            if frame_bbox is None or frame_bbox[2] * frame_bbox[3] <= 0:
                continue
            roots = _frame_paint_roots(sub, frame_bbox)
            if len(roots) != 1:
                continue
            root_bbox = _node_bbox(roots[0])
            root_area = root_bbox[2] * root_bbox[3]
            frame_area = frame_bbox[2] * frame_bbox[3]
            if root_area <= _FRAME_PAINT_MIN * frame_area:
                continue
            if root_area >= _FRAME_PAINT_COVER * frame_area:
                continue
            visual_regions.add(id(roots[0]))
            embedded.discard(id(roots[0]))
            embedded.add(id(sub))
            holder = parent_map.get(sub)
            while holder is not None and holder is not desc["node"]:
                holder_bbox = _node_bbox(holder)
                if holder_bbox is not None and holder_bbox != frame_bbox:
                    break
                if holder_bbox == frame_bbox:
                    embedded.add(id(holder))
                holder = parent_map.get(holder)
        index_of: dict[int, int] = {}

        for node, bbox in row_nodes(desc["node"], parent_map):
            text_payload = _text_region_payload(node)
            x, y, w, h = bbox
            text = (node.get("name") or "").strip()
            role = effective_role(node)

            if role in _CONTROL_ROLES and text and w * h <= _UNPAINTABLE_AREA:
                adopted = _label_box_for_control(node, parent_map)
                if adopted:
                    bbox = list(adopted)
                    x, y, w, h = bbox

            area_ok = w > 0 and h > 0
            on_screen = area_ok and x < screen_w and y < screen_h and x + w > 0 and y + h > 0

            parent = None
            skipped_ancestors = []
            cur = parent_map.get(node)
            while cur is not None:
                if id(cur) in index_of:
                    parent = index_of[id(cur)]
                    break
                sig = {"role": effective_role(cur)}
                if cur.get(_q("attr", "id")):
                    sig["id"] = cur.get(_q("attr", "id"))
                if cur.get(_q("attr", "class")):
                    sig["class"] = cur.get(_q("attr", "class"))
                if len(sig) > 1:
                    skipped_ancestors.append(sig)
                cur = parent_map.get(cur)

            ev = NodeEvidence(
                node, role=role, text=text, bbox=[x, y, w, h], screen=(screen_w, screen_h),
                parent_map=parent_map, is_web=is_web,
                visual_region=id(node) in visual_regions,
                text_region=bool(text_payload),
                embedded_frame=id(node) in embedded,
                parent_row=rows[parent] if parent is not None else None,
            )
            if ev.name_from is not None and ev.name_from not in _KNOWN_NAME_SOURCES:
                UNKNOWN_NAME_SOURCES[ev.name_from] += 1
            skip_reason = _label_target(ev)
            if skip_reason is None and id(node) in inactive_tab_ids:
                skip_reason = SKIP_OFFSCREEN

            key = (role, x, y, w, h, text)
            ordinal = ordinals[key]
            ordinals[key] += 1

            index_of[id(node)] = len(rows)
            rows.append({
                "file": filename,
                "elem_index": len(rows),
                "sig": _sig(role, [x, y, w, h], text, ordinal),
                "role": role,
                "text": text,
                "bbox": [x, y, w, h],
                "visible": on_screen,
                "tier": "structural" if skip_reason else "content",
                "skip_reason": skip_reason,
                "paint_status": paint_status_of(skip_reason)[0],
                "represented_elsewhere": paint_status_of(skip_reason)[1],
                "name_from": ev.name_from,
                "symbolic": not _has_alnum(text),
                "visual_region": ev.visual_region,
                "text_region": ev.text_region,
                "text_content": text_payload[:2000],
                "capacity": capacity.capacity_class(text, text_payload),
                "capacity_fragment": False,
                "parent": parent,
                "skipped_ancestors": skipped_ancestors,
                "root_index": root_index,
                "layer": _layer_of.get(node, 0),
                "root_kind": desc["kind"],
                "root_provenance": desc["provenance"],
                "attr_class": node.get(_q("attr", "class")),
                "attr_id": node.get(_q("attr", "id")),
            })

    _mark_capacity_fragments(rows)
    _demote_names_painted_by_descendants(rows)
    _demote_control_surfaces(rows)
    _apply_dom_curation(rows, dom_excluded)
    _sync_paint_fields(rows)

    return {
        "file": filename,
        "index": _numeric_prefix(filename),
        "platform": platform_used,
        "category": meta.get("category"),
        "url": meta.get("url"),
        "title": meta.get("title"),
        "capture_status": meta.get("status"),
        "screen": [screen_w, screen_h],
        "screenshot": screenshot_name,
        "has_screenshot": has_screenshot,
        "roots": [{k: v for k, v in d.items() if k != "node"} for d in descriptors],
        "rows": rows,
    }


def _mark_capacity_fragments(rows: list) -> int:
    """Flags every capacity-bound row that is a PIECE of a rendered line rather than a value in its
    own slot. -> number flagged.

    THE RULE (gate "A"): a text-bearing sibling under the same parent overlaps this row's vertical
    band. Nothing else -- no gap, no count, no length, no role list. Vertical overlap of two boxes
    is a predicate, not a measurement, so there is no constant here for an attacker to slide under.

    WHY IT EXISTS. `capacity.py` asks whether a string can carry an instruction and answers
    honestly for `256`: no. What it cannot ask is whether `256` is a VALUE or a token of somebody
    else's prose. On 016_stackoverflow a highlight.js C++ block inside the question body puts every
    numeric literal in its own AT-SPI node, so `256`, `128`, `32768` and four `0`s were exempted
    and showed through a masked code block -- eight tokens of attacker-posted source, sharing their
    line with `rand`, `() %` and `;`. A value occupies a slot; a fragment is part of a run.

    WHAT THIS IS AND IS NOT. It is HYGIENE, not a security control, and it must never be what any
    safety argument rests on. Every layout- or tree-shaped gate is evadable, because the attacker
    controls the shape of their own content: this one is dodged by putting each value alone on its
    line, and the parent-scoped comparison is dodged by wrapping each piece in its own element.
    The rule does not decide WHETHER an attacker gets the exemption, only what shape they must
    adopt to get it. What makes the exemption safe regardless is that the strings it admits are
    drawn from a closed, verbless vocabulary -- see capacity.py's two pinned tests.

    WHAT IT COSTS, MEASURED. 682 of 2,059 exempt rows corpus-wide, including 190 file-manager
    sizes and dates (`13 KB`, `2 years ago`) that are perfectly good values sharing a line with a
    filename. That over-masking was chosen deliberately over the alternatives: a role-based test
    caught only 24% of the population (186 rows) because a `<div>` parent is indistinguishable
    from an inline wrapper, and the pure-screen version -- any text anywhere on the same baseline
    -- refused 2,059 of 2,059, i.e. switched the feature off.
    """
    kids: dict = {}
    for row in rows:
        if row["parent"] is not None:
            kids.setdefault(row["parent"], []).append(row)
    flagged = 0
    for row in rows:
        if not row.get("capacity") or row["parent"] is None:
            continue
        _x, y, _w, h = row["bbox"]
        for sib in kids.get(row["parent"], ()):
            if sib["elem_index"] == row["elem_index"] or not sib["text"].strip():
                continue
            sy, sh = sib["bbox"][1], sib["bbox"][3]
            if y < sy + sh and sy < y + h:
                row["capacity_fragment"] = True
                flagged += 1
                break
    return flagged


def assert_row_node_alignment(universe: dict, content_root) -> None:
    """Guard the invariant `regions_from_tree()` silently depends on.

    Its `index_of` maps tree nodes to rows by ZIPPING a fresh walk of the content root against
    `universe["rows"]`, which is only correct while both enumerate the same nodes in the same
    order. That held by construction when one function did both; with the walk now split across
    build_universe_from_root() and its callers, an ordering change in either place would produce
    regions that describe the wrong elements -- and produce them quietly, since a zip of unequal
    sequences just stops early. Cheap to check, so check it.
    """
    walked = row_nodes(content_root)
    rows = [r for r in universe.get("rows", []) if r.get("root_index") == 0]
    if len(walked) != len(rows):
        raise AssertionError(
            f"row/node walk misaligned for {universe.get('file')!r}: "
            f"{len(walked)} geometry nodes vs {len(rows)} rows in root 0. "
            "build_universe_from_root() and the region walk must enumerate identically.")


def _numeric_prefix(filename: str) -> int:
    m = re.match(r"^(\d+)_", filename)
    return int(m.group(1)) if m else -1


EXCLUDE_BOT_WALLS = True


def bot_walled_files(manifest: dict | None = None) -> set[str]:
    """Filenames the detector marked as anti-bot challenge pages."""
    manifest = load_manifest() if manifest is None else manifest
    return {fn for fn, row in manifest.items() if row.get("bot_wall")}


def corpus_files(xml_dir: str = XML_DIR) -> list[str]:
    names = sorted((os.path.basename(p) for p in glob.glob(os.path.join(xml_dir, "*.xml"))),
                   key=lambda f: (_numeric_prefix(f), f))
    if EXCLUDE_BOT_WALLS:
        walled = bot_walled_files()
        names = [f for f in names if f not in walled]
    return names


def _schema_key() -> str:
    """The cache's version, wide enough to cover the code the schema number cannot see.

    UNIVERSE_SCHEMA has to be bumped by hand, and the failure when it is forgotten is the silent
    one the constant's own comment describes. capacity.py's grammars shape a row field, so a
    grammar edit is exactly that kind of change -- and it is the kind someone tunes repeatedly
    while red-teaming it. Folding CAPACITY_SCHEMA in makes that half automatic.
    """
    return f"{UNIVERSE_SCHEMA}.{capacity.CAPACITY_SCHEMA}"


def _cache_stamp(xml_dir: str) -> dict:
    """Every input a built universe depends on, so a stale cache cannot masquerade as fresh.

    labels/dom_excluded.json is in here for the same reason UNIVERSE_SCHEMA is: it changes rows
    (curated ones become `not_laid_out`) without touching a single XML. Re-running
    dom_curation.py --apply over the full corpus after a 20-capture trial left the on-disk cache
    describing the trial, and every reader -- label_gt included -- saw uncurated captures with no
    way to tell."""
    stamp = {os.path.basename(p): [os.path.getmtime(p), os.path.getsize(p)]
             for p in glob.glob(os.path.join(xml_dir, "*.xml"))}
    if os.path.exists(EXCLUDED_PATH):
        stamp["@dom_excluded"] = [os.path.getmtime(EXCLUDED_PATH),
                                  os.path.getsize(EXCLUDED_PATH)]
    return stamp


def build_universe(xml_dir: str = XML_DIR, use_cache: bool = False,
                   files: list[str] | None = None,
                   write_cache: bool = False) -> dict[str, dict]:
    """{filename: file universe}.

    CACHING IS OFF BY DEFAULT, and that is a deliberate correctness-over-speed trade. The cache
    key is the XML stamp plus UNIVERSE_SCHEMA plus dom_excluded.json -- it cannot see the CODE, so
    every rule change here has to remember to bump the schema, and forgetting produces the worst
    possible failure: a fix verified in a fresh build, a server restarted, and the fix simply not
    there, with nothing anywhere reporting a problem. That happened repeatedly. A full rebuild of
    311 captures takes ~15s, which is cheaper than one round of doubting a change that shipped
    correctly. Pass use_cache=True explicitly if a caller genuinely needs the speed.

    Cached to labels/.universe_cache.json keyed by every XML's
    (mtime, size), so the ~250-file parse happens once rather than on every server start -- and
    invalidates itself automatically when a re-capture rewrites the corpus.

    `write_cache=False` is for callers that deliberately build under NON-DEFAULT settings --
    dom_curation flips DOM_CURATION_ENABLED off to avoid a self-referential build, and the
    result must not be published as the shared cache. The cache key is the XML stamp plus the
    schema; it cannot see a module flag, so a cache written under a flipped flag looks perfectly
    valid to the next reader and silently serves an uncurated universe."""
    stamp = _cache_stamp(xml_dir)
    if use_cache and files is None and os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                cached = json.load(f)
            if (cached.get("stamp") == stamp and cached.get("xml_dir") == xml_dir
                    and cached.get("schema") == _schema_key()):
                return cached["universe"]
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    manifest = load_manifest()
    targets = files if files is not None else corpus_files(xml_dir)
    universe: dict[str, dict] = {}
    for filename in targets:
        try:
            universe[filename] = build_file_universe(filename, xml_dir, manifest)
        except (ET.ParseError, OSError) as e:
            universe[filename] = {
                "file": filename, "index": _numeric_prefix(filename), "platform": None,
                "category": None, "url": None, "title": None, "capture_status": "parse_error",
                "screen": list(DEFAULT_SCREEN), "screenshot": None, "has_screenshot": False,
                "roots": [], "rows": [], "error": str(e),
            }

    if files is None and write_cache:
        os.makedirs(LABELS_DIR, exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"xml_dir": xml_dir, "stamp": stamp, "schema": _schema_key(),
                       "universe": universe}, f)
        os.replace(tmp, CACHE_PATH)
    return universe


def descendants(fu: dict, elem_index: int) -> list[int]:
    """elem_indexes of every node below `elem_index` in the same capture -- the subtree-propagate
    set, and the set a structural node's derived label is joined over."""
    by_parent: dict[int, list[int]] = defaultdict(list)
    for row in fu["rows"]:
        if row["parent"] is not None:
            by_parent[row["parent"]].append(row["elem_index"])
    out, stack = [], list(by_parent.get(elem_index, ()))
    while stack:
        i = stack.pop()
        out.append(i)
        stack.extend(by_parent.get(i, ()))
    return sorted(out)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append_records(records: list[dict]) -> None:
    """One fsync'd append per commit. Bulk operations (a whole page, a subtree) pass their records
    together so a page never lands half-written, but the file itself is only ever appended to."""
    if not records:
        return
    os.makedirs(LABELS_DIR, exist_ok=True)
    with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def make_decision(file: str, elem_index: int, sig: str, verdict: str, mode: str,
                  origin: int | None = None, note: str | None = None) -> dict:
    if verdict not in _VERDICTS:
        raise ValueError(f"verdict must be one of {_VERDICTS}, got {verdict!r}")
    return {"type": "set", "file": file, "elem_index": elem_index, "sig": sig,
            "verdict": verdict, "mode": mode, "origin": origin, "note": note, "at": _now()}


def set_by_text(universe: dict[str, dict], text: str, verdict: str, role: str | None = None,
                only_file: str | None = None, changed_only: bool = True) -> list[dict]:
    """Finds every ALREADY-LABELED element whose text matches `text` exactly and returns the
    correction records needed to set it to `verdict` (caller decides whether to write them).

    This exists because the repeated-UI-element mistake is the one that scales badly by hand: a
    separator, byline glyph or icon that appears once per comment gets the same wrong verdict on
    every page it occurs, and hunting them down one at a time in the UI costs more than the
    original labeling did. Recorded with mode "correction" -- a deliberate human judgment about
    the whole class, so it counts as hand-labeled, but distinguishable in the journal from an
    element that was individually looked at.

    Only touches elements that already carry a decision: an unlabeled element is left for the
    normal flow rather than being silently pre-filled from a text match.
    """
    if verdict not in _VERDICTS:
        raise ValueError(f"verdict must be one of {_VERDICTS}, got {verdict!r}")
    state = effective_decisions()
    out = []
    for filename, file_state in state.items():
        if only_file and filename != only_file:
            continue
        fu = universe.get(filename)
        if fu is None:
            continue
        by_index, _ = align_decisions(fu, file_state)
        for elem_index, rec in by_index.items():
            row = fu["rows"][elem_index]
            if row["text"] != text:
                continue
            if role is not None and row["role"] != role:
                continue
            if changed_only and rec["verdict"] == verdict:
                continue
            out.append(make_decision(filename, elem_index, row["sig"], verdict, "correction",
                                     note=f"bulk: text=={text!r} -> {verdict}"))
    return out


def make_reset(file: str, note: str | None = None) -> dict:
    """Invalidates every decision recorded for `file` up to this point. This is what --relabel
    writes: without it, re-opening an already-labeled capture shows the old boxes and every one you
    don't happen to revisit silently survives as ground truth."""
    return {"type": "reset", "file": file, "note": note, "at": _now()}


def make_drop(file: str, sig: str, note: str | None = None) -> dict:
    """Retires ONE decision, where `reset` retires a whole file. Written for orphans: after a
    re-capture, a decision whose element no longer exists aligns to nothing, so it is neither
    ground truth nor a to-do -- it just accumulates in --stats. Dropping it says "this element is
    gone, the label is retired" without discarding the rest of the file's work, and the record
    stays in the journal so --history still shows what was once decided and why it went away."""
    return {"type": "drop", "file": file, "sig": sig, "note": note, "at": _now()}


def read_journal(path: str | None = None) -> list[dict]:
    path = JOURNAL_PATH if path is None else path
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"WARN: {os.path.basename(path)}:{lineno} is not valid JSON, skipped",
                      file=sys.stderr)
    return out


def effective_decisions(records: list[dict] | None = None) -> dict[str, dict[str, dict]]:
    """{file: {sig: decision}} -- the current state. Applies the three override mechanisms in
    order: a `reset` retires everything recorded for that file so far, a `drop` retires one sig,
    and within what survives, the last `set` for a sig wins. A `set` after a `drop` revives the
    sig -- position in the journal is what decides, not record type."""
    records = read_journal() if records is None else records
    state: dict[str, dict[str, dict]] = defaultdict(dict)
    for rec in records:
        if rec.get("type") == "reset":
            state[rec["file"]] = {}
        elif rec.get("type") == "drop":
            state[rec["file"]].pop(rec["sig"], None)
        elif rec.get("type") == "set":
            state[rec["file"]][rec["sig"]] = rec
    return dict(state)


def history_for(file: str, sig: str, records: list[dict] | None = None) -> list[dict]:
    records = read_journal() if records is None else records
    out = []
    for rec in records:
        if rec.get("file") != file:
            continue
        if rec.get("type") == "reset":
            out.append(rec)
        elif rec.get("type") in ("set", "drop") and rec.get("sig") == sig:
            out.append(rec)
    return out


def align_decisions(fu: dict, decided: dict[str, dict]) -> tuple[dict[int, dict], list[dict]]:
    """Maps this capture's stored decisions onto its CURRENT rows, returning
    ({elem_index: decision}, [orphans]). `elem_index` is trusted only when the row sitting there
    still has the recorded sig; otherwise the sig is looked up across the file. A decision whose
    sig is gone entirely (the element no longer exists after a re-capture) is returned as an
    orphan rather than dropped, so it shows up in --stats instead of vanishing."""
    by_sig = {row["sig"]: row for row in fu["rows"]}
    rows = fu["rows"]
    out: dict[int, dict] = {}
    orphans: list[dict] = []
    lifted: list[tuple[int, dict]] = []
    for sig, rec in decided.items():
        row = by_sig.get(sig)
        if row is None:
            orphans.append(rec)
            continue
        if row["tier"] != "content" and row["skip_reason"] in ("control_paints", "echo"):
            up = rows[row["parent"]] if row["parent"] is not None else None
            while up is not None and up["tier"] != "content":
                up = rows[up["parent"]] if up["parent"] is not None else None
            if up is not None:
                lifted.append((up["elem_index"], rec))
                continue
        out[row["elem_index"]] = rec
    for index, rec in lifted:
        out.setdefault(index, rec)
    return out, orphans


def derive_structural(fu: dict, by_index: dict[int, dict]) -> dict[int, str]:
    """Labels for structural rows (unnamed containers, zero-area and off-screen nodes) that carry
    no hand decision: the join over their descendants -- untrusted if any descendant is untrusted,
    trusted if any descendant is labeled at all and none is untrusted, absent otherwise.

    This is the Biba-correct reading of a container (a div wrapping a UGC comment IS tainted) and
    it is what keeps the exported ground truth complete over the whole bbox universe while the
    human only ever presses keys on rows that have something readable in them. Off-screen rows are
    deliberately NOT filled in this way when they have no labeled descendants -- they are left
    absent (verdict null) rather than guessed at, since nothing on the screenshot justifies a
    verdict for them.
    """
    children: dict[int, list[int]] = defaultdict(list)
    for row in fu["rows"]:
        if row["parent"] is not None:
            children[row["parent"]].append(row["elem_index"])

    derived: dict[int, str] = {}

    def resolve(i: int) -> str | None:
        rec = by_index.get(i)
        if rec is not None:
            return rec["verdict"]
        if i in derived:
            return derived[i]
        verdicts = [v for v in (resolve(c) for c in children.get(i, ())) if v is not None]
        if not verdicts:
            return None
        value = "untrusted" if "untrusted" in verdicts else "trusted"
        derived[i] = value
        return value

    for row in fu["rows"]:
        if row["elem_index"] not in by_index:
            resolve(row["elem_index"])
    return derived


def compact(path: str | None = None) -> tuple[int, int]:
    """Rewrites the journal down to its effective records, archiving the previous one under
    labels/_archive/. Returns (records_before, records_after)."""
    path = JOURNAL_PATH if path is None else path
    records = read_journal(path)
    if not records:
        return 0, 0
    state = effective_decisions(records)
    kept = [rec for file_state in state.values() for rec in file_state.values()]
    kept.sort(key=lambda r: (r["file"], r["elem_index"]))

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copy2(path, os.path.join(ARCHIVE_DIR, f"{os.path.basename(path)}.{stamp}.bak"))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in kept:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return len(records), len(kept)


def _modal_suppressed(fu: dict) -> bool:
    """Flags a capture as WORTH EYEBALLING before labeling -- deliberately not a verdict.

    It fires when a page lays out large but exposes very few accessible elements, which is the
    signature of an open modal having hidden the document (see _SUPPRESSED_MIN_EXTENT). But that
    same shape is produced by a perfectly healthy page that is simply image-heavy or renders most
    of its content without accessible names, and the consent-vocabulary test additionally fires on
    any page merely carrying a "Cookies policy" link in its footer. Measured on the 2026-08-12
    corpus it selects 21 captures of which only a few are truly suppressed (012_www.ebay.com,
    106_www.booking.com confirmed by inspection). Treat the output as a review queue: open the
    screenshot, compare it against --dump, and re-capture only what is actually broken.

    Web captures only; a native app window legitimately exposes few nodes and has no aria-hidden
    equivalent."""
    if fu.get("platform") != "web":
        return False
    rows = fu["rows"]
    if len(rows) >= _SUPPRESSED_MAX_NODES:
        return False
    extent = max((r["bbox"][1] + r["bbox"][3] for r in rows), default=0)
    if extent < _SUPPRESSED_MIN_EXTENT:
        return False
    return any(r["role"] == "dialog" for r in rows) or any(
        any(w in r["text"].lower() for w in _CONSENT_WORDS) for r in rows)


def coverage(universe: dict[str, dict], state: dict[str, dict] | None = None) -> dict:
    """Corpus-wide counts. The coverage denominator is CONTENT-tier rows only -- structural rows
    get derived labels and off-screen rows are out of scope for hand labeling, so counting them
    would understate progress against work that is never going to be done by keystroke."""
    state = effective_decisions() if state is None else state
    totals = Counter()
    per_file = []
    qa = {"no_roots": [], "empty_a11y_document": [], "sparse_tree_review": [],
          "no_content_rows": [], "no_screenshot": [], "parse_error": [], "capture_error": [],
          "orphans": []}

    for filename, fu in universe.items():
        rows = fu["rows"]
        content = [r for r in rows if r["tier"] == "content"]
        by_index, orphans = align_decisions(fu, state.get(filename, {}))
        labeled = sum(1 for r in content if r["elem_index"] in by_index)

        totals["files"] += 1
        totals["rows"] += len(rows)
        totals["content"] += len(content)
        totals["labeled"] += labeled
        totals["roots"] += len(fu["roots"])
        for r in rows:
            if r["skip_reason"]:
                totals["skip_" + r["skip_reason"]] += 1
            elif r.get("visual_region"):
                totals["visual_region"] += 1
            elif r.get("symbolic"):
                totals["symbolic"] += 1
        totals["decisions"] += len(by_index)

        if not fu["roots"]:
            qa["no_roots"].append(filename)
        elif all(not r.get("children") for r in fu["roots"]):
            qa["empty_a11y_document"].append(filename)
        elif _modal_suppressed(fu):
            qa["sparse_tree_review"].append(filename)
        elif not content:
            qa["no_content_rows"].append(filename)
        if not fu["has_screenshot"]:
            qa["no_screenshot"].append(filename)
        if fu.get("error"):
            qa["parse_error"].append(filename)
        if fu.get("capture_status") not in (None, "ok"):
            qa["capture_error"].append(filename)
        if orphans:
            qa["orphans"].append((filename, len(orphans)))

        per_file.append({
            "file": filename, "platform": fu["platform"], "roots": len(fu["roots"]),
            "rows": len(rows), "content": len(content), "labeled": labeled,
            "done": bool(content) and labeled == len(content),
        })

    totals["files_done"] = sum(1 for p in per_file if p["done"])
    return {"totals": dict(totals), "per_file": per_file, "qa": qa}


def next_unlabeled(universe: dict[str, dict], state: dict[str, dict] | None = None,
                   start_file: str | None = None) -> tuple[str, int] | None:
    """(file, elem_index) of the first content-tier row with no decision, scanning captures in
    corpus order from `start_file` (wrapping around to the beginning). None when nothing is left."""
    state = effective_decisions() if state is None else state
    files = sorted(universe, key=lambda f: (_numeric_prefix(f), f))
    if start_file in files:
        i = files.index(start_file)
        files = files[i:] + files[:i]
    for filename in files:
        fu = universe[filename]
        by_index, _ = align_decisions(fu, state.get(filename, {}))
        for row in fu["rows"]:
            if row["tier"] == "content" and row["elem_index"] not in by_index:
                return filename, row["elem_index"]
    return None


def resolve_file(universe: dict[str, dict], token: str) -> str:
    """Accepts a numeric prefix ('008', '8') or any unique substring of a filename."""
    if token in universe:
        return token
    if token.isdigit():
        want = int(token)
        hits = [f for f in universe if _numeric_prefix(f) == want]
        if len(hits) == 1:
            return hits[0]
    hits = [f for f in universe if token in f]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise SystemExit(f"No capture matches {token!r}")
    raise SystemExit(f"{token!r} is ambiguous: {', '.join(sorted(hits)[:8])}")


def _print_stats(universe: dict, per_file: bool) -> None:
    cov = coverage(universe)
    t = cov["totals"]
    pct = (100.0 * t["labeled"] / t["content"]) if t.get("content") else 0.0
    print(f"captures            {t['files']}  ({t['files_done']} fully labeled)")
    print(f"content roots       {t['roots']}")
    print(f"bbox rows           {t['rows']}")
    print(f"  content tier      {t['content']}   <- hand-labeled")
    print(f"    of which symbolic {t.get('symbolic', 0)}  (icons, punctuation -- judged from "
          f"the pixels, not text)")
    print(f"    visual regions   {t.get('visual_region', 0)}  (textless boxes: product photos and "
          f"other images the tree exposes no element for)")
    for reason in ("unnamed", "echo", "zero_area", "offscreen"):
        n = t.get("skip_" + reason, 0)
        if n:
            print(f"  structural: {reason:11} {n}")
    print(f"labeled             {t['labeled']} / {t['content']}  ({pct:.1f}%)")

    qa = cov["qa"]
    flagged = {k: v for k, v in qa.items() if v}
    if flagged:
        print("\ncapture QA")
        for key, items in flagged.items():
            if key == "sparse_tree_review":
                print("  sparse_tree_review  -- large layout, few accessible elements. NOT a verdict:")
                print("     inspect the screenshot before labeling; some are healthy image-heavy pages.")
            shown = ", ".join(str(i) for i in items[:6])
            more = f" (+{len(items) - 6} more)" if len(items) > 6 else ""
            print(f"  {key:16} {len(items):4}  {shown}{more}")

    if per_file:
        print(f"\n{'file':44} {'plat':6} {'roots':>5} {'rows':>6} {'content':>7} {'labeled':>7}")
        print("-" * 80)
        for p in cov["per_file"]:
            print(f"{p['file']:44} {str(p['platform']):6} {p['roots']:5} {p['rows']:6} "
                  f"{p['content']:7} {p['labeled']:7}{'  done' if p['done'] else ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xml_dir", default=XML_DIR)
    ap.add_argument("--stats", action="store_true", help="Coverage + capture-QA report")
    ap.add_argument("--per_file", action="store_true", help="With --stats: one line per capture")
    ap.add_argument("--dump", metavar="FILE", help="Print one capture's universe (numeric prefix or substring)")
    ap.add_argument("--tier", choices=["content", "structural", "all"], default="content",
                    help="With --dump: which rows to print (default: content)")
    ap.add_argument("--history", metavar="FILE", help="Print the decision history for one element")
    ap.add_argument("--elem", type=int, help="With --history: elem_index")
    ap.add_argument("--set_text", metavar="TEXT",
                    help="Bulk-correct every already-labeled element whose text is exactly TEXT "
                         "(e.g. --set_text '–' --verdict trusted). Use for a repeated UI "
                         "element you labeled the same wrong way on many pages.")
    ap.add_argument("--verdict", choices=list(_VERDICTS), help="With --set_text")
    ap.add_argument("--role", help="With --set_text: also require this role")
    ap.add_argument("--only_file", help="With --set_text: restrict to one capture")
    ap.add_argument("--dry_run", action="store_true", help="With --set_text: show, do not write")
    ap.add_argument("--compact", action="store_true", help="Collapse the journal to effective records")
    ap.add_argument("--cache", action="store_true",
                    help="Reuse labels/.universe_cache.json. OFF by default -- the cache "
                         "key cannot see code changes. See build_universe().")
    args = ap.parse_args()

    if args.compact:
        before, after = compact()
        print(f"journal compacted: {before} -> {after} records "
              f"(previous copy archived under {os.path.relpath(ARCHIVE_DIR, _HERE)}/)")
        return

    universe = build_universe(args.xml_dir, use_cache=args.cache, write_cache=args.cache)

    if args.set_text is not None:
        if not args.verdict:
            raise SystemExit("--set_text requires --verdict trusted|untrusted")
        only = resolve_file(universe, args.only_file) if args.only_file else None
        recs = set_by_text(universe, args.set_text, args.verdict, args.role, only)
        if not recs:
            print(f"Nothing to change: no labeled element has text {args.set_text!r}"
                  f"{' with role ' + args.role if args.role else ''} and a different verdict.")
            return
        by_file: dict[str, list] = defaultdict(list)
        for r in recs:
            by_file[r["file"]].append(r)
        print(f"{'WOULD SET' if args.dry_run else 'SETTING'} {len(recs)} element(s) "
              f"to {args.verdict}:")
        for filename, rs in sorted(by_file.items()):
            print(f"  {filename}  ->  {', '.join('#' + str(r['elem_index']) for r in rs)}")
        if args.dry_run:
            print("\n(dry run -- nothing written)")
            return
        append_records(recs)
        print(f"\nWritten to {os.path.relpath(JOURNAL_PATH, _HERE)}. Previous verdicts remain in "
              f"the journal; --history shows them.")
        return

    if args.dump:
        filename = resolve_file(universe, args.dump)
        fu = universe[filename]
        print(f"{filename}  platform={fu['platform']}  category={fu['category']}  "
              f"screen={fu['screen']}  screenshot={'yes' if fu['has_screenshot'] else 'MISSING'}")
        for i, r in enumerate(fu["roots"]):
            print(f"  root #{i}  kind={r['kind']:6} active={r['active']}  bbox={r['bbox']}  {r['provenance']}")
        state = effective_decisions()
        by_index, orphans = align_decisions(fu, state.get(filename, {}))
        if orphans:
            print(f"  !! {len(orphans)} stored decision(s) no longer match any element")
        print(f"\n{'idx':>5} {'tier':10} {'role':18} {'bbox':26} {'label':10} text")
        print("-" * 118)
        for row in fu["rows"]:
            if args.tier != "all" and row["tier"] != args.tier:
                continue
            rec = by_index.get(row["elem_index"])
            label = rec["verdict"] if rec else (row["skip_reason"] or "-")
            print(f"{row['elem_index']:>5} {row['tier']:10} {row['role'][:18]:18} "
                  f"{str(row['bbox']):26} {label:10} {row['text'][:48]!r}")
        return

    if args.history:
        if args.elem is None:
            raise SystemExit("--history requires --elem")
        filename = resolve_file(universe, args.history)
        rows = universe[filename]["rows"]
        row = next((r for r in rows if r["elem_index"] == args.elem), None)
        if row is None:
            raise SystemExit(f"{filename} has no elem_index {args.elem}")
        print(f"{filename} #{args.elem}  sig={row['sig']}  {row['role']}  {row['text'][:60]!r}")
        recs = history_for(filename, row["sig"])
        if not recs:
            print("  (no decisions recorded)")
        for rec in recs:
            if rec["type"] == "reset":
                print(f"  {rec['at']}  RESET (whole capture)")
            elif rec["type"] == "drop":
                note = f"  -- {rec['note']}" if rec.get("note") else ""
                print(f"  {rec['at']}  DROPPED (label retired){note}")
            else:
                origin = f" from #{rec['origin']}" if rec.get("origin") is not None else ""
                print(f"  {rec['at']}  {rec['verdict']:10} mode={rec['mode']}{origin}")
        return

    _print_stats(universe, args.per_file)


if __name__ == "__main__":
    main()
