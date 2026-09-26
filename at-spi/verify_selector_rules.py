#!/usr/bin/env python3
"""Verification step 1 (plan: vectorized-leaping-lamport.md): the hand-authored candidate
structural selector sets, RULES and TRUSTED_RULES, plus the predicates that match them.

This is an ORACLE artifact: the rules below were authored by inspecting each row's actual text +
ground-truth verdict (full information), not a blind Phase-1-style sanitized-view LLM call.
They answer "is a small structural rule set even capable of reproducing the ground truth at all"
(the ceiling), not "can the sanitized-view LLM find such a set unassisted" (a separate,
follow-up question -- that is labelers/selector_propose.py). Rules match on role +
own-class-prefix + ancestor-path class-prefixes only -- never on text -- consistent with the live
mechanism's actual inputs.

WHAT THIS FILE NO LONGER DOES. It used to score those rules with evaluate() /
evaluate_default_untrusted(), against labels/ground_truth.json over extraction.py's interactable
rows. Both went with the gen-1 universe; scoring lives in selector_propose.score_file(), over
labeling_store's universe and ground_truth_fulltree's hand-labeled rows. What is exported from
here is the rule sets, the match predicates (used by labelers/selector_rules.py), build_self_index
(used by selector_propose.py), and check_full_tree_coverage() below.

WHY THE GEN-1 SCORING HAD TO GO: `ground_truth.json` only ever covered the ~86-159-row
INTERACTABLE subset per file. Per the plan's finding 5,
that subset excludes `static`/`paragraph`/`heading`/`section`/`list`/`block quote` -- exactly the
roles the actual free-text BODY of a question/answer/post is rendered in. Measured on
002_stackoverflow.com.xml: the full `document-web` subtree has 2,455 named nodes; only 41 of them
are also reachable through extraction.py's 86 interactable rows -- 2,414 named nodes (98.3%),
almost entirely `static` (1,470 of them), were structurally invisible to ground_truth.json and to
the oracle check it supported. Manually walking that full tree (see `_dump_answer_container_children`
below and the investigation that produced it) confirms this is exactly where the actual answer
body text lives (`section#answer-<id>.answer js-answer` on Stack Overflow-family sites), and that
it is NOT the same trust picture as a flat "everything in the container is untrusted" rule would
assume: vote/save/timeline controls, an "Accepted" badge, and a bare-static vote count number are
literal siblings of the untrusted body content in the exact same container (the same
sibling-mixing pattern already seen in the GitHub commit row and the SO comment permalink).
`check_full_tree_coverage` below extends the rule set with a body-container rule (using
`own_role_exclude`, the control_shape-shaped carve-out already in the plan's design) and audits
coverage against the FULL tree.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "labelers"))

import xml.etree.ElementTree as ET
from critical_path import build_ancestor_index, critical_path_for, _atspi_tag_to_role, _node_bbox, _q
import biba


def build_self_index(filename):
    fp = os.path.join(REPO, "xml", filename)
    root = ET.fromstring(open(fp, encoding="utf-8", errors="replace").read())
    idx = {}
    for node in root.iter():
        bbox = _node_bbox(node)
        if bbox is None:
            continue
        role = _atspi_tag_to_role(node.tag)
        idx.setdefault((role, bbox), (node.get(_q("attr", "id")), node.get(_q("attr", "class"))))
    return idx


def has_prefix(value, prefix):
    return bool(value) and prefix in value


_CONTROL_SHAPE_ROLES = frozenset({"toggle button", "push button", "combo box", "checkbox", "radio button"})
_QA_BODY_EXCLUDE = frozenset({
    "js-post-issue",
    "js-share-link",
    "js-suggest-edit-post",
    "js-post-tag-list",
    "s-notice",
    "user-action-time",
    "comments",
})


def path_matches_exclude(path, own_class, excludes):
    """Checks the exclusion set against every ancestor's class AND the row's own class -- some
    exclusions are ancestor-level wrappers (js-post-tag-list, s-notice), others are the row's own
    class (js-post-issue on the "Timeline" link itself, which has no distinguishing ancestor)."""
    if any(has_prefix(own_class, ex) for ex in excludes):
        return True
    return any(has_prefix(sig.get("class"), ex) for sig in path for ex in excludes)


RULES = {
    "002_stackoverflow.com.xml": [
        ("link", "question-hyperlink", None, None, None, None, None, "question title (own + related sidebar)"),
        ("heading", "fs-headline1", None, None, None, None, None,
         "question title heading -- WRAPS the question-hyperlink link above, needed separately "
         "since a wrapping ancestor can't be caught by the descendant-cascade fix (finding 9)"),
        ("link", "comment-user", None, None, None, None, None, "comment author link"),
        (None, None, "comment-body", "section", "comment-link", None, None, "inline link inside a comment body"),
        (None, None, "user-details", "section", None, None, None, "question/answer author link"),
        (None, None, "user-gravatar", "section", None, None, None, "avatar-wrapped username link"),
        ("list item", "comment js-comment", "comments-list", "list", "comment-link", None, None,
         "whole comment container -- excludes comment-link (the trusted \"Commented <timestamp>\" "
         "permalink), which the new ancestor-cascade (finding 9) would otherwise also catch since "
         "it sits nested inside this same container"),
        (None, None, "question js-question", "section", None, _CONTROL_SHAPE_ROLES, _QA_BODY_EXCLUDE,
         "question body -- everything except its own vote/save/share/tag/timestamp controls"),
        (None, None, "answer js-answer", "section", None, _CONTROL_SHAPE_ROLES, _QA_BODY_EXCLUDE,
         "answer body -- everything except its own vote/save/share/tag/timestamp controls"),
        (None, "s-sidebarwidget--item", None, None, None, None, None,
         "sidebar widget item (own class) -- shared component also used for 'Featured on Meta' "
         "community meta-question titles, see finding 8; can't separate from the genuinely "
         "editorial 'Overflow Blog' items structurally, default-deny the whole item class"),
        (None, None, "s-sidebarwidget--item", "list item", None, None, None,
         "sidebar widget item (nested link) -- same rule as above, ancestor-matched"),
    ],
    "006_unix.stackexchange.com.xml": [
        ("link", "question-hyperlink", None, None, None, None, None, "question title (own + related sidebar)"),
        ("heading", "fs-headline1", None, None, None, None, None,
         "question title heading -- WRAPS the question-hyperlink link above, needed separately "
         "since a wrapping ancestor can't be caught by the descendant-cascade fix (finding 9)"),
        ("link", "comment-user", None, None, None, None, None, "comment author link"),
        (None, None, "comment-body", "section", "comment-link", None, None, "inline link inside a comment body"),
        (None, None, "user-gravatar", "section", None, None, None, "avatar-wrapped username link"),
        ("list item", "comment js-comment", "comments-list", "list", "comment-link", None, None,
         "whole comment container -- excludes comment-link (the trusted \"Commented <timestamp>\" "
         "permalink), which the new ancestor-cascade (finding 9) would otherwise also catch since "
         "it sits nested inside this same container"),
        (None, None, "question js-question", "section", None, _CONTROL_SHAPE_ROLES, _QA_BODY_EXCLUDE,
         "question body -- everything except its own vote/save/share/tag/timestamp controls"),
        (None, None, "answer js-answer", "section", None, _CONTROL_SHAPE_ROLES, _QA_BODY_EXCLUDE,
         "answer body -- everything except its own vote/save/share/tag/timestamp controls"),
        (None, "s-sidebarwidget--item", None, None, None, None, None,
         "sidebar widget item (own class) -- shared component also used for 'Featured on Meta' "
         "community meta-question titles, see finding 8; can't separate from the genuinely "
         "editorial 'Overflow Blog' items structurally, default-deny the whole item class"),
        (None, None, "s-sidebarwidget--item", "list item", None, None, None,
         "sidebar widget item (nested link) -- same rule as above, ancestor-matched"),
    ],
    "053_github.com.xml": [
        ("table cell", "react-directory-row-name-cell", None, None, None, None, None, "filename cell"),
        ("table cell", "react-directory-row-commit-cell", None, None, None, None, None, "commit message cell"),
        (None, None, "react-directory-commit-message", "section", None, None, None, "commit message link"),
        (None, None, "react-directory-filename-cell", "section", None, None, None, "filename link"),
        (None, None, "react-directory-truncate", "section", None, None, None, "filename link (truncated variant)"),
        ("link", "issue-link", None, None, None, None, None, "auto-linked issue number in commit message"),
        ("link", "AuthorAvatar-module__authorHoverableLink", None, None, None, None, None,
         "commit author avatar link"),
    ],
}


def path_matches_ancestor(path, class_prefix, role):
    for sig in path:
        if role is not None and sig.get("role") != role:
            continue
        if has_prefix(sig.get("class"), class_prefix):
            return True
    return False


def _own_or_ancestor_class_match(row, own_class, path, own_role, own_prefix):
    """True if the row's OWN (role, class) matches (own_role/own_prefix), OR any ANCESTOR's does.
    Finding 9: an accessible element's identity is frequently split across more than one AT-SPI
    node -- an outer wrapper (e.g. a `heading` around a title `link`) or an inner leaf (a bare
    `static` text node nested inside that same `link`, carrying no class of its own). A rule
    written to match "the link with class=question-hyperlink" would, without this, silently miss
    that nested bare `static` -- its own class is None, and only an ANCESTOR-cascade check (not a
    same-node check) catches it. This is additive-only (can only turn MORE nodes untrusted, never
    fewer), so it cannot break an already-correct verdict from an earlier, more specific rule.
    Does NOT help the opposite case (an ancestor wrapping a matched descendant, like that
    `heading`) -- that direction needs its own explicit rule; there is no general fix for it here,
    since "does this node contain a matching descendant" needs a forward tree walk, not a
    backward one."""
    if own_prefix is None:
        return own_role is None or row["role"] == own_role
    if has_prefix(own_class, own_prefix) and (own_role is None or row["role"] == own_role):
        return True
    return path_matches_ancestor(path, own_prefix, own_role)


def predict(row, own_class, path, rules):
    """Returns (verdict, note): note is the matched rule's own note string, or None when no rule
    fired and the file-wide default ("trusted" here) applied -- used by labelers/selector_rules.py
    to set confidence (a matched rule is a "high"-confidence claim; the bare default is "medium",
    a guess) and rationale."""
    for own_role, own_prefix, anc_prefix, anc_role, own_exclude, role_exclude, anc_exclude, note in rules:
        if not _own_or_ancestor_class_match(row, own_class, path, own_role, own_prefix):
            continue
        if anc_prefix is not None and not path_matches_ancestor(path, anc_prefix, anc_role):
            continue
        if own_exclude is not None and has_prefix(own_class, own_exclude):
            continue
        if role_exclude is not None and row["role"] in role_exclude:
            continue
        if anc_exclude is not None and path_matches_exclude(path, own_class, anc_exclude):
            continue
        return "untrusted", note
    return "trusted", None


def check_full_tree_coverage(filename):
    """Walks the FULL raw AT-SPI tree (every role, not just extraction.py's interactable subset)
    and applies the same RULES used above. There is no ground truth for this universe (that's
    exactly the gap this function measures), so this reports coverage/soundness, not
    precision/recall: how much of the page's actual text content extraction.py never sees at
    all, how much of that the rule set marks untrusted, and -- the important sanity check -- a
    sample of what it leaves trusted *inside* a body container, to confirm the control_shape-style
    carve-out (own_role_exclude) is actually doing its job rather than either over- or
    under-masking."""
    from critical_path import _node_signature
    fp = os.path.join(REPO, "xml", filename)
    root = ET.fromstring(open(fp, encoding="utf-8", errors="replace").read())
    parent_map = {child: parent for parent in root.iter() for child in parent}
    docweb = next((n for n in root.iter() if n.tag == "document-web"), None)
    if docweb is None:
        print(f"  (no document-web node in {filename}, skipping full-tree check)")
        return
    rules = RULES[filename]

    total_named = 0
    untrusted_count = 0
    interactable_role_count = 0
    untrusted_samples = []
    trusted_in_body_samples = []
    for node in docweb.iter():
        name = (node.get("name") or "").strip()
        if not name:
            continue
        total_named += 1
        role = _atspi_tag_to_role(node.tag)
        own_class = node.get(_q("attr", "class"))
        chain = []
        cur = parent_map.get(node)
        while cur is not None:
            chain.append(_node_signature(cur))
            cur = parent_map.get(cur)
        chain.reverse()
        path = biba._trim_to_content_root(chain)
        pred, _note = predict({"role": role, "text": name}, own_class, path, rules)
        in_body = path_matches_ancestor(path, "question js-question", "section") or \
            path_matches_ancestor(path, "answer js-answer", "section")
        if pred == "untrusted":
            untrusted_count += 1
            if in_body and len(untrusted_samples) < 8:
                untrusted_samples.append((role, name[:55]))
        elif in_body and len(trusted_in_body_samples) < 15:
            trusted_in_body_samples.append((role, own_class, name[:45]))

    print(f"  full-tree named nodes in document-web: {total_named} "
          f"(extraction.py's interactable rows never see {total_named} - covered-elsewhere)")
    print(f"  full-tree rule verdict: {untrusted_count} untrusted / {total_named - untrusted_count} trusted")
    print("  sample of body-container content correctly caught as untrusted (previously invisible):")
    for role, text in untrusted_samples:
        print(f"    untrusted  role={role:12s} text={text!r}")
    print("  sample of body-container content the control_shape-style carve-out kept trusted:")
    for role, cls, text in trusted_in_body_samples:
        print(f"    trusted    role={role:12s} class={str(cls)[:30]:30s} text={text!r}")


_EMPTY = object()


def path_matches_ancestor_id_or_class(path, needle, role):
    for sig in path:
        if role is not None and sig.get("role") != role:
            continue
        if has_prefix(sig.get("class"), needle) or has_prefix(sig.get("id"), needle):
            return True
    return False


TRUSTED_RULES = {
    "053_github.com.xml": [
        ("document web", None, None, None, None, "browser tab title (frame-title -- always trusted)"),
        (None, None, "repository-container-header", "section", None,
         "repo header: breadcrumb, nav tabs -- EXCLUDES pagehead-actions (see note)",
         {"pagehead-actions"}),
        (None, None, "header-wrapper", "section", None,
         "top marketing header: skip-link, homepage, nav dropdowns, search, sign in/up, appearance"),
        ("link", "OverviewContent-module", None, None, None, "7 Branches / 661 Tags nav pills"),
        ("table cell", _EMPTY, "react-directory-row", "table row", None, "commit timestamp cell (bare, no class)"),
        ("link", "Link--secondary", "bgColor-muted", "table cell", None, "commit-hash link in summary row"),
        ("link", "prc-Button-ButtonBase", None, None, None, "commit-count link styled as a button"),
        ("link", None, "BorderGrid-cell", "section",
         {"Link--muted", "Link--primary no-underline Link",
          "Link--primary no-underline Link d-flex flex-item-fluid min-width-0", "text-bold"},
         "fixed About-panel link vocabulary (Readme/License/.../Releases/Packages/homepage) -- "
         "deliberately excludes \"Link Link--muted\" (Activity/Custom properties): identical to "
         "\"35k forks\"'s own class, unfixable, left unmatched per FN>>FP"),
        ("link", "Button--secondary", None, None, None, "Sponsor button/link"),
    ],
    "002_stackoverflow.com.xml": [
        ("document web", None, None, None, None, "browser tab title (frame-title -- always trusted)"),
        (None, None, "onetrust-consent-sdk", "section", None, "cookie-consent banner (id-scoped)"),
        (None, None, "s-topbar", "landmark", None, "top navigation bar"),
        (None, None, "left-sidebar", "section", None, "left nav sidebar"),
        ("link", "site-header--link", None, None, None, "site home logo/name link"),
        ("link", "js-dismiss", None, None, None, "banner dismiss control"),
        ("link", "js-chat-ad-link", None, None, None, "chat-room promo link"),
        (None, None, "js-post-tag-list", None, None, "question tag badges"),
        (None, None, "s-notice", None, None, "status badges (community effort, closed, etc.)"),
        (None, None, "user-action-time", None, None, "edited/asked timestamp"),
        ("link", "js-post-issue", None, None, None, "Timeline link"),
        ("link", "js-share-link", None, None, None, "Share link"),
        ("link", "js-suggest-edit-post", None, None, None, "Improve this question/answer link"),
        ("link", "comment-link", None, None, None, "Commented <timestamp> comment permalink"),
        ("link", _EMPTY, "show-votes", "landmark", None,
         "vote-count number in the related-questions sidebar -- own_role='link' is load-bearing: "
         "without it this rule also matched the bare 'static' text nested inside each unrelated "
         "question-hyperlink link (same empty own-class, same show-votes ancestor scope, wrong "
         "role), trusting the actual related-question title text while its own <link> stayed "
         "correctly untrusted -- found by inspecting a rendered review image, not by the scorer"),
        ("list item", "s-sidebarwidget--header", None, None, None,
         "sidebar widget section title (e.g. \"The Overflow Blog\"/\"Featured on Meta\") -- fixed "
         "platform vocabulary, safe to trust on its own"),
        (None, None, "js-sofa-sidebar-promo", None, None, "AI-product sidebar promo widget"),
        ("link", "s-btn__filled", None, None, None, "Ask Question button-styled link"),
        ("menu item", _EMPTY, None, None, None, "sort-order menu item (Highest score, Newest, ...)"),
    ],
    "006_unix.stackexchange.com.xml": [
        ("document web", None, None, None, None, "browser tab title (frame-title -- always trusted)"),
        (None, None, "onetrust-consent-sdk", "section", None, "cookie-consent banner (id-scoped)"),
        (None, None, "s-topbar", "landmark", None, "top navigation bar"),
        (None, None, "left-sidebar", "section", None, "left nav sidebar"),
        ("link", "site-header--link", None, None, None, "site home logo/name link"),
        ("link", "js-dismiss", None, None, None, "banner dismiss control"),
        (None, None, "js-post-tag-list", None, None, "question tag badges"),
        (None, None, "s-notice", None, None, "status badges"),
        (None, None, "user-action-time", None, None, "edited/asked timestamp"),
        ("link", "js-post-issue", None, None, None, "Timeline link"),
        ("link", "js-share-link", None, None, None, "Share link"),
        ("link", "js-suggest-edit-post", None, None, None, "Improve this question/answer link"),
        ("link", "comment-link", None, None, None, "Commented <timestamp> comment permalink"),
        ("link", _EMPTY, "show-votes", "landmark", None,
         "vote-count number in the related-questions sidebar -- own_role='link' is load-bearing: "
         "without it this rule also matched the bare 'static' text nested inside each unrelated "
         "question-hyperlink link (same empty own-class, same show-votes ancestor scope, wrong "
         "role), trusting the actual related-question title text while its own <link> stayed "
         "correctly untrusted -- found by inspecting a rendered review image, not by the scorer"),
        ("list item", "s-sidebarwidget--header", None, None, None,
         "sidebar widget section title (e.g. \"The Overflow Blog\"/\"Featured on Meta\") -- fixed "
         "platform vocabulary, safe to trust on its own"),
        ("link", "s-link__inherit", None, None, None, "asked/answered <timestamp> ago link"),
        ("link", "s-btn__filled", None, None, None, "Ask Question button-styled link"),
        ("menu item", _EMPTY, None, None, None, "sort-order menu item (Highest score, Newest, ...)"),
    ],
}


def predict_default_untrusted(row, own_class, path, trusted_rules):
    """Returns (verdict, note) -- see predict()'s docstring; note is None for both the
    control_shape global carve-out and the bare untrusted default (neither is a named rule)."""
    if row["role"] in _CONTROL_SHAPE_ROLES:
        return "trusted", "control_shape role (toggle/push button, combo box, checkbox, radio button)"
    for rule in trusted_rules:
        own_role, own_prefix, anc_prefix, anc_role, own_exact, note = rule[:6]
        anc_exclude = rule[6] if len(rule) > 6 else None
        if own_role is not None and row["role"] != own_role:
            continue
        if own_prefix is _EMPTY:
            if own_class:
                continue
        elif own_prefix is not None and not has_prefix(own_class, own_prefix):
            if not path_matches_ancestor_id_or_class(path, own_prefix, own_role):
                continue
        if own_exact is not None:
            if isinstance(own_exact, set):
                if own_class not in own_exact:
                    continue
            elif own_class != own_exact:
                continue
        if anc_prefix is not None and not path_matches_ancestor_id_or_class(path, anc_prefix, anc_role):
            continue
        if anc_exclude is not None and any(
                has_prefix(sig.get("class"), ex) or has_prefix(sig.get("id"), ex)
                for sig in path for ex in anc_exclude):
            continue
        return "trusted", note
    return "untrusted", None


def main():
    """Full-tree coverage report. Used to be a precision/recall run of evaluate() against
    labels/ground_truth.json over extraction.py's interactable-only rows; both are gone with the
    gen-1 universe, and the surviving question this file can answer on its own is coverage."""
    for filename in ("002_stackoverflow.com.xml", "006_unix.stackexchange.com.xml",
                     "053_github.com.xml"):
        if filename in RULES:
            check_full_tree_coverage(filename)


if __name__ == "__main__":
    main()
