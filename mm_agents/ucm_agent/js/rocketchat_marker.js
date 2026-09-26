// Untrusted-region marker for the TheAgentCompany_RocketChat environment.
//
// *** BEST-EFFORT — NOT LIVE-VERIFIED. *** See the header comment in
// reddit_marker.js for the caveat and the recommended bootstrap workflow.
// RocketChat's class names are more stable across versions than
// Reddit/ownCloud's (both the classic and the newer Fuselage-based UI use
// fairly predictable `rcx-*` / `.message-*` naming), so this one is likely
// closer to correct out of the box, but still needs a live check.
//
// What must be covered (this is the actual injection surface —
// adv/TheAgentCompany_RocketChat/rocketchat_bot.py / run_multi_npc.py drive
// NPC-authored chat messages into channels/DMs the agent reads):
//   - message body text in channel/DM history
//   - sender display name / username on each message
//   - attached-file name shown inline in chat

(function () {
    'use strict';

    function getCurrentUser() {
        const candidates = [
            document.querySelector('[data-username]'),
            document.querySelector('.rcx-sidebar .rcx-avatar[title]'),
            document.querySelector('.avatar-image[title]'),
        ];
        for (const el of candidates) {
            if (el) {
                const v = el.getAttribute('data-username') || el.getAttribute('title');
                if (v) return v.replace('@', '').trim();
            }
        }
        // Account/settings menu button often shows the username as text.
        const accountBtn = document.querySelector('[aria-label="User"] , .sidebar__toolbar-user, .rcx-sidebar__account');
        if (accountBtn && accountBtn.textContent) {
            const t = accountBtn.textContent.trim();
            if (t) return t;
        }
        return null;
    }

    function usernameFromMessage(el) {
        const container = el.closest(
            '.message, [class*="message"][role="listitem"], li[class*="message"], .rcx-message'
        ) || el;
        const nameEl = container.querySelector(
            '.message-header .author, .rcx-message-header__name, [class*="message"] [class*="username"], .user-card-message'
        );
        if (nameEl) return nameEl.textContent.trim().replace('@', '');
        const dataUser = container.getAttribute && container.getAttribute('data-username');
        return dataUser || null;
    }

    function shouldHideByAuthor(el, currentUser) {
        if (!currentUser) return true;
        const author = usernameFromMessage(el);
        if (!author) return true;
        return author.toLowerCase() !== currentUser.toLowerCase();
    }

    window.__ucmCurrentUser = getCurrentUser;

    window.__ucmSiteSelectors = [
        // ---- Message body text ----
        {
            selector:
                '.message-body, .rcx-message-body, .rcx-message-body-wrapper, ' +
                '[class*="message"] [class*="body"], .body.color-primary-font-color',
            tagName: 'chat-message',
            description: 'Chat message body (channel/DM)',
            shouldHide: shouldHideByAuthor,
        },
        // ---- Sender display name / username on each message ----
        {
            selector:
                '.message-header .author, .rcx-message-header__name, ' +
                '[class*="message"] [class*="username"], .user-card-message',
            tagName: 'chat-username',
            description: 'Message sender display name/username',
            shouldHide: shouldHideByAuthor,
        },
        // ---- Attached file name shown inline in chat ----
        {
            selector:
                '.rcx-message-attachment .rcx-message-attachment__title, ' +
                '.attachment-title, [class*="attachment"] [class*="title"]',
            tagName: 'chat-attachment-name',
            description: 'Attached file name in a chat message',
            shouldHide: shouldHideByAuthor,
        },
        // ---- Channel/room topic or description (can be edited by any
        // member, so treat as untrusted like a message body) ----
        {
            selector: '.rcx-box--topic, [class*="header"] [class*="topic"], .room-header .subtitle',
            tagName: 'channel-topic',
            description: 'Channel topic/description text',
            shouldHide: function () { return true; },
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
