"""
Lightweight AT-SPI XML parser for capability enforcement in SA-Agent.

Parses raw accessibility tree XML from obs["accessibility_tree"] into
flat bounding-box records and provides coordinate → element lookup.
No dependency on mm_agents/agent.py.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

_COMPONENT_NS = "https://accessibility.ubuntu.example.org/ns/component"
_STATE_NS = "https://accessibility.ubuntu.example.org/ns/state"

_COORD_RE = re.compile(r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)")

INTERACTIVE_TAGS = frozenset({
    "push-button", "button", "link", "menu item", "menu-item", "menuitem",
    "check-box", "radio-button", "combo-box", "list-item", "toggle-button",
})

ENTRY_TAGS = frozenset({
    "entry", "text", "editable text", "text box", "textbox",
})


@dataclass
class A11yNode:
    tag: str
    name: str
    text: str
    x: int
    y: int
    w: int
    h: int
    focused: bool = False

    @property
    def area(self) -> int:
        return self.w * self.h

    def contains(self, cx: int, cy: int) -> bool:
        return self.x <= cx <= self.x + self.w and self.y <= cy <= self.y + self.h

    @property
    def label(self) -> str:
        """Best human-readable label: prefer name, fall back to text content."""
        return (self.name or self.text).strip()


def _parse_coord(s: str) -> tuple[int, int] | None:
    m = _COORD_RE.search(s)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _node_from_el(el) -> "A11yNode | None":
    """One A11yNode from an XML element, or None when it carries no geometry (a layout wrapper)."""
    pos = _parse_coord(el.get(f"{{{_COMPONENT_NS}}}screencoord", ""))
    size = _parse_coord(el.get(f"{{{_COMPONENT_NS}}}size", ""))
    if pos is None or size is None:
        return None
    return A11yNode(
        tag=el.tag,
        name=el.get("name", ""),
        text=(el.text or "").strip(),
        x=pos[0], y=pos[1],
        w=size[0], h=size[1],
        focused=el.get(f"{{{_STATE_NS}}}focused", "false") == "true",
    )


def parse_a11y_nodes(a11y_xml: str) -> list[A11yNode]:
    """Parse raw AT-SPI XML into a flat list of A11yNode records."""
    try:
        root = ET.fromstring(a11y_xml)
    except ET.ParseError:
        return []
    return [n for n in (_node_from_el(el) for el in root.iter()) if n is not None]


def active_window_nodes(a11y_xml: str) -> list[A11yNode]:
    """A11yNodes of the ACTIVE top-level window ONLY -- the one the compositor has raised
    (``st:active="true"``), i.e. the window in FRONT / with focus -- and its descendants.

    Returns ``[]`` when no window is marked active, so a caller can fall back to the whole tree. Used
    by ``find_by_text`` so a presence check sees only the front window and never text sitting in a
    window stacked BEHIND it (e.g. a download terminal left open behind the fresh terminal a `ls` was
    run in): those behind-window nodes are exactly the ones that alias a self-typed command with real
    output. ``st:active`` is the same single z-fact the masking pipeline's occlusion map trusts, and a
    capture carries it on exactly one top-level frame."""
    try:
        root = ET.fromstring(a11y_xml)
    except ET.ParseError:
        return []
    active = next((el for el in root.iter()
                   if el.get(f"{{{_STATE_NS}}}active", "false") == "true"), None)
    if active is None:
        return []
    return [n for n in (_node_from_el(el) for el in active.iter()) if n is not None]
