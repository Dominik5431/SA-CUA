#!/usr/bin/env python3
"""
full_tree_extraction.py -- walks the FULL raw AT-SPI tree: every named, visible node in the page's
content root, regardless of role.

It replaced an interactable-only extraction (the gen-1 extraction.py, deleted) that filtered to
roles carrying an AT-SPI action. Per the plan's finding 5/6 (vectorized-leaping-lamport.md), most
of a page's actual UGC prose -- paragraph/heading/static/list content, a question or answer's
rendered body text -- has no AT-SPI action and was therefore invisible to that universe entirely.
Measured on 002_stackoverflow.com.xml: 2,455 named nodes in the content root, only 41 of them
reachable through its 86 interactable rows. This is what Phase 2 (plan section 4) has to walk
against in the live mechanism; this module is its offline/at-spi counterpart.

Note this is still not the labeling universe: labeling_store.py is, and it is wider again (it
keeps unnamed containers, and routes rather than drops). This module feeds selector_rules.py and
review_benchmark.py; ground truth is expressed in labeling_store's namespace, never this one.

Row schema (file, elem_index, category, role, text, ...) with three deliberate properties:
  - `elem_index` here is a SEPARATE numbering namespace (position in this module's own full-tree
    walk, document order) -- NOT comparable to extraction.py's interactable-only elem_index for
    the same file, even though both start at 0. Never mix the two.
  - `current_level`/`current_untrusted` are always `"NOT_TRACKED"`/`False`: the current production
    classifier (screen_labels.label_screen) only ever judges the interactable/affordance subset
    extraction.py already covers, so it has no opinion at all about a bare `static`/`paragraph`
    node -- this is "never evaluated", not a claim that the content is safe. `mismatch` (set by
    whatever labeler consumes these rows) will therefore never fire for full-tree-only content
    under the standard `current_untrusted and verdict=="trusted"` convention -- expected, not a
    bug: there is nothing yet to be "wrong" relative to on this subset.
  - a `bbox` field (`[x, y, w, h]`) is embedded directly in each row. extraction.py's rows instead
    rely on a caller-held, non-serializable `InteractableAction` list (`with_actions=True`) --
    that mechanism is keyed by extraction.py's own elem_index and doesn't generalize cleanly to a
    much bigger, differently-deduplicated universe. review_benchmark.py reads `row["bbox"]`
    directly for sources built from this module instead.

Not run directly -- imported by labelers/selector_rules.py's full-tree mode and
review_benchmark.py (which auto-detects a `bbox` key on a row to pick this extraction path).
"""
import json
import os
import sys
import xml.etree.ElementTree as ET

_AT_SPI_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_AT_SPI_DIR))
sys.path.insert(0, _AT_SPI_DIR)

from critical_path import _atspi_tag_to_role, _node_bbox, _node_signature, _q

XML_DIR = os.path.join(_AT_SPI_DIR, "xml")
MANIFEST_PATH = os.path.join(_AT_SPI_DIR, "manifest.json")


def _is_decorative(text: str) -> bool:
    """True only for text with no alphanumeric content at all (bullets, separators, stray
    punctuation) -- genuine noise regardless of length. A flat length cutoff (the original
    MIN_TEXT_LEN=8 approach) was measured to silently drop 28.7% of all DOCUMENT-zone elements
    corpus-wide, asymmetrically: 'edit'(4), 'Search'(6), 'Up vote'(7) all got cut while a same-row
    'Down vote'(9) survived purely because it is two characters longer -- meaningful,
    security-relevant UI was being lost, not decorative noise.

    Inlined from the retired extraction.py when the gen-1 interactable-only universe was removed.
    """
    return not any(c.isalnum() for c in text)


_CATEGORY_BY_FILE: "dict | None" = None


def _category_of(filename: str):
    """A capture's category, read from manifest.json.

    This used to come from extraction.CATEGORY_BY_INDEX, which built the mapping by *position* in
    sites.py -- `{i: cat for i, (cat, _url) in enumerate(SITES)}`. That coupling silently breaks the
    moment sites.py is reordered or rewritten, and it could never describe an app capture, whose
    category never came from SITES at all. manifest.json records the category per capture at
    capture time, which is the only place it is actually known.
    """
    global _CATEGORY_BY_FILE
    if _CATEGORY_BY_FILE is None:
        try:
            with open(MANIFEST_PATH, encoding="utf-8") as f:
                _CATEGORY_BY_FILE = {r["filename"]: r.get("category") for r in json.load(f)}
        except (OSError, ValueError, KeyError):
            _CATEGORY_BY_FILE = {}
    return _CATEGORY_BY_FILE.get(filename)

_CACHE: dict = {}


def _load_tree(filename: str):
    if filename not in _CACHE:
        fp = os.path.join(XML_DIR, filename)
        with open(fp, encoding="utf-8", errors="replace") as f:
            root = ET.fromstring(f.read())
        parent_map = {child: parent for parent in root.iter() for child in parent}
        _CACHE[filename] = (root, parent_map)
    return _CACHE[filename]


def _content_root(root):
    return next((n for n in root.iter() if n.tag == "document-web"), None)


def extract_full_tree_elements(index: int, filename: str, with_nodes: bool = False):
    """Returns the list of full-tree row dicts for one capture -- every named, visible node inside
    the page's content root (document-web), in document order. Mirrors the retired extraction.py's
    with_actions convention: with_nodes=True also returns the raw ET.Element behind each
    row (same order/length), for ancestor_path_for()/own_class_of() -- not JSON-serializable, a
    labeler should only use these in-process, never write them out."""
    category = _category_of(filename)
    root, _parent_map = _load_tree(filename)
    docweb = _content_root(root)
    if docweb is None:
        return ([], []) if with_nodes else []

    rows, nodes = [], []
    for node in docweb.iter():
        name = (node.get("name") or "").strip()
        if not name or _is_decorative(name):
            continue
        bbox = _node_bbox(node)
        if bbox is None:
            continue
        rows.append({
            "file": filename,
            "elem_index": len(rows),
            "category": category,
            "role": _atspi_tag_to_role(node.tag),
            "region_id": None,
            "region_role": None,
            "region_value_shape": None,
            "cross_origin_embed": False,
            "text": name[:200],
            "current_level": "NOT_TRACKED",
            "current_untrusted": False,
            "bbox": list(bbox),
        })
        nodes.append(node)
    return (rows, nodes) if with_nodes else rows


def ancestor_path_for(filename: str, node) -> list:
    """Root-first ancestor signature chain for one node (from extract_full_tree_elements's `nodes`
    list), matching critical_path.critical_path_for()'s return shape exactly, so biba.py-style
    trimming/matching code (e.g. verify_selector_rules.predict()) works unchanged on either
    extraction source."""
    _root, parent_map = _load_tree(filename)
    chain = []
    cur = parent_map.get(node)
    while cur is not None:
        chain.append(_node_signature(cur))
        cur = parent_map.get(cur)
    chain.reverse()
    return chain


def own_class_of(node) -> "str | None":
    return node.get(_q("attr", "class"))
