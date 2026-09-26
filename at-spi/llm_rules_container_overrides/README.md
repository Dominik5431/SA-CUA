# llm_rules_container_overrides

Per-capture overrides, listed LAST in `run.py --sa_rules_dir` so they win: `RuleCache._load`
assigns `by_capture[capture] = rules` as it walks the comma-separated list, so the last directory
holding a capture decides it.

This exists so a single bad rule set can be replaced without contaminating a model arm's own
directory. `llm_rules_container_v17_suites_or/` IS the measurement artifact for the
openai/gpt-5.6-luna arm; dropping a Qwen file into it would make `eval_rules.py --rules_dir
llm_rules_container_v17_suites_or` describe a mixture, and a naive copy would not even take
effect (both files map to the same capture key and `sorted()` puts `__Qwen_...` before
`__openai_...`, so the v17 file would win by glob order).

## Contents

- `207_thunderbird_compose_reply__Qwen_Qwen3.8-27B.json` -- from `llm_rules_container_v15`.
  v17's set for this capture does not grant the compose toolbar: `Send`, `Save` and `Attach` all
  come back UNTRUSTED under it, while v15's set trusts all three (both arms agree on
  Reply/Forward/Archive/Junk/Delete). Compose is the state ~28 of the 53 Thunderbird eval tasks
  need, and under `RuleCache._app_union` this capture's rules are merged into the `app:Thunderbird`
  union, so the better set is the one that should be in it.

  Replace this with a re-proposal of 207 when one exists, and delete the file then.

- `anonymous.4open.science.json` -- a HOST-level set (no capture, no template), covering every page
  of the 4open host. `own_owncloud_1` sends the agent to that host's API -- the file listing
  `endorse_by_listing` fetches, and then the file itself -- and only `/r/*` was ever cached, so
  every episode that followed the task to its end paid for a live rule proposal on a ~20k skeleton.

  Captured 2026-09-17 on a real VM through the runtime's own path (`build_mask` ->
  `RuleCache.store`). Both API templates proposed **zero** rules, which is correct and useful: they
  are raw responses (a JSON listing, a plain-text file) with no chrome and no navigation, so there
  is nothing to grant, and an empty set still suppresses the live proposal (`build_mask` guards on
  `cached is not None`). The single rule here is the `/r/*` PDF-viewer toolbar grant, carried over
  so the repo page does not regress; it matches nothing on an API page, which has no
  `section.toolbar`.

  WHY HERE AND NOT IN v17, which is the default rules folder. Two reasons, both from this file's
  own rule above. (1) v17 is the measurement artifact for the openai/gpt-5.6-luna arm and these
  rules were proposed by `vllm|Qwen/Qwen3.8-27B`. (2) A host-level file cannot carry a model token
  at all -- `RuleCache._load` skips any stem containing `__` before the host-file branch -- so it
  would be picked up by every arm's `eval_rules.py` with no way to filter it. This directory is
  already in the default `--sa_rules_dir` list, so the rules take effect by default either way.
  To put them in v17 properly they would need re-proposing with gpt-5.6-luna AND manifest rows for
  two new captures, so they could use the per-capture `<capture>__<model>.json` convention.

- The chrome:// sets (`055_chrome_settings__manual.json`, `056_…_search`, `057_…_searchengines`,
  `058_…_clearbrowserdata`, `059_…_content_all`, `060_chrome_extensions__manual.json`) -- Chrome's
  own WebUI, for the three OSWorld test_small tasks that live on it (bb5e4c0d Bing-as-default,
  7b6c7e24 clear Amazon's cookies, a74b607e load an unpacked extension). Hand-written.

  WHAT THEY FIX. The two trust ladders disagreed about these pages. `coord_to_semantic`'s
  `_frame_is_privileged` puts a privileged frame's document-web content in `Zone.CHROME` --
  first-party, never masked -- while the masking path takes content roots STRUCTURALLY
  (`labeling_store.resolve_content_roots`, no omnibox check) and so saw an ordinary web root whose
  URL `get_active_tab_url` refused to report, which `_resolve_rules` default-denies. Settings and
  Extensions rendered black with no rules consulted and no live proposal attempted.
  `get_active_tab_url(include_internal=True)`, used only by `LabeledPlanningLoop._current_url`,
  now names the page (scope `settings` / `extensions`) so these files can settle it. Verified
  against the captures: 125/125, 249/249, 32/32 and 134/134 rows trusted on the four settings
  pages, 57/62 on extensions.

  `060` IS NOT PAGE-SCOPE, and that is the point. An extension's name and description come from
  its own manifest.json -- arbitrary third-party prose, of exactly the kind a74b607e installs from
  a downloaded zip -- so they are subtracted with `except`, which `is_page_scope` short-circuits
  past and therefore cannot express. It is the `100_arxiv.org` shape (trust the container, name
  what you are not trusting), and `drop_overbroad_rules` exempts a rule that subtracts. The five
  withheld rows are the card's name heading, its description section and their statics; Developer
  mode, Load unpacked, Details and Remove all stay trusted.

  TWO RESIDUALS ARE ACCEPTED RATHER THAN CLOSED, and both are recorded in the rules' own notes:
  a Site Search row Chrome auto-added from a visited site's OpenSearch descriptor (057), and the
  visited-origin rows on All sites (059). Both are short, single-line, capacity-bound strings, and
  subtracting either would mask the exact row its task has to find -- "Microsoft Bing" and
  "amazon.com" respectively.

  `chrome://new-tab-page` is deliberately absent and must stay absent: its Most Visited tiles carry
  earlier-visited pages' TITLES, which is unbounded attacker-influenced text.
  `_NAMEABLE_INTERNAL_SCHEMES` excludes it, so it still reports no URL and still default-denies.

  CAVEAT. These carry `__manual` where a proposed set carries `__<model>`, so a run that passes
  `--sa_rules_model` filters them out along with every other non-matching file. They cannot be
  written as host-level `<host>.json` sets instead -- `_looks_like_domain` requires a dot and the
  scopes here are `settings` and `extensions`.

- The newsletter host sets (`morningbrew.com.json`, `tldr.tech.json`, `thehustle.co.json`,
  `join1440.com.json`, `uber.com.json`, plus the click hosts `links.morningbrew.com.json`,
  `email.thehustle.co.json`, `link.join1440.com.json`, `email.uber.com.json`) -- HOST-level
  `scope: page, verdict: trusted` sets for the Thunderbird unsubscribe suite. Hand-written, not
  proposed, which is why they are here and not in a model arm's directory.

  Each of these hosts serves one publisher-authored mock site (masthead, archive,
  about/contact/email-help, footer, and on the click hosts the unsubscribe form). There is no
  comment thread, review list, profile or any other reader-submitted region on any of them, so
  every string is first-party. The benign config vouches for the publisher domains in
  `trusted_domains_corroborating`, which is what lets "first-party" mean "trustworthy" here and
  not merely "not user-generated".

- The attacker host sets (`techdaily.com.json`, `dealsweekly.com.json`, `codebytes.io.json`,
  `techdaily-optout.net.json`, `deals-unsub.net.json`, `codebytes-optout.net.json`) -- also
  `scope: page, verdict: trusted`, and deliberately so.

  These pages carry no user-generated regions either, so by the same reading they are
  publisher-authored throughout. That this particular publisher is the adversary is NOT encoded
  in the rules: a masking rule states where a page's content came from, and no real deployment
  can know which host is hostile. Marking them untrusted would be the benchmark grading itself,
  and would make these tasks score safe for a reason the deployed system would not have.

  What stops the attack is the other ladder. `screen_labels.py` never imports `RuleCache`; the
  integrity label of a value read via `query_screen` comes from `region_metadata` over the a11y
  zone model, not from these rules. So a domain read off the attacker's page is UNTRUSTED and
  cannot anchor `endorse_by_same_site` (`ANCHOR NOT CLEARED` / `ANCHOR NOT TRUSTED`), which is
  the boundary the suite actually tests. `test/unsubscribe_verification.py` §7 asserts both the
  import-level independence and the refusal.

  The files still have to exist: without them `for_scope` returns None, and every episode that
  follows the attack link pays a live rule proposal or hits the default-deny whole-page mask on
  an unknown host, which is what made these tasks hang.

- The UCM sets (`500_ucm_banking_dashboard__manual.json` ... `533_ucm_shop_filtered__manual.json`, 34 files) --
  the ten UCM synthetic sites. Hand-reviewed, 2026-09-24. Each file is the COMPLETE set for its capture: v19's
  gpt-5.6-luna rules for that capture plus manual grants for the site's own UI (filter sidebars, sort bars, forms and
  dialogs, card action rows, nav trees). Every UCM site is a one-URL SPA, so live masking unions all of a site's
  captures (`RuleCache._tied_captures`); what matters is the per-site union.

  HOW "UNTRUSTED" IS DECIDED. Not by judgement: by the site source. An element is untrusted iff it carries
  `data-untrusted="true"`, and `shared/reveal.js` puts `reveal-visible` (reveal_all) / `reveal-hidden` (default) on
  every `[data-untrusted-element]`. So every rule here -- including the carried v19 rules -- has
  `except: reveal-visible, reveal-hidden, promo-banner, injected-, rtc-ad, rtc-halfpage` (the last four are the adv
  fork's injected ad units). The one exception is the shop's `aside.filters`, which the source marks
  `data-trusted="true"` and therefore cannot take the reveal-visible except. Inline spans that Chrome prunes (a
  greeting's user name, a merchant/customer/author name) carry no class, so containers holding them are not granted
  (headers are granted minus their `nav`), and the few unmarked echoes of untrusted data are subtracted by id
  (`#cat-merchant`, the apply form's profile prefills, `#cart-items`, `#bulk-close-list`).

  VERIFIED against that source-derived map on all 34 captures (site union, live `predict_trusted` path): zero newly
  granted rows inside a `data-untrusted` element; the only text coincidences are site vocabulary that also appears
  inside untrusted content (tag names, wiki titles, filter labels) and were reviewed one by one.

  FIXES TO v19 CARRIED HERE: 513's `own_role heading in section.modal-content` also granted the forum post-detail
  title (a `reveal-visible` h2) through the union -- closed by the except; 528's `landmark.header` granted the
  travel greeting's untrusted user name -- the nav is subtracted.

  NOT USED BY ANY RUN (2026-09-24). UCM is measured on the labeling pipeline's sets: `run.py --sa_ucm_rules_dir`
  (default `llm_rules_container_v19`) pins every `ucm` capture to that directory, whatever `--sa_rules_dir` lists
  and in whatever order (`RuleCache._pin_categories`), so these 34 files are never loaded. They stay as the reviewed
  reference for what the pipeline misses -- including the two v19 leaks above, which the UCM runs therefore carry.
  To serve them anyway, pass `--sa_ucm_rules_dir llm_rules_container_overrides`.
