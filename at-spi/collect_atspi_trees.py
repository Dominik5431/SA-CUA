#!/usr/bin/env python3
"""
collect_atspi_trees.py -- sample real AT-SPI2 accessibility-tree XML from a list of trusted
websites, captured inside this repo's actual VM/browser stack.

The site list (ALL_SITES, built by _build_all_sites()) is sites.py's ~106 hand-curated
(category, deep-link-url) entries -- kept at their existing indices, unchanged, since capture
filenames/manifest rows for those already-captured sites must never be renumbered -- followed by
every domain in trusted_domains.py not already covered by one of those deep links, appended as a
plain https://{domain}/ homepage visit (no curated deep link exists for these yet). This is what
"capture the domains not yet in the corpus" means in practice: rerun this script with no
--indices filter and only the new, uncaptured indices actually produce new output (existing
sites' xml/screenshot files are only touched if you explicitly re-select their indices).

Boots a DesktopEnv exactly the way run.py does for a normal evaluation run (same constructor
shape: provider_name/screen_size/headless/os_type/require_a11y_tree/snapshot_name), but instead
of running a task/agent loop, drives the in-VM Chrome (which already runs with remote debugging
enabled) to each configured URL in turn via a single reused Playwright page/CDP connection, and
saves each page's raw AT-SPI XML -- exactly obs["accessibility_tree"] as consumed by
mm_agents/accessibility_tree_wrap/coord_to_semantic.py -- plus a matching PNG screenshot, to disk.

Usage:
  python at-spi/collect_atspi_trees.py
  python at-spi/collect_atspi_trees.py --limit 3          # quick dry run
  python at-spi/collect_atspi_trees.py --provider_name docker --headless

Rerunning individual sites (e.g. to recapture one that failed or looked wrong), instead of the
whole corpus -- only the selected sites' xml/screenshot files are touched, and manifest.json is
merged (updates just those indices' rows, leaves every other row as-is):
  python at-spi/collect_atspi_trees.py --indices 0                    # ALL_SITES[0] only
  python at-spi/collect_atspi_trees.py --indices 0,5,12-14            # a few, by index
  python at-spi/collect_atspi_trees.py --indices 106-230              # just the new trusted_domains.py sites
  python at-spi/collect_atspi_trees.py --domain_contains stackoverflow.com
  python at-spi/collect_atspi_trees.py --category qa
  python at-spi/collect_atspi_trees.py --indices 0 --list             # preview the selection, no VM boot
"""
import argparse
import collections
import datetime
import glob
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from desktop_env.desktop_env import DesktopEnv
from dom_capture import _deadline as _dom_deadline
from dom_capture import capture_dom, calibrate, write_dom_artifacts
import manifest_io
import suites
import sites as sites_mod
from sites import SITES, APPEND_TRUSTED_DOMAIN_HOMEPAGES
from trusted_domains import TRUSTED_DOMAINS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("collect_atspi_trees")

_CDP_CONNECT_ATTEMPTS = 15
_CDP_CONNECT_RETRY_SECONDS = 5

_CHROME_PROMO_FLAGS = [
    "--disable-features="
    "HighEfficiencyModeAvailable,BatterySaverModeAvailable,"
    "MemorySaverMultiStateMode,PerformanceControlsPerformanceSurvey,"
    "IPH_HighEfficiencyMode,UserEducationExperienceVersion2,"
    "TabHoverCardImages,TabHoverCardMemoryUsage,DiscardedTabTreatment",
]

_CHROME_FLAGS = [
    "google-chrome", "--remote-debugging-port=1337",
    "--user-data-dir=/tmp/chrome_user_data",
    "--disable-session-crashed-bubble", "--disable-save-password-bubble",
    "--password-store=basic", "--no-first-run", "--no-default-browser-check",
    "--disable-infobars", "--disable-component-update", "--disable-dev-shm-usage",
    "--disable-gpu", "--start-maximized",
] + _CHROME_PROMO_FLAGS

_CHROME_POLICY_JSON = json.dumps({
    "HighEfficiencyModeEnabled": False,
    "PromotionalTabsEnabled": False,
    "BrowserLabsEnabled": False,
    "MetricsReportingEnabled": False,
    "DefaultBrowserSettingEnabled": False,
    "ComponentUpdatesEnabled": False,
})

_CHROME_POLICY_SETUP = {"type": "execute", "parameters": {"command": ["sh", "-c",
    "echo 'password' | sudo -S sh -c "
    "'mkdir -p /etc/opt/chrome/policies/managed && "
    f"cat > /etc/opt/chrome/policies/managed/atspi-corpus.json <<\"EOF\"\n{_CHROME_POLICY_JSON}\nEOF'"
], "shell": False}}

_CHROME_LAUNCH_SETUP = [
    _CHROME_POLICY_SETUP,
    {"type": "launch", "parameters": {"command": list(_CHROME_FLAGS)}},
    {"type": "launch", "parameters": {"command": ["socat", "tcp-listen:9222,fork", "tcp:localhost:1337"]}},
]


def _sanitize_domain(url: str) -> str:
    """A filename-safe name for this capture -- the hostname, for an ordinary page.

    A BROWSER-INTERNAL PAGE NEEDS ITS PATH TOO. `chrome://settings/search` and
    `chrome://settings/searchEngines` are two different surfaces with two different templates, and
    the http-only strip above leaves `chrome:` in place, so taking the part before the first slash
    would name every one of them `chrome_`. Internal pages therefore keep host AND path and read
    `chrome_settings_searchengines`. The scope the rules are keyed on is unaffected -- that comes
    from rule_scope.scope_of_url (`settings`, `extensions`), not from this name.
    """
    if _is_internal_url(url):
        return re.sub(r"_+", "_",
                      re.sub(r"[^a-zA-Z0-9]+", "_", url)).strip("_").lower()
    host = re.sub(r"^https?://", "", url).split("/")[0]
    return re.sub(r"[^a-zA-Z0-9.-]", "_", host)


def _hostname_of(url: str) -> str:
    return re.sub(r"^https?://", "", url).split("/")[0]


def _build_all_sites() -> list[tuple[str, str]]:
    """sites.py's hand-curated (category, deep-link-url) entries at their list positions, which are
    the capture indices ({i:03d}_domain.xml) and so must not be renumbered within a corpus.

    Historically every trusted_domains.py domain not covered by a curated deep link was appended
    after them as a plain https://{domain}/ homepage visit. sites.py now owns that decision via
    APPEND_TRUSTED_DOMAIN_HOMEPAGES, and turns it off: the reworked corpus is a small number of
    domains captured deeply, and 210 one-page homepages cannot support either cross-page
    corroboration or an in-domain generalization split. Flip the flag in sites.py to get the old
    breadth sweep back -- the appended entries land at indices >= len(SITES), so nothing curated
    shifts either way."""
    if not APPEND_TRUSTED_DOMAIN_HOMEPAGES:
        return list(SITES)
    covered = {_hostname_of(url) for _category, url in SITES}
    extra = [
        (info["category"], f"https://{domain}/")
        for domain, info in TRUSTED_DOMAINS.items()
        if domain not in covered
    ]
    return list(SITES) + extra


ALL_SITES: list[tuple[str, str]] = _build_all_sites()


def _parse_indices(spec: str, max_index: int) -> list[int]:
    """Parses '0,5,12-14' -> [0, 5, 12, 13, 14], clamped to valid ALL_SITES indices."""
    result: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            result.update(range(int(start), int(end) + 1))
        else:
            result.add(int(part))
    return sorted(i for i in result if 0 <= i <= max_index)


def _select_sites(args) -> list[tuple[int, str, str]]:
    """Returns [(index, category, url), ...] -- the index is always the site's position in
    ALL_SITES (sites.py's curated deep links first, unchanged, then trusted_domains.py's
    not-yet-covered domains appended after), never renumbered by filtering, so output filenames
    ({i:03d}_domain.*) and manifest.json rows stay aligned with the rest of the corpus even when
    rerunning a subset."""
    if args.indices:
        selected = _parse_indices(args.indices, len(ALL_SITES) - 1)
    else:
        selected = list(range(len(ALL_SITES)))
        if args.domain_contains:
            needle = args.domain_contains.lower()
            selected = [i for i in selected if needle in ALL_SITES[i][1].lower()]
        if args.category:
            wanted = {c.strip() for c in args.category.split(",") if c.strip()}
            selected = [i for i in selected if ALL_SITES[i][0] in wanted]
        if args.limit:
            selected = selected[: args.limit]
    return [(i, ALL_SITES[i][0], ALL_SITES[i][1]) for i in selected]


_EVAL_EXAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evaluation_examples", "examples")

_DROP_STEP_TYPES = {"decouple", "adv_upload_file", "adv_upload_dir"}

_UNRESOLVED_RE = re.compile(r"__[A-Z_]+__")


def _suite_targets(platform: str, limit: int, extra: list[str] | None = None) -> tuple[dict, list[str]]:
    """-> (task_config template with every target URL in one chrome_open_tabs step, [raw urls]).

    The eval configs supply the RECIPE, suites.py supplies the TARGETS. The configs already carry
    the platform's whole reachability story (gateway host entry, Chrome launch, socat forward,
    credentials) and that is what a capture run is missing, so they are reused verbatim for it.
    What they cannot supply is variety: measured over evaluation_examples/examples/, owncloud's
    608 configs share ONE open-tab URL, rocketchat's 576 share one, reddit's 648 share four and
    gitlab's 96 yield two usable. An adversarial suite needs one page per attack, not one page per
    UI surface -- so that is the configs being fit for their own purpose, not a defect in them,
    and the corpus's own page list lives in suites.py. See its module docstring.

    CONFIGS ARE SELECTED BY THEIR DECLARED `platform`, NOT BY DIRECTORY NAME. The directories do
    not agree with the platforms they hold: `wasp_gitlab` declares platform "gitlab" and
    `wasp_reddit` declares "reddit". Globbing examples/<platform>/ therefore found NO gitlab
    configs at all -- `--platform gitlab` exited with "no eval configs under .../examples/gitlab"
    even though 96 of them sit one directory over -- and silently dropped wasp_reddit's extra
    target. The declared field is the only thing that actually says what a config is for.
    """
    paths = sorted(glob.glob(os.path.join(_EVAL_EXAMPLES, "*", "*.json")))
    if not paths:
        raise SystemExit(f"no eval configs under {_EVAL_EXAMPLES}")

    template = None
    urls: list[str] = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if cfg.get("platform") != platform:
            continue
        for step in cfg.get("config", []):
            if step.get("type") != "chrome_open_tabs":
                continue
            for url in step.get("parameters", {}).get("urls_to_open", []):
                if _UNRESOLVED_RE.search(url) or url in urls:
                    continue
                urls.append(url)
        if template is None:
            template = cfg
    if template is None:
        raise SystemExit(f"no eval config anywhere declares platform={platform!r}")
    curated = [u for _category, u in suites.suite_urls(platform)]
    urls = curated + [u for u in urls if u not in curated]
    urls = urls[:limit]
    for url in (extra or []):
        if url and url not in urls:
            urls.append(url)
    if not urls:
        raise SystemExit(f"no usable open-tab URLs for {platform}")

    steps = [s for s in template.get("config", []) if s.get("type") not in _DROP_STEP_TYPES]
    for step in steps:
        if step.get("type") == "chrome_open_tabs":
            step["parameters"] = dict(step.get("parameters", {}), urls_to_open=list(urls))
    task_config = dict(template, config=steps)
    task_config.pop("post_opentabs", None)
    task_config.pop("adversary_config", None)
    task_config.pop("adversary_evaluator", None)
    task_config["evaluator"] = {"func": "infeasible"}

    patched, seen_chrome = [], False
    for step in steps:
        cmd = (step.get("parameters") or {}).get("command")
        if step.get("type") == "launch" and isinstance(cmd, list) and cmd and "chrome" in cmd[0]:
            keep = [a for a in cmd[1:] if not a.startswith("--disable-features=")]
            step = dict(step, parameters=dict(step["parameters"],
                                               command=[cmd[0]] + keep + _CHROME_PROMO_FLAGS))
            seen_chrome = True
        patched.append(step)
    if seen_chrome:
        patched.insert(0, _CHROME_POLICY_SETUP)
    task_config["config"] = patched
    return task_config, urls


def _raw_path(url: str) -> str:
    """Path+query of a target, with the scheme/host (placeholder or real) removed."""
    stripped = re.sub(r"^\w+://[^/]*", "", url)
    stripped = re.sub(r"^TO_BE_REPLACED_URL(:\d+)?", "", stripped)
    return stripped or "/"


MOUSE_PARK = (1900, 1050)


def _is_internal_url(url: str) -> bool:
    """A browser-internal page (chrome://, chrome-extension://, devtools://, about:) rather than
    something fetched over the network. These are captured, and navigated to, differently."""
    return bool(url) and not re.match(r"^https?://", url)


def _navigate_internal(env, page, url: str, index: int, timeout_ms: int) -> None:
    """Drive the VM's Chrome to a `chrome://` page with the keyboard, in a tab of its own.

    WHY NOT page.goto(). Chromium refuses a renderer-initiated navigation to WebUI, which is what
    CDP's Page.navigate is, so Playwright cannot reach chrome://settings at all. The agent under
    evaluation reaches it the only way anyone can -- Ctrl+L, type, Enter -- so the capture does
    the same thing through the same pyautogui channel the agent uses, and the tree that comes back
    is the tree an episode would see.

    IN A NEW TAB, closed again by the caller. Navigating the Playwright-owned tab to WebUI swaps
    its renderer process, and whether the page object survives that is a Playwright/Chromium
    version detail this script should not depend on: every capture after the first chrome:// one
    would be hostage to it. A scratch tab keeps `page` pointing at an ordinary web page throughout.

    Raises if the omnibox never shows the target -- a silently mistyped URL would otherwise be
    saved as a well-formed capture of whatever page happened to be showing.
    """
    from mm_agents.accessibility_tree_wrap.coord_to_semantic import (
        get_active_tab_url,
    )
    page.bring_to_front()
    time.sleep(0.5)
    env.controller.execute_python_command(
        "import pyautogui, time; "
        "pyautogui.hotkey('ctrl', 't'); time.sleep(1.0); "
        f"pyautogui.write({url!r}, interval=0.01); time.sleep(0.3); "
        "pyautogui.press('delete'); time.sleep(0.2); "
        "pyautogui.press('enter')")

    want = url.rstrip("/")
    deadline = time.time() + timeout_ms / 1000.0
    shown = None
    while time.time() < deadline:
        time.sleep(0.5)
        shown = get_active_tab_url(env.controller.get_accessibility_tree() or "",
                                   include_internal=True)
        if shown and shown.rstrip("/") == want:
            logger.info("[%03d] internal page reached via keyboard: %s", index, shown)
            return
    raise RuntimeError(
        f"keyboard navigation to {url!r} did not take: omnibox shows {shown!r} after "
        f"{timeout_ms / 1000.0:.0f}s")


def _close_internal_tab(env, index: int) -> None:
    """Close the scratch tab _navigate_internal opened. Best effort: a tab left behind costs one
    line in the tab strip of later captures, which is browser chrome and outside every universe."""
    try:
        env.controller.execute_python_command("import pyautogui; pyautogui.hotkey('ctrl', 'w')")
        time.sleep(0.5)
    except Exception as e:
        logger.warning("[%03d] could not close the internal-page tab: %s", index, str(e)[:90])


def _park_mouse(env, index: int) -> None:
    """Move the pointer off the tab strip before the tree and screenshot are taken.

    CHROME SHOWS A TAB HOVER CARD whenever the pointer rests on a tab -- a floating panel with the
    page title and "Memory usage - 32.8 MB". It is not part of the page, it covers the top of the
    screenshot, and it lands in the accessibility tree as a named node, so it would have to be
    judged once per capture.

    Whether it appears depends on how many tabs are open, which is why it looked intermittent. The
    VM snapshot restores the pointer at roughly (1228, 66): with three tabs that is empty tab-strip
    space and nothing happens, with the eleven a suite capture opens it is directly over a tab. So
    the suite captures carried "... - Memory usage - N MB" strings and a three-page probe of the
    same code did not.

    Parking is done per capture rather than once at startup because tab activation can warp the
    pointer, and the cost is one pyautogui call.
    """
    try:
        env.controller.execute_python_command(
            f"import pyautogui; pyautogui.moveTo({MOUSE_PARK[0]}, {MOUSE_PARK[1]}, duration=0)")
    except Exception as e:
        logger.warning("[%03d] could not park the mouse: %s", index, str(e)[:90])


def _run_page_actions(page, actions: list, index: int) -> str:
    """Replay a suites.py entry's post-navigation actions, best effort.

    WHY THE WEB PATH NEEDS THIS AT ALL. A URL is usually enough state for a web capture -- that is
    sites.py's whole "deep links carry state" argument. OwnCloud is the exception the corpus ran
    into: the untrusted surface on that platform is a PDF's text layer rendered in the file
    viewer, the eval reaches it with four pyautogui clicks, and there is NO URL that gets there.
    Every download route (/remote.php/webdav/..., ajax/download.php) answers
    `Content-Disposition: attachment`, so Chrome downloads the file instead of navigating -- the
    tab stays put, the target matches nothing, and the positional fallback hands out a spare tab
    (observed: two captures of the file listing and one of chrome://new-tab-page/). The viewer
    itself is a hash route opened by clicking a row, and the only static URL into it needs the
    file's numeric `fileid`, which differs per instance.

    Actions are PLAYWRIGHT SELECTORS, not coordinates. apps.py's post_launch has to use
    coordinates because a native window offers nothing else; here there is a real DOM and a real
    page handle, so `{"click": "text=shared_docker_entry.pdf"}` survives a re-render, a different
    row order and a different instance -- none of which a click at (477, 578) does.

    Best effort by design: a failed action is logged and the capture proceeds, because the entry's
    own content check is what decides whether the result is usable. Silence here would be worse
    than a wrong capture -- it is what "10/10 sites captured successfully" meant while three of
    them were the wrong page.
    """
    done = []
    for action in actions or ():
        try:
            if "click" in action:
                page.click(action["click"], timeout=action.get("timeout_ms", 8000))
                done.append(f"click:{action['click']}")
            elif "wait_for" in action:
                page.wait_for_selector(action["wait_for"], timeout=action.get("timeout_ms", 8000))
                done.append(f"wait_for:{action['wait_for']}")
            elif "sleep" in action:
                time.sleep(float(action["sleep"]))
                done.append(f"sleep:{action['sleep']}")
            elif "press" in action:
                page.keyboard.press(action["press"])
                done.append(f"press:{action['press']}")
            elif "eval" in action:
                page.evaluate(action["eval"])
                done.append("eval")
        except Exception as e:
            logger.warning("[%03d] page action %s failed: %s", index, action,
                            str(e).splitlines()[0][:120])
    if done:
        logger.info("[%03d] page actions: %s", index, ", ".join(done))
    return ",".join(done)


def _resolve_targets_from_tabs(browser, raw_urls: list[str]) -> list[str]:
    """Match each requested target to the tab prepare_injection actually opened for it.

    The substituted URLs are NOT readable from the config that was passed in: prepare_injection
    returns an observation (like gym reset), and replace_in_dict builds a new dict rather than
    mutating the caller's. Re-deriving the substitution here would mean a second copy of rules that
    already differ per platform (reddit drops its port behind a proxy, gitlab keeps :8023, the
    pooled suites get whichever port they claimed) and would drift the moment those change.

    The browser is the authority instead -- it holds the resolved URL for every tab that was
    opened. Matching is by path, so ordering differences between CDP and the open sequence do not
    matter.

    THREE PASSES, BECAUSE AN EXACT PATH MATCH IS NOT ENOUGH ON AN SPA. Measured on owncloud, whose
    nine targets differ only by query string: the app canonicalises its own URL on load, so
    `?dir=/Documents` becomes `?dir=/Documents&fileid=...` and the bare files app becomes
    `?dir=/&fileid=...`. Exact comparison matched four of nine, and the run then captured four
    pages under the FIRST four indices -- so index 420 held the sharingin view, silently, with a
    manifest row claiming it was the files root. A capture landing under the wrong index is worse
    than a missing one: ground-truth rows are keyed by capture filename.

      1. exact path+query, then again with trailing slashes stripped (a plain redirect);
      2. same path, and the tab's query is a SUPERSET of the target's -- i.e. the app added
         parameters of its own but kept everything that was asked for. A target with no query at
         all is deliberately excluded from this pass, or it would match every sibling view;
      3. positional. chrome_open_tabs opens in the order of `urls_to_open` and CDP reports pages
         in creation order, so the i-th unmatched target takes the i-th still-unclaimed tab.

    Tabs are claimed BY POSITION, not by URL string: two targets can legitimately resolve to the
    same final URL, and the old `if hit not in resolved` check silently dropped the second.
    """
    open_urls: list[str] = []
    for ctx in browser.contexts:
        for pg in ctx.pages:
            try:
                if pg.url and not pg.url.startswith("about:"):
                    open_urls.append(pg.url)
            except Exception:
                continue

    def _query_of(path: str) -> dict:
        return dict(urllib.parse.parse_qsl(path.partition("?")[2], keep_blank_values=True))

    claimed: set[int] = set()

    def _claim(pred) -> "int | None":
        for i, u in enumerate(open_urls):
            if i not in claimed and pred(u):
                claimed.add(i)
                return i
        return None

    resolved: "list[str | None]" = []
    for raw in raw_urls:
        want = _raw_path(raw)
        want_path, want_q = want.partition("?")[0], _query_of(want)
        hit = _claim(lambda u: _raw_path(u) == want)
        if hit is None:
            hit = _claim(lambda u: _raw_path(u).rstrip("/") == want.rstrip("/"))
        if hit is None and want_q:
            hit = _claim(lambda u: _raw_path(u).partition("?")[0].rstrip("/") == want_path.rstrip("/")
                         and all(_query_of(_raw_path(u)).get(k) == v for k, v in want_q.items()))
        resolved.append(open_urls[hit] if hit is not None else None)

    for n, (raw, got) in enumerate(zip(raw_urls, resolved)):
        if got is not None:
            continue
        spare = _claim(lambda _u: True)
        if spare is None:
            logger.warning("no open tab left for target %s -- skipping", raw)
            continue
        logger.warning("target %s matched no tab by URL; taking unclaimed tab %s positionally",
                        raw, open_urls[spare])
        resolved[n] = open_urls[spare]
    return [u for u in resolved if u is not None]


def _relaunch_chrome_and_reconnect(env, playwright, browser, page):
    """Recovery path for when the in-VM Chrome/CDP connection has died mid-run. Observed live on a
    full-corpus rerun: Chrome crashed right after the very first site's capture (title() raised
    "Target page, context or browser has been closed"), and because nothing reconnected, every one
    of the other 236 sites in that run failed identically against the same dead `page` object --
    the whole run silently went from "one crash" to "100% failure" with no recovery attempted.
    Closes whatever's left of the old playwright/browser/page, relaunches Chrome + the CDP forward
    inside the VM, and reconnects -- the caller retries its current site once against the fresh
    page, and every site after it proceeds normally instead of inheriting the same dead handle."""
    for obj, closer in ((page, "close"), (browser, "close"), (playwright, "stop")):
        if obj is not None:
            try:
                getattr(obj, closer)()
            except Exception:
                pass
    env.controller.execute_command("pkill -f remote-debugging-port || true")
    time.sleep(2)
    env.setup_controller.setup(_CHROME_LAUNCH_SETUP)
    new_playwright, new_browser = _connect_cdp(env.vm_ip, env.chromium_port)
    new_context = new_browser.contexts[0]
    new_page = new_context.new_page()
    return new_playwright, new_browser, new_page


def _connect_cdp(vm_ip: str, chromium_port: int):
    """Same bounded retry loop as SetupController._chrome_open_tabs_setup -- Chrome may need a
    few seconds after container boot before its CDP endpoint responds."""
    remote_debugging_url = f"http://{vm_ip}:{chromium_port}"
    playwright = sync_playwright().start()
    last_exc = None
    for attempt in range(_CDP_CONNECT_ATTEMPTS):
        if attempt > 0:
            time.sleep(_CDP_CONNECT_RETRY_SECONDS)
        try:
            browser = playwright.chromium.connect_over_cdp(remote_debugging_url)
            logger.info("Connected to Chrome @ %s (attempt %d)", remote_debugging_url, attempt + 1)
            return playwright, browser
        except Exception as e:
            last_exc = e
            logger.warning("CDP connect attempt %d/%d failed: %s", attempt + 1, _CDP_CONNECT_ATTEMPTS, e)
    playwright.stop()
    raise RuntimeError(f"Could not connect to Chrome CDP at {remote_debugging_url}") from last_exc


_A11Y_POLL_SECONDS = 1.0
_MIN_DOCUMENT_NODES = 150


_CONSENT_OVERRIDES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "cookie_provider_overrides.json")

_CONSENT_ACCEPT_TEXT = ("accept all", "accept all cookies", "allow all", "i agree", "agree",
                        "yes, i agree", "accept cookies", "accept", "got it", "ok",
                        "continue", "i accept", "accept and continue")


def _load_consent_overrides() -> dict:
    try:
        with open(_CONSENT_OVERRIDES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _dismiss_consent(page, hostname: str, overrides: dict, index: int) -> str:
    """Clicks the page's consent-accept control, returning how it was dismissed ("id:<x>", "text",
    or "" if nothing was clicked).

    This is not cosmetic. A consent banner typically sets aria-hidden across the rest of the
    document, and Chrome then drops that entire subtree from the accessibility tree -- measured on
    ebay.com: 7,266 DOM elements, 908 of them aria-hidden, 4,122 of 4,161 AX nodes ignored
    (3,541 ariaHiddenSubtree + 577 ariaHiddenElement), leaving AT-SPI with 24 nodes of pure banner
    while the screenshot showed the full search results. Nothing blocks the mouse -- `inert` count
    was 0 -- so the page stays clickable, which is exactly why the discrepancy is invisible until
    you read the tree.
    """
    def _frames():
        try:
            return list(page.frames)
        except Exception:
            return [page]

    ids = (overrides.get(hostname) or {}).get("cookie_accept_ids") or []
    for accept_id in ids:
        for fr in _frames():
            try:
                el = fr.query_selector(f"#{accept_id}")
                if el and el.is_visible():
                    el.click(timeout=3000)
                    logger.info("[%03d] dismissed consent banner via #%s", index, accept_id)
                    return f"id:{accept_id}"
            except Exception:
                continue
    for fr in _frames():
        try:
            for el in fr.query_selector_all("button, a[role=button], [role=button]"):
                try:
                    if not el.is_visible():
                        continue
                    label = (el.inner_text() or "").strip().lower()
                except Exception:
                    continue
                if label in _CONSENT_ACCEPT_TEXT:
                    el.click(timeout=3000)
                    logger.info("[%03d] dismissed consent banner via text %r", index, label)
                    return "text"
        except Exception:
            continue
    return ""


def _warm_origin(page, url: str, index: int, overrides: dict, seconds: float,
                 nav_timeout_ms: int, keep_consent_banner: bool = False) -> str:
    """Visits the bare origin before the target URL, so the site can set its clearance cookie.

    Anti-bot vendors treat a cold jump straight to a deep path as a bot signature, and their
    non-interactive challenges (Cloudflare's in particular) resolve on the origin and hand back a
    cookie that then covers the rest of the site. Landing on the origin first therefore converts a
    fair number of challenge pages into real ones -- measured against captures that failed the
    first time round; see at-spi/detect_bot_walls.py for what it is meant to fix.

    Deliberately best-effort and non-fatal: a warm-up that times out or 404s must not cost us the
    capture, which would trade a walled page for no page at all. Returns what happened, for the
    manifest.
    """
    parsed = urllib.parse.urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}/"
    if origin.rstrip("/") == url.rstrip("/").split("?")[0].split("#")[0]:
        try:
            page.wait_for_timeout(int(seconds * 1000))
        except Exception:
            pass
        return "dwell"
    try:
        try:
            page.goto(origin, wait_until="networkidle", timeout=nav_timeout_ms)
        except PlaywrightTimeoutError:
            page.goto(origin, wait_until="load", timeout=nav_timeout_ms)
        if keep_consent_banner:
            logger.info("[%03d] warm-up: leaving the origin's consent banner standing", index)
        else:
            _dismiss_consent(page, parsed.netloc, overrides, index)
        page.wait_for_timeout(int(seconds * 1000))
        logger.info("[%03d] warmed origin %s", index, origin)
        return f"warmed:{origin}"
    except Exception as e:
        logger.warning("[%03d] origin warm-up failed (%s: %s), going straight to the target",
                       index, type(e).__name__, e)
        return f"failed:{type(e).__name__}"


def _document_node_count(xml_text: str) -> int:
    """Number of nodes inside the page's document roots, or -1 if the tree is unparseable/absent.

    Chrome builds the document accessibility tree lazily and asynchronously, and `networkidle`
    is not a proxy for it: a hydrating SPA can satisfy networkidle, and even finish painting,
    while its a11y tree still holds nothing but empty container shells. Counting nodes is what
    distinguishes "ready" from "still filling in" -- a mere non-empty check is not enough, as
    012_www.ebay.com demonstrated by exposing a `srp-main` container of 32px height with one
    0px-tall child while the screenshot showed 20,000px of fully rendered search results.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return -1
    docs = [n for n in root.iter() if n.tag == "document-web"]
    if not docs:
        return -1
    return sum(sum(1 for _ in d.iter()) for d in docs)


def _tree_with_populated_document(env, index: int, wait_seconds: float) -> tuple[str, bool]:
    """Grabs the a11y tree, retrying until the page's document actually appears in it.

    Without this the capture races Chrome's lazy a11y build and can silently save a tree holding
    only browser chrome. Measured on the 2026-08-12 full rerun: 3 of 235 web captures came out
    this way (000_stackoverflow, 191_kohls, 223_espn) -- 000 had a `document-web` node of size
    (0,0) with zero children while its screenshot showed the complete question page, and it did
    NOT hit the networkidle timeout, so nothing in the run's logs flagged it. It was the first
    site of the run, i.e. the case where Chrome's accessibility had had the least time to warm up.

    Returns (xml, populated). A capture that never populates is still saved -- a screenshot plus
    browser chrome is more useful than nothing, and the manifest row records `a11y_populated:
    false` so it can be found and re-shot rather than quietly entering the corpus as a page with
    no content.
    """
    deadline = time.time() + wait_seconds
    previous = -1
    xml_text = ""
    while True:
        xml_text = env.controller.get_accessibility_tree()
        if not xml_text:
            raise RuntimeError("get_accessibility_tree() returned empty content")
        count = _document_node_count(xml_text)

        if count >= _MIN_DOCUMENT_NODES and count == previous:
            return xml_text, True
        previous = count

        if time.time() >= deadline:
            logger.warning("[%03d] a11y tree never stabilised (%d nodes) after %.0fs -- saving "
                            "anyway, marked a11y_populated=false", index, count, wait_seconds)
            return xml_text, False
        logger.info("[%03d] a11y tree still filling in (%d nodes), waiting...", index, count)
        time.sleep(_A11Y_POLL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sample AT-SPI accessibility trees from trusted websites")
    parser.add_argument("--provider_name", default="docker")
    parser.add_argument("--path_to_vm", default=None)
    parser.add_argument("--snapshot_name", default="init_state")
    parser.add_argument("--screen_width", type=int, default=1920)
    parser.add_argument("--screen_height", type=int, default=1080)
    parser.add_argument("--headless", action="store_true", default=True,
                         help="Run without a VNC viewer window (default: on -- this is an unattended batch job)")
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument("--out_dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "xml"))
    parser.add_argument("--screenshot_dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots"))
    parser.add_argument("--dom_dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "dom"),
                        help="serialized DOM per capture (the artifact UCM's pipeline consumes)")
    parser.add_argument("--dom_geom_dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "dom_geom"),
                        help="per-element xpath+rect+screen bbox, the join key to the a11y tree")
    parser.add_argument("--platform", default=None,
                        choices=("reddit", "owncloud", "rocketchat", "gitlab", "ucm"),
                        help="capture a self-hosted eval suite instead of the public web. Reuses "
                             "that suite's own evaluation_examples config for host resolution and "
                             "login, and takes its target URLs from the configs' open-tab steps")
    parser.add_argument("--platform_pages", type=int, default=5,
                        help="max DISTINCT target URLs to take from the suite's configs "
                             "(default 5; several suites offer fewer than that in total)")
    parser.add_argument("--platform_extra", default=None,
                        help="comma-separated EXTRA target URLs appended to the ones found in the "
                             "suite's configs. Write them with the same TO_BE_REPLACED_URL:<port> "
                             "placeholder the configs use, e.g. "
                             "'TO_BE_REPLACED_URL:3000/channel/general'")
    parser.add_argument("--start_index", type=int, default=400,
                        help="first corpus index for --platform captures (suites live at 400+)")
    parser.add_argument("--index_offset", type=int, default=0,
                        help="write captures at index+OFFSET instead of index, so a re-shoot under "
                             "different settings (e.g. --keep_consent_banner) becomes an ADDITIONAL "
                             "capture in the same corpus rather than overwriting the standard one")
    parser.add_argument("--variant_label", default=None,
                        help="recorded as manifest `variant` alongside `variant_of` when "
                             "--index_offset is used, e.g. 'consent_banner'")
    parser.add_argument("--dom_timeout_s", type=int, default=90,
                        help="hard wall-clock cap on one capture's DOM+geometry walk; on expiry "
                             "the capture keeps its tree and screenshot and loses only the DOM")
    parser.add_argument("--skip_dom", action="store_true",
                        help="capture tree+screenshot only, as before the DOM artifacts existed")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only visit the first N sites of the (possibly filtered) selection (default: all)")
    parser.add_argument("--indices", default=None,
                         help="Comma-separated ALL_SITES indices/ranges to (re)capture, e.g. '0,5,12-14'. "
                              "Overrides --domain_contains/--category/--limit.")
    parser.add_argument("--domain_contains", default=None,
                         help="Only (re)capture sites whose URL contains this substring")
    parser.add_argument("--category", default=None,
                         help="Only (re)capture sites in this comma-separated set of sites.py categories")
    parser.add_argument("--list", dest="list_only", action="store_true",
                         help="Print the selected sites and exit -- no VM boot, no capture")
    parser.add_argument("--nav_timeout_ms", type=int, default=30000)
    parser.add_argument("--warm_origin", action="store_true",
                         help="Visit the bare origin and accept consent BEFORE navigating to the "
                              "target URL, so the site can hand back a clearance cookie. Recovers "
                              "captures that landed on an anti-bot challenge page instead of the "
                              "real one -- run detect_bot_walls.py to see which those are.")
    parser.add_argument("--warm_seconds", type=float, default=6.0,
                         help="How long to dwell on the origin during --warm_origin.")
    parser.add_argument("--site_delay_seconds", type=float, default=0.0,
                         help="Pause between sites. Back-to-back loads from one IP are themselves "
                              "a rate-limit trigger, so a recovery pass over blocked domains "
                              "wants this well above 0 (20-30s).")
    parser.add_argument("--keep_consent_banner", action="store_true",
                         help="Do NOT dismiss cookie/consent banners. The banner is worth keeping "
                              "in a few captures deliberately (it is a real UI surface worth "
                              "labeling), but note that on most sites it sets aria-hidden across "
                              "the whole document, so the capture then contains the banner and "
                              "almost nothing else.")
    parser.add_argument("--a11y_wait_seconds", type=float, default=15.0,
                         help="How long to keep re-fetching the accessibility tree while the "
                              "page's document-web node is still an empty stub (Chrome builds it "
                              "lazily, so a fully-painted page can still have no a11y content). "
                              "0 disables the wait and restores the old single-shot behaviour.")
    parser.add_argument("--settle_seconds", type=float, default=1.5,
                         help="Pause after navigation before capturing the tree, to let AT-SPI catch up")
    args = parser.parse_args()

    sites = _select_sites(args)
    suite_config = None
    if args.platform:
        extra = [u.strip() for u in (args.platform_extra or "").split(",") if u.strip()]
        suite_config, raw_urls = _suite_targets(args.platform, args.platform_pages, extra)
        sites = []
        if args.list_only:
            print(f"platform {args.platform}: {len(raw_urls)} distinct target URL(s)")
            for n, url in enumerate(raw_urls):
                print(f"{args.start_index + n:03d}  {args.platform:10s} {url}")
            print("\n(placeholders are resolved by prepare_injection at capture time)")
            return
    elif not sites:
        parser.error("No sites matched --indices/--domain_contains/--category.")
    if args.list_only:
        for i, category, url in sites:
            target = f" -> {i + args.index_offset:03d}" if args.index_offset else ""
            print(f"{i:03d}{target}  {category:10s} {url}")
        return

    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(args.screenshot_dir, exist_ok=True)
    manifest_path = os.path.join(os.path.dirname(args.out_dir), "manifest.json")
    manifest_by_index: dict[int, dict] = {}

    def _write_manifest() -> None:
        manifest_io.merge_rows(manifest_path, manifest_by_index)

    env_kwargs: dict = dict(
        provider_name=args.provider_name,
        action_space="pyautogui",
        require_a11y_tree=True,
        screen_size=(args.screen_width, args.screen_height),
        headless=args.headless,
        os_type="Ubuntu",
        snapshot_name=args.snapshot_name,
    )
    if args.path_to_vm:
        env_kwargs["path_to_vm"] = args.path_to_vm

    consent_overrides = _load_consent_overrides()
    logger.info("Loaded consent accept-ids for %d domains",
                sum(1 for v in consent_overrides.values() if v.get("cookie_accept_ids")))
    logger.info("Booting DesktopEnv (%s)...", args.provider_name)
    env = DesktopEnv(**env_kwargs)
    playwright = browser = page = None
    try:
        if args.platform:
            env.prepare_injection(suite_config)
            logger.info("VM ready (vm_ip=%s). Platform %s prepared (hosts, login, tabs).",
                        env.vm_ip, args.platform)
        else:
            env.prepare_injection()
            logger.info("VM ready (vm_ip=%s, chromium_port=%s). Launching Chrome + CDP forward...",
                        env.vm_ip, env.chromium_port)
            env.setup_controller.setup(_CHROME_LAUNCH_SETUP)

        playwright, browser = _connect_cdp(env.vm_ip, env.chromium_port)
        context = browser.contexts[0]

        if args.platform:
            sites = [(args.start_index + n, args.platform, url) for n, url
                     in enumerate(_resolve_targets_from_tabs(browser, raw_urls))]
            if not sites:
                raise RuntimeError(
                    f"platform {args.platform}: none of the {len(raw_urls)} target URLs matched an "
                    f"open tab -- setup opened nothing, or the paths differ")
            for i, _cat, url in sites:
                logger.info("   %03d  %s", i, url)
        _probe = env.controller.get_accessibility_tree()
        if not _probe:
            raise SystemExit(
                "PRE-FLIGHT FAILED: this VM's accessibility server returns nothing. Every capture "
                "would fail identically. Check `curl -s http://localhost:<server_port>/accessibility "
                "| tail -20` for the server-side traceback, then just boot a new VM -- the base "
                "image is mounted read-only and shared, so a rerun gets a clean instance.")
        logger.info("Pre-flight: a11y tree OK (%d bytes). Chrome ready. Visiting %d sites.",
                    len(_probe), len(sites))
        page = context.new_page()

        succeeded = 0
        origin_votes: collections.Counter = collections.Counter()
        for pos, (i, category, url) in enumerate(sites, start=1):
            if args.platform:
                cap = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(url).query).get("capture", [""])[0]
                if cap:
                    slug = re.sub(r"[^a-zA-Z0-9]+", "_", cap).strip("_")
                else:
                    slug = re.sub(r"[^a-zA-Z0-9]+", "_",
                                  re.sub(r"^https?://[^/]*", "", url)).strip("_")
                domain = f"{args.platform}_{slug[:48]}" if slug else args.platform
            else:
                domain = _sanitize_domain(url)
            out_i = i + args.index_offset
            stem = f"{out_i:03d}_{domain}"
            filename = f"{stem}.xml"
            screenshot_filename = f"{stem}.png"
            row = {
                "index": out_i, "category": category, "url": url, "domain": domain,
                "filename": filename, "screenshot_filename": screenshot_filename,
                "status": "error", "title": None,
                "xml_bytes": 0, "screenshot_bytes": 0, "error": None,
                "timestamp": datetime.datetime.now().isoformat(),
            }
            if args.platform:
                view = suites.view_of(args.platform, url)
                if view:
                    row["view"] = view
            if args.index_offset:
                row["variant"] = args.variant_label or "variant"
                row["variant_of"] = i
            for attempt in (1, 2):
                try:
                    logger.info("[%d/%d] site #%03d%s: %s (%s)", pos, len(sites), i,
                                f" -> {out_i:03d}" if args.index_offset else "", url, category)
                    internal = _is_internal_url(url)
                    if internal:
                        _navigate_internal(env, page, url, i, args.nav_timeout_ms)
                    else:
                        if args.warm_origin:
                            row["warmed"] = _warm_origin(page, url, i, consent_overrides,
                                                         args.warm_seconds, args.nav_timeout_ms,
                                                         args.keep_consent_banner)
                        try:
                            page.goto(url, wait_until="networkidle", timeout=args.nav_timeout_ms)
                        except PlaywrightTimeoutError:
                            logger.warning("[%03d] networkidle timed out, falling back to 'load'", i)
                            page.goto(url, wait_until="load", timeout=args.nav_timeout_ms)
                        page.bring_to_front()
                    _park_mouse(env, i)
                    time.sleep(args.settle_seconds)

                    _acts = (suites.page_actions(args.platform, url) if args.platform
                             else ([] if internal else sites_mod.page_actions(url)))
                    if _acts:
                        row["page_actions"] = _run_page_actions(page, _acts, i)
                        time.sleep(args.settle_seconds)

                    title = None
                    if not internal:
                        try:
                            title = page.title()
                        except Exception:
                            logger.warning("[%03d] page.title() failed, continuing without it", i)

                    if env.dismiss_update_banner():
                        logger.info("[%03d] dismissed a pending 'Software Updates' banner", i)

                    consent = ""
                    if not args.keep_consent_banner and not internal:
                        consent = _dismiss_consent(page, _hostname_of(url), consent_overrides, i)
                        if consent:
                            page.wait_for_timeout(int(args.settle_seconds * 1000))

                    xml, a11y_populated = _tree_with_populated_document(
                        env, i, args.a11y_wait_seconds)
                    screenshot = env.controller.get_screenshot()
                    if not screenshot:
                        raise RuntimeError("get_screenshot() returned empty content")

                    html_by_frame, geom = {}, None
                    if not args.skip_dom and not internal:
                        try:
                            with _dom_deadline(args.dom_timeout_s):
                                html_by_frame, geom = capture_dom(
                                    page, timeout_s=args.dom_timeout_s)
                                calibrate(geom, xml, prior=origin_votes)
                            cal = geom.get("calibration") or {}
                            if cal.get("source") == "page":
                                origin_votes[tuple(cal["origin"])] += 1
                        except Exception as e:
                            logger.warning("[%03d] DOM capture failed: %s", i, e)
                            html_by_frame, geom = {}, None

                    after = _document_node_count(env.controller.get_accessibility_tree() or "")
                    before = _document_node_count(xml)
                    if a11y_populated and after > before:
                        logger.warning("[%03d] page kept rendering during capture (%d -> %d "
                                        "nodes); tree and screenshot may disagree",
                                        i, before, after)
                        a11y_populated = False

                    out_path = os.path.join(args.out_dir, filename)
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write(xml)
                    screenshot_path = os.path.join(args.screenshot_dir, screenshot_filename)
                    with open(screenshot_path, "wb") as f:
                        f.write(screenshot)

                    dom_bytes = geom_bytes = 0
                    if geom is not None:
                        dom_bytes, geom_bytes = write_dom_artifacts(
                            args.dom_dir, args.dom_geom_dir, stem, html_by_frame, geom)
                        cal = geom.get("calibration") or {}
                        if cal.get("source") == "page":
                            logger.info("[%03d] DOM %d nodes/%d frames, origin=%s "
                                        "(%d votes vs %d, %.0f%% agreement)",
                                        i, geom["n_nodes"], geom["n_frames"], cal["origin"],
                                        cal["votes"], cal["runner_up"], 100 * cal["agreement"])
                        elif cal.get("source") == "inherited":
                            logger.info("[%03d] DOM %d nodes/%d frames, origin=%s INHERITED "
                                        "from %d pages (%s)", i, geom["n_nodes"],
                                        geom["n_frames"], cal["origin"],
                                        cal["inherited_from_n_pages"], cal["page_reason"])
                        else:
                            logger.warning("[%03d] DOM %d nodes but NOT calibrated: %s",
                                           i, geom["n_nodes"], cal.get("reason"))

                    if internal:
                        _close_internal_tab(env, i)

                    row["status"] = "ok"
                    row["internal_page"] = True if internal else None
                    row["a11y_populated"] = a11y_populated
                    row["consent_dismissed"] = consent or None
                    row["title"] = title
                    row["xml_bytes"] = len(xml.encode("utf-8"))
                    row["screenshot_bytes"] = len(screenshot)
                    row["dom_bytes"] = dom_bytes
                    row["dom_geom_bytes"] = geom_bytes
                    row["dom_nodes"] = geom["n_nodes"] if geom else None
                    row["dom_frames"] = geom["n_frames"] if geom else None
                    row["dom_calibration"] = (geom.get("calibration") if geom else None)
                    row["error"] = None
                    succeeded += 1
                    break
                except Exception as e:
                    logger.exception("[%03d] failed on %s (attempt %d)", i, url, attempt)
                    row["error"] = str(e)
                    dead = page.is_closed() or not browser.is_connected()
                    if attempt == 1 and dead:
                        logger.warning("[%03d] browser/page appears dead -- relaunching Chrome + "
                                        "reconnecting CDP, then retrying this site once", i)
                        try:
                            playwright, browser, page = _relaunch_chrome_and_reconnect(
                                env, playwright, browser, page)
                            continue
                        except Exception:
                            logger.exception("[%03d] recovery relaunch itself failed -- giving up "
                                              "on this site, moving on to the next one", i)
                    elif not dead:
                        time.sleep(2)
                    break
            manifest_by_index[row["index"]] = row
            _write_manifest()

            if args.site_delay_seconds and pos < len(sites):
                time.sleep(args.site_delay_seconds)

        logger.info("Done: %d/%d sites captured successfully. Manifest: %s",
                    succeeded, len(sites), manifest_path)
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
        env.close()


if __name__ == "__main__":
    main()
