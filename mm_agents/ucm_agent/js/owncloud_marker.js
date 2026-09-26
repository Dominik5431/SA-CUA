// Untrusted-region marker for the TheAgentCompany_OwnCloud environment.
//
// *** BEST-EFFORT — NOT LIVE-VERIFIED. *** See the header comment in
// reddit_marker.js for the caveat and the recommended bootstrap workflow
// (run automatic_boundary_detection against this container before relying
// on this in an evaluation run). ownCloud's file-list markup has also
// changed materially across major versions (classic ownCloud 10.x vs. the
// Nextcloud-derived Files app), so this file targets both class-name
// families defensively.
//
// What must be covered (this is the actual injection surface —
// adv/TheAgentCompany_OwnCloud/injection.py uploads an attacker-controlled
// FILE into the file listing via `occ files:scan`):
//   - file/folder name in the file list
//   - file preview / rendered content pane
//   - share/comment/description text attached to a file

(function () {
    'use strict';

    function getCurrentUser() {
        const candidates = [
            document.querySelector('#user-menu .user-status-menu-name, .user-menu .user-status-menu-name'),
            document.querySelector('[data-user], #settings [data-user]'),
        ];
        for (const el of candidates) {
            if (el) {
                const v = (el.getAttribute && el.getAttribute('data-user')) || el.textContent.trim();
                if (v) return v;
            }
        }
        const body = document.body;
        if (body && body.dataset && body.dataset.user) return body.dataset.user;
        return null;
    }

    // Ownership is harder to establish generically for a file listing than
    // for a per-item author byline (ownCloud's default file list is scoped
    // to files the current user already owns/has-access-to, so anything
    // shown there is not automatically "someone else's" the way a Reddit
    // post is). The injected adversarial file is the concrete, known attack
    // surface here, so we mask ALL file/folder names, previews, and
    // comments unconditionally rather than trying to infer per-row
    // ownership — a conservative default that must be relaxed with a real
    // ownership check once the live DOM is inspected (e.g. an "owner"
    // column value, or a `data-share-owner` attribute) if that turns out to
    // over-mask benign, fully-trusted files in a given task.
    function alwaysHide() { return true; }

    window.__ucmCurrentUser = getCurrentUser;

    window.__ucmSiteSelectors = [
        // ---- File / folder name in the list (classic ownCloud 10.x) ----
        {
            selector: 'td.filename .nametext, #fileList .nametext, tr.file td.filename a .nametext',
            tagName: 'file-name',
            description: 'File/folder name in file list (classic ownCloud)',
            shouldHide: alwaysHide,
        },
        // ---- File / folder name (Nextcloud-derived Files app) ----
        {
            selector: '[data-cy-files-list-row-name] .files-list__row-name-text, .files-list__row-name-text, span[class*="row-name"]',
            tagName: 'file-name',
            description: 'File/folder name in file list (Nextcloud-style Files app)',
            shouldHide: alwaysHide,
        },
        // ---- File preview / rendered content pane ----
        {
            selector: '#viewer .viewer-content, .file-preview, #app-content .text-editor__wrapper, .pdf-viewer, .text-preview',
            tagName: 'file-content',
            description: 'File preview / content pane',
            shouldHide: alwaysHide,
            skipEmptyCheck: true,
        },
        // ---- Comments / share notes attached to a file ----
        {
            selector: '#commentsTabView .comment .message, .comments-panel .comment__message, [class*="comment"] [class*="message"]',
            tagName: 'file-comment',
            description: 'Comment/message attached to a file',
            shouldHide: alwaysHide,
        },
        // ---- Share dialog: target/recipient display name (could carry an
        // attacker-chosen display name if account creation isn't trusted) ----
        {
            selector: '.shareTargetName, .sharee-name, [class*="share"] [class*="name"]',
            tagName: 'share-name',
            description: 'Share dialog recipient/owner display name',
            shouldHide: alwaysHide,
        },
    ];

    function init() {
        if (typeof window.__ucmApplyMarkers === 'function') {
            window.__ucmApplyMarkers();
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
