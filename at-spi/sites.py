"""
sites.py -- the web half of the at-spi corpus: a small number of domains captured *deeply*.

Rewritten for the reworked corpus. The previous list was ~100 curated deep links plus every
trusted_domains.py entry as a bare homepage, which produced 230 domains at a median of one page
each. That shape cannot support the two things the corpus now has to measure:

  * **k-of-N cross-page corroboration** -- keep a proposed selector rule only if it was proposed
    independently on at least k pages of the same domain. Needs N independent pages per domain.
    A page and its own consent-banner variant do NOT count as two: same DOM plus an overlay, so
    the same selectors get proposed almost by construction and the gate passes without evidence.
  * **in-domain generalization** -- propose on pages 1..k, score on held-out pages k+1..N. Needs
    N >= 6 to leave a real test split; 8 is the working target.

So: ~8 pages per domain, ~13 domains, instead of ~1 page per domain across 230. Sites with only
one page are not a smaller version of this experiment, they are a different (FP-only) one -- a
page with no untrusted rows cannot produce a false negative, it can only measure over-masking.
Those live in the holdout blocks at the bottom and are never proposal input.

WHY THESE DOMAINS -- the selection spans *UGC shapes*, not sites. A rule has to generalize over
the shape of the untrusted region, so one domain per shape beats many domains sharing one shape:

    threaded comments ....... reddit      (docker suite, --platform reddit -- not here)
    chat messages ........... rocketchat  (docker suite, --platform rocketchat -- not here)
    email bodies ............ thunderbird (apps.py)
    file listings ........... owncloud    (docker suite) + nautilus (apps.py)
    repo / MR / CI .......... gitlab      (docker suite) + github.com (here)
    article prose + talk .... en.wikipedia.org
    Q&A posts + tag lists ... stackoverflow.com
    product reviews ......... www.amazon.com
    property reviews, rates . www.booking.com
    terminal output ......... terminal    (apps.py)
    NOTHING untrusted ....... www.nytimes.com  <- the fully-trusted control

The docker-suite platforms (reddit, gitlab, rocketchat, owncloud) are NOT in this list: they are
captured through `collect_atspi_trees.py --platform <name> --platform_pages N`, which replays the
suite's own reachability recipe (hosts entry, Chrome launch, socat forward, login). Native apps
come from apps.py. This file is only the public web.

DEEP LINKS CARRY STATE, NOT JUST IDENTITY. A homepage is the least informative page a domain has
-- mostly nav chrome, little content, and none of the markup a mid-task agent actually meets. The
entries below deliberately sit *inside* a flow with state already applied: dates and guest counts
already chosen, a facet filter already ticked, a sort order already changed, page 2 rather than
page 1, a review tab already open. Those states are not cosmetic; they add real structure --
applied-filter chips, "N of M results" summaries, room/rate tables that only render once dates are
known, sort dropdowns showing a non-default value -- and that structure is exactly where a selector
rule proposed on a page-1 default view stops generalizing. One bare homepage per domain is kept on
purpose as the empty-state contrast.

CHECK-IN DATES ARE COMPUTED, NOT PINNED. Booking.com silently redirects a past check-in date back
to the search form, which yields a well-formed capture of the wrong page -- worse than a failed
one, because nothing downstream flags it. So travel dates are derived from the run date (see
_DAYS_AHEAD below). The manifest records the exact URL used, so a capture stays reproducible as a
record even though the list is not byte-stable across months. Pin _DAYS_AHEAD to a fixed date only
if you need two runs to share URLs exactly.

URL LIVENESS was checked against the live web when this list was written. Everything resolved to
the page it claims; four entries were replaced because they did not:

    amazon.com/dp/B08N5WRWNW ............ 404, ASIN is dead      -> B07FZ8S74R + B0BSHF7WHW
    amazon.com/product-reviews/<asin> ... redirects to sign-in   -> reviews render inline on /dp/
    booking.com/reviews/us/hotel/<name> . redirects to the hotel -> a price-facet search instead
    bbc.com/news/science_and_environment . JS shell, title "BBC"  -> /business + /culture

Stack Exchange (stackoverflow.com and every holdout_qa host), booking.com, amazon.com and ebay
answer a plain HTTP client with a Cloudflare interstitial or a soft 202, so their liveness could
only be confirmed structurally, not by title. Real Chrome in the VM captured all of them cleanly in
the previous corpus -- but not always: the old manifest has stackoverflow.com, etsy, walmart,
reuters and wsj flagged `bot_wall: true` on at least one run. Run detect_bot_walls.py after every
capture batch; a challenge page is a well-formed capture of the wrong thing and nothing else
downstream will notice it.

Each entry is (category, url). Hand-curated, not fetched from any ranking service. Categories are
only for `--category` filtering and analysis grouping; the corroboration unit is the *domain*.
"""



from __future__ import annotations

import datetime as _dt

APPEND_TRUSTED_DOMAIN_HOMEPAGES = False

_DAYS_AHEAD = 78
_NIGHTS = 3


def _stay() -> tuple[str, str]:
    today = _dt.date.today()
    checkin = today + _dt.timedelta(days=_DAYS_AHEAD)
    return checkin.isoformat(), (checkin + _dt.timedelta(days=_NIGHTS)).isoformat()


_CI, _CO = _stay()


SITE_PAGE_ACTIONS: dict[str, list] = {
    "https://webext.eu/": [
        {"click": "button:has-text(\"Let's create\")"},
        {"wait_for": "input[data-question='name']"},
    ],
}


def page_actions(url: str) -> list:
    """Actions for this exact SITES url, or []."""
    return SITE_PAGE_ACTIONS.get(url) or []


SITES: list[tuple[str, str]] = [
    ("code", "https://github.com/torvalds/linux"),
    ("code", "https://github.com/python/cpython/blob/main/README.rst"),
    ("code", "https://github.com/microsoft/vscode/issues?q=is%3Aissue+is%3Aopen+label%3Abug+sort%3Acomments-desc"),
    ("code", "https://github.com/microsoft/vscode/issues/1"),
    ("code", "https://github.com/pallets/flask/pulls?q=is%3Apr+is%3Aclosed+sort%3Aupdated-desc"),
    ("code", "https://github.com/torvalds/linux/commits/master"),
    ("code", "https://github.com/microsoft/vscode/releases"),
    ("code", "https://github.com/search?q=accessibility+tree&type=repositories"),

    ("reference", "https://en.wikipedia.org/wiki/Web_accessibility"),
    ("reference", "https://en.wikipedia.org/wiki/HTTP_cookie"),
    ("reference", "https://en.wikipedia.org/wiki/Transport_Layer_Security"),
    ("reference", "https://en.wikipedia.org/wiki/Talk:Web_accessibility"),
    ("reference", "https://en.wikipedia.org/w/index.php?title=Web_accessibility&action=history"),
    ("reference", "https://en.wikipedia.org/wiki/Special:RecentChanges?namespace=0&limit=50"),
    ("reference", "https://en.wikipedia.org/w/index.php?search=accessibility+tree&title=Special%3ASearch&fulltext=1"),
    ("reference", "https://en.wikipedia.org/wiki/Category:Web_accessibility"),

    ("qa", "https://stackoverflow.com/questions/11227809/why-is-processing-a-sorted-array-faster-than-processing-an-unsorted-array"),
    ("qa", "https://stackoverflow.com/questions/231767/what-does-the-yield-keyword-do"),
    ("qa", "https://stackoverflow.com/questions/2003505/how-do-i-delete-a-git-branch-locally-and-remotely"),
    ("qa", "https://stackoverflow.com/questions/tagged/python?tab=votes&page=2&pagesize=15"),
    ("qa", "https://stackoverflow.com/questions/tagged/accessibility"),
    ("qa", "https://stackoverflow.com/questions/477816/which-json-content-type-do-i-use"),
    ("qa", "https://stackoverflow.com/users/22656/jon-skeet"),
    ("qa", "https://stackoverflow.com/questions?tab=frequent&page=3"),

    ("booking", "https://www.booking.com/index.html"),
    ("booking", f"https://www.booking.com/searchresults.html?ss=New+York&checkin={_CI}&checkout={_CO}&group_adults=2&no_rooms=1&group_children=0"),
    ("booking", f"https://www.booking.com/searchresults.html?ss=Paris&checkin={_CI}&checkout={_CO}&group_adults=2&no_rooms=1&group_children=1&age=8&nflt=class%3D4%3Breview_score%3D80"),
    ("booking", f"https://www.booking.com/searchresults.html?ss=Berlin&checkin={_CI}&checkout={_CO}&group_adults=2&no_rooms=1&group_children=0&order=price&offset=25"),
    ("booking", f"https://www.booking.com/hotel/us/the-plaza.html?checkin={_CI}&checkout={_CO}&group_adults=2&no_rooms=1"),
    ("booking", f"https://www.booking.com/hotel/gb/the-savoy.html?checkin={_CI}&checkout={_CO}&group_adults=2&no_rooms=1"),
    ("booking", f"https://www.booking.com/searchresults.html?ss=Rome&checkin={_CI}&checkout={_CO}&group_adults=2&no_rooms=1&group_children=0&nflt=price%3DUSD-100-200-1"),
    ("booking", f"https://www.booking.com/searchresults.html?ss=Tokyo&checkin={_CI}&checkout={_CO}&group_adults=1&no_rooms=1&group_children=0&nflt=ht_id%3D204"),

    ("shopping", "https://www.amazon.com/s?k=laptop"),
    ("shopping", "https://www.amazon.com/s?k=wireless+headphones&s=review-rank"),
    ("shopping", "https://www.amazon.com/s?k=desk+lamp&rh=p_72%3A1248879011"),
    ("shopping", "https://www.amazon.com/s?k=mechanical+keyboard&page=2"),
    ("shopping", "https://www.amazon.com/dp/B07FZ8S74R"),
    ("shopping", "https://www.amazon.com/dp/B0BSHF7WHW"),

    ("news", "https://www.nytimes.com/section/world"),
    ("news", "https://www.nytimes.com/section/technology"),
    ("news", "https://www.nytimes.com/section/business"),
    ("news", "https://www.nytimes.com/section/arts"),
    ("news", "https://www.nytimes.com/section/science"),
    ("news", "https://www.nytimes.com/section/us"),

    ("holdout_qa", "https://superuser.com/questions/154150/how-to-clear-bash-history-completely"),
    ("holdout_qa", "https://serverfault.com/questions/2733/is-there-a-command-to-list-all-open-ports-and-their-owning-processes"),
    ("holdout_qa", "https://askubuntu.com/questions/89826/how-to-clean-up-and-free-up-disk-space-on-ubuntu"),
    ("holdout_qa", "https://unix.stackexchange.com/questions/4420/is-a-hard-link-to-a-file-the-same-as-the-original-file"),
    ("holdout_qa", "https://english.stackexchange.com/questions/121737/whats-the-difference-between-affect-and-effect"),
    ("holdout_reference", "https://simple.wikipedia.org/wiki/Computer"),
    ("holdout_reference", "https://simple.wikipedia.org/wiki/Talk:Computer"),

    ("holdout_docs", "https://docs.python.org/3/tutorial/index.html"),
    ("holdout_docs", "https://docs.python.org/3/library/functions.html"),
    ("holdout_docs", "https://www.rfc-editor.org/rfc/rfc2616"),
    ("holdout_docs", "https://www.rfc-editor.org/rfc/rfc8446"),

    ("osworld_chrome_ui", "chrome://settings/"),
    ("osworld_chrome_ui", "chrome://settings/search"),
    ("osworld_chrome_ui", "chrome://settings/searchEngines"),
    ("osworld_chrome_ui", "chrome://settings/clearBrowserData"),
    ("osworld_chrome_ui", "chrome://settings/content/all"),
    ("osworld_chrome_ui", "chrome://extensions/"),

    ("osworld_papers", "https://arxiv.org/list/cs.CL/recent"),
    ("osworld_papers", "https://arxiv.org/abs/2310.06825"),

    ("osworld_forum", "https://www.flightaware.com/"),
    ("osworld_forum", "https://discussions.flightaware.com/categories"),
    ("osworld_forum", "https://discussions.flightaware.com/c/aviation/general-aviation/17?order=posts"),
    ("osworld_forum", "https://discussions.flightaware.com/t/tracking-fellow-members/3650"),

    ("osworld_tool", "https://webext.eu/"),

    ("osworld_game", "https://www.mathsisfun.com/games/2048.html"),

    ("osworld_scholar", "https://scholar.google.com/citations?user=WLN3QrAAAAAJ"),
    ("osworld_scholar", "https://scholar.google.com/scholar?q=%22Yann+LeCun%22"),

    ("osworld_maps", "https://www.google.com/maps/search/restaurants+in+Hong+Kong"),
    ("osworld_maps", "https://www.google.com/maps/place/Tim+Ho+Wan"),
]
