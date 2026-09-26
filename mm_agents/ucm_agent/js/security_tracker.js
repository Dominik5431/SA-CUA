// Reveal-event telemetry for ucm_agent.
//
// Adapted from the source UCM repo's
// environments/custom_websites/shared/security-tracker.js. That version
// POSTed every event to a `/logs/write` endpoint served by each demo site;
// RedTeamCUA's platforms have no such endpoint, so this version just keeps
// counters/records on `window.__ucmStats`, readable from Python via
// `browser_guard.py`'s `reveal_stats()` (a `page.evaluate()` call) so the
// episode's audit log (mirroring the source repo's security-tracker.js
// intent: "was untrusted content ever revealed before a sensitive action?")
// can be written into the RedTeamCUA results directory instead.

(function () {
    'use strict';

    if (window.__ucmTrackerInstalled) return;
    window.__ucmTrackerInstalled = true;

    window.__ucmStats = {
        revealedTrusted: 0,
        revealedUntrusted: 0,
        revealedElements: [],
    };

    document.addEventListener('ucm-element-revealed', function (e) {
        const element = e.detail.element;
        const isTrusted = element.getAttribute('data-trusted') === 'true';
        const isUntrusted = element.getAttribute('data-untrusted') === 'true';
        const record = {
            timestamp: new Date().toISOString(),
            type: isTrusted ? 'trusted' : (isUntrusted ? 'untrusted' : 'unknown'),
            qllm_id: element.getAttribute('data-qllm-id'),
            tag_name: element.getAttribute('data-tag-name'),
            url: window.location.href,
        };
        window.__ucmStats.revealedElements.push(record);
        if (isTrusted) window.__ucmStats.revealedTrusted++;
        else if (isUntrusted) window.__ucmStats.revealedUntrusted++;
    });
})();
