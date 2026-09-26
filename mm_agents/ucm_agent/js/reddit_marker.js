// Untrusted-region marker for the WebArena/RTC-Bench Reddit environment
// (a Postmill-based forum clone).
//
// *** BEST-EFFORT — NOT LIVE-VERIFIED. ***
// Written from general knowledge of Postmill's Twig template class names,
// not from inspecting the actual running container. Per
// UCM_AGENT_INTEGRATION_PLAN.md §6, the recommended way to firm this up is
// to run the source UCM repo's src/automatic_boundary_detection/ pipeline
// against a page snapshot of this Reddit instance to get an LLM-proposed
// selector list, then hand-verify against the live container the same way
// that pipeline's hand_labels.json files are curated for its own three
// sites. Until that pass happens, treat this file as a first draft: check
// every selector against real rendered HTML (e.g. via ucm_agent's own
// get_html()) before trusting it in an evaluation run.
//
// What must be covered (this is the actual injection surface —
// adv/WebArena_Reddit/injection.py writes arbitrary text into arbitrary
// DB columns, in practice post titles/bodies and comment bodies):
//   - post title, post self-text body
//   - comment body (all nesting levels)
//   - author/username display anywhere it isn't the logged-in user

(function () {
    'use strict';

    function getCurrentUser() {
        // Try a few plausible places Postmill surfaces the logged-in user.
        const candidates = [
            document.querySelector('meta[name="current-user"]'),
            document.querySelector('[data-current-user]'),
        ];
        for (const el of candidates) {
            if (el) {
                const v = el.getAttribute('content') || el.getAttribute('data-current-user');
                if (v) return v.replace(/^\/?u\//, '').replace(/^\/?user\//, '');
            }
        }
        // Header account menu is usually a link to the user's own profile,
        // e.g. <a class="header__account" href="/user/MarvelsGrantMan136">.
        const accountLink = document.querySelector(
            '.header__account, .header-account, a[href^="/user/"].header__account, ' +
            'nav a[href^="/user/"]:first-of-type'
        );
        if (accountLink && accountLink.getAttribute('href')) {
            const m = accountLink.getAttribute('href').match(/\/user\/([^\/?#]+)/);
            if (m) return m[1];
        }
        return null;
    }

    function authorFromElement(el) {
        // Look for the nearest author link within this post/comment.
        const container = el.closest('.submission, .comment, article, [class*="submission"], [class*="comment"]') || el;
        const authorLink = container.querySelector(
            'a[href^="/user/"], .submission__author, .comment__author, [class*="author"] a'
        );
        if (authorLink) {
            const href = authorLink.getAttribute('href') || '';
            const m = href.match(/\/user\/([^\/?#]+)/);
            if (m) return m[1];
            return authorLink.textContent.trim();
        }
        return null;
    }

    function shouldHideByAuthor(el, currentUser) {
        if (!currentUser) return true; // can't determine own identity -> hide to be safe
        const author = authorFromElement(el);
        if (!author) return true;
        return author.toLowerCase() !== currentUser.toLowerCase();
    }

    window.__ucmCurrentUser = getCurrentUser;

    window.__ucmSiteSelectors = [
        // ---- Post title ----
        {
            selector: '.submission__title, .submission-title, article h1, [class*="submission"] h1, [class*="submission"] a[class*="title"]',
            tagName: 'post-title',
            description: 'Post/submission title',
            shouldHide: shouldHideByAuthor,
        },
        // ---- Post self-text body ----
        {
            selector: '.submission__body, .submission-body, [class*="submission"] [class*="body"], article .md, article .content',
            tagName: 'post-body',
            description: 'Post/submission self-text body',
            shouldHide: shouldHideByAuthor,
        },
        // ---- Comment body (all nesting levels) ----
        {
            selector: '.comment__body, .comment-body, [class*="comment"] [class*="body"], .comment .md',
            tagName: 'comment-body',
            description: 'Comment body text',
            shouldHide: shouldHideByAuthor,
        },
        // ---- Author / username display ----
        {
            selector: 'a[href^="/user/"], .submission__author, .comment__author, [class*="author"] a, [class*="author"]',
            tagName: 'username',
            description: 'Author/username link',
            shouldHide: function (el, currentUser) {
                if (!currentUser) return true;
                const href = el.getAttribute('href') || '';
                const m = href.match(/\/user\/([^\/?#]+)/);
                const name = m ? m[1] : el.textContent.trim().replace(/^u\//, '');
                if (!name) return true;
                return name.toLowerCase() !== currentUser.toLowerCase();
            },
        },
        // ---- Forum/community name (only relevant if tasks care about
        // community descriptions being attacker-influenced; harmless to mask) ----
        {
            selector: '.submission__forum, .forum-name, [class*="forum"] a',
            tagName: 'forum-name',
            description: 'Forum/community name link',
            shouldHide: function () { return false; }, // trusted by default: platform-owned, not user content
            skipEmptyCheck: true,
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
