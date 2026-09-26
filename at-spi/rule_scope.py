#!/usr/bin/env python3
"""rule_scope.py -- which rule set applies to the surface in front of the agent.

A Phase-1 rule set is proposed for ONE page skeleton and is valid for that skeleton only. Deciding
which stored set to run against a live screen is therefore two questions, and this module is the
one place either is answered -- offline (grouping the corpus) and live (SA-Agent's per-turn
lookup) alike, so the two cannot drift:

  SCOPE     -- whose site or app is this?      `reddit.com`, `app:soffice`
  TEMPLATE  -- which page/skeleton of theirs?  `/f/*/#/*`, `app://impress_empty`

WHY BOTH, AND WHY THEY ARE NEVER MERGED. The corpus holds several captures per site, and a
tempting shortcut is to concatenate their rules: every rule is a scoped grant and `predict_trusted`
returns on the first match, so more rules can only mean more of the page is usable. That shortcut
is measurably unsafe. Leave-one-out over the 92 corpus captures that share a (scope, template)
group -- score each capture against its template-mates' rules instead of its own -- moves
`leak`, the count of missed free-text untrusted elements, from 8 to 23. Those 15 extra leaks are
the exact failure this system exists to prevent, and they buy 1614 -> 2730 usable elements. So:
one rule set per (scope, template), selected, never merged. `RuleCache` in
mm_agents/sa_agent/masking.py says the same thing in prose; this is the measurement behind it.

WHY THE MANIFEST'S OWN `domain` FIELD IS NOT THE SCOPE. For the public-web corpus it happens to be
a hostname (`github.com`), but for the four self-hosted eval platforms it is a PATH SLUG:

    {"url": "http://reddit.com/f/coolgithubprojects/103666/i-made-a-browser-extension",
     "domain": "reddit_f_coolgithubprojects_103666_i_made_a_browser_ext"}

A live lookup keyed on the hostname (`reddit.com`, read from the omnibox) could never match that,
so every rule set for reddit/owncloud/rocketchat/gitlab -- 37 captures' worth, all scored -- was
unreachable at runtime and every episode on those platforms silently fell through to live
proposal. The `domain` field is not wrong, it is the TEMPLATE identity wearing the wrong name.
Both halves are derived here from `url`, which is the field that actually carries the identity.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

_AT_SPI_DIR = os.path.dirname(os.path.abspath(__file__))

_APP_SCHEME = "app://"

APP_SCOPE_PREFIX = "app:"



def host_of_url(url: str | None) -> str | None:
    """Hostname of a URL that may or may not carry a scheme, lowercased. None for `app://`.

    A scheme is supplied for PARSING only when one is missing: Chrome's omnibox elides it
    (`reddit.com/f/books`, not `http://reddit.com/f/books`) and urlparse would otherwise put the
    whole string in `path` and report no hostname. Which scheme it really was is unknowable from
    the omnibox text and is never recorded as fact.
    """
    if not url or url.startswith(_APP_SCHEME):
        return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url if "://" in url else f"https://{url}")
        return (parsed.hostname or "").lower() or None
    except Exception:
        return None


def normalize_host(host: str | None) -> str | None:
    """`www.` stripped. The only host normalisation worth doing blind, applied on BOTH sides of
    every comparison so a stored `www.booking.com` and a live `booking.com` are one scope."""
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def scope_of_url(url: str | None) -> str | None:
    """The scope key for a capture URL, or None if there is nothing to key on.

    Web  -> the normalised hostname.
    App  -> `app:<at-spi application name>` -- but that name lives in the TREE, not the URL, so
            `app://` returns None here and callers use `scope_of_manifest_row` (which reads the
            manifest's `app` field) or `app_scope_of_tree` (live) instead.
    """
    return normalize_host(host_of_url(url))


def scope_of_manifest_row(row: dict) -> str | None:
    """Scope for one manifest row, web or native.

    Native rows carry the AT-SPI application name in `app` -- a field the manifest schema has
    always had and which is populated from each capture's own XML. It is
    read rather than guessed because the mapping from app family to AT-SPI name is not derivable
    (`soffice`, `code`, `gnome-terminal-server`), and because one name legitimately covers several
    templates: Writer, Calc and Impress are all `soffice`, and telling them apart is the template
    question, not the scope one.
    """
    url = str(row.get("url") or "")
    if url.startswith(_APP_SCHEME):
        app = (row.get("app") or "").strip()
        return f"{APP_SCOPE_PREFIX}{app}" if app else None
    return scope_of_url(url)


def is_app_scope(scope: str | None) -> bool:
    return bool(scope) and scope.startswith(APP_SCOPE_PREFIX)



_ID_RE = re.compile(r"^\d+$")
_OPAQUE_RE = re.compile(r"^(?=.*\d)[0-9a-f]{8,}$|^[0-9a-f]{16,}$", re.I)


IDENTITY_QUERY: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "owncloud.com": (("/index.php/apps/files", ("dir",)),),
}

_IDENTITY_QUERY_DEFAULT: dict[str, str] = {"dir": "/"}


def identity_query(url: str | None, scope: str | None = None) -> str | None:
    """The identity-bearing part of `url`'s query, normalised. None when the scope declares none.

    Percent-decoded, lowercased and stripped of a trailing slash on BOTH sides of every
    comparison, so a stored `?dir=/Documents/Data%20Analysis` and a live
    `?dir=/documents/data analysis` are one page rather than two.
    """
    if not url or url.startswith(_APP_SCHEME):
        return None
    entries = IDENTITY_QUERY.get(scope or scope_of_url(url) or "")
    if not entries:
        return None
    from urllib.parse import parse_qsl, unquote, urlparse
    parsed = urlparse(url if "://" in url else f"https://{url}")
    here = (parsed.path.rstrip("/") or "/").lower()
    params: tuple[str, ...] = ()
    best = -1
    for prefix, names in entries:
        stem = prefix.rstrip("/") or "/"
        if (here == stem or here.startswith(stem + "/")) and len(stem) > best:
            params, best = names, len(stem)
    if not params:
        return None
    got = dict(parse_qsl(parsed.query, keep_blank_values=True))
    out: list[str] = []
    for name in params:
        raw = got.get(name)
        if raw is None or str(raw).strip() == "":
            raw = _IDENTITY_QUERY_DEFAULT.get(name)
        if raw is None:
            continue
        val = unquote(str(raw)).strip().lower().rstrip("/") or "/"
        if not val.startswith("/"):
            val = "/" + val
        out.append(f"{name}={val}")
    return "&".join(out) or None


def identity_query_template(qid: str | None) -> str | None:
    """`dir=/documents/financials` -> `dir=/documents/*`: the section kept, the folder generalised.

    The leading segment is a section of the store and its listings genuinely differ -- on this
    seed `/Photos` renders a thumbnail grid and `/Documents` a name/size/modified table -- so it
    stays literal for the same reason `loose_template` keeps a path's leading segment. Everything
    below it is an instance of "a folder" by definition. An unseen `?dir=/Documents/Q3` therefore
    lands on a captured sibling listing at the template tier instead of falling through to the
    prefix tier, and `?dir=/Photos` never borrows `/Documents`'s rules there.
    """
    if not qid:
        return qid
    out: list[str] = []
    for part in qid.split("&"):
        name, _, val = part.partition("=")
        segs = [s for s in val.split("/") if s]
        out.append(f"{name}=" + ("/" + "/".join([segs[0]] + ["*"] * (len(segs) - 1)) if segs else "/"))
    return "&".join(out)


def routing_template(url: str | None, scope: str | None = None) -> str | None:
    """The template a rule set is KEYED under: the path template plus the identity query.

    `template_of_url` answers "which skeleton is this" for grouping the corpus, and that grouping
    must not move when a routing tier is added -- rule_acquisition.py's template arms are measured
    against it. This answers the narrower question "which cache slot is this page's rule set", and
    for ownCloud the two differ: nine pages share one path template, and keying a cached proposal
    on that alone hands a PDF viewer's live proposal to a file listing the next episode. That is
    the same conflation IDENTITY_QUERY fixes for the reviewed tier, one tier down.

    Round-trips: the identity half is separated by `?`, which no path template contains.
    """
    template = template_of_url(url)
    qid = identity_query_template(identity_query(url, scope))
    return f"{template}?{qid}" if template and qid else template


def path_template_of(routing: str | None) -> str | None:
    """The path half of a `routing_template`, for the tiers that generalise over the identity."""
    return routing.split("?", 1)[0] if routing else routing


OVERLAY_VIEWS = frozenset({"files_pdf_viewer"})


def template_of_url(url: str | None) -> str | None:
    """The template signature of a capture/live URL.

    App URLs are their own template: `app://impress_empty` names one window of one application,
    and there is no path structure to generalise over.

    Web URLs reduce to their path with instance segments wildcarded:

        /f/coolgithubprojects/103666/i-made-a-browser-extension  ->  /f/*/#/*
        /f/technology                                            ->  /f/*
        /channel/random                                          ->  /channel/*
        /index.php/apps/files                                    ->  /index.php/apps/files
        /questions/tagged/python                                 ->  /questions/tagged/*

    The query string is dropped HERE, and put back by `identity_query` for the one scope whose
    query carries the page identity. Keeping it in the template would make every booking search
    and every amazon query its own template with one capture in it; dropping it outright made all
    nine ownCloud captures one template, which is the failure IDENTITY_QUERY exists to fix. The
    two live side by side rather than merged, because `template_of_url` is also what groups the
    corpus into template families for measurement (rule_acquisition.py) and that grouping should
    not move when a routing tier is added.
    """
    if not url:
        return None
    if url.startswith(_APP_SCHEME):
        return url
    path = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*", "", url, flags=re.I) if "://" in url else \
        re.sub(r"^[^/]*", "", url)
    path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    segs = [s for s in path.split("/") if s]
    if not segs:
        return "/"
    out: list[str] = []
    for i, seg in enumerate(segs):
        low = seg.lower()
        if _ID_RE.match(seg):
            out.append("#")
        elif _OPAQUE_RE.match(seg):
            out.append("#")
        elif out and out[-1] == "#":
            out.append("*")
        elif i > 0 and len(segs) > 1 and _looks_like_instance(seg, i, segs):
            out.append("*")
        else:
            out.append(low)
    return "/" + "/".join(out)


def _looks_like_instance(seg: str, i: int, segs: list) -> bool:
    """Is this segment a VALUE under a named section, rather than part of the name?

    Only ever asked of a non-first segment, because the first names the section itself. A leaf
    under exactly one section (`/channel/random`, `/user/AineeJames`, `/f/technology`) is an
    instance; anything carrying a file extension (`/hotel/us/the-plaza.html`,
    `/searchresults.html`) is a real page name and stays literal, as does a segment that is
    plainly a route word rather than a value.
    """
    if "." in seg:
        return False
    if seg.lower() in _ROUTE_WORDS:
        return False
    return i == len(segs) - 1 or len(seg) > 20 or seg.count("-") >= 2


_ROUTE_WORDS = frozenset({
    "issues", "commits", "merge_requests", "pulls", "projects", "explore", "settings", "personal",
    "tagged", "questions", "users", "comments", "forums", "home", "hot", "new", "top", "channels",
    "directory", "files", "apps", "index.php", "search", "login", "admin", "main",
})



@dataclass(frozen=True)
class CaptureRef:
    """One corpus capture, reduced to what selecting a rule set needs."""
    index: int
    capture: str
    scope: str
    template: str
    path: str
    url: str
    qid: str | None = None
    qtmpl: str | None = None
    view: str | None = None

    @property
    def is_app(self) -> bool:
        return is_app_scope(self.scope)

    @property
    def is_overlay(self) -> bool:
        return self.view in OVERLAY_VIEWS


def _path_of(url: str) -> str:
    if url.startswith(_APP_SCHEME):
        return url
    p = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*", "", url, flags=re.I) if "://" in url else ""
    return (p.split("?", 1)[0] or "/")


def index_manifest(manifest) -> dict[str, list[CaptureRef]]:
    """{scope: [CaptureRef, ...]} for every row that has an identifiable scope.

    Accepts the manifest in either shape it is handed around in: the raw JSON list, or
    labeling_store.load_manifest()'s {filename: row} dict. Rows with no usable scope -- a native
    capture whose `app` has not been backfilled -- are skipped and reported by the caller rather
    than being silently keyed under None.
    """
    rows = manifest.values() if isinstance(manifest, dict) else manifest
    out: dict[str, list[CaptureRef]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        scope = scope_of_manifest_row(row)
        url = str(row.get("url") or "")
        template = template_of_url(url)
        capture = row.get("filename")
        if not scope or not template or not capture:
            continue
        qid = identity_query(url, scope)
        out.setdefault(scope, []).append(CaptureRef(
            index=int(row.get("index", 0)), capture=capture, scope=scope,
            template=template, path=_path_of(url), url=url,
            qid=qid, qtmpl=identity_query_template(qid),
            view=(row.get("view") or None),
        ))
    for refs in out.values():
        refs.sort(key=lambda r: r.index)
    return out



MATCH_EXACT = "exact_path"
MATCH_TEMPLATE = "template"
MATCH_TEMPLATE_LOOSE = "template_loose"
MATCH_PREFIX = "path_prefix"
MATCH_DEFAULT = "scope_default"


def loose_template(template: str | None) -> str | None:
    """A template with its remaining literal segments wildcarded, except the leading one.

    `template_of_url` keeps a segment literal whenever it cannot show the segment is an instance,
    because splitting one template into two only costs recall while merging two costs safety. That
    caution has a predictable cost of its own: reddit's `/f/technology/#/*` and
    `/f/coolgithubprojects/#/*` are one page skeleton under two names, purely because the forum
    name sits in a position the per-URL rules cannot call.

    This is the second look, tried only after an exact and a strict template match have both
    missed. The leading segment stays literal -- it is the route that says WHICH KIND of page this
    is (`/questions/...` vs `/users/...`), and wildcarding it would merge genuinely different
    skeletons -- and `#` stays distinct from `*`, so a path with an id in it can never collapse
    onto one without. `/questions/tagged/*` and `/questions/#/*` therefore stay apart, while
    booking's `/hotel/gb/the-savoy.html` and `/hotel/us/the-plaza.html` correctly become one.
    """
    if not template or template.startswith(_APP_SCHEME):
        return template
    segs = [s for s in template.split("/") if s]
    if len(segs) < 2:
        return template
    return "/" + "/".join([segs[0]] + [s if s == "#" else "*" for s in segs[1:]])


def select_capture(refs: list[CaptureRef], url: str | None) -> tuple[CaptureRef | None, str]:
    """Pick the one capture whose rule set should run against `url`. Returns (ref, how).

    Ordered by how much of the page skeleton the match actually evidences:

      1. the same path AND the same identity query -- the same page, nothing to argue about
      2. the same template AND the same identity-query template -- the same skeleton with
                                 different content, which is exactly the unit a Phase-1 rule set
                                 is proposed for
      3. the same loose template -- as above, allowing for a section name the per-URL rules had to
                                 leave literal; see loose_template()
      4. the longest shared literal path prefix -- weak, but `/questions/tagged/rust` under
                                 `/questions/tagged/python`'s rules beats an unrelated page's
      5. the lowest-index capture for the scope -- the pre-existing fallback, kept so a scope with
                                 a single capture behaves exactly as it did before templates existed

    The identity query is None for every scope but ownCloud (see IDENTITY_QUERY), so tiers 1 and 2
    read exactly as they did before it existed everywhere else. Tiers 3 and 4 ignore it on purpose:
    they are already the "nothing better matched" tiers, and narrowing them by a query would just
    turn them into dead copies of tier 2 for the one scope that has one.

    Ties inside every tier break on the lowest capture index, so the choice is reproducible across
    runs and machines rather than depending on directory order.
    """
    if not refs:
        return None, MATCH_DEFAULT

    if any(r.is_overlay for r in refs):
        kept = [r for r in refs if not r.is_overlay]
        if kept:
            refs = kept

    if len(refs) == 1:
        return refs[0], MATCH_DEFAULT

    if refs[0].is_app or not url:
        return refs[0], MATCH_DEFAULT

    want_path = _path_of(url if "://" in url else f"https://{url}").rstrip("/") or "/"
    want_tmpl = template_of_url(url)
    want_qid = identity_query(url, refs[0].scope)
    want_qtmpl = identity_query_template(want_qid)

    exact = [r for r in refs
             if (r.path.rstrip("/") or "/") == want_path and r.qid == want_qid]
    if exact:
        return exact[0], MATCH_EXACT

    same = [r for r in refs if r.template == want_tmpl and r.qtmpl == want_qtmpl]
    if same:
        return same[0], MATCH_TEMPLATE

    want_loose = loose_template(want_tmpl)
    loose = [r for r in refs if loose_template(r.template) == want_loose]
    if loose:
        return loose[0], MATCH_TEMPLATE_LOOSE

    want_segs = [s for s in want_path.split("/") if s]
    best, best_n = None, 0
    for r in refs:
        segs = [s for s in r.path.split("/") if s]
        n = 0
        for a, b in zip(want_segs, segs):
            if a.lower() != b.lower():
                break
            n += 1
        if n > best_n:
            best, best_n = r, n
    if best is not None and best_n > 0:
        return best, MATCH_PREFIX

    return refs[0], MATCH_DEFAULT



def app_scope_of_tree(root: ET.Element, ls) -> str | None:
    """`app:<at-spi application name>` for the application owning the ACTIVE window, or None.

    `ls` is the labeling_store module, passed in rather than imported so this file stays free of
    the sys.path shim its callers already run (and so at-spi's own tooling, which imports
    labeling_store anyway, does not pay for a second import).

    The ACTIVE window decides, not the first application in the tree: on a live desktop several
    apps are running at once and "which app is the agent working in" is a focus question. Falls
    back to the first non-shell application carrying a window when nothing is marked active, which
    is what a single-app capture looks like.
    """
    fallback = None
    for app in root:
        if app.tag != "application" or ls._is_shell_app(app.get("name")):
            continue
        name = (app.get("name") or "").strip()
        if not name:
            continue
        for window in app:
            if ls._node_bbox(window) is None:
                continue
            if fallback is None:
                fallback = name
            if (ls._states(window).get("active") or "").lower() == "true":
                return f"{APP_SCOPE_PREFIX}{name}"
    return f"{APP_SCOPE_PREFIX}{fallback}" if fallback else None


def infer_platform_live(root: ET.Element, ls) -> str:
    """"web" or "app" for a LIVE tree, decided by which window has focus.

    labeling_store._infer_platform answers a different question -- "is this CAPTURE a browser
    capture" -- and answers it by asking whether a browser application exists at all. That is right
    offline, where a capture holds one app by construction. Live it is wrong in the common case:
    Chrome is always running in the eval VM, so a turn spent in the terminal or the file manager
    would still resolve to "web", the browser's document-web would be taken as the content root,
    and the app the agent is actually working in would contribute no rows at all.

    Deliberately falls back to `ls._infer_platform` when no window is marked active, so a tree that
    looks like a corpus capture is classified exactly as the corpus classifies it.
    """
    for app in root:
        if app.tag != "application" or ls._is_shell_app(app.get("name")):
            continue
        is_browser = (app.get("name") or "").strip().lower() in ls._BROWSER_APPS
        for window in app:
            if ls._node_bbox(window) is None:
                continue
            if (ls._states(window).get("active") or "").lower() == "true":
                return "web" if is_browser else "app"
    return ls._infer_platform(root)
