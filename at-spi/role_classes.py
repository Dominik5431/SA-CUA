#!/usr/bin/env python3
"""role_classes.py -- a TOTAL classification of the AT-SPI role vocabulary.

AT-SPI's roles are a C enum in atspi-constants.h, not an open string space: there are exactly
131 of them. That is what makes this file possible and what makes it worth having. Every
role list in the labeling pipeline used to be a partial allowlist grown from whatever the
corpus happened to contain, so a capture from a new domain that emitted `rating`, `switch` or
`math fraction` fell through to the structural tier for no reason anyone had decided. Here
every role is assigned exactly once, so "does this generalize to a site I have not captured"
has a checkable answer rather than an optimistic one.

Verified against the corpus on 2026-08-15: 95 distinct roles appear across the 251 trees in
xml/, and not one of them falls outside ATSPI_ROLES. `python role_classes.py` re-runs that
check plus the internal totality check.

NAMING. AT-SPI reports role names with SPACES ("push button"); the tree serialization uses
the tag form with hyphens ("push-button"). Everything here is keyed on the space form, which
is what `row["role"]` carries. `tag_to_role()` converts.
"""
from __future__ import annotations

_ENUM = """invalid accelerator_label alert animation arrow calendar canvas check_box
check_menu_item color_chooser column_header combo_box date_editor desktop_icon desktop_frame
dial dialog directory_pane drawing_area file_chooser filler focus_traversable font_chooser
frame glass_pane html_container icon image internal_frame label layered_pane list list_item
menu menu_bar menu_item option_pane page_tab page_tab_list panel password_text popup_menu
progress_bar push_button radio_button radio_menu_item root_pane row_header scroll_bar
scroll_pane separator slider spin_button split_pane status_bar table table_cell
table_column_header table_row_header tearoff_menu_item terminal text toggle_button tool_bar
tool_tip tree tree_table unknown viewport window extended header footer paragraph ruler
application autocomplete editbar embedded entry chart caption document_frame heading page
section redundant_object form link input_method_window table_row tree_item
document_spreadsheet document_presentation document_text document_web document_email comment
list_box grouping image_map notification info_bar level_bar title_bar block_quote audio video
definition article landmark log marquee math rating timer static math_fraction math_root
subscript superscript description_list description_term description_value footnote
content_deletion content_insertion mark suggestion push_button_menu switch"""

ROLES: tuple[str, ...] = tuple(r.replace("_", " ") for r in _ENUM.split())

CLASSES = ("TEXT", "GRAPHIC", "CONTROL", "SURFACE", "GROUP", "META")

_MEMBERS: dict[str, str] = {
    "TEXT": """label static text paragraph heading caption comment block_quote
        description_term description_value footnote subscript superscript math math_fraction
        math_root terminal editbar log marquee mark suggestion content_insertion
        content_deletion definition accelerator_label column_header row_header
        table_column_header table_row_header page title_bar status_bar""",

    "GRAPHIC": """image icon canvas image_map video animation drawing_area chart desktop_icon
        arrow separator ruler progress_bar level_bar audio timer""",

    "CONTROL": """link push_button toggle_button check_box radio_button menu menu_item
        check_menu_item radio_menu_item tearoff_menu_item combo_box slider spin_button
        page_tab entry password_text list_box switch push_button_menu date_editor calendar
        color_chooser file_chooser font_chooser dial rating autocomplete scroll_bar list_item
        tree_item table_cell""",

    "SURFACE": """application desktop_frame frame window dialog alert internal_frame
        popup_menu tool_tip notification info_bar option_pane directory_pane
        input_method_window document_frame document_text document_web document_email
        document_spreadsheet document_presentation embedded""",

    "GROUP": """panel filler section landmark list form grouping root_pane glass_pane
        layered_pane viewport split_pane scroll_pane tool_bar menu_bar page_tab_list table
        table_row tree tree_table header footer article description_list html_container""",

    "META": "invalid unknown redundant_object extended focus_traversable",
}

ROLE_CLASS: dict[str, str] = {
    name.replace("_", " "): cls for cls, names in _MEMBERS.items() for name in names.split()
}

PRECEDENCE: tuple[str, ...] = ("CONTROL", "GRAPHIC", "TEXT", "SURFACE", "GROUP", "META")
_RANK = {c: i for i, c in enumerate(PRECEDENCE)}

REGION_HANDLE: frozenset[str] = frozenset({
    "article", "landmark", "section", "comment", "block quote", "list item", "table row",
    "table cell", "document web", "dialog", "tool bar", "list", "description list", "form",
    "header", "footer", "page tab list", "tree item",
})


def tag_to_role(tag: str) -> str:
    """XML tag form ("push-button") -> AT-SPI role name ("push button")."""
    return tag.replace("-", " ")


def paint_class(role: str) -> str:
    """-> one of CLASSES. Unknown roles cannot occur (the vocabulary is closed and this table
    is total over it), but a tree from a newer at-spi than this table would be a real event
    rather than a typo, so it is reported instead of silently bucketed."""
    try:
        return ROLE_CLASS[role]
    except KeyError:
        raise KeyError(
            f"role {role!r} is not in the AT-SPI vocabulary this table was built against "
            f"({len(ROLES)} roles). If at-spi added a role, add it to _ENUM and _MEMBERS."
        ) from None


def rank(role: str) -> int:
    """Position in PRECEDENCE; lower wins when two rows account for the same ink."""
    return _RANK[paint_class(role)]


def is_region_handle(role: str) -> bool:
    return role in REGION_HANDLE


def _self_check() -> list[str]:
    problems = []
    if len(ROLES) != len(set(ROLES)):
        problems.append("ROLES contains duplicates")
    missing = [r for r in ROLES if r not in ROLE_CLASS]
    if missing:
        problems.append(f"unclassified roles: {missing}")
    stray = [r for r in ROLE_CLASS if r not in ROLES]
    if stray:
        problems.append(f"classified roles not in the enum: {stray}")
    bad = [r for r in REGION_HANDLE if r not in ROLES]
    if bad:
        problems.append(f"region handles not in the enum: {bad}")
    return problems


def main() -> None:
    import collections
    import glob
    import os
    import sys
    import xml.etree.ElementTree as ET

    problems = _self_check()
    print(f"vocabulary   {len(ROLES)} roles")
    counts = collections.Counter(ROLE_CLASS.values())
    print("classes      " + "  ".join(f"{c}={counts[c]}" for c in CLASSES))
    print(f"region handles {len(REGION_HANDLE)}")

    xml_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xml")
    if os.path.isdir(xml_dir):
        seen: collections.Counter = collections.Counter()
        for path in sorted(glob.glob(os.path.join(xml_dir, "*.xml"))):
            for el in ET.parse(path).getroot().iter():
                seen[tag_to_role(el.tag.split("}")[-1])] += 1
        outside = sorted(set(seen) - set(ROLES))
        print(f"corpus       {len(seen)} distinct roles over "
              f"{len(glob.glob(os.path.join(xml_dir, '*.xml')))} trees")
        if outside:
            problems.append(f"corpus roles outside the enum: {outside}")
        else:
            print("             every corpus role is in the enum")
        print(f"unseen       {len(set(ROLES) - set(seen))} roles classified but not in the corpus")

    if problems:
        print("\nFAIL")
        for p in problems:
            print("  " + p)
        sys.exit(1)
    print("\nOK -- classification is total")


if __name__ == "__main__":
    main()
