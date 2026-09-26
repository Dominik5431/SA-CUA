#!/usr/bin/env python3
"""
show_tree.py -- prints an indented tree visualization of one at-spi/xml/*.xml capture's raw
AT-SPI structure (role, accessible name, id/class where present), for manually inspecting how a
page's accessibility tree is actually shaped -- e.g. to sanity-check critical_path.py's ancestor
chains, or to see where a cookie banner/UGC region sits before writing a rule for it.

Defaults to starting from the "Google Chrome" <application> node, skipping the surrounding
desktop/OS chrome (background services, GNOME shell, other apps) that's irrelevant to a captured
page's own content -- pass --full to see the entire desktop-frame tree instead.

Usage:
  python at-spi/show_tree.py 021_www.nytimes.com.xml
  python at-spi/show_tree.py --index 21
  python at-spi/show_tree.py 021_www.nytimes.com.xml --max-depth 8
  python at-spi/show_tree.py 021_www.nytimes.com.xml --full                 # whole desktop tree, not just Chrome
  python at-spi/show_tree.py 021_www.nytimes.com.xml --collapse-blank       # hide nameless/id-less/class-less layout noise
  python at-spi/show_tree.py 021_www.nytimes.com.xml --grep review          # only paths leading to a role/name/id/class match
"""
import argparse
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
XML_DIR = os.path.join(HERE, "xml")

_NS = {
    "attr": "https://accessibility.ubuntu.example.org/ns/attributes",
    "cp": "https://accessibility.ubuntu.example.org/ns/component",
}


def _q(prefix: str, name: str) -> str:
    return f"{{{_NS[prefix]}}}{name}"


def _atspi_tag_to_role(tag: str) -> str:
    """Mirrors critical_path.py / coord_to_semantic.py's tag->role normalization (hyphens to
    spaces), so what's printed here matches role strings seen elsewhere in this project."""
    if tag.startswith("{"):
        tag = tag.split("}", 1)[1]
    return tag.replace("-", " ")


def _resolve_filename(args) -> str:
    if args.index is not None:
        matches = sorted(glob.glob(os.path.join(args.xml_dir, f"{args.index:03d}_*.xml")))
        if not matches:
            sys.exit(f"No capture found for index {args.index} under {args.xml_dir}")
        return os.path.basename(matches[0])
    if args.filename:
        fp = os.path.join(args.xml_dir, args.filename)
        if not os.path.exists(fp):
            sys.exit(f"No such file: {fp}")
        return os.path.basename(fp)
    sys.exit("Provide a filename (e.g. 021_www.nytimes.com.xml) or --index 21")


def _node_text(node: ET.Element) -> tuple:
    """(role, name, attr_id, attr_class) -- the fields both the display and --grep matching use."""
    role = _atspi_tag_to_role(node.tag)
    name = (node.get("name") or "").strip()
    attr_id = node.get(_q("attr", "id"))
    attr_class = node.get(_q("attr", "class"))
    return role, name, attr_id, attr_class


def _node_label(node: ET.Element) -> str:
    role, name, attr_id, attr_class = _node_text(node)
    bits = [role]
    if name:
        bits.append(f'"{name[:70]}"')
    extras = []
    if attr_id:
        extras.append(f"id={attr_id}")
    if attr_class:
        extras.append(f"class={attr_class[:40]}")
    if extras:
        bits.append(f"[{' '.join(extras)}]")
    return " ".join(bits)


def _is_blank(node: ET.Element) -> bool:
    role, name, attr_id, attr_class = _node_text(node)
    return not name and not attr_id and not attr_class


def _find_chrome_root(root: ET.Element) -> ET.Element:
    for app in root.findall("application"):
        if app.get("name") == "Google Chrome":
            return app
    sys.exit('No <application name="Google Chrome"> found in this capture -- pass --full to '
             "view the whole desktop tree instead.")


def _matches(node: ET.Element, pattern: re.Pattern) -> bool:
    role, name, attr_id, attr_class = _node_text(node)
    return bool(pattern.search(role) or pattern.search(name or "") or
                pattern.search(attr_id or "") or pattern.search(attr_class or ""))


def _compute_keep_set(root: ET.Element, pattern: re.Pattern) -> set:
    """Nodes that match --grep, plus every ancestor of a match (so the path down to each match
    stays visible) -- descendants of a match that don't themselves match are NOT kept, keeping
    output focused on locating matches rather than dumping their whole subtree."""
    parent_map = {child: parent for parent in root.iter() for child in parent}
    keep: set = set()
    for node in root.iter():
        if _matches(node, pattern):
            keep.add(node)
            cur = parent_map.get(node)
            while cur is not None and cur not in keep:
                keep.add(cur)
                cur = parent_map.get(cur)
    return keep


def _print_tree(node: ET.Element, prefix: str, is_last: bool, depth: int,
                 args, keep: "set | None", counters: dict, out) -> None:
    children = list(node)
    if keep is not None:
        children = [c for c in children if c in keep]

    show_this = depth >= 0
    collapse = args.collapse_blank and _is_blank(node) and depth > 0
    if show_this and not collapse:
        connector = "" if depth == 0 else ("`-- " if is_last else "|-- ")
        counters["shown"] += 1
        out.write(f"{prefix}{connector}{_node_label(node)}\n")
        child_prefix = prefix if depth == 0 else prefix + ("    " if is_last else "|   ")
        next_depth = depth + 1
    else:
        child_prefix = prefix
        next_depth = depth

    counters["total"] += 1
    if args.max_depth is not None and keep is None and next_depth > args.max_depth:
        if children:
            counters["truncated"] += 1
        return

    for i, child in enumerate(children):
        _print_tree(child, child_prefix, i == len(children) - 1, next_depth, args, keep, counters, out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("filename", nargs="?", default=None, help="e.g. 021_www.nytimes.com.xml")
    ap.add_argument("--index", type=int, default=None, help="Select by capture index instead of filename")
    ap.add_argument("--xml_dir", default=XML_DIR)
    ap.add_argument("--full", action="store_true",
                     help="Show the entire desktop-frame tree, not just the Google Chrome application")
    ap.add_argument("--max-depth", type=int, default=None, dest="max_depth",
                     help="Stop descending past this depth (ignored when --grep is given, since "
                          "reaching a match may require going deeper)")
    ap.add_argument("--collapse-blank", action="store_true",
                     help="Hide nameless/id-less/class-less wrapper nodes (panel/filler layout noise) "
                          "from the display -- their children still show, spliced in at the same level")
    ap.add_argument("--grep", default=None,
                     help="Only show paths leading to a node whose role/name/id/class matches this "
                          "regex (case-insensitive) -- e.g. --grep review")
    args = ap.parse_args()

    filename = _resolve_filename(args)
    fp = os.path.join(args.xml_dir, filename)
    with open(fp, encoding="utf-8", errors="replace") as f:
        xml_text = f.read()
    root = ET.fromstring(xml_text)

    display_root = root if args.full else _find_chrome_root(root)

    keep = None
    if args.grep:
        pattern = re.compile(args.grep, re.IGNORECASE)
        keep = _compute_keep_set(display_root, pattern)
        if display_root not in keep and not any(_matches(n, pattern) for n in display_root.iter()):
            scope = "the whole tree" if args.full else "the Chrome application"
            sys.exit(f"No node matched --grep {args.grep!r} under {scope}.")

    counters = {"total": 0, "shown": 0, "truncated": 0}
    print(f"{filename}{'  (--full, whole desktop tree)' if args.full else '  (Google Chrome application only)'}")
    _print_tree(display_root, "", True, 0, args, keep, counters, sys.stdout)

    print(f"\n{counters['shown']} node(s) shown, {counters['total']} visited"
          + (f", {counters['truncated']} subtree(s) truncated by --max-depth {args.max_depth}"
             if counters["truncated"] else "")
          + (f", filtered by --grep {args.grep!r}" if args.grep else ""))


if __name__ == "__main__":
    main()
