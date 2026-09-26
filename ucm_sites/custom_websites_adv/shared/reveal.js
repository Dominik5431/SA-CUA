// Reveal/mask runtime shared by the 10 custom-built websites under
// environments/custom_websites/ (banking, calendar, email, forum, jobboard,
// restaurant, support, travel, webshop, wiki). Reveal happens only via the
// Q-Model tool call (programmatic) — clicks on a marked element forward to
// any underlying link/button but keep the content hidden.
// The WebArena GitLab proxy ships its own copy — see
// environments/webarena/gitlab-setup/nginx-files/.

(function() {
    'use strict';

    let config = {
        autoObserve: true,           // Automatically watch for dynamically added elements
        logEndpoint: '/logs/write',  // Where to send logs
        appName: 'app',              // Application identifier for qllm_id prefix
        publishElementMap: true      // Whether to publish element bounding boxes
    };

    let initialized = false;
    let elementMapPublished = false;

    // Initialize on DOM load
    document.addEventListener('DOMContentLoaded', function() {
        loadConfiguration();
        initializeRevealSystem();
        
        if (config.publishElementMap) {
            publishElementMapOnce();
        }
        
        // Set up MutationObserver for dynamic content if enabled
        if (config.autoObserve) {
            observeDynamicContent();
        }
        
        initialized = true;
    });

    /**
     * Load configuration from the data-reveal-config attribute on <body>.
     */
    function loadConfiguration() {
        const body = document.body;
        if (body && body.hasAttribute('data-reveal-config')) {
            try {
                const userConfig = JSON.parse(body.getAttribute('data-reveal-config'));
                config = Object.assign({}, config, userConfig);
            } catch (e) {
                console.warn('Invalid reveal config JSON:', e);
            }
        }
    }

    function initializeRevealSystem() {
        const urlParams = new URLSearchParams(window.location.search);
        // Reveal policy comes from data-reveal-config on <body> as well as the URL, so the
        // forked value-attack sites can bake revealAll:true (undefended baseline) without a
        // ?reveal_all=true query string cluttering the address bar. The URL still wins if set.
        const maskUntrusted = urlParams.get('mask_untrusted') === 'true' || config.maskUntrusted === true;
        const revealAll = urlParams.get('reveal_all') === 'true' || config.revealAll === true;
        
        const hiddenElements = document.querySelectorAll('[data-untrusted-element]');
        
        hiddenElements.forEach(function(element, idx) {
            // Skip if already initialized
            if (element.hasAttribute('data-reveal-initialized')) {
                return;
            }
            element.setAttribute('data-reveal-initialized', 'true');
            
            const isTrusted = element.hasAttribute('data-trusted') && element.getAttribute('data-trusted') === 'true';
            const isUntrusted = element.hasAttribute('data-untrusted') && element.getAttribute('data-untrusted') === 'true';
            const isQuarantineAccessible = element.hasAttribute('data-quarantine-accessible') && 
                                        element.getAttribute('data-quarantine-accessible') === 'true';
            
            // Get tag name and assign unique qllm-id
            const tagName = element.getAttribute('data-tag-name') || 
                        element.className.split(' ')[0] || 
                        element.id || 
                        'element';

            // Auto-generate qllm_id if not explicitly set
            if (!element.hasAttribute('data-qllm-id')) {
                element.setAttribute('data-qllm-id', `${tagName}:${idx}`);
            }
            
            // If reveal_all is true, reveal everything
            // Use setTimeout(0) to ensure SecurityTracker's DOMContentLoaded handler runs first
            if (revealAll) {
                // Set placeholder for logging purposes (SecurityTracker uses it)
                const qllmIdForPlaceholder = element.getAttribute('data-qllm-id');
                let placeholderText = `id: ${qllmIdForPlaceholder}`;
                if (isTrusted) {
                    placeholderText = `trusted: ${placeholderText}`;
                }
                if (!element.hasAttribute('data-reveal-placeholder') && !element.hasAttribute('data-reveal-hint')) {
                    element.setAttribute('data-reveal-placeholder', placeholderText);
                }
                // Delay slightly to ensure SecurityTracker event listener is set up
                setTimeout(() => {
                    revealElement(element);
                }, 0);
                return;
            }
            
            // If mask_untrusted is true, reveal only trusted elements
            // Quarantine-accessible elements stay hidden but registered
            if (maskUntrusted && isTrusted) {
                revealElement(element);
                return;
            }

            // Use data-qllm-id (includes element index) rather than data-tag-name
            // (shared across elements of the same type) so each element's reveal
            // state is tracked independently.
            const elementId = element.getAttribute('data-qllm-id');
            let wasRevealed = false;
            if (elementId && !revealAll) {
                try {
                    const revealedElements = JSON.parse(localStorage.getItem('revealedElements') || '[]');
                    wasRevealed = revealedElements.includes(elementId);
                } catch (e) {
                    console.warn('Could not read from localStorage:', e);
                }
            }
            
            if (wasRevealed) {
                revealElement(element);
                return;
            }
            
            // Mark as hidden
            element.classList.add('reveal-hidden')
            const qllmIdForPlaceholder = element.getAttribute('data-qllm-id');
            
            // Generate placeholder text - show element id directly
            let placeholderText = `id: ${qllmIdForPlaceholder}`;
            
            if (isTrusted) {
                placeholderText = `trusted: ${placeholderText}`;
            }

            if (!element.hasAttribute('data-reveal-placeholder') && !element.hasAttribute('data-reveal-hint')) {
                element.setAttribute('data-reveal-placeholder', placeholderText);
            }
            
            // Set up click handler
            const triggerSelector = element.getAttribute('data-reveal-trigger');
            const trigger = triggerSelector 
                ? document.querySelector(triggerSelector) 
                : element;
            
            if (trigger) {
                // DISABLED: Click-to-reveal is now disabled
                // Clicks forward to underlying links/buttons WITHOUT revealing content
                
                trigger.addEventListener('click', function(e) {
                    if (element.classList.contains('reveal-hidden')) {
                        // Find underlying link or button
                        const link = element.querySelector('a') || (element.tagName === 'A' ? element : null);
                        const button = element.querySelector('button') || (element.tagName === 'BUTTON' ? element : null);
                        
                        if (link && link.href) {
                            // Navigate to link WITHOUT revealing content
                            console.log('[Reveal] Forwarding click to link (content stays hidden):', link.href);
                            e.preventDefault();
                            e.stopPropagation();
                            window.location.href = link.href;
                        } else if (button) {
                            // Trigger button action WITHOUT revealing content
                            console.log('[Reveal] Forwarding click to button (content stays hidden)');
                            e.preventDefault();
                            e.stopPropagation();
                            button.click();
                        } else {
                            // No underlying clickable element, just prevent default
                            // Content stays masked
                            console.log('[Reveal] Click blocked - content stays hidden. Use reveal tool to reveal.');
                            e.preventDefault();
                            e.stopPropagation();
                        }
                        
                        // NEVER call revealElement() on click!
                    }
                    // If already revealed, let the click through normally
                });
                
                if (element.hasAttribute('data-reveal-hint')) {
                    addRevealHint(trigger, element.getAttribute('data-reveal-hint'));
                }
            }
        });
    }



    /**
     * MutationObserver that auto-initializes reveals on dynamically inserted
     * elements so app code doesn't have to re-init after every re-render.
     */
    function observeDynamicContent() {
        const observer = new MutationObserver(function(mutations) {
            let hasNewElements = false;
            
            mutations.forEach(function(mutation) {
                if (mutation.type === 'childList' && mutation.addedNodes.length > 0) {
                    mutation.addedNodes.forEach(function(node) {
                        if (node.nodeType === Node.ELEMENT_NODE) {
                            // Check if the node itself or any children have data-untrusted-element
                            if (node.hasAttribute && node.hasAttribute('data-untrusted-element')) {
                                hasNewElements = true;
                            }
                            if (node.querySelectorAll) {
                                const revealElements = node.querySelectorAll('[data-untrusted-element]');
                                if (revealElements.length > 0) {
                                    hasNewElements = true;
                                }
                            }
                        }
                    });
                }
            });
            
            // If new reveal elements were added, re-initialize
            if (hasNewElements) {
                initializeRevealSystem();
                
                // Optionally refresh element map
                if (config.publishElementMap) {
                    setTimeout(publishElementMap, 250);
                }
            }
        });
        
        observer.observe(document.body, {
            childList: true,
            subtree: true
        });
    }

    function publishElementMapOnce() {
        if (elementMapPublished) return;
        elementMapPublished = true;
        try {
            setTimeout(publishElementMap, 250);
            window.addEventListener('load', function() {
                setTimeout(publishElementMap, 250);
            }, { once: true });
        } catch (e) {}
    }

    function publishElementMap() {
        try {
            const elements = Array.from(document.querySelectorAll('[data-untrusted-element]'));
            const map = [];
            const viewportScreenX = (typeof window.mozInnerScreenX === 'number') ? window.mozInnerScreenX : 0;
            const viewportScreenY = (typeof window.mozInnerScreenY === 'number') ? window.mozInnerScreenY : 0;

            elements.forEach(function(el) {
                const rect = el.getBoundingClientRect();
                if (!rect || rect.width <= 0 || rect.height <= 0) return;

                const isTrusted = el.getAttribute('data-trusted') === 'true';
                const isUntrusted = el.getAttribute('data-untrusted') === 'true';
                const type = isUntrusted ? 'untrusted' : (isTrusted ? 'trusted' : 'unknown');
                const dataTagName = el.getAttribute('data-tag-name') || '';

                map.push({
                    type: type,
                    data_tag_name: dataTagName,
                    bbox: {
                        left: Math.round(viewportScreenX + rect.left),
                        top: Math.round(viewportScreenY + rect.top),
                        right: Math.round(viewportScreenX + rect.right),
                        bottom: Math.round(viewportScreenY + rect.bottom),
                    }
                });
            });

            fetch(config.logEndpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    timestamp: new Date().toISOString(),
                    action: 'element_map',
                    elements: map
                })
            }).catch(() => {});
        } catch (e) {
            // Best-effort only
        }
    }

    function revealElement(element) {
        element.classList.remove('reveal-hidden');
        element.classList.add('reveal-visible');
        element.removeAttribute('data-reveal-placeholder');
        
        const hints = element.querySelectorAll('.reveal-hint');
        hints.forEach(hint => hint.remove());
        
        // Save to localStorage
        const urlParams = new URLSearchParams(window.location.search);
        const revealAll = urlParams.get('reveal_all') === 'true';
        if (!revealAll) {
            // data-qllm-id is per-element; data-tag-name would collide across
            // elements of the same type.
            const elementId = element.getAttribute('data-qllm-id');
            if (elementId) {
                try {
                    const revealedElements = JSON.parse(localStorage.getItem('revealedElements') || '[]');
                    if (!revealedElements.includes(elementId)) {
                        revealedElements.push(elementId);
                        localStorage.setItem('revealedElements', JSON.stringify(revealedElements));
                    }
                } catch (e) {
                    console.warn('Could not save to localStorage:', e);
                }
            }
        }
        
        // Trigger custom event
        const event = new CustomEvent('elementRevealed', {
            detail: { element: element }
        });
        document.dispatchEvent(event);
        
        console.log('Element revealed:', element);
    }

    function addRevealHint(trigger, hintText) {
        const hint = document.createElement('div');
        hint.className = 'reveal-hint';
        hint.textContent = hintText;
        
        if (trigger) {
            trigger.style.position = 'relative';
            trigger.appendChild(hint);
        }
    }

    // Public API exposed to applications.
    window.RevealSystem = {
        reveal: function(selector) {
            const element = document.querySelector(selector);
            if (element && element.classList.contains('reveal-hidden')) {
                revealElement(element);
            }
        },
        hide: function(selector) {
            const element = document.querySelector(selector);
            if (element) {
                element.classList.add('reveal-hidden');
                element.classList.remove('reveal-visible');
            }
        },
        isRevealed: function(selector) {
            const element = document.querySelector(selector);
            return element && element.classList.contains('reveal-visible');
        },
        refreshElementMap: function() {
            if (config.publishElementMap) {
                publishElementMap();
            }
        },
        reinitialize: function() {
            initializeRevealSystem();
            if (config.publishElementMap) {
                setTimeout(publishElementMap, 250);
            }
        },
        getConfig: function() {
            return Object.assign({}, config);
        }
    };
})();