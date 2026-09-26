#!/usr/bin/env python3
"""
critical_path.py -- pure extraction, sibling to extraction.py. Reconstructs each interactable
element's full ancestor chain (its "critical path", in Prismata's terms -- see
arXiv:2607.08147, "Prismata: Confining Cross-Site Prompt Injection in Web Agents") by parsing
the raw AT-SPI XML directly, independently of coord_to_semantic.py's affordance extraction.

Why this is a separate parser rather than reusing coord_to_semantic.py: that module only ever
turns "interactable" AT-SPI roles into tracked InteractableAction objects (see
_ATSPI_ROLE_ACTION there) -- headings, sections, and generic containers are discarded before
they ever become an object with a role/label a caller could inspect.
That's the right scope for coord_to_semantic.py's actual job (deciding what the agent can click/
type), but it means the ancestor context Prismata's Biba parsing depends on (a "Reviews" heading,
a `review-card` class name, a landmark role) is invisible downstream of it. The raw XML capture
itself still has all of this -- attr:id, attr:class, attr:tag, and real DOM nesting survive the
AT-SPI serialization -- so this module walks that tree directly, for ancestor-path purposes only.
It assigns no trust judgment and adds no new tracked affordances; the element universe being
labeled is still exactly what extraction.extract_elements() already produces.

Matching an extracted row back to its XML node: extraction.py's rows don't carry a pointer into
the raw tree, so nodes are matched by (role, bbox) -- role via the same tag->role normalization
coord_to_semantic.py uses (_atspi_tag_to_role: hyphens to spaces), bbox via the same
cp:screencoord/cp:size parsing coord_to_semantic.py uses for InteractableAction.bbox. Since both
modules derive these from the identical two XML attributes, the match is exact.
"""
import os
import re
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
XML_DIR = os.path.join(HERE, "xml")

_NS = {
    "attr": "https://accessibility.ubuntu.example.org/ns/attributes",
    "cp": "https://accessibility.ubuntu.example.org/ns/component",
}

_OVERSIZED_RATIO = 2.5


def _q(prefix: str, name: str) -> str:
    return f"{{{_NS[prefix]}}}{name}"


def _atspi_tag_to_role(tag: str) -> str:
    """Mirrors coord_to_semantic.py's _atspi_tag_to_role exactly (hyphens -> spaces), so role
    strings match between the two modules' independent parses of the same XML."""
    if tag.startswith("{"):
        tag = tag.split("}", 1)[1]
    return tag.replace("-", " ")


_MEDIA_TAGS = frozenset({"img", "svg", "canvas", "video", "picture", "image",
                          "iframe", "embed", "object"})
_MEDIA_ROLES = frozenset({"image", "icon", "canvas", "video", "image map", "animation"})


def effective_role(node: ET.Element) -> str:
    """The node's AT-SPI role, corrected by its HTML tag when Chrome mapped a media element onto a
    generic container.

    Measured on the 2026-08-12 corpus: 451 media elements arrive under a non-image role -- 367 as
    `section` (AP News' <picture> elements, with correct geometry), plus push-button, landmark and
    panel. Left uncorrected they read as unnamed structural wrappers and never reach a human, even
    though they are exactly the attacker-controlled image regions the trust labeling cares about.

    Applied on READ rather than in the capture server on purpose: it fixes the 250 captures already
    on disk as well as future ones, keeps the emitted XML faithful to what AT-SPI actually reported
    (a role/tag disagreement is itself signal worth being able to inspect), and gives the live
    runtime tree the same correction from the same code path.
    """
    role = _atspi_tag_to_role(node.tag)
    if role in _MEDIA_ROLES:
        return role
    if (node.get(_q("attr", "tag")) or "").lower() in _MEDIA_TAGS:
        return "image"
    return role


def _parse_coord(s) -> "tuple[int, int] | None":
    r"""AT-SPI screen coordinates, which CAN BE NEGATIVE.

    The `\d+` this used to use could not match a minus sign, so every node positioned at a
    negative x or y failed to parse, `_node_bbox` returned None, and `build_file_universe` skipped
    it outright -- not demoted to the structural tier, dropped before a row existed, invisible to
    the labeling UI and to every consumer. 803 nodes corpus-wide across all 311 captures, 74 of
    them PARTIALLY ON SCREEN and therefore real things a labeler should have been judging.

    160_store.steampowered.com is the clean example: Steam's featured carousel peeks the previous
    and next game at either edge, and the two are structurally identical -- `store_main_capsule` at
    screencoord (1628, 343) on the right, (-408, 343) on the left, both size (756, 434), both
    st:showing="true". The right one was labelable and the left one did not exist. 348 of its 756
    pixels are on screen. The DOM agrees: screen=[-408, 343, 756, 433], visible: true.

    Sizes stay non-negative in practice, but the same pattern parses both, so a negative one would
    now flow through to the `area_ok` check rather than silently deleting the node."""
    m = re.match(r"\((-?\d+),\s*(-?\d+)\)", (s or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def _node_bbox(node: ET.Element) -> "tuple[int, int, int, int] | None":
    coord = _parse_coord(node.get(_q("cp", "screencoord")))
    size = _parse_coord(node.get(_q("cp", "size")))
    if coord is None or size is None:
        return None
    return (coord[0], coord[1], size[0], size[1])


def _find_viewport_bbox(root: ET.Element) -> "tuple[int, int, int, int] | None":
    """The page's actual viewport, used as the reference for oversized-ancestor detection: the
    first `document web` node in document order (excludes browser chrome like tabs/URL bar that
    the outer <frame> node's bbox would include; for pages with iframes, the first one in document
    order is always the outermost full-page one). Falls back to the first node anywhere with a
    nonzero bbox if no `document web` node exists, and never raises."""
    fallback = None
    for node in root.iter():
        bbox = _node_bbox(node)
        if bbox is None:
            continue
        if fallback is None:
            fallback = bbox
        if _atspi_tag_to_role(node.tag) == "document web":
            return bbox
    return fallback


def _node_signature(node: ET.Element, viewport_bbox: "tuple[int, int, int, int] | None" = None) -> dict:
    """One ancestor's structural-cue-relevant fields -- exactly what Prismata's Biba parsing
    reads to detect a trust boundary before revealing what's below it: role, accessible name
    (this is where heading text like "Reviews" lives), and the AT-SPI-exposed HTML id/class.

    `oversized` is a separate, purely geometric disqualification flag (only ever set to True, never
    False) -- NOT part of the structural-identity fields above. It must never be folded into
    biba.py's _ancestor_key cache-key hash (role/id/class/name only): geometry can jitter slightly
    across structurally-identical elements, which would break cache-key stability."""
    sig = {"role": _atspi_tag_to_role(node.tag)}
    name = (node.get("name") or "").strip()
    if name:
        sig["name"] = name[:120]
    attr_id = node.get(_q("attr", "id"))
    if attr_id:
        sig["id"] = attr_id
    attr_class = node.get(_q("attr", "class"))
    if attr_class:
        sig["class"] = attr_class
    if viewport_bbox is not None:
        bbox = _node_bbox(node)
        if bbox is not None:
            vw, vh = viewport_bbox[2], viewport_bbox[3]
            w, h = bbox[2], bbox[3]
            if (vw and w > _OVERSIZED_RATIO * vw) or (vh and h > _OVERSIZED_RATIO * vh):
                sig["oversized"] = True
    return sig


def build_ancestor_index(filename: str) -> dict:
    """Parses one capture's raw XML and returns {(role, bbox): [ancestor_signature, ...]},
    root-first, NOT including the element itself -- for every node in the tree that carries a
    bbox, including roles coord_to_semantic.py never tracks (heading, section, div-equivalents),
    so those are available here purely as ancestor context."""
    fp = os.path.join(XML_DIR, filename)
    with open(fp, encoding="utf-8", errors="replace") as f:
        xml_text = f.read()
    root = ET.fromstring(xml_text)

    parent_map = {child: parent for parent in root.iter() for child in parent}
    viewport_bbox = _find_viewport_bbox(root)

    index: dict = {}
    for node in root.iter():
        bbox = _node_bbox(node)
        if bbox is None:
            continue
        role = _atspi_tag_to_role(node.tag)
        ancestors = []
        cur = parent_map.get(node)
        while cur is not None:
            ancestors.append(_node_signature(cur, viewport_bbox))
            cur = parent_map.get(cur)
        ancestors.reverse()
        index.setdefault((role, bbox), ancestors)
    return index


def critical_path_for(index: dict, role: str, bbox: tuple) -> "list[dict] | None":
    """Ancestor chain (root-first) for one extraction.py row, matched by (role, bbox). Returns
    None if no XML node matches (shouldn't happen for rows extraction.py itself produced from
    the same file, but callers should handle it defensively)."""
    return index.get((role, tuple(bbox)))


def build_own_id_index(filename: str) -> dict:
    """Companion to build_ancestor_index: {(role, bbox): attr_id_or_None} for the element's OWN
    id (not its ancestors') -- needed to match an element against
    trusted_domains.py's cookie_accept_ids, which are the accept/dismiss control's own attr:id,
    not any ancestor's. Separate function (rather than folding into build_ancestor_index) so that
    function's already-verified behavior/signature is untouched."""
    fp = os.path.join(XML_DIR, filename)
    with open(fp, encoding="utf-8", errors="replace") as f:
        xml_text = f.read()
    root = ET.fromstring(xml_text)

    index: dict = {}
    for node in root.iter():
        bbox = _node_bbox(node)
        if bbox is None:
            continue
        role = _atspi_tag_to_role(node.tag)
        index.setdefault((role, bbox), node.get(_q("attr", "id")))
    return index
