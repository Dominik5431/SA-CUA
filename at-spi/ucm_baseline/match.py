"""Run a UCM selector config against a saved snapshot and record which elements it matched.

This is UCM's pipeline/analyze_selectors.py step, with two deliberate differences and no others:

  1. ONE BROWSER FOR THE WHOLE CORPUS. Their function launches and tears down chromium per
     invocation, which is right for a three-page run and costs eleven minutes of nothing over 115
     captures. The per-page semantics are identical.
  2. THE NETWORK IS CUT. Every request except the snapshot document itself is aborted. Their
     evaluator serves the snapshot over http.server, where the page's own scripts and stylesheets
     404 anyway; cutting the network makes that explicit and, more importantly, deterministic --
     a snapshot that could still reach the live internet would re-render differently on every run
     and its xpaths would stop agreeing with the geometry dom_capture.py recorded.

The element resolution itself -- `document.querySelectorAll(selector)` and the getXPath walk --
is UCM's own GET_ELEMENTS_JS, imported rather than copied, so the xpath strings this produces are
the same strings their pipeline produces and the same strings dom_geom.json holds.

TOP FRAME ONLY. UCM's evaluator calls document.querySelectorAll on the main document; a selector
never reaches into an iframe. 50 of the 115 web captures have subframes and their content is
simply invisible to the method. That is a property of UCM, not of this harness, so it is
reproduced rather than papered over -- but it is counted, per capture, as `frames_ignored`, so a
reader can see how much of a page the baseline never had a chance at.
"""
import json
import os

from . import ucm_import

_AT_SPI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOM_DIR = os.path.join(_AT_SPI, "dom")


def snapshot_path(filename: str) -> str:
    """`012_en.wikipedia.org.xml` -> the top-frame snapshot for that capture."""
    return os.path.join(DOM_DIR, filename.replace(".xml", "") + ".html")


def has_snapshot(filename: str) -> bool:
    return os.path.exists(snapshot_path(filename))


class SnapshotMatcher:
    """Holds one chromium across many captures. Use as a context manager."""

    def __init__(self, viewport=(1920, 1080)):
        self._viewport = viewport
        self._pw = self._browser = self._context = self._page = None
        self._expect_url = None
        self._committed = False

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(
            viewport={"width": self._viewport[0], "height": self._viewport[1]},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        )
        self._page = self._context.new_page()
        self._page.route("**/*", self._route)
        return self

    def __exit__(self, *exc):
        for closer in (self._context, self._browser):
            try:
                closer.close()
            except Exception:
                pass
        try:
            self._pw.stop()
        except Exception:
            pass
        return False

    def _route(self, route):
        """Serve the snapshot document once; abort everything else, including later navigations.

        The "everything else" has to include navigations. Several snapshots carry inline scripts
        that redirect on load -- 025_www.booking.com does, and before this the page had already
        left by the time the first query ran, so every selector matched zero elements and the
        capture looked like a page UCM simply had no selectors for. Allowing exactly one document
        commit pins the DOM to the artifact on disk.
        """
        try:
            req = route.request
            first = req.resource_type == "document" and req.is_navigation_request() \
                and req.url == self._expect_url and not self._committed
            if first:
                self._committed = True
                route.continue_()
            else:
                route.abort()
        except Exception:
            pass

    def load(self, filename: str) -> None:
        """Navigate to the snapshot.

        `domcontentloaded`, not `load`: with the network cut, an aborted <img> or stylesheet can
        leave the load event pending forever -- 025_www.booking.com hangs the full 60s on `load`
        and returns instantly on this. Nothing here needs subresources; the DOM is the artifact.
        """
        path = snapshot_path(filename)
        self._expect_url = "file://" + path
        self._committed = False
        self._page.goto(self._expect_url, wait_until="domcontentloaded", timeout=90000)

    def run_config(self, filename: str, selectors: list) -> dict:
        """-> UCM-shaped {"summary":..., "results":[...]} for one capture."""
        asel = ucm_import.analyze_selectors()
        self.load(filename)
        results, total, with_matches, unique = [], 0, 0, set()
        for idx, item in enumerate(selectors, 1):
            selector = item.get("selector", "")
            entry = {"index": idx, "selector": selector, "tagName": item.get("tagName", "unknown"),
                     "description": item.get("description", ""), "count": 0,
                     "element_ids": [], "element_info": {}, "previews": []}
            try:
                count = self._page.locator(selector).count()
            except Exception as e:
                entry["error"] = str(e)
                results.append(entry)
                continue
            entry["count"] = count
            total += count
            with_matches += count > 0
            elements = asel.get_elements_with_ids(self._page, selector)
            ids = [e.get("xpath") for e in elements if e.get("xpath")]
            entry["element_ids"] = ids
            entry["element_info"] = {
                e["xpath"]: {"tagName": e.get("tagName", ""), "className": e.get("className", ""),
                             "id": e.get("id", ""), "textPreview": e.get("textPreview", "")}
                for e in elements if e.get("xpath")}
            unique.update(ids)
            results.append(entry)
        return {"summary": {"file": filename, "totalSelectors": len(selectors),
                            "selectorsWithMatches": with_matches, "totalElements": total,
                            "uniqueElements": len(unique),
                            "url": "file://" + snapshot_path(filename)},
                "results": results}

    def collect_all_xpaths(self, filename: str) -> list:
        """Every element's xpath in the reloaded snapshot -- the test hook for xpath stability.

        Uses UCM's own GET_ELEMENTS_JS via the universal selector, so what comes back is exactly
        what their evaluator would have said about a selector matching everything.
        """
        asel = ucm_import.analyze_selectors()
        self.load(filename)
        return [e["xpath"] for e in asel.get_elements_with_ids(self._page, "*") if e.get("xpath")]


def load_config(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg["untrusted_selectors"]
