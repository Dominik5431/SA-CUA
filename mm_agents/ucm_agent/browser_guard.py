"""
BrowserGuard — CDP-based DOM masking + HTML retrieval for ucm_agent.

This is the one genuinely new piece this integration needed (see
UCM_AGENT_INTEGRATION_PLAN.md §0 and §2). The source UCM repo has to fake
both live-HTML retrieval and JS injection through a VNC-only Docker browser
(xdotool + DevTools + clipboard tricks, or an nginx-injected script). This
repo's VMs already expose Chrome's remote-debugging port
(`env.chromium_port`, see desktop_env/controllers/setup.py and
desktop_env/evaluators/getters/chrome.py, both of which already connect via
`playwright.chromium.connect_over_cdp(...)`), so both jobs collapse to one
Playwright/CDP connection:

  - masking: inject reveal.js + a per-site "marker" script (CSS-selector
    untrusted-region labeling, see js/*_marker.js) into the live page via
    `context.add_init_script()` (future navigations/tabs) and one explicit
    `page.evaluate()` for whatever page is already open. The injected JS's
    own MutationObserver + polling interval (see js/reveal.js,
    js/marker_common.js) keeps re-masking as the page mutates — no Python-
    side polling loop needed.
  - get_html(): `page.content()` over the same connection returns the live
    rendered DOM (including the `data-qllm-id`/`data-untrusted` attributes
    the injected JS just set), for the QLLM executor to parse.

Caveat carried over from the integration plan (§10, "open risks"): picking
the "active"/foreground tab from `context.pages` is a heuristic (newest
non-blank page), not a verified foreground-window check. This is fine for
the single-tab-per-task shape of the adv/ platforms (Reddit/OwnCloud/
RocketChat each run as one Chrome tab) but should be revisited if a task
ever legitimately needs multiple simultaneous tabs.
"""

import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("desktopenv.agent")

_JS_DIR = Path(__file__).parent / "js"

_SITE_MARKER_FILES = {
    "reddit": "reddit_marker.js",
    "owncloud": "owncloud_marker.js",
    "rocketchat": "rocketchat_marker.js",
}

_SITE_URL_HINTS = {
    "reddit": ["reddit", "postmill"],
    "owncloud": ["owncloud"],
    "rocketchat": ["rocketchat", "rocket.chat"],
}

CONNECT_RETRIES = 15
CONNECT_RETRY_INTERVAL_S = 5


def detect_site(url: str) -> Optional[str]:
    url_l = (url or "").lower()
    for site, hints in _SITE_URL_HINTS.items():
        if any(h in url_l for h in hints):
            return site
    return None


class BrowserGuard:
    """Owns the Playwright/CDP connection used for masking + HTML retrieval.

    One instance per episode (constructed by UCMLoop, which owns `env` and
    therefore knows `vm_ip`/`chromium_port`).
    """

    def __init__(self, vm_ip: str, chromium_port: int = 9222):
        self.vm_ip = vm_ip
        self.chromium_port = chromium_port
        self._playwright = None
        self._browser = None
        self._injected_sites: set = set()


    def connect(self):
        """(Re)connect over CDP, retrying the way setup.py/chrome.py do."""
        from playwright.sync_api import sync_playwright

        if self._playwright is None:
            self._playwright = sync_playwright().start()

        remote_debugging_url = f"http://{self.vm_ip}:{self.chromium_port}"
        last_err = None
        for attempt in range(CONNECT_RETRIES):
            try:
                self._browser = self._playwright.chromium.connect_over_cdp(remote_debugging_url)
                logger.info("[UCM BrowserGuard] Connected over CDP at %s", remote_debugging_url)
                return
            except Exception as e:
                last_err = e
                if attempt < CONNECT_RETRIES - 1:
                    time.sleep(CONNECT_RETRY_INTERVAL_S)
        raise RuntimeError(
            f"[UCM BrowserGuard] Failed to connect over CDP at {remote_debugging_url} "
            f"after {CONNECT_RETRIES} attempts: {last_err}"
        )

    def _ensure_connected(self):
        if self._browser is None or not self._browser.is_connected():
            self.connect()

    def _context(self):
        self._ensure_connected()
        contexts = self._browser.contexts
        if not contexts:
            raise RuntimeError("[UCM BrowserGuard] No browser context available over CDP")
        return contexts[0]

    def active_page(self):
        """Best-effort pick of the tab the agent is actually looking at.

        Heuristic: the most recently created non-blank page. See the module
        docstring's caveat — this is not a verified foreground-window check.
        """
        pages = [p for p in self._context().pages if p.url and not p.url.startswith("about:blank")]
        if not pages:
            pages = self._context().pages
        if not pages:
            raise RuntimeError("[UCM BrowserGuard] No open pages in the browser context")
        return pages[-1]

    def close(self):
        """Disconnect our CDP client only — never closes the shared browser."""
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._playwright = None
        self._browser = None
        self._injected_sites = set()


    @staticmethod
    def _read_js(filename: str) -> str:
        return (_JS_DIR / filename).read_text(encoding="utf-8")

    def _combined_script(self, site: str) -> str:
        marker_file = _SITE_MARKER_FILES.get(site)
        if marker_file is None:
            raise ValueError(f"[UCM BrowserGuard] Unknown site '{site}', expected one of {list(_SITE_MARKER_FILES)}")
        parts = [
            self._read_js("marker_common.js"),
            self._read_js(marker_file),
            self._read_js("reveal.js"),
            self._read_js("security_tracker.js"),
        ]
        return "\n;\n".join(parts)

    def start_masking(self, site: str):
        """Install masking for `site` on all future navigations in this
        context, and inject it into whatever page is already open now."""
        script = self._combined_script(site)
        context = self._context()
        context.add_init_script(script)
        for page in context.pages:
            try:
                page.evaluate(script)
            except Exception as e:
                logger.warning("[UCM BrowserGuard] Initial inject failed on %s: %s", page.url, e)
        self._injected_sites.add(site)

    def ensure_masking_for_current_page(self):
        """Auto-detect the platform from the active page's URL and make sure
        its marker script has been injected (used after navigations this
        module itself performs, e.g. goto()/back()/forward())."""
        page = self.active_page()
        site = detect_site(page.url)
        if site is None:
            logger.warning(
                "[UCM BrowserGuard] Could not detect platform from URL '%s' — "
                "no marker script applied on this page.", page.url,
            )
            return
        if site not in self._injected_sites:
            self.start_masking(site)
        else:
            try:
                page.evaluate(self._combined_script(site))
            except Exception as e:
                logger.warning("[UCM BrowserGuard] Re-inject failed on %s: %s", page.url, e)


    def get_html(self) -> str:
        """Live rendered DOM of the active page, `data-qllm-id`/
        `data-untrusted` attributes included — this is what qllm_executor
        parses to resolve a `qllm_id` to its text content."""
        return self.active_page().content()

    def get_current_url(self) -> str:
        return self.active_page().url

    def goto(self, url: str, timeout_ms: int = 30000):
        page = self.active_page()
        page.goto(url, timeout=timeout_ms)
        page.wait_for_load_state("load", timeout=timeout_ms)
        self.ensure_masking_for_current_page()

    def back(self, timeout_ms: int = 30000):
        page = self.active_page()
        page.go_back(timeout=timeout_ms)
        self.ensure_masking_for_current_page()

    def forward(self, timeout_ms: int = 30000):
        page = self.active_page()
        page.go_forward(timeout=timeout_ms)
        self.ensure_masking_for_current_page()

    def reveal_stats(self) -> Optional[dict]:
        """Read back window.__ucmStats (see js/security_tracker.js) for the
        episode's audit log — did the QLLM tool ever get more revealed to
        it than expected, was anything untrusted ever revealed at all."""
        try:
            return self.active_page().evaluate("() => window.__ucmStats || null")
        except Exception as e:
            logger.warning("[UCM BrowserGuard] Could not read reveal stats: %s", e)
            return None
