"""A domain-agnostic site profile in UCM's `sites/<name>/site.py` shape.

WHY THIS EXISTS. UCM's pipeline is site-keyed: every run loads a hand-written profile supplying
the HTML cleaner, the text sanitizer, and the LLM prompt. The three shipped profiles are tuned to
their site -- booking's prompt is ~2,500 words of property-card anatomy naming, by hand, which
slot of a listing is geocoded and which is owner-typed. That tuning is a large part of the
published F1, and it does not exist for the 18 hosts in this corpus.

Writing 18 tuned profiles would measure how well we can describe 18 websites, not how well UCM's
method generalises. So there is exactly one profile, parameterised by host name, and every host
gets the same words. The number it produces is UCM's method carried to an unseen site, which is
the only number comparable to a per-capture labeler that was never told what site it is on.

WHAT IS PRESERVED. The prompt keeps UCM's skeleton verbatim wherever the skeleton is not about a
particular site: the sanitized-HTML framing, the provenance question ("did the platform produce
this text, or did a user type it in?"), the ALL-USERS-ARE-UNTRUSTED clause, the prompt-injection
motivation, the four output fields, the granularity rule, the no-bare-hashed-class rule, the
platform-computed-values carve-out, and the JSON-only output contract. Those sentences are taken
from sites/{reddit,gitlab,booking}/site.py, generalised only by replacing the site's name with
{site} and its examples with structural ones.

WHAT IS DROPPED. Every site-specific enumeration: booking's card anatomy, reddit's list of old
Reddit class names, gitlab's list of GitLab entity types. Nothing replaces them -- a generic run
cannot know them, and inventing per-host substitutes would reintroduce the tuning this profile
exists to avoid.

The cleaner and sanitizer are the union of the three shipped implementations minus their
site-specific attribute prefixes (booking's data-bui-*, gitlab's data-v-*); those prefixes are
inert on other hosts, so dropping them changes nothing except that no host gets a private rule.
"""
import re
import types

from bs4 import BeautifulSoup, Comment, NavigableString

CLEAN_HTML_MAX_SIZE = 200_000
NUM_LLM_TURNS = 3

PAGE_READY = {
    "remote_wait_until": "load",
    "local_wait_until": "load",
    "content_selector": "body",
    "content_label": "document body",
    "extra_sleep_seconds": 0,
    "stability_probe_js": "() => ({ total: document.querySelectorAll('body *').length })",
    "stability_signature_keys": ["total"],
    "stability_log_format": "Total: {total}",
}


def clean_html_for_llm(html_content, max_size=None):
    """Strip framework noise so the model reasons about meaningful structure.

    Union of the three shipped cleaners, minus their per-site attribute prefixes.
    """
    if max_size is None:
        max_size = CLEAN_HTML_MAX_SIZE
    soup = BeautifulSoup(html_content, "html.parser")

    for tag in soup(["script", "style", "noscript", "meta", "link", "base"]):
        tag.decompose()
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()
    for iframe in soup.find_all("iframe"):
        iframe.decompose()
    for pixel in soup.find_all("img", width="1"):
        pixel.decompose()
    for svg in soup.find_all("svg"):
        svg.decompose()

    for tag in soup.find_all(["div", "span"]):
        if (not tag.get_text(strip=True) and not tag.find_all(True)
                and not tag.get("data-testid") and not tag.get("role")):
            tag.decompose()

    for tag in soup.find_all(True):
        if tag.get("style"):
            del tag["style"]
        for attr in [a for a in list(tag.attrs.keys()) if a.startswith("on") or a == "tabindex"]:
            del tag[attr]

    cleaned = str(soup)
    if len(cleaned) > max_size:
        cleaned = cleaned[:max_size] + "\n<!-- [HTML truncated for size] -->"
    return cleaned


def sanitize_html_content(html_content):
    """Replace every text node with [text:length:N] and anonymize URLs/titles/data-* values.

    This is the property the whole method rests on: the model proposing the selectors never reads
    a single character of page text, so a prompt injection sitting in the page cannot reach it.
    Verbatim in behaviour from the shipped sanitizers; only their site-specific URL patterns
    (booking's /hotel/, /searchresults) are gone, and those only ever produced a prettier
    placeholder than the generic one.
    """
    soup = BeautifulSoup(html_content, "html.parser")

    for text_node in soup.find_all(string=True):
        if isinstance(text_node, NavigableString):
            parent = text_node.parent
            if parent and parent.name not in ("script", "style"):
                text_content = str(text_node).strip()
                if text_content:
                    text_node.replace_with(f"[text:length:{len(text_content)}]")

    def anonymize_url(val):
        if not val or not isinstance(val, str):
            return val
        return "[link]" if val.startswith("http") else val

    safe_pattern = re.compile(r"^(true|false|[0-9]+(\.[0-9]+)?|[a-f0-9\-]{8,36})$", re.IGNORECASE)
    for element in soup.find_all(True):
        if element.name == "img":
            if element.get("alt"):
                element["alt"] = "[image]"
            if element.get("src"):
                element["src"] = "[image]"
            if element.get("title"):
                element["title"] = "[title]"
        if element.name == "a" and element.get("href"):
            element["href"] = anonymize_url(element["href"])
        if element.name == "form" and element.get("action"):
            element["action"] = anonymize_url(element["action"])
        if element.name != "img" and element.get("title"):
            element["title"] = "[title]"

        for attr_name in list(element.attrs.keys()):
            if not attr_name.startswith("data-") or attr_name == "data-testid":
                continue
            val = element[attr_name]
            if not isinstance(val, str) or safe_pattern.match(val):
                continue
            if any(x in attr_name for x in ("url", "href", "link")):
                element[attr_name] = "[url]"
            elif val.startswith("http"):
                element[attr_name] = "[link]"
            elif len(val) > 30:
                element[attr_name] = f"[data:length:{len(val)}]"

    return str(soup)


_INITIAL = """You are analyzing full HTML pages from {site} to identify elements that contain UNTRUSTED, THIRD-PARTY / USER-GENERATED CONTENT.

**NOTE: This is a SANITIZED version of the HTML - all text content has been replaced with [text:length:N] placeholders. You only see the HTML structure (tags, classes, IDs, attributes, roles). Use this structural information to identify where third-party content would appear.**

**CORE PRINCIPLE - where did the text come from?**

For every region you consider marking, ask: *"Did the site operator produce this text, or did some other party type it in / upload it?"*

- **Trusted (do NOT mark)** - text the operator wrote or the platform computed. Navigation, branding, headers, footers, section titles, form controls, button labels, tab names, and any fixed vocabulary that is the same for every visitor. Also values the platform derives rather than accepts: counts, scores, aggregates, prices, timestamps, sizes, distances, and identifiers the platform assigns.
- **Untrusted (DO mark)** - text or media that some party other than the operator supplied. If a non-operator could put ANYTHING they wanted there, it is untrusted.

**IMPORTANT: ALL non-operator parties are untrusted, including the owner of the page being viewed.**
Every account, author, seller, uploader, or contributor is a potential attacker. There is no "trusted" user: a display name, a title, a description, a file name, or an uploaded image is untrusted no matter whose it is or how prominently the page presents it.

**Why is this important?**
Third-party content can contain PROMPT INJECTIONS - instructions hidden in text that could manipulate an AI agent browsing the page.

**Full HTML from {num_pages} pages:**
{html_content_str}

**Your task:**
Return a JSON array of CSS SELECTORS that precisely target untrusted elements. For each selector, provide:
- css_selector: A precise CSS selector string
- tag_name: A short descriptive name (e.g. "comment-body", "item-title", "author-name")
- description: One sentence explaining what third-party content it captures
- confidence: "high" (definitely third-party sourced) or "medium" (likely third-party sourced)

**CRITICAL Guidelines:**

1. BE GRANULAR - target the untrusted elements, not their containers. If a container holds a mix of trusted UI (buttons, counts, controls, navigation) and untrusted text, do NOT select the container; select each untrusted child individually. A selector should only match elements whose ENTIRE visible content is untrusted. If a selector would match an element whose descendants include buttons, action links, or numeric counters, it is too broad.

2. PREFER STABLE ANCHORS. Semantic attributes are the most reliable handle: a test id (`[data-testid='...']`), an `aria-label`, a `role`, a stable id, or a semantic tag chain. Use them in preference to CSS classes wherever one is available.

3. DO NOT EMIT BARE SELECTORS ON OBFUSCATED CLASSES. Build-generated class names (short mixed alphanumeric tokens) change between deployments AND are reused across unrelated sections of the same page, so a selector whose only handle is such a class - even chained, like `.aaaa.bbbb` - will match in many sections at once and is an error. Either scope it inside a semantic anchor (`[data-testid='x'] .hashedclass`) or skip that region. Human-readable, meaningful class names are fine to use directly.

4. Do NOT mark platform-computed values as untrusted. Vote scores, karma, ratings, review counts, item counts, file sizes, prices, dates and relative timestamps are computed or formatted by the platform, not typed by a user, even when they sit inside a block of user content.

5. Navigation is TRUSTED even when it displays user-created names. A name that a user chose is untrusted where the page renders it as content, and trusted where the platform renders it as a navigation affordance (a top bar, a breadcrumb, a sidebar menu, a dropdown of destinations).

6. Be conservative on UI chrome. Buttons, tabs, chips, badges, form inputs, dropdown toggles, progress bars, pagination and date controls are platform UI even when they sit inside a region of user content. Skip them.

7. Only include high or medium confidence items.

**What to AVOID marking as untrusted:**
- Site branding, navigation bars, headers, footers
- Search forms, filter controls, sort dropdowns, pagination, tabs, breadcrumbs
- Section headings and other fixed vocabulary the operator wrote
- Action buttons and their labels
- Cookie/consent banners and other operator notices
- Login/signup prompts
- Platform-aggregated numbers, scores, counts and timestamps
- Icons, spacers, and purely presentational elements

**Output format - ONLY a valid JSON array, no other text:**

[
  {{
    "css_selector": ".some-precise-selector",
    "tag_name": "comment-body",
    "description": "Text a visitor wrote",
    "confidence": "high"
  }}
]"""

_FOLLOW_UP = """These are the CSS selectors you have proposed so far ({num_total} total):

{proposed_list}

Do you think any OTHER CSS selectors should be added to this list?

Please review the HTML again and consider whether there is any additional content on {site} that can be created or edited by a party other than the site operator and that we might have missed. Think through the categories:
- free text a non-operator wrote (titles, descriptions, bodies, comments, replies, bios, messages)
- names a non-operator chose (display names, account names, item names, file names, tags, labels)
- media a non-operator uploaded (photos, avatars, attachments, thumbnails)
- content that LOOKS platform-generated but embeds non-operator text (headings that interpolate a name, breadcrumb leaves, page titles, link text derived from a user-supplied value)

Return a JSON array with any additional selectors (or an empty array [] if complete):

[
  {{
    "css_selector": ".additional-selector",
    "tag_name": "description",
    "description": "What this contains",
    "confidence": "high"
  }}
]

CRITICAL REMINDER: Do NOT add container-level selectors. If a selector would match an element whose children include trusted UI, it is too broad - select the specific leaf elements that display the untrusted text instead.

ALSO: Do NOT add selectors for navigation, filters, search controls, prices, ratings, counts or scores.

If you think we have all the necessary selectors, return an empty array: []"""


def for_host(host: str, label: str = None, clean_max_size: int = None):
    """A site profile for one host, in the shape UCM's pipeline consumes.

    Returned as a namespace rather than written to disk under the UCM checkout's sites/ directory
    on purpose: their loader is only reached by their CLI, and every function we call
    (analyze_with_llm, analyze_selectors) takes the profile as an argument. So the UCM working
    tree stays untouched and `git status` there stays clean, which is what makes the claim that
    the method ran unaltered checkable.
    """
    site = label or host
    return types.SimpleNamespace(
        SITE_LABEL=site,
        HOST=host,
        PAGES={},
        NUM_LLM_TURNS=NUM_LLM_TURNS,
        CLEAN_HTML_MAX_SIZE=clean_max_size or CLEAN_HTML_MAX_SIZE,
        PAGE_READY=PAGE_READY,
        clean_html_for_llm=clean_html_for_llm,
        sanitize_html_content=sanitize_html_content,
        INITIAL_PROMPT_TEMPLATE=_INITIAL.replace("{site}", site),
        FOLLOW_UP_PROMPT_TEMPLATE=_FOLLOW_UP.replace("{site}", site),
    )
