// Shared marking engine for ucm_agent's per-site "marker" scripts
// (reddit_marker.js / owncloud_marker.js / rocketchat_marker.js).
//
// Generalizes the approach used by the source UCM repo's
// environments/webarena/gitlab-setup/nginx-files/gitlab-marker.js: a table
// of CSS selectors, each with a `shouldHide` predicate (an ownership/
// authorship check — "is this content actually from someone other than the
// logged-in user?"), applied on load and re-applied via a MutationObserver +
// polling interval as the SPA re-renders.
//
// A site marker script defines `window.__ucmSiteSelectors` (an array, see
// shape below) and `window.__ucmCurrentUser()` (returns the logged-in
// username or null), then calls `window.__ucmApplyMarkers()`. This file
// must run before reveal.js.
//
// Selector entry shape:
//   {
//     selector: "<css selector>",
//     tagName: "<short semantic tag used in the qllm_id / placeholder>",
//     description: "<human-readable description, for debugging>",
//     shouldHide: (el, currentUser) => bool,   // omit => always hide (shouldHide: () => true)
//     skipEmptyCheck: bool,                     // don't skip elements with empty textContent
//   }

(function () {
    'use strict';

    if (window.__ucmMarkerEngineInstalled) return;
    window.__ucmMarkerEngineInstalled = true;

    const markedElements = new WeakSet();

    function applyMarkers() {
        const selectors = window.__ucmSiteSelectors || [];
        const currentUser = (typeof window.__ucmCurrentUser === 'function') ? window.__ucmCurrentUser() : null;
        let markedCount = 0;

        selectors.forEach(function (config) {
            let elements;
            try {
                elements = document.querySelectorAll(config.selector);
            } catch (e) {
                return;
            }

            elements.forEach(function (element) {
                if (markedElements.has(element)) return;
                if (element.hasAttribute('data-untrusted-element')) {
                    markedElements.add(element);
                    return;
                }

                // Don't double-mask nested matches (a parent already marked).
                let parent = element.parentElement;
                while (parent) {
                    if (parent.hasAttribute('data-untrusted-element')) {
                        markedElements.add(element);
                        return;
                    }
                    parent = parent.parentElement;
                }

                const hasImage = element.querySelector('img') !== null;
                if (!hasImage && !config.skipEmptyCheck &&
                    (!element.textContent || element.textContent.trim().length === 0)) {
                    return;
                }

                const shouldHide = config.shouldHide ? !!config.shouldHide(element, currentUser) : true;
                if (!shouldHide) {
                    markedElements.add(element);
                    return;
                }

                element.setAttribute('data-untrusted', 'true');
                element.setAttribute('data-untrusted-element', 'true');
                element.setAttribute('data-tag-name', config.tagName || 'element');

                // Strip title/aria-label so hover tooltips / a11y tree text
                // can't leak the untrusted content around the mask.
                ['title', 'aria-label'].forEach(function (attr) {
                    if (element.hasAttribute(attr)) element.removeAttribute(attr);
                    element.querySelectorAll('[' + attr + ']').forEach(function (child) {
                        child.removeAttribute(attr);
                    });
                });

                markedElements.add(element);
                markedCount++;
            });
        });

        return markedCount;
    }

    function setupObserver() {
        const observer = new MutationObserver(function (mutations) {
            let shouldMark = false;
            mutations.forEach(function (m) {
                if (m.addedNodes && m.addedNodes.length > 0) shouldMark = true;
            });
            if (shouldMark) applyMarkers();
        });
        observer.observe(document.documentElement, { childList: true, subtree: true });
    }

    function setupTooltipCleaner() {
        setInterval(function () {
            document.querySelectorAll('[data-untrusted="true"]').forEach(function (el) {
                if (el.hasAttribute('title')) el.removeAttribute('title');
                if (el.hasAttribute('aria-label')) el.removeAttribute('aria-label');
            });
        }, 1000);
    }

    window.__ucmApplyMarkers = function () {
        applyMarkers();
        setupObserver();
        setupTooltipCleaner();
        // Re-sweep periodically for markup that mutates without a childList
        // change the observer catches (framework re-renders in place).
        setInterval(applyMarkers, 1500);
    };
})();
