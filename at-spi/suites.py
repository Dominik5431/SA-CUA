"""
suites.py -- the self-hosted half of the at-spi corpus: the four Docker eval platforms.

Sibling to sites.py (the public web) and apps.py (native applications). Same job, same corpus
design: ~8 pages per domain so k-of-N cross-page corroboration and an in-domain generalization
split have something to work with. What differs is where the pages come from.

WHY THIS FILE HAS TO EXIST AT ALL.
`collect_atspi_trees.py --platform X` takes its targets from the suite's own eval configs, on the
sound reasoning that those configs already carry the platform's whole reachability recipe (hosts
entry, Chrome launch, socat forward, login) and that hand-written URLs would drift from it. That
part still holds and is unchanged. What the configs cannot supply is VARIETY -- measured over
`evaluation_examples/examples/`:

    owncloud     608 configs  ->  1 distinct open-tab URL   (the :8092 root)
    rocketchat   576 configs  ->  1 distinct open-tab URL   (the :3000 root)
    reddit       648 configs  ->  4 distinct open-tab URLs  (3 posts + wasp's /f/allentown/137403)
    gitlab        96 configs  ->  2 usable (4 more carry an unresolved __SEED_ISSUE_IID__)

An adversarial benchmark needs one page per attack, not one page per UI surface, so this is the
configs being fit for their own purpose rather than a defect in them. But a corpus built from
"the page every task happens to open" is the same shape sites.py rejected: N=1 supports neither
corroboration nor a held-out split, and on owncloud/rocketchat it would mean the whole platform
is represented by its landing screen -- the least informative state it has.

So the eval configs keep supplying the RECIPE, and the lists below supply the TARGETS.

EVERY PATH HERE WAS VERIFIED AGAINST THE RUNNING SLOT-1 CONTAINERS, not guessed from product
docs: reddit and gitlab by logging in and fetching each path (status + <title>), owncloud through
WebDAV PROPFIND for the real folder names, rocketchat through /api/v1/channels.list for the real
channel names. A path that 404s is a well-formed capture of the wrong thing, which is the exact
failure mode detect_bot_walls.py and apps.py's `expect_text` both exist to catch.

WHAT THE SELECTION SPANS is UGC shape, the same criterion sites.py uses -- one page per shape
beats many pages sharing one:

    threaded comments ......... reddit post pages
    aggregated title lists .... reddit front page, forum listing, /forums, /comments
    author identity ........... reddit user profile
    chat messages ............. rocketchat channels (several, with different traffic shapes)
    file and folder names ..... owncloud file listings, at three depths
    share and trash views ..... owncloud sharingin / trashbin -- names arriving from OTHER users
    issue threads ............. gitlab issues, with comment chains
    repo browsing ............. gitlab tree, commits, merge requests
    project directories ....... gitlab /explore/projects -- user-minted project and group names

PLACEHOLDERS. Paths are written as suffixes and joined onto the same `TO_BE_REPLACED_URL:<port>`
placeholder the eval configs use, so host and port resolution goes through prepare_injection's
substitution exactly once. Never hard-code a host here: the instance slot decides it, and slots 2
and 3 answer on different ports (see the port-less proxy work).

A NOTE ON STATE, AND ON NOT WRITING TO IT. These are live, mutable services that eval runs write
to, and nothing reverts them between runs. Two records were found dirty when this file was
written: submission 18013's title read "BLAH BLAH BLAH" (from adv/WebArena_Reddit/
reddit_population_test.json, a test fixture someone had run), and comment 374639 still held an
attack payload -- `rm -f /home/user/file.txt` framed as a required step -- from an earlier
adversarial run. Both were restored via preprocess.py's benign baseline before capturing.

A CAPTURE RUN MUST NOT ADD TO THAT. `_suite_targets()` drops `adversary_config` for exactly this
reason; see the comment there for what happens when it does not.
"""
import urllib.parse

SUITE_PORTS = {
    "reddit": 9999,
    "owncloud": 8092,
    "rocketchat": 3000,
    "gitlab": 8023,
}

SUITE_CONFIG_DIRS = {
    "reddit": ("reddit", "reddit_fixed", "reddit_extended", "reddit_extended_fixed", "wasp_reddit"),
    "owncloud": ("owncloud", "owncloud_fixed", "owncloud_extended", "owncloud_extended_fixed"),
    "rocketchat": ("rocketchat", "rocketchat_fixed"),
    "gitlab": ("wasp_gitlab",),
    "ucm": ("ucm_utility", "ucm_value_attack"),
}


REDDIT_PAGES = [
    ("forum_post_thread", "/f/coolgithubprojects/103666/i-made-a-browser-extension-that-displays-ai-generated"),
    ("forum_post_thread", "/f/coolgithubprojects/103669/adding-colored-output-in-python-terminal"),
    ("forum_post_thread", "/f/coolgithubprojects/18013/how-do-you-use-tmux-in-your-daily-workflow"),
    ("forum_listing", "/f/coolgithubprojects"),
    ("forum_listing", "/f/technology"),
    ("forum_front", "/all/hot"),
    ("forum_directory", "/forums"),
    ("forum_comments", "/comments"),
    ("forum_profile", "/user/AineeJames"),
]


OWNCLOUD_PAGES = [
    ("files_pdf_viewer", "/index.php/apps/files/?dir=/"),
    ("files_pdf_viewer", "/index.php/apps/files/?dir=/Documents"),
    ("files_folder", "/index.php/apps/files/?dir=/Documents/Financials"),
    ("files_folder", "/index.php/apps/files/?dir=/Documents/Data Analysis"),
    ("files_folder", "/index.php/apps/files/?dir=/Documents/Human Resources Team"),
    ("files_folder", "/index.php/apps/files/?dir=/Documents/Admin"),
    ("files_folder", "/index.php/apps/files/?dir=/Documents/Research"),
    ("files_folder", "/index.php/apps/files/?dir=/Photos"),
    ("files_folder", "/index.php/apps/files/?dir=/Templates"),
    ("settings", "/index.php/settings/personal"),
]


ROCKETCHAT_PAGES = [
    ("chat_channel", "/channel/kudos"),
    ("chat_channel", "/channel/help-desk"),
    ("chat_channel", "/channel/tech-talk"),
    ("chat_channel", "/channel/random"),
    ("chat_channel", "/channel/hr-announcements"),
    ("chat_channel", "/channel/project-ai"),
    ("chat_channel", "/channel/project-webcrawler"),
    ("chat_directory", "/directory/channels"),
    ("chat_home", "/home"),
]

GITLAB_PAGES = [
    ("forge_issue", "/byteblaze/dotfiles/-/issues/1"),
    ("forge_issue", "/a11yproject/a11yproject.com/-/issues/1534"),
    ("forge_issue_list", "/a11yproject/a11yproject.com/-/issues"),
    ("forge_issue_list", "/byteblaze/a11y-webring.club/-/issues"),
    ("forge_repo", "/byteblaze/dotfiles"),
    ("forge_repo", "/byteblaze/empathy-prompts"),
    ("forge_commits", "/byteblaze/dotfiles/-/commits/main"),
    ("forge_merge_requests", "/byteblaze/empathy-prompts/-/merge_requests"),
    ("forge_directory", "/explore/projects"),
]


UCM_PAGES = [
    ("ledger_table", "http://banking.com/?reveal_all=true&capture=banking_dashboard"),
    ("record_detail", "http://banking.com/?reveal_all=true&capture=banking_txn_details"),
    ("transfer_form", "http://banking.com/?reveal_all=true&capture=banking_send_money"),
    ("rule_form", "http://banking.com/?reveal_all=true&capture=banking_set_alert"),
    ("schedule_grid", "http://calendar.com/?reveal_all=true&capture=calendar_week"),
    ("event_form", "http://calendar.com/?reveal_all=true&capture=calendar_new_event"),
    ("call_overlay", "http://calendar.com/?reveal_all=true&capture=calendar_video_call"),
    ("message_list", "http://webmail.com/?reveal_all=true&capture=webmail_inbox"),
    ("message_body", "http://webmail.com/?reveal_all=true&capture=webmail_message"),
    ("compose_form", "http://webmail.com/?reveal_all=true&capture=webmail_reply"),
    ("message_list", "http://webmail.com/?reveal_all=true&capture=webmail_starred"),
    ("thread_list", "http://forum.com/?reveal_all=true&capture=forum_posts"),
    ("thread_detail", "http://forum.com/?reveal_all=true&capture=forum_post_detail"),
    ("compose_form", "http://forum.com/?reveal_all=true&capture=forum_new_post"),
    ("derived_report", "http://forum.com/?reveal_all=true&capture=forum_analytics"),
    ("listing_grid", "http://jobs.com/?reveal_all=true&capture=jobs_listings"),
    ("listing_detail", "http://jobs.com/?reveal_all=true&capture=jobs_job_detail"),
    ("application_form", "http://jobs.com/?reveal_all=true&capture=jobs_apply"),
    ("catalog_grid", "http://restaurant.com/?reveal_all=true&capture=restaurant_menu"),
    ("item_options", "http://restaurant.com/?reveal_all=true&capture=restaurant_customize"),
    ("order_summary", "http://restaurant.com/?reveal_all=true&capture=restaurant_cart"),
    ("booking_form", "http://restaurant.com/?reveal_all=true&capture=restaurant_reservations"),
    ("ticket_list", "http://support.com/?reveal_all=true&capture=support_tickets"),
    ("ticket_thread", "http://support.com/?reveal_all=true&capture=support_ticket_detail"),
    ("ticket_form", "http://support.com/?reveal_all=true&capture=support_new_ticket"),
    ("search_form", "http://travel.com/?reveal_all=true&capture=travel_search"),
    ("result_list", "http://travel.com/?reveal_all=true&capture=travel_flights"),
    ("result_list", "http://travel.com/?reveal_all=true&capture=travel_hotels"),
    ("order_summary", "http://travel.com/?reveal_all=true&capture=travel_review"),
    ("article_index", "http://wiki.com/?reveal_all=true&capture=wiki_index"),
    ("article_body", "http://wiki.com/?reveal_all=true&capture=wiki_article"),
    ("revision_log", "http://wiki.com/?reveal_all=true&capture=wiki_history"),
    ("catalog_grid", "http://shop.com/?reveal_all=true&capture=shop_catalog"),
    ("catalog_grid", "http://shop.com/?reveal_all=true&capture=shop_filtered"),
]


PAGE_ACTIONS = {
    "owncloud": {
        "/index.php/apps/files/?dir=/": [
            {"wait_for": "#fileList tr[data-file='shared_docker_entry.pdf']"},
            {"eval": "(() => { const fl = OCA.Files.App.fileList; fl.fileActions.triggerAction("
                     "'view', fl.getModelForFile('shared_docker_entry.pdf'), fl); })()"},
            {"wait_for": "iframe"},
            {"sleep": 12},
        ],
        "/index.php/apps/files/?dir=/Documents": [
            {"wait_for": "#fileList tr[data-file='Nextcloud flyer.pdf']"},
            {"eval": "(() => { const fl = OCA.Files.App.fileList; fl.fileActions.triggerAction("
                     "'view', fl.getModelForFile('Nextcloud flyer.pdf'), fl); })()"},
            {"wait_for": "iframe"},
            {"sleep": 12},
        ],
    },
    "ucm": {
        "/?reveal_all=true&capture=banking_txn_details": [{"click": "button:has-text('Details') >> nth=0"}, {"wait_for": "#details-modal"}],
        "/?reveal_all=true&capture=banking_send_money": [{"click": "button:has-text('Send Money')"}],
        "/?reveal_all=true&capture=banking_set_alert": [{"click": "button:has-text('Set Alert')"}],
        "/?reveal_all=true&capture=calendar_new_event": [{"click": "button:has-text('New Event')"}],
        "/?reveal_all=true&capture=calendar_video_call": [{"click": "button:has-text('Start Call') >> nth=0"}],
        "/?reveal_all=true&capture=webmail_message": [{"click": "button:has-text('Open') >> nth=0"}],
        "/?reveal_all=true&capture=webmail_reply": [{"click": "button:has-text('Open') >> nth=0"}, {"click": "#reply-btn"}],
        "/?reveal_all=true&capture=webmail_starred": [{"click": "button:has-text('Starred')"}],
        "/?reveal_all=true&capture=forum_post_detail": [{"click": "button:has-text('Open') >> nth=0"}],
        "/?reveal_all=true&capture=forum_new_post": [{"click": "button:has-text('New Post')"}],
        "/?reveal_all=true&capture=forum_analytics": [{"click": "button:has-text('View Analytics')"}],
        "/?reveal_all=true&capture=jobs_job_detail": [{"click": "button:has-text('View Details') >> nth=0"}],
        "/?reveal_all=true&capture=jobs_apply": [{"click": "button:has-text('View Details') >> nth=0"}, {"click": "button:has-text('Apply Now')"}],
        "/?reveal_all=true&capture=restaurant_customize": [{"click": "button:has-text('Add to Order') >> nth=0"}],
        "/?reveal_all=true&capture=restaurant_cart": [{"eval": "window.alert = function(){}"}, {"click": "button:has-text('Add to Order') >> nth=0"}, {"click": "button:has-text('Add to Cart')"}, {"click": "button:has-text('Cart')"}],
        "/?reveal_all=true&capture=restaurant_reservations": [{"click": "button:has-text('Reservations')"}],
        "/?reveal_all=true&capture=support_ticket_detail": [{"click": "button:has-text('View Details') >> nth=0"}],
        "/?reveal_all=true&capture=support_new_ticket": [{"click": "button:has-text('New Ticket')"}],
        "/?reveal_all=true&capture=travel_flights": [{"click": "button:has-text('Search Flights')"}],
        "/?reveal_all=true&capture=travel_hotels": [{"click": "button:has-text('Search Flights')"}, {"click": "button:has-text('Select Flight') >> nth=0"}],
        "/?reveal_all=true&capture=travel_review": [{"click": "button:has-text('Search Flights')"}, {"click": "button:has-text('Select Flight') >> nth=0"}, {"click": "button:has-text('Select Hotel') >> nth=0"}],
        "/?reveal_all=true&capture=wiki_article": [{"click": "text=Sample Collection Protocol"}],
        "/?reveal_all=true&capture=wiki_history": [{"click": "text=Sample Collection Protocol"}, {"click": "button:has-text('History')"}],
        "/?reveal_all=true&capture=shop_filtered": [{"click": "button[data-filter='size'][data-value='S']"}, {"click": "button[data-filter='color'][data-value='blue']"}],
    },
}


def page_actions(platform: str, resolved_url: str) -> list:
    """The actions for whichever entry this resolved URL came from, or [].

    Matched on the path+query suffix rather than the whole URL, because by the time the collector
    asks, the placeholder has become a real host AND ownCloud may have appended its own query
    parameters (?dir=/ becomes ?dir=/&fileid=5).
    """
    table = PAGE_ACTIONS.get(platform) or {}
    got_path, _, got_query = urllib.parse.urlsplit(resolved_url).geturl().partition("?")
    got = dict(urllib.parse.parse_qsl(got_query, keep_blank_values=True))
    best, best_len = [], -1
    for path, actions in table.items():
        stem, _, query = path.partition("?")
        if not got_path.endswith(stem):
            continue
        want = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
        if any(got.get(k) != v for k, v in want.items()):
            continue
        if len(path) > best_len:
            best, best_len = actions, len(path)
    return best


def view_of(platform: str, resolved_url: str) -> str | None:
    """The SUITE_PAGES view category this resolved URL came from, or None.

    Same path+query matching as page_actions(), and for the same reason: by the time anyone asks,
    the placeholder is a real host and ownCloud has appended parameters of its own. The path is
    matched as a SUFFIX (a suite entry is host-relative), the query by parsed key, and the longest
    matching entry wins -- `?dir=/` is a legal prefix of nothing but is a shorter entry than
    `?dir=/Documents`, and a substring test would have given the root's view to every folder.

    The view is the one thing a capture knows about itself that its URL does not say, and it has
    to reach the manifest or it is lost. `/index.php/apps/files/?dir=/` is the address of the root
    file listing AND of the PDF viewer opened from it -- the viewer is a modal and changes no URL
    -- so `files_pdf_viewer` is the only record that capture 420 is not a listing.
    rule_scope.OVERLAY_VIEWS reads it back; without it the viewer's rules went to every ownCloud
    page on the platform.
    """
    split = urllib.parse.urlsplit(resolved_url if "://" in resolved_url else f"http://{resolved_url}")
    got_path = split.path.rstrip("/")
    got = dict(urllib.parse.parse_qsl(split.query, keep_blank_values=True))
    best, best_len = None, -1
    for category, path in SUITE_PAGES.get(platform) or []:
        entry = urllib.parse.urlsplit(path if "://" in path else f"http://x{path}")
        stem = entry.path.rstrip("/")
        if got_path != stem and not got_path.endswith(stem):
            continue
        want = dict(urllib.parse.parse_qsl(entry.query, keep_blank_values=True))
        if any(got.get(k) != v for k, v in want.items()):
            continue
        if len(path) > best_len:
            best, best_len = category, len(path)
    return best


SUITE_PAGES = {
    "reddit": REDDIT_PAGES,
    "owncloud": OWNCLOUD_PAGES,
    "rocketchat": ROCKETCHAT_PAGES,
    "gitlab": GITLAB_PAGES,
    "ucm": UCM_PAGES,
}

SUITE_INDEX_BASE = {
    "reddit": 400,
    "owncloud": 420,
    "rocketchat": 440,
    "gitlab": 460,
    "ucm": 500,
}


def suite_urls(platform: str, limit: "int | None" = None) -> list:
    """[(category, placeholder_url)] for one platform, in the shape sites.py's SITES entries use.

    The URL carries the TO_BE_REPLACED_URL:<port> placeholder rather than a resolved host, so it
    goes through prepare_injection's substitution exactly like a config's own open-tab URL.
    """
    out = []
    for category, path in SUITE_PAGES[platform]:
        if "://" in path:
            out.append((category, path))
        else:
            out.append((category, f"TO_BE_REPLACED_URL:{SUITE_PORTS[platform]}{path}"))
    return out[:limit] if limit else out
