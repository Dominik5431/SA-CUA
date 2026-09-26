// Reveal/mask runtime for ucm_agent (RedTeamCUA).
//
// Adapted from the source UCM repo's
// environments/custom_websites/shared/reveal.js. Differences from that
// version:
//   - No `data-reveal-config` / URL-param (`?mask_untrusted=true`) switch —
//     this runtime always runs in masked mode (RedTeamCUA has no "reveal_all"
//     baseline condition; that's just "don't inject this file").
//   - No networked element-map / log publishing (fetch()/sendBeacon() to a
//     log server) — this environment has no such endpoint. State is exposed
//     on `window.__ucmStats` / `window.__ucmElementMap()` instead, for the
//     Python side to read back via a CDP `page.evaluate()` call if needed.
//   - Injected once per page load via Playwright's `context.add_init_script`
//     (new navigations) plus an explicit `page.evaluate()` for the page
//     already open when ucm_agent attaches (see browser_guard.py).
//
// A companion "marker" script (reddit_marker.js / owncloud_marker.js /
// rocketchat_marker.js) must run BEFORE this file and is responsible for
// setting `data-untrusted="true"`, `data-untrusted-element="true"`, and
// `data-tag-name="..."` on every untrusted DOM region — this file only
// consumes those attributes to mask/placeholder them.

(function () {
    'use strict';

    if (window.__ucmRevealInstalled) return;
    window.__ucmRevealInstalled = true;

    function revealElement(element) {
        element.classList.remove('ucm-reveal-hidden');
        element.classList.add('ucm-reveal-visible');
        element.removeAttribute('data-reveal-placeholder');

        if (!window.__ucmRevealAll) {
            const elementId = element.getAttribute('data-qllm-id');
            if (elementId) {
                try {
                    const revealed = JSON.parse(localStorage.getItem('__ucmRevealedElements') || '[]');
                    if (!revealed.includes(elementId)) {
                        revealed.push(elementId);
                        localStorage.setItem('__ucmRevealedElements', JSON.stringify(revealed));
                    }
                } catch (e) { /* best-effort only */ }
            }
        }

        document.dispatchEvent(new CustomEvent('ucm-element-revealed', { detail: { element: element } }));
    }

    function initializeRevealSystem() {
        const hiddenElements = document.querySelectorAll('[data-untrusted-element]');

        hiddenElements.forEach(function (element, idx) {
            if (element.hasAttribute('data-reveal-initialized')) return;
            element.setAttribute('data-reveal-initialized', 'true');

            const isTrusted = element.getAttribute('data-trusted') === 'true';
            const tagName = element.getAttribute('data-tag-name') ||
                (element.className && element.className.split ? element.className.split(' ')[0] : '') ||
                element.id || 'element';

            if (!element.hasAttribute('data-qllm-id')) {
                element.setAttribute('data-qllm-id', `${tagName}:${idx}`);
            }
            const qllmId = element.getAttribute('data-qllm-id');

            if (window.__ucmRevealAll) {
                revealElement(element);
                return;
            }

            // Trusted elements are always shown (they are still tagged so the
            // Q-Model / bookkeeping can see them, but they are not attacker
            // content and never need to go through the quarantine).
            if (isTrusted) {
                revealElement(element);
                return;
            }

            let wasRevealed = false;
            try {
                const revealed = JSON.parse(localStorage.getItem('__ucmRevealedElements') || '[]');
                wasRevealed = revealed.includes(qllmId);
            } catch (e) { /* ignore */ }
            if (wasRevealed) {
                revealElement(element);
                return;
            }

            element.classList.add('ucm-reveal-hidden');
            if (!element.hasAttribute('data-reveal-placeholder')) {
                element.setAttribute('data-reveal-placeholder', `id: ${qllmId}`);
            }

            // Clicking a masked element forwards the click to any underlying
            // link/button (so navigation/actions still work) WITHOUT ever
            // revealing the hidden text. This is enforced here in JS, not
            // just by prompting the agent.
            element.addEventListener('click', function (e) {
                if (!element.classList.contains('ucm-reveal-hidden')) return; // already revealed, let it through
                const link = element.querySelector('a[href]') || (element.tagName === 'A' ? element : null);
                const button = element.querySelector('button') || (element.tagName === 'BUTTON' ? element : null);
                if (link && link.href) {
                    e.preventDefault();
                    e.stopPropagation();
                    window.location.href = link.href;
                } else if (button) {
                    e.preventDefault();
                    e.stopPropagation();
                    button.click();
                } else {
                    e.preventDefault();
                    e.stopPropagation();
                }
                // NEVER call revealElement() here.
            }, true);
        });
    }

    function installStyles() {
        if (document.getElementById('ucm-reveal-style')) return;
        const style = document.createElement('style');
        style.id = 'ucm-reveal-style';
        style.textContent = `
            .ucm-reveal-hidden { position: relative !important; overflow: hidden !important; min-height: 24px; min-width: 60px; display: inline-block; border-left: 3px solid #e74c3c !important; }
            .ucm-reveal-hidden > * { visibility: hidden !important; }
            .ucm-reveal-hidden[data-reveal-placeholder]::before {
                content: attr(data-reveal-placeholder);
                position: absolute; inset: 0; z-index: 2147483000;
                display: flex; align-items: center; justify-content: center;
                background: #ecf0f1; border: 1px solid #b8c2cc; border-radius: 4px;
                color: #1f2328; font-size: 11px; line-height: 1.3; padding: 2px 4px;
                white-space: pre-line; word-break: break-all; pointer-events: auto;
            }
            .ucm-reveal-visible { border-left: 3px solid #28a745 !important; }
        `;
        (document.head || document.documentElement).appendChild(style);
    }

    function observeDynamicContent() {
        const observer = new MutationObserver(function (mutations) {
            let hasNew = false;
            mutations.forEach(function (m) {
                if (m.type !== 'childList') return;
                m.addedNodes.forEach(function (node) {
                    if (node.nodeType !== Node.ELEMENT_NODE) return;
                    if (node.hasAttribute && node.hasAttribute('data-untrusted-element')) hasNew = true;
                    if (node.querySelectorAll && node.querySelectorAll('[data-untrusted-element]').length > 0) hasNew = true;
                });
            });
            if (hasNew) initializeRevealSystem();
        });
        observer.observe(document.documentElement, { childList: true, subtree: true });
    }

    function run() {
        installStyles();
        initializeRevealSystem();
        observeDynamicContent();
        // Re-sweep periodically too — cheap insurance against mutations the
        // observer's childList-only scope misses (e.g. attribute-only
        // re-renders in some SPA frameworks that replace innerHTML without
        // triggering a childList mutation on an already-observed ancestor).
        setInterval(initializeRevealSystem, 1500);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', run);
    } else {
        run();
    }

    window.__ucmElementMap = function () {
        return Array.from(document.querySelectorAll('[data-untrusted-element]')).map(function (el) {
            return {
                qllm_id: el.getAttribute('data-qllm-id'),
                tag_name: el.getAttribute('data-tag-name'),
                untrusted: el.getAttribute('data-untrusted') === 'true',
                trusted: el.getAttribute('data-trusted') === 'true',
                revealed: el.classList.contains('ucm-reveal-visible'),
            };
        });
    };
})();
