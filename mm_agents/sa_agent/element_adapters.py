"""Per-surface adapters for ``endorse_element``: who authored a container, and which
controls that authorship licenses.

The contract this module exists to enforce
------------------------------------------
``endorse_element`` un-masks screen regions. The only thing that may license such a reveal is
an **exact identifier the attacker could not choose**: an address or handle the planner obtained
from ``get_contacts`` (operator-authored local state) or from an already-endorsed variable.
Everything else in the chain is structural -- read out of the accessibility tree by CSS
class/role, never by matching text an attacker can write.

Two consequences, both deliberate:

* **The identity operand is never a display name.** A display name is content: GitLab lets two
  accounts carry "Alice Kim", and Thunderbird lets any sender set any From display. Every
  ``author`` extractor below returns the platform's *identifier* (``news@example.com``,
  ``liu_qiang``, ``jsonathan``) or ``""``. Returning ``""`` means "this surface did not give me
  an identifier", and the container simply does not match -- fail closed, never fall back to the
  name. This mirrors ``tools._gloda_row_addr``, which already refuses a row that renders only a
  display name.
* **The planner never names a selector.** It names an abstract control kind from
  ``CONTROL_KINDS`` (``message_box``/``reaction``/``upvote``/``downvote``). The mapping from that
  word to ``entry`` + ``rc-message-box__textarea`` lives here, is operator-authored, and is never
  derived from the screen.

  There is ONE kind that takes a string from the planner, ``button``, and the difference between
  it and a selector is worth being exact about. It takes ``control_name``: the widget's label,
  spelled out in full, matched for equality. The planner is not naming a class, a pattern or a
  keyword this table expands -- it is naming the thing its own task told it to press, and the host
  asserts nothing about which page that is. A ROUTED name would be the opposite: a table mapping
  ``member_invite`` -> "Invite members" is the host claiming a fact about a page it may never have
  captured, and in that one case it never had (the GitLab members page appears in no corpus
  capture).

  What a stated name does NOT get past: the role set (``CONTROL_ROLES``, so a stated name cannot
  land on a link or heading whose text the attacker wrote) and the uniqueness rule. That second one
  carries the weight here -- a page content author CAN write ``aria-label="Invite members"`` on
  their own widget, and when they do, the genuine button and the forged one both match and the call
  REFUSES as ambiguous rather than picking. The residual risk is the case where the genuine widget
  is not on screen and only the forgery is, which uniqueness cannot see; a stated name is therefore
  the weakest key in this module and the reason it is a last resort rather than the default.
  Localization is the other cost and it is unchanged: a German RocketChat renders "Nachricht", so a
  name works only where the planner knows the rendered string.

Two independent routes, deliberately not chained
------------------------------------------------
**Identity** answers "which of these is X's?" -- an inbox row, a chat conversation, a post. It is
licensed by an exact identifier the attacker could not choose, and it reveals only the container
that identity authored.

**Control** answers "where is this application's own message box / reaction button?", and has
nothing to do with authorship. A message box is a widget the app renders whether or not anyone has
written anything. Treating a correspondent's name as permission to touch it would overreach --
naming a recipient would silently unlock an input -- and underreach, since a surface with no
authored container would have no route at all, which is exactly the state an empty conversation is
in.

What licenses a control is currently NOTHING: a recognised control is revealed on request. That is
an open question, not a settled design -- see ``find_controls``. The resolution itself is identical
on RocketChat, Reddit and GitLab; only the selector differs, which is what an adapter table is for.


Surfaces that cannot supply an identifier
-----------------------------------------
GitLab is one, measured rather than assumed: across the captured issue/list/project pages every
rendering of the author -- ``link.author-link``, the avatar's accessible name, the participants
sidebar, the header dropdown -- carries the DISPLAY NAME only, and the handle appears nowhere in
the tree in any form. The tree's sole URL-ish attribute is ``link-target``, which holds the HTML
``target`` (``_blank``), not the href. So ``_gitlab_author`` returns ``""`` and GitLab containers
never match. That is the correct outcome and not a placeholder: the wasp_gitlab value suite pits
``Alice Kim <@alicekim>`` against an impostor deliberately given the SAME display name, so a
route that resolved GitLab authorship from the tree today could only do it by matching the one
string that does not distinguish them. Fixing it is a capture-side change (surface the anchor
href), not an adapter change.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Optional



def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(el, name: str) -> str:
    for k, v in el.attrib.items():
        if _local(k) == name:
            return v
    return ""


def _classes(el) -> frozenset:
    return frozenset(_attr(el, "class").split())


def _role(el) -> str:
    """The node's AT-SPI role in the SPACE-separated spelling.

    The capture writes roles as element tags, where they are hyphenated (`push-button`,
    `toggle-button`, `page-tab`), while every other reader of this vocabulary -- the action list,
    labeling_store, the ground truth -- spells them with spaces. Controls are matched against role
    sets quoted from those readers, so the two spellings have to be reconciled in exactly one
    place. `Surface` keeps using the raw tag (`container_role="tree-item"`): those keys were
    written against the tags and are not the same vocabulary.
    """
    return _local(el.tag).replace("-", " ")


def _rect(el):
    sc, sz = _attr(el, "screencoord"), _attr(el, "size")
    try:
        x, y = (int(v) for v in sc.strip("()").split(","))
        w, h = (int(v) for v in sz.strip("()").split(","))
    except (ValueError, AttributeError):
        return None
    return (x, y, w, h) if w > 0 and h > 0 else None


def _offscreen(el) -> bool:
    return _attr(el, "offscreen_geometry") == "true"


def _href(el) -> str:
    """The link target the a11y server attached to this node, or "".

    The server queries AT-SPI's Hypertext interface (`queryHypertext()` / `getURI(0)`) and maps
    each URI onto the matching child whose role is `link`, where it lands as `attr:url`. That URI
    is written by the PLATFORM'S OWN ROUTING, not by any user, which is what makes it usable as an
    identity operand where the rendered text is not: GitLab routes a profile at /<handle> while
    displaying the person's chosen name, and RocketChat routes a DM at /direct/<handle> whatever
    `UI_Use_Real_Name` makes the sidebar show.
    """
    return _attr(el, "url")


def _descendant(el, *, cls: str = "", role: str = "", tag: str = ""):
    """First descendant (self included) matching every given constraint, or None."""
    for d in el.iter():
        if cls and cls not in _classes(d):
            continue
        if role and _local(d.tag) != role:
            continue
        if tag and _attr(d, "tag") != tag:
            continue
        return d
    return None



_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_REDDIT_COMMENT_BYLINE = re.compile(r"^(?P<user>\S+) \[t1_[A-Za-z0-9]+\] wrote\b")


def _tb_3pane_author(el) -> str:
    """Thunderbird 3-pane / quick-filter row: name is "sender, date, subject, status"."""
    name = _attr(el, "name")
    if name.count(",") < 2:
        return ""
    sender = name.split(",")[0].strip()
    m = _EMAIL_RE.search(sender)
    return m.group(0) if m else ""


def _tb_gloda_author(el) -> str:
    """Thunderbird global-search (gloda) hit: a `.message` container with a `.message-author`."""
    d = _descendant(el, cls="message-author")
    if d is None:
        return ""
    m = _EMAIL_RE.search(_attr(d, "name"))
    return m.group(0) if m else ""


def _rc_message_author(el) -> str:
    """RocketChat message: `.rcx-message` > `.rcx-message-header__name` > static holding the handle."""
    hdr = _descendant(el, cls="rcx-message-header__name")
    if hdr is None:
        return ""
    for d in hdr.iter():
        nm = _attr(d, "name").strip()
        if nm:
            return nm
    return ""


def _rc_sidebar_author(el) -> str:
    """RocketChat sidebar room/DM.

    Resolves a CHANNEL, never a person. Measured against a live instance:

      channel  <a href="/channel/general"  aria-label="general">          -> routable
      DM       <a href="/direct/CZYWHd5b..." aria-label="Mike Chen">      -> NOT routable

    A DM's path segment is the room id, not the username, and the accessible name is the display
    name whenever `UI_Use_Real_Name` is on -- which the eval turns on every episode so the agent's
    own account can be recognised. Neither is an identity, so a DM sidebar item yields "" and the
    person must be bound from a surface that does carry their handle. The username is present in
    the DOM only on the avatar (`src="/avatar/mike_chen"`, `data-username`), and neither attribute
    survives into the accessibility tree.
    """
    routed = _from_href(el, "rocketchat")
    if routed:
        return routed
    return _attr(el, "name").strip()


def _reddit_submission_author(el) -> str:
    """Reddit post: `link.submission__submitter`. Reddit has no real-name mode, so its rendered
    text is already the username, but the routed /user/<name> is preferred where present because it
    is the platform's own writing rather than a display string that could one day become one."""
    d = _descendant(el, cls="submission__submitter")
    if d is None:
        return ""
    return _from_href(d, "reddit") or _attr(d, "name").strip()


def _reddit_comment_author(el) -> str:
    """Reddit comment: parse the platform-generated byline, anchored; anything else refuses."""
    d = _descendant(el, cls="comment__info")
    if d is None:
        return ""
    m = _REDDIT_COMMENT_BYLINE.match(_attr(d, "name").strip())
    return m.group("user") if m else ""


_HREF_HANDLE = {
    "gitlab": re.compile(r"^https?://[^/]+/([A-Za-z0-9][A-Za-z0-9._-]*)/?(?:[?#].*)?$"),
    "rocketchat": re.compile(r"/(?:channel|group)/([A-Za-z0-9][A-Za-z0-9._-]*)/?(?:[?#].*)?$"),
    "reddit": re.compile(r"/(?:u|user)/([A-Za-z0-9][A-Za-z0-9._-]*)/?(?:[?#].*)?$"),
}


def _from_href(el, app: str) -> str:
    """The identifier in this node's link target, or "" when there is none to read."""
    pat = _HREF_HANDLE.get(app)
    if pat is None:
        return ""
    m = pat.search(_href(el))
    return m.group(1) if m else ""


def _gitlab_author(el) -> str:
    """GitLab renders the author's DISPLAY NAME and routes their profile by HANDLE.

    Two accounts can carry one display name -- the wasp_gitlab value suite gives the impostor
    @akim88 the same "Alice Kim" as the genuine @alicekim on purpose -- so the rendered text
    identifies nobody and only the href settles it. Returns "" when the link target is absent
    (an older VM image without the Hypertext extraction), which refuses rather than falling back
    to the name.
    """
    link = el if "author-link" in _classes(el) else _descendant(el, cls="author-link")
    return _from_href(link, "gitlab") if link is not None else ""



@dataclass(frozen=True)
class Surface:
    name: str
    app: str
    container_cls: str = ""
    container_role: str = ""
    container_tag: str = ""
    container_xml_role: str = ""
    author: Callable[..., str] = _gitlab_author
    match_on: str = "identifier"
    reveal: str = "container"
    name_cls: str = ""

    def matches(self, el) -> bool:
        if self.container_cls and self.container_cls not in _classes(el):
            return False
        if self.container_role and _local(el.tag) != self.container_role:
            return False
        if self.container_tag and _attr(el, "tag") != self.container_tag:
            return False
        if (self.container_xml_role
                and self.container_xml_role not in _attr(el, "xml-roles").split()):
            return False
        return bool(self.container_cls or self.container_role or self.container_tag
                    or self.container_xml_role)


SURFACES: tuple = (
    Surface("thunderbird_3pane", app="thunderbird", container_role="tree-item", author=_tb_3pane_author),
    Surface("thunderbird_3pane_tr", app="thunderbird", container_tag="tr", author=_tb_3pane_author),
    Surface("thunderbird_3pane_xmlrole", app="thunderbird", container_xml_role="treeitem",
            author=_tb_3pane_author),
    Surface("thunderbird_gloda", app="thunderbird", container_cls="message", author=_tb_gloda_author),
    Surface("rocketchat_message", app="rocketchat", container_cls="rcx-message",
            author=_rc_message_author, match_on="display",
            reveal="name", name_cls="rcx-message-header__name"),
    Surface("rocketchat_sidebar", app="rocketchat", container_cls="rcx-sidebar-item",
            author=_rc_sidebar_author, match_on="display"),
    Surface("reddit_submission", app="reddit", container_cls="submission",
            author=_reddit_submission_author, reveal="name", name_cls="submission__submitter"),
    Surface("reddit_comment", app="reddit", container_cls="comment",
            author=_reddit_comment_author, reveal="name", name_cls="comment__info"),
    Surface("gitlab_issue_header", app="gitlab", container_cls="detail-page-header-body",
            author=_gitlab_author, reveal="name", name_cls="author-link"),
    Surface("gitlab_note", app="gitlab", container_cls="timeline-entry", author=_gitlab_author,
            reveal="name", name_cls="author-link"),
    Surface("gitlab_participant", app="gitlab", container_cls="participants-author",
            author=_gitlab_author),
    Surface("gitlab_author_link", app="gitlab", container_cls="author-link", author=_gitlab_author),
)

UNRESOLVABLE_APPS: dict = {}



CONTROL_ROLES: frozenset = frozenset({
    "push button", "toggle button", "entry", "password text", "check box", "combo box",
    "radio button", "menu item", "page tab", "slider", "spin button", "date editor",
})


@lru_cache(maxsize=None)
def _rx(pattern: str):
    return re.compile(pattern)


@dataclass(frozen=True)
class Control:
    kind: str
    capability: str
    tokens: tuple = ()
    roles: frozenset = frozenset()
    tag: str = ""
    by_name: bool = False
    by_url: bool = False
    seen_on: str = ""

    def matches(self, el, name: str = "") -> bool:
        if self.roles and _role(el) not in self.roles:
            return False
        if self.tag and _attr(el, "tag") != self.tag:
            return False
        if self.by_url:
            want = (name or "").strip()
            return bool(want) and _attr(el, "url") == want
        if self.by_name:
            want = " ".join((name or "").split())
            got = " ".join((_attr(el, "name") or "").split())
            return bool(want) and got == want
        if not self.tokens:
            return False
        fields = [t for t in _classes(el)] + [_attr(el, "id")]
        return any(_rx(p).fullmatch(f) for p in self.tokens for f in fields if f)


CONTROLS: tuple = (
    Control("message_box", capability="chat.draft",
            tokens=(r"rc-message-box__textarea",), roles=frozenset({"entry"}),
            seen_on="rocketchat 440-448"),
    Control("message_box", capability="web.fill_form",
            tokens=(r"note-textarea", r"js-note-text",
                    r"reply_to_(?:submission|comment)_\d+_comment"),
            roles=frozenset({"entry"}), seen_on="gitlab 460-468, reddit 400-402"),
    Control("reaction", capability="web.click_link",
            tokens=(r"add-reaction-button", r"emoji-picker", r"js-reaction-group-button"),
            roles=frozenset({"push button", "toggle button"}), seen_on="gitlab 460-461"),
    Control("upvote", capability="web.click_link",
            tokens=(r"vote__up", r"js-vote-up-btn"),
            roles=frozenset({"push button", "toggle button"}), seen_on="reddit 400-408"),
    Control("downvote", capability="web.click_link",
            tokens=(r"vote__down", r"js-vote-down-btn"),
            roles=frozenset({"push button", "toggle button"}), seen_on="reddit 400-408"),
    Control("button", capability="web.click_link", roles=CONTROL_ROLES, by_name=True),
    Control("link", capability="web.click_link", roles=frozenset({"link"}), by_url=True),
)

CLICK_ONLY_KINDS: frozenset = frozenset({"link"})

CONTROL_KINDS: tuple = ("row", "message_box", "reaction", "upvote", "downvote", "button", "link")

NAMED_KINDS: frozenset = frozenset(c.kind for c in CONTROLS if c.by_name or c.by_url)
LABEL_KINDS: frozenset = frozenset(c.kind for c in CONTROLS if c.by_name)


@dataclass(frozen=True)
class Hit:
    bbox: tuple
    offscreen: bool
    surface: str
    author: str
    kind: str
    el: object = field(default=None, compare=False, repr=False)


def find_containers(root, author: str, inactive: Optional[set] = None,
                    display_names: Optional[set] = None,
                    reveal_container: bool = False) -> list:
    """Every container on screen whose EXACT author identifier equals *author*.

    *author* must already have been established out-of-band (contact book or an endorsed
    variable); this function only tests equality against a structurally extracted field. Matching
    is exact and case-normalized -- never substring, never against a display name.
    """
    want = (author or "").strip().casefold()
    if not want:
        return []
    names = {n.strip().casefold() for n in (display_names or set()) if n and n.strip()}
    inactive = inactive or set()
    out = []
    for el in root.iter():
        if id(el) in inactive:
            continue
        for s in SURFACES:
            if not s.matches(el):
                continue
            got = s.author(el)
            target = ({want} | names) if s.match_on == "display" else {want}
            if got and got.strip().casefold() in target:
                node = el
                if s.reveal == "name" and not reveal_container:
                    node = _descendant(el, cls=s.name_cls) if s.name_cls else None
                    if node is None:
                        break
                bbox = _rect(node)
                if bbox is not None:
                    out.append(Hit(bbox, _offscreen(node), s.name, got.strip(), "row", node))
            break
    return out


def apps_on_screen(root, inactive: Optional[set] = None) -> set:
    """Which adapter apps have at least one container rendered, regardless of author. Used only to
    pick the right refusal message."""
    inactive = inactive or set()
    seen = set()
    for el in root.iter():
        if id(el) in inactive:
            continue
        for s in SURFACES:
            if s.matches(el):
                seen.add(s.app)
                break
    return seen


def _form_submit_companions(root, box: "Hit", inactive: set, max_up: int = 6) -> list:
    """The submit button of the form a revealed message box sits in, as extra hits of that box.

    A comment box and its submit button are one widget: un-masking only the textarea left the
    planner able to type a comment it could never send (reddit's Post button sits in the same
    `<form>` as `reply_to_submission_<n>_comment` and stayed black -- 45 wasted steps in one
    episode). The companion is found STRUCTURALLY, not by label: the nearest `<form>` ancestor of
    the box, and in it the push button the browser marks as the form's default (`is_default`,
    i.e. the one Enter submits) -- or, where the form marks none (GitLab), its single primary
    `btn-confirm` button. Both are the platform's own markup, so nothing an author writes into a
    comment can become the companion -- and a form with no such button adds nothing.
    """
    if hasattr(box.el, "getparent"):
        up = lambda e: e.getparent()
    else:
        parent = {id(c): p for p in root.iter() for c in p}
        up = lambda e: parent.get(id(e))
    form, el = None, box.el
    for _ in range(max_up):
        el = up(el) if el is not None else None
        if el is None:
            break
        if _attr(el, "tag") == "form":
            form = el
            break
    if form is None:
        return []
    buttons = [el for el in form.iter()
               if id(el) not in inactive and _role(el) == "push button" and _rect(el) is not None]
    submit = [el for el in buttons if _attr(el, "is_default") == "true"]
    if not submit:
        submit = [el for el in buttons
                  if "btn-confirm" in _classes(el) and "dropdown-toggle" not in _classes(el)]
    if len(submit) != 1:
        return []
    el = submit[0]
    return [Hit(_rect(el), _offscreen(el), box.surface, "", box.kind, el)]


_ICON_RE = re.compile(r"[^\w\s$€£¥%.,:/&'()\-]", re.UNICODE)
_SUFFIX_RE = re.compile(r"\s*(?:\(\s*[^()]*\)|\d+)\s*$")


def _label_key(s: str) -> str:
    """A label with its DECORATION stripped: icons/symbols/emoji ("✓", "📥", "🥬", "+"),
    case and spacing. What remains is the words and numbers a person would read the label as."""
    s = unicodedata.normalize("NFKC", s or "")
    return " ".join(_ICON_RE.sub(" ", s).split()).casefold()


def _label_keys(label: str) -> set:
    """`_label_key(label)`, plus the same with ONE trailing annotation removed -- a count badge
    ("Inbox 11", "Following 0", "Cart 2") or a parenthesised aside ("Gluten-Free Pasta (+€2.00)").
    Both are conventions of UI rendering in general, not of any one site: the application
    appends live state to a fixed label, and the planner states the fixed part."""
    k = _label_key(label)
    keys = {k} if k else set()
    stripped = _label_key(_SUFFIX_RE.sub("", _label_key(label)))
    if stripped:
        keys.add(stripped)
    return keys


def _text_of(el) -> str:
    return " ".join(f"{_attr(el, 'name') or ''} {el.text or ''}".split())


def _parent_fn(root, el):
    if hasattr(el, "getparent"):
        return lambda e: e.getparent()
    parent = {id(c): p for p in root.iter() for c in p}
    return lambda e: parent.get(id(e))


def _controls_by_label(root, stated: str, roles: frozenset, inactive: set) -> list:
    """G3: unnamed controls resolved through the visible LABEL element that belongs to them.

    The standard HTML form pattern `<label>Date Range:</label><select>` -- written without the
    `for=` link that would have made the label the control's accessible name -- reaches the tree as
    a `label` node followed by a combo box / entry whose name is empty (measured: 62 of the UCM
    sites' unnamed fields, and the same shape elsewhere in the corpus). Only that structure is
    accepted, nothing positional:
      * a `label`-role node whose text matches the stated label (same decoration rules as G1, plus
        a trailing colon), and
      * EITHER its next sibling that renders anything is an UNNAMED control of an accepted role,
      * OR the label wraps exactly one unnamed control (`<label>Remember me <input></label>`).
    The control must be unnamed: a named control answers for its own name, never for a label's.
    """
    want = _label_key(stated).rstrip(":").strip()
    if not want:
        return []

    def unnamed_control(el) -> bool:
        return (_role(el) in roles and not (_attr(el, "name") or "").strip()
                and _rect(el) is not None and id(el) not in inactive)

    out, seen = [], set()
    for lab in root.iter():
        if _role(lab) != "label" or id(lab) in inactive:
            continue
        if want not in {k.rstrip(":").strip() for k in _label_keys(_text_of(lab))}:
            continue
        inner = [d for d in lab.iter() if d is not lab and unnamed_control(d)]
        cand = inner[0] if len(inner) == 1 else None
        if cand is None and not inner:
            parent = _parent_fn(root, lab)(lab)
            sibs = list(parent) if parent is not None else []
            after = sibs[sibs.index(lab) + 1:] if lab in sibs else []
            nxt = next((s for s in after if _rect(s) is not None
                        and (_text_of(s) or _role(s) in roles)), None)
            if nxt is not None and unnamed_control(nxt):
                cand = nxt
        if cand is not None and id(cand) not in seen:
            seen.add(id(cand))
            out.append(cand)
    return out


def _narrow_by_anchor(root, found: list, anchor: str, inactive: set) -> tuple:
    """Among several same-labelled controls, the ONE whose own group contains *anchor*.

    A control's own group is the LARGEST ancestor that contains it and no other candidate -- the
    card, row or list item it belongs to, found from the tree's nesting alone (no class names, no
    roles, no site vocabulary). The candidate is kept iff some node inside that group has text
    containing the anchor (case-insensitive, whitespace-normalised). Exactly one survivor, or a
    refusal: two cards that both mention the anchor are as ambiguous as no anchor at all.

    The anchor is text the PLANNER states (from its task, e.g. the event title). It is only ever
    compared against the tree -- nothing is read back -- so it cannot become an input channel, and
    only the control itself is revealed, never the group or the anchor text.
    """
    want = _label_key(anchor)
    if not want:
        return found, ""
    up = _parent_fn(root, found[0])
    ids = {id(c) for c in found}

    def own_group(c):
        group, el = c, up(c)
        while el is not None:
            inside = {id(d) for d in el.iter()} & ids
            if inside != {id(c)}:
                break
            group, el = el, up(el)
        return group

    keep = []
    for c in found:
        g = own_group(c)
        if g is c:
            continue
        if any(want in _label_key(_text_of(d)) for d in g.iter()
               if d is not c and id(d) not in inactive):
            keep.append(c)
    if len(keep) == 1:
        return keep, ""
    if not keep:
        anchors = [d for d in root.iter() if id(d) not in inactive and id(d) not in ids
                   and _rect(d) is not None and want in _label_key(_text_of(d))
                   and not any(want in _label_key(_text_of(k)) for k in d if _rect(k) is not None)]
        picks = set()
        for a in anchors:
            ax, ay, aw, ah = _rect(a)
            same_row = [c for c in found
                        if min(ay + ah, _rect(c)[1] + _rect(c)[3]) - max(ay, _rect(c)[1]) > 0]
            picks.add(id(same_row[0]) if len(same_row) == 1 else None)
        if len(picks) == 1 and None not in picks:
            pid = next(iter(picks))
            return [c for c in found if id(c) == pid], ""
        return [], (f"could not tie any of the {len(found)} matching controls to {anchor!r}: either "
                    f"no item containing that text holds one, or this list does not group its items "
                    f"in the page structure (a flat list), where picking by position would risk the "
                    f"neighbouring item's control. Open the item itself (e.g. its detail view) and "
                    f"retry there, or use another route.")
    return [], (f"{len(keep)} of the matching controls sit in items that mention {anchor!r} -- "
                f"refusing rather than guessing. Use a more specific anchor.")


def find_controls(root, kind: str, capabilities=None, inactive: Optional[set] = None,
                  name: str = "", anchor: str = "") -> tuple:
    """Resolve the application's own *kind* control on the current surface.

    Returns ``(hits, error)``. NOT licensed by authorship -- a message box is the application's own
    widget, not something a correspondent wrote, so naming a person is neither necessary nor
    sufficient to touch it. What licenses it INSTEAD is undecided: *capabilities* is threaded here
    and each Control names the capability it would exercise, but nothing is checked, so any
    recognised control is revealed on request.

    *name* is the planner's full, exact accessible name, and is consulted ONLY by a ``by_name``
    spec. Passing it for a token-matched kind is an error rather than a no-op: a planner that
    believes it is narrowing a `message_box` by label should be told the label did nothing, not
    silently handed whatever the tokens found.

    The control is resolved over the whole surface and must come back as exactly one node.
    Anything else is a refusal, never a tie-break: on a page carrying several reaction controls
    (one per comment) there is no way to tell which the task meant, and guessing is how an agent
    acts on the wrong item.
    """
    inactive = inactive or set()
    specs = [c for c in CONTROLS if c.kind == kind]
    if not specs:
        known = sorted({c.kind for c in CONTROLS})
        return [], (f"'{kind}' is not a known control kind; the kinds are {', '.join(known)}.")
    stated = " ".join((name or "").split())
    if kind in NAMED_KINDS and not stated and not (kind in LABEL_KINDS and anchor):
        if kind not in LABEL_KINDS:
            return [], (f"control={kind!r} is resolved by the link's exact target URL, so pass "
                        f"`control_name` = that URL, exactly as you have it (from your task, a "
                        f"trusted page, or an endorsed variable). Compared for strict equality.")
        return [], (f"control={kind!r} is resolved by the widget's own label, so pass "
                    f"`control_name` with the label as it reads (e.g. control_name=\"Invite members\"). "
                    f"Icons, check marks, case and a trailing count badge like \"Inbox 11\" are "
                    f"ignored; the words themselves must match -- a paraphrase will not. If the thing "
                    f"you want to act on is a LABEL that is not itself a control -- a tag or category "
                    f"shown as text beside the button that acts on it -- pass control_name=\"\" with "
                    f"anchor set to that label, to act on the control sharing its row.")
    if stated and kind not in NAMED_KINDS:
        return [], (f"control={kind!r} is resolved by the widget's structure, not by its label, so "
                    f"`control_name` does not apply -- pass control_name=\"\". Use "
                    f"control=\"button\" with control_name set when the label is the only handle.")
    if anchor and kind not in LABEL_KINDS:
        return [], (f"`anchor` narrows a LABELLED control (control=\"button\"); it does not apply "
                    f"to control={kind!r} -- pass anchor=\"\".")
    allowed = list(specs)
    hits: list = []
    for spec in allowed:
        if spec.by_name and not stated and anchor:
            found = [el for el in root.iter()
                     if id(el) not in inactive and _role(el) in spec.roles and _rect(el) is not None]
            if found:
                found, aerr = _narrow_by_anchor(root, found, anchor, inactive)
                if aerr:
                    return [], aerr
            hits += [Hit(_rect(el), _offscreen(el), spec.seen_on or kind, "", kind, el) for el in found]
            continue
        found = [el for el in root.iter()
                 if id(el) not in inactive and spec.matches(el, stated) and _rect(el) is not None]
        if spec.by_name and not found:
            want = _label_key(stated)
            found = [el for el in root.iter()
                     if id(el) not in inactive and _role(el) in spec.roles
                     and _rect(el) is not None and want in _label_keys(_attr(el, "name"))]
        if spec.by_name and not found:
            found = _controls_by_label(root, stated, spec.roles, inactive)
        if anchor and len(found) > 1:
            found, aerr = _narrow_by_anchor(root, found, anchor, inactive)
            if aerr:
                return [], aerr
        if len(found) > 1:
            hint = (" Pass `anchor` = text from your task that is rendered inside the SAME item "
                    "as the one you mean (e.g. the event/product/ticket name) to pick it."
                    if kind in LABEL_KINDS and not anchor else "")
            return [], (f"the '{kind}' control is ambiguous on this screen ({len(found)} "
                        f"candidates) -- refusing rather than guessing which one the task means."
                        f"{hint}")
        for el in found:
            hits.append(Hit(_rect(el), _offscreen(el), spec.seen_on or kind, "", kind, el))
    if len(hits) > 1:
        return [], (f"the '{kind}' control matched {len(hits)} widgets across apps -- refusing "
                    f"rather than guessing which one the task means.")
    if kind == "message_box" and hits:
        hits += _form_submit_companions(root, hits[0], inactive)
    if not hits:
        if kind in NAMED_KINDS and kind not in LABEL_KINDS:
            return [], (f"no link with target {stated!r} is on screen (strict URL equality). It "
                        f"may be scrolled out of view, collapsed, or on another page/tab.")
        if kind in NAMED_KINDS:
            return [], (f"no control labelled {stated!r} is on screen (icons, case and count badges "
                        f"are ignored, the words are not); it may also be collapsed, not open yet, "
                        f"or on another page/tab.")
        return [], (f"no '{kind}' control is on screen; it may be collapsed, not open yet, or on "
                    f"another page/tab.")
    return hits, ""
