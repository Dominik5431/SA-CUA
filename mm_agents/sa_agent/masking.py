"""masking.py -- masked-screenshot exposure for the SA-Agent planner.

WHAT THIS FILE IS, AND WHAT IT DELIBERATELY IS NOT.

It is an ADAPTER. Every algorithm it relies on -- how a page's trusted regions are decided, how a
screenshot is redacted, how masked areas are grouped into numbered query regions -- lives in
`at-spi/` and is imported, never copied. The rule is deliberate: `at-spi/` is where that work is
developed and evaluated, and a change made there (a new guardrail in `predict_trusted`, a fix to
`_subtree_box`, a different region-split threshold) must reach SA-Agent runs with nobody
re-porting anything. A second implementation that agrees today is a defense that silently stops
agreeing later.

So this module holds exactly four things:
  1. the sys.path shim that makes `at-spi/` importable (it has a hyphen and no __init__.py),
  2. marshalling from a live observation to the arguments those functions already take,
  3. the rule cache, keyed by (scope, template) -- see RuleCache and at-spi/rule_scope.py,
  4. the Mask object the policy gate and the loop consume.

If trust logic or geometry starts accumulating here, the seam was cut in the wrong place: push it
back into at-spi/ and call it.

THE PIPELINE, in one paragraph. Phase 1 (offline, `at-spi/labelers/selector_propose.py`) has an LLM
propose a small reusable rule set naming a site's TRUSTED regions, from a text-free structural
skeleton of its a11y tree. Phase 2 (here, every turn) runs `predict_trusted()` -- pure, deterministic,
no LLM -- over the live tree to get a per-element verdict, renders the redacted screenshot through
`mask_screenshot.mask_image()`, and returns the boolean mask behind it. The planner sees the picture;
the policy gate reads the mask. Because both come from one call, "what is hidden" and "what is
refused" are the same pixels by construction rather than by agreement.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from io import BytesIO

logger = logging.getLogger("desktopenv.sa.masking")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AT_SPI = os.path.join(_REPO_ROOT, "at-spi")


def _ensure_at_spi_importable() -> None:
    """The same sys.path shim every module inside at-spi/ already uses on itself.

    `at-spi` contains a hyphen and carries no top-level __init__.py, so it is not importable as a
    package; its modules import each other flat and insert both directories on the path. We follow
    that convention rather than inventing a second one -- if the directory is ever renamed to
    `at_spi/` with an __init__.py, this collapses to a plain import and nothing else here moves.

    NOTE ON CYCLES. at-spi/extraction.py imports mm_agents.sa_agent.screen_labels, and three
    labelers import mm_agents.sa_agent.llm, so the dependency already runs at-spi -> sa_agent.
    This module closing the loop the other way is a DIAMOND, not a cycle, for exactly one reason:
    mm_agents/sa_agent/__init__.py re-exports nothing. Adding a single convenience import there
    turns this into a genuine circular import whose traceback will point at extraction.py rather
    than at the line that caused it. See the warning in that __init__.py.
    """
    for path in (_AT_SPI, os.path.join(_AT_SPI, "labelers")):
        if path not in sys.path:
            sys.path.insert(0, path)



class RuleCache:
    """Phase-1 rule sets, keyed by (scope, template), loaded once per episode.

    A Phase-1 run writes its proposed rules with `--out_dir`, one file per capture named
    `<capture>__<model>.json` holding a plain JSON list of rules (see at-spi/llm_rules_container_v*/).
    Captures map to a scope and a template through at-spi/manifest.json, via `rule_scope`. This
    class is that lookup and nothing more.

    WHY THE KEY IS NOT THE MANIFEST'S `domain` FIELD. It used to be, and for the four self-hosted
    eval platforms that field is a PATH SLUG rather than a hostname --
    `reddit_f_coolgithubprojects_103666_i_made_a_browser_ext`, not `reddit.com`. The live side
    looks up by the hostname it reads from the omnibox, so those 37 scored rule sets could never
    be found and every RTC-Bench episode fell through to live proposal without a word about it.
    `rule_scope` derives both halves from each row's `url`, which is the field that carries the
    identity, and answers the same two questions offline and live.

    WHY RULE SETS ARE STILL NEVER MERGED. Several captures of one site are common (the corpus has
    eight en.wikipedia.org pages), and concatenating their rules looks harmless because every rule
    is a scoped grant and `predict_trusted` returns on the first match. It is not harmless, and
    that is now measured rather than argued: scoring each of the 92 corpus captures that share a
    (scope, template) group against its template-mates' rules instead of its own moves `fn_free`
    -- missed untrusted FREE-TEXT elements, the leak this system exists to prevent -- from 8 to 23.
    The 15 extra leaks buy 1614 -> 2730 usable elements, which is not a trade this defense is
    allowed to make. So one capture's set is SELECTED, never merged; `rule_scope.select_capture`
    ranks the candidates and reports how it matched, and `capture_pref` overrides it outright.
    """

    def __init__(self, rules_dir: str | None = None, model: str | None = None,
                 capture_pref: str | None = None, cache_dir: str | None = None,
                 strict: bool = True, category_dirs: dict[str, str] | None = None):
        self.rules_dir = rules_dir
        self.category_dirs = {c: d for c, d in (category_dirs or {}).items() if d}
        self.model = model
        self.capture_pref = capture_pref
        self.cache_dir = cache_dir
        self.strict = strict
        self._by_scope: dict[str, dict[str, list]] = {}
        self._index: dict[str, list] = {}
        self._generated: dict[tuple, list] = {}
        self._proposed: dict[tuple, list] = {}
        self._loaded = False


    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.rules_dir:
            logger.warning("[mask] no --sa_rules_dir given -- no reviewed rule sets")
            self._load_generated()
            return
        _ensure_at_spi_importable()
        import glob

        import rule_scope as rsc

        try:
            import labeling_store as ls
            manifest = ls.load_manifest()
        except Exception as e:
            logger.warning("[mask] manifest unavailable (%s) -- cannot map captures to scopes", e)
            manifest = {}
        self._index = rsc.index_manifest(manifest)

        by_capture: dict[str, list] = {}
        direct: dict[str, list] = {}
        for raw in [d.strip() for d in self.rules_dir.split(",") if d.strip()]:
            base = raw if os.path.isabs(raw) else os.path.join(_AT_SPI, raw)
            if not os.path.isdir(base):
                available = sorted(
                    d for d in os.listdir(_AT_SPI)
                    if d.startswith("llm_rules") and os.path.isdir(os.path.join(_AT_SPI, d))
                ) if os.path.isdir(_AT_SPI) else []
                msg = (f"--sa_rules_dir names {base}, which does not exist. Masking would then "
                       f"run with NO reviewed rules, which is indistinguishable downstream from a "
                       f"site that legitimately grants nothing. Rule directories present: "
                       f"{', '.join(available) or '(none)'}.")
                if self.strict:
                    raise MaskConfigError(msg)
                logger.warning("[mask] %s -- skipped", msg)
                continue
            for path in sorted(glob.glob(os.path.join(base, "*.json"))):
                name = os.path.basename(path)
                if name.endswith((".meta.json", ".proposed.json")):
                    continue
                stem = name[:-5]
                capture = stem.rpartition("__")[0] + ".xml" if "__" in stem else stem + ".xml"
                if capture in manifest:
                    if (self.model and stem.rpartition("__")[2] != "manual"
                            and self.model.replace("/", "_") not in name):
                        continue
                    rules = self._read(path)
                    if rules is not None:
                        by_capture[capture] = rules
                    continue
                if "__" in stem or not _looks_like_domain(stem):
                    continue
                rules = self._read(path)
                if rules is not None:
                    direct[rsc.normalize_host(stem) or stem] = rules

        self._pin_categories(manifest, by_capture)

        for scope, refs in self._index.items():
            got = {r.capture: by_capture[r.capture] for r in refs if r.capture in by_capture}
            if got:
                self._by_scope[scope] = got
        for host, rules in direct.items():
            self._by_scope.setdefault(host, {})[f"<{host}>"] = rules
            self._index.setdefault(host, [])

        n_sets = sum(len(v) for v in self._by_scope.values())
        logger.info("[mask] loaded %d reviewed rule set(s) across %d scope(s) from %s",
                    n_sets, len(self._by_scope), self.rules_dir)
        self._load_generated()

    def _pin_categories(self, manifest: dict, by_capture: dict) -> None:
        """Replace every pinned category's rule sets with the ones in its own directory.

        WHY. The per-capture "last directory wins" walk makes a category's rules depend on how a
        launch happens to ORDER --sa_rules_dir, and for UCM the order has already flipped twice:
        `llm_rules_container_overrides/` carries hand-edited `5xx_ucm_*__manual.json` sets for the
        same 34 captures v19's labeling pipeline proposed, so `...,overrides,v19` served the
        pipeline's sets and `...,v19,overrides` the hand-edited ones, with nothing in the run to
        say which. A pin makes it a property of the category: UCM is measured on
        `--sa_ucm_rules_dir` (default llm_rules_container_v19), and live masking unions each
        site's captures from those sets (_tied_captures -- every UCM site is a one-URL SPA).

        A pinned capture the directory has no file for keeps NO rule set rather than falling back
        to another directory's: a fallback would silently mix provenances again. The model filter
        (--sa_rules_model) is not applied -- the pin names the set explicitly, and filtering a
        luna-proposed v19 out of a Qwen run would leave UCM with no rules at all.
        """
        for category, raw in self.category_dirs.items():
            base = raw if os.path.isabs(raw) else os.path.join(_AT_SPI, raw)
            if not os.path.isdir(base):
                msg = f"--sa_{category}_rules_dir names {base}, which does not exist."
                if self.strict:
                    raise MaskConfigError(msg)
                logger.warning("[mask] %s -- %s captures not pinned", msg, category)
                continue
            caps = {f for f, row in manifest.items() if row.get("category") == category}
            displaced = sum(c in by_capture for c in caps)
            for c in caps:
                by_capture.pop(c, None)
            import glob
            for path in sorted(glob.glob(os.path.join(base, "*.json"))):
                name = os.path.basename(path)
                if name.endswith((".meta.json", ".proposed.json")) or "__" not in name:
                    continue
                capture = name[:-5].rpartition("__")[0] + ".xml"
                if capture in caps:
                    rules = self._read(path)
                    if rules is not None:
                        by_capture[capture] = rules
            pinned = sum(c in by_capture for c in caps)
            logger.info("[mask] %s: %d/%d capture(s) pinned to %s (replacing %d set(s) from "
                        "--sa_rules_dir)", category, pinned, len(caps), raw, displaced)

    def _read(self, path: str) -> "list | None":
        try:
            with open(path, encoding="utf-8") as f:
                rules = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning("[mask] could not read %s (%s) -- skipped", path, e)
            return None
        return rules if isinstance(rules, list) else None

    def _load_generated(self) -> None:
        """The runtime-generated tier: `<cache_dir>/<scope>__<template>.json`, written by store().

        Same on-disk format as `selector_propose.py --out_dir` (a bare JSON list of rules), so a
        cached set can be scored, hand-edited, or promoted into --sa_rules_dir by moving the
        file. Provenance sits beside it in a `.meta.json`, which is skipped by name and is also
        where the un-slugged scope and template are recorded -- the filename is a slug and cannot
        be parsed back into them reliably.
        """
        if not self.cache_dir:
            return
        base = self.cache_dir if os.path.isabs(self.cache_dir) else os.path.join(_AT_SPI, self.cache_dir)
        if not os.path.isdir(base):
            return
        import glob
        for path in sorted(glob.glob(os.path.join(base, "*.meta.json"))):
            try:
                with open(path, encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError) as e:
                logger.warning("[mask] could not read cache metadata %s (%s) -- skipped", path, e)
                continue
            scope, template = meta.get("scope"), meta.get("template")
            if not scope:
                continue
            rules = self._read(path[: -len(".meta.json")] + ".json")
            if rules is not None:
                self._generated[(scope, template)] = rules
        if self._generated:
            logger.info("[mask] loaded %d generated rule set(s) from %s",
                        len(self._generated), base)


    def for_scope(self, scope: str | None, url: str | None) -> tuple["list | None", str, str]:
        """The rule set for this page: (rules, capture, how). `rules` is None when nothing applies.

        None is NOT an empty rule set. An empty list means "a proposer ran and granted nothing",
        which legitimately masks the page; None means "we have never looked at this page", which
        the caller must decide about explicitly -- propose live, or fall to on_unknown_domain.

        Reviewed sets (--sa_rules_dir) beat generated ones (--sa_rules_cache_dir): a scored or
        hand-checked rule set is evidence, a cached proposal is a shortcut, and the shortcut must
        never quietly replace the evidence.
        """
        self._load()
        if not scope:
            return None, "", ""
        _ensure_at_spi_importable()
        import rule_scope as rsc

        sets = self._by_scope.get(scope)
        if sets:
            refs = [r for r in self._index.get(scope, []) if r.capture in sets]
            if self.capture_pref:
                pref = [r for r in refs if r.capture.startswith(self.capture_pref)]
                if pref:
                    return sets[pref[0].capture], pref[0].capture, "capture_pref"
            if len(refs) > 1 and rsc.is_app_scope(scope):
                return self._app_union(scope, refs, sets)
            if refs:
                ref, how = rsc.select_capture(refs, url)
                if ref is not None:
                    ties = self._tied_captures(rsc, refs, ref, how)
                    if len(ties) > 1:
                        return self._app_union(scope, ties, sets)
                    return sets[ref.capture], ref.capture, how
            capture = sorted(sets)[0]
            return sets[capture], capture, rsc.MATCH_DEFAULT

        template = rsc.routing_template(url) if url else None
        if (scope, template) in self._generated:
            return self._generated[(scope, template)], f"<generated {template or 'scope'}>", "generated"
        loose = rsc.loose_template(rsc.path_template_of(template))
        for (s, t), rules in self._generated.items():
            if s == scope and t is not None and rsc.loose_template(rsc.path_template_of(t)) == loose:
                return rules, f"<generated {t}>", "generated_loose"
        if (scope, None) in self._generated:
            return self._generated[(scope, None)], "<generated scope>", "generated"
        return None, "", ""

    @staticmethod
    def _tied_captures(rsc, refs: list, ref, how: str) -> list:
        """The captures that match `url` exactly as well as the one select_capture picked.

        select_capture breaks every tie on the lowest capture index. Between two DIFFERENT pages
        (reddit's listing vs a post) that never happens -- their paths/templates differ, so one wins
        outright and the template routing is untouched. It happens where the URL cannot tell the
        captures apart at all: a single-page app (every UCM site) serves inbox, starred, message and
        reply dialog from ONE path, so all of them tie on `exact_path` and the lowest index silently
        won -- webmail was graded on the inbox capture's 5 rules in every state, and the folder list,
        message actions and reply form the other captures' rules grant were never applied. Those
        ties are one skeleton in different states, the application case _app_union already merges.

        ONLY an exact-path tie is widened -- the one case where the URL genuinely cannot tell the
        captures apart. A TEMPLATE tie is two different pages of one kind (two reddit posts, two
        GitLab issues): the template router already picks a same-skeleton set for them, and their
        reviewed eval-platform rules stay exactly as they were. Every weaker tier stays single too.

        AND ONLY BETWEEN RECORDED VIEWS of one application: every tied capture must carry the
        manifest's `view` (suites.py captures an app's states that way -- inbox, starred, reply
        dialog). Two captures of one ordinary page that merely share a path (booking.com's
        searchresults.html for Paris and for Rome, two Wikipedia snapshots) are different content,
        not states of one skeleton, and merging them was measured to leak: 012_en.wikipedia.org
        went from 0 to 1 free-text untrusted row shown (2026-09-24).
        """
        if how == rsc.MATCH_EXACT and ref.view:
            want = ((ref.path.rstrip("/") or "/"), ref.qid)
            ties = [r for r in refs if not r.is_overlay
                    and ((r.path.rstrip("/") or "/"), r.qid) == want]
            if all(r.view for r in ties):
                return ties
        return [ref]

    def _app_union(self, scope: str, refs: list, sets: dict) -> tuple[list, str, str]:
        """Every capture's rules for one APPLICATION, merged. The one exception to "never merge".

        WHY AN EXCEPTION EXISTS AT ALL. A native turn has no URL, so `select_capture` cannot ask
        the template question and short-circuits to the lowest-index capture for the scope. That
        is not a tie-break, it is a silent loss: the eight Thunderbird captures collapse to
        `200_thunderbird_inbox`, which was taken with NO MESSAGE SELECTED -- its right-hand pane
        is Thunderbird's "Welcome to Freedom" splash, `grep 'name="Reply"'` finds nothing in it,
        and three of its rules describe the chat tab's "No accounts configured" placeholder. So
        every message the agent opens is graded by a rule set proposed on a screen the evaluation
        never displays, and Reply/Forward/Archive/Junk/Delete are masked in every episode. Same
        shape on `app:soffice`, where Calc and Impress are graded by Writer's rules.

        WHY THE UNION AND NOT A DISCRIMINATOR. The obvious live signal is the window title, and on
        a compose window that title is `Write: Re: <subject>` -- the subject comes from the
        attacker's email. Selecting a rule set by it would let the attacker choose which rules
        apply, which is worse than the problem. A structural signature (a hash of the container
        ids present) is not attacker-controlled but matches nothing on a state the corpus does not
        hold, which is where we started.

        WHY IT IS SAFE, MEASURED RATHER THAN ARGUED. The 8 -> 23 leak that `rule_scope` records
        against merging is across different SITES and PAGE TYPES, where each template emits its own
        class vocabulary and a foreign rule lands on unrelated content. An application is one
        widget tree with stable ids (`spacesToolbar`, `messagePane`, `folderPaneHeaderBar`); a
        state change reveals or hides subtrees, it does not rename them. The geometric propagation
        gate then makes the union self-limiting: a rule naming a container that is not drawn on
        this screen grants nothing, so the compose-window rules are inert until a compose window
        exists. Scored over all 8 multi-capture app scopes (40 captures, v15, allowlist):

            arm                     leak   FN    FP      usable rows
            each capture's own set     0    0   76.8%           539     <- unreachable ideal
            UNION (this function)      0    4   59.0%           950
            lowest-index capture       0    4   85.2%           343     <- the old behaviour

        No union introduces a leak on any app scope, the 4 FN are capacity-bound and are already
        present in the old behaviour, and the union beats even the per-capture oracle on
        over-masking -- each state's proposer only ever saw part of the chrome, and the union
        restores what the others missed.

        THE STANDING OBLIGATION. A union is monotone in grants: it can only ever trust more. leak=0
        above is a measurement over 40 captures, not a proof, so a new app state added to the
        corpus must be re-measured before it is trusted here. WEB scopes keep `select_capture`,
        which has a real URL to answer the template question with, and are merged ONLY across the
        captures that TIE for that URL (see _tied_captures) -- a single-page app whose every state
        lives at one path, where the router has nothing to choose between.

        Ordered by manifest index and deduplicated on everything but `note`, so the result is
        reproducible across runs and machines rather than depending on directory order.
        """
        merged: list = []
        seen: set = set()
        for r in refs:
            for rule in sets[r.capture]:
                key = json.dumps({k: v for k, v in rule.items() if k != "note"}, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(rule)
        logger.info("[mask] %s: union of %d capture(s), %d rule(s)", scope, len(refs), len(merged))
        return merged, f"<union of {len(refs)}>", "app_union"

    def proposed_for(self, scope: str | None, template: str | None) -> "list | None":
        """A rule set proposed earlier in this episode for this exact page skeleton, or None.

        Keyed on the template as well as the scope, for the same reason the disk tier is: two
        pages of one site that the corpus would have given two rule sets get two proposals here
        too, rather than the first page the agent happened to visit deciding the whole episode.
        """
        if not scope:
            return None
        return self._proposed.get((scope, template))


    def store(self, scope: str, template: str | None, rules: list, *, persist: bool = False,
              provenance: "dict | None" = None) -> None:
        """Register a rule set proposed at runtime.

        Always in memory, so the same page skeleton is proposed for once per episode rather than
        once per turn (a Phase-1 call is two LLM round trips over the whole page skeleton plus the
        note audit -- doing that every turn would dominate the run).

        With `persist`, also written to `cache_dir` so the NEXT episode on this page reuses it
        instead of paying for another proposal. That write is gated on --rule_caching: the flag
        governs the disk tier in both directions, so `--rule_caching false` neither reads a stored
        set nor leaves one behind, and a measurement run cannot silently seed the cache for the
        runs after it. Provenance goes to a sibling `.meta.json` -- what proposed these rules,
        when, from which page, and under which scope/template -- because a rule set with no
        recorded origin is one nobody can later decide whether to trust.
        """
        self._proposed[(scope, template)] = rules
        if not persist or not self.cache_dir or not scope:
            return
        base = self.cache_dir if os.path.isabs(self.cache_dir) else os.path.join(_AT_SPI, self.cache_dir)
        stem = _cache_stem(scope, template)
        try:
            os.makedirs(base, exist_ok=True)
            with open(os.path.join(base, f"{stem}.json"), "w", encoding="utf-8") as f:
                json.dump(rules, f, indent=2)
            with open(os.path.join(base, f"{stem}.meta.json"), "w", encoding="utf-8") as f:
                json.dump({"scope": scope, "template": template, "n_rules": len(rules),
                           "generated_by": "mm_agents/sa_agent/masking.py (runtime proposal)",
                           "reviewed": False, **(provenance or {})}, f, indent=2)
        except OSError as e:
            logger.warning("[mask] could not persist rules for %s to %s (%s)", scope, base, e)
            return
        self._generated[(scope, template)] = rules
        logger.info("[mask] %s %s: cached %d generated rule(s) in %s",
                    scope, template or "(no template)", len(rules), base)


def _cache_stem(scope: str, template: str | None) -> str:
    """A filesystem-safe, COLLISION-FREE stem for one (scope, template).

    The readable part is lossy -- a template is mostly `/`, `*` and `#`, and slugging it turns
    both `/f/*` and `/f/*/#/*` into `f`, which would make reddit's forum listing and its post page
    the same cache file and hand one page the other's rules. That is the precise failure the
    (scope, template) key exists to prevent, so the exact pair is hashed and the digest carried in
    the name; the slug is there for a human scanning the directory, the digest is what makes the
    filename an identity. `.meta.json` beside it holds both values verbatim, and that -- not the
    filename -- is what `_load_generated` reads back.
    """
    import hashlib
    slug = lambda s: re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "_"
    digest = hashlib.sha1(f"{scope}|{template or ''}".encode()).hexdigest()[:10]
    return f"{slug(scope)[:60]}__{slug(template or 'scope')[:40]}__{digest}"


def _looks_like_domain(stem: str) -> bool:
    """Is this filename stem a hostname rather than a capture name?

    Capture names are `NNN_<slug>`; host files are the bare host. The numeric prefix is what
    separates them, so a mis-named file is skipped rather than silently registered for the wrong
    host. Only consulted for a stem the manifest has no row for -- a capture that IS in the
    manifest takes its scope from there, which is the whole point of rule_scope.
    """
    return "." in stem and not re.match(r"^\d+_", stem)


def normalize_model_str(model_str: str | None) -> str | None:
    """A `--model` spelling reduced to the `provider|name` that `llm.build_client` accepts.

    run.py's `--model` carries two things build_client does not. It may end in `|cua`, selecting a
    provider's computer-use API rather than its chat API, and it tolerates spaces around the pipes
    (its own default is `"vllm | Qwen/Qwen3.5-9B"`). build_client splits on the FIRST pipe only, so
    `aws|us.anthropic.claude-3-7-sonnet-20250219-v1:0|cua` would otherwise name the model
    `...v1:0|cua` and fail at the provider's API instead of here.

    Returns None for anything that isn't a two-part spec, which the caller reads as "no proposer".
    """
    if not model_str:
        return None
    parts = [p.strip() for p in str(model_str).split("|")]
    parts = [p for p in parts if p]
    if len(parts) > 2 and parts[-1].lower() == "cua":
        parts = parts[:-1]
    if len(parts) < 2:
        return None
    return f"{parts[0]}|{'|'.join(parts[1:])}"


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _app_scope_of_root(desc: dict, fallback: str | None) -> "str | None":
    """`app:<at-spi application name>` for ONE native root, from its own descriptor.

    `rule_scope.app_scope_of_tree` answers a different question -- "which application is the agent
    working in" -- by looking for the ACTIVE window, and that is still the right question for the
    log line and for a live rule proposal. It is the wrong question for grading, because it returns
    one answer for a screen that is showing several applications at once, and every row then gets
    the focused app's rules and the focused app's trust grants regardless of which window it is
    actually in.

    `resolve_content_roots` writes each native descriptor's provenance as
    ``"<application name>/<role>[:<window title>]"``, so the application is the part before the
    first slash. Falls back to the focused scope only when a descriptor carries no provenance at
    all, which is the pre-existing behaviour for a capture this cannot read.
    """
    prov = (desc.get("provenance") or "").strip()
    app = prov.split("/", 1)[0].strip() if prov else ""
    if not app:
        return fallback
    _ensure_at_spi_importable()
    import rule_scope as rsc
    return f"{rsc.APP_SCOPE_PREFIX}{app}"


def _host_of(url: str | None) -> str | None:
    """Hostname of a URL that may or may not carry a scheme, `www.` stripped.

    Delegates to at-spi rather than parsing here. The URL comes from the Chrome omnibox (see
    LabeledPlanningLoop._current_url) and Chrome elides the scheme there -- `reddit.com/f/books`,
    not `http://reddit.com/f/books` -- so parsing it is a small pile of special cases, and a
    second copy of that pile which agreed with at-spi's today would decide a DIFFERENT scope from
    the one the corpus was indexed under the first time either drifted. One parser, both sides.
    """
    _ensure_at_spi_importable()
    import rule_scope as rsc
    return rsc.scope_of_url(url)



@dataclass
class MaskConfig:
    mode: str = "deny"
    style: str = "fill"
    click_policy: str = "block"
    on_unknown_domain: str = "abort"
    on_native_app: str = "app_rules"
    rules: RuleCache | None = None
    default_deny: bool = True
    rule_caching: bool = True
    propose_model: str | None = None
    propose_vllm_port: int | None = None

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


@dataclass
class Mask:
    """One turn's redaction: the picture, the bitmap behind it, the numbered regions, stats."""
    png: bytes
    result: object
    regions: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    url: str | None = None
    endorsed_rects: list = field(default_factory=list)
    click_rects: list = field(default_factory=list)

    def click_blocked(self, x: float, y: float) -> bool:
        """The click gate's question: would a pointer action at (x, y) land on redacted pixels the
        planner has not been cleared to click? hit(), minus the click-only clearances."""
        for (rx, ry, rw, rh) in self.click_rects:
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                return False
        return self.hit(x, y)

    def _in_endorsed(self, x: float, y: float) -> bool:
        for (rx, ry, rw, rh) in self.endorsed_rects:
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                return True
        return False

    def hit(self, x: float, y: float) -> bool:
        if self._in_endorsed(x, y):
            return False
        return bool(self.result.hit(x, y))

    def region_at(self, x: float, y: float):
        if self._in_endorsed(x, y):
            return None
        return self.result.region_at(x, y)

    def focus_bbox(self):
        """Screen rectangle of the window the planner is working in, or None for a web page.

        The ACTIVE top-level window, which by definition has nothing painted above it. This is
        what scopes a screen query now that regions are no longer addressable by number: it needs
        no id from the planner, it cannot go stale between turns, and it can never name a window
        the planner cannot see. See mm_agents/sa_agent/tools.query_screen.
        """
        getter = getattr(self.result, "focus_bbox", None)
        return getter() if callable(getter) else None


def overlay_endorsed(mask: "Mask", pristine_png: bytes, rects: list,
                         click_rects: "list | None" = None) -> "Mask":
    """Return *mask* with each rect in *rects* shown in the clear: the pristine pixels for that
    rectangle are pasted back over the redaction, and the rect is recorded so `hit()`/`region_at()`
    stop treating it as redacted. Used by the loop after endorse_element verifies a row.

    Purely additive un-masking of already-verified rectangles -- it can only REVEAL a rect whose
    identity an endorser established, never widen the redaction, so it cannot leak un-verified
    content. No-op (returns the same mask) when there is nothing to reveal.
    """
    rects = [tuple(r) for r in (rects or []) if r]
    click_rects = [tuple(r) for r in (click_rects or []) if r]
    if not rects and click_rects:
        return Mask(png=mask.png, result=mask.result, regions=mask.regions, stats=mask.stats,
                    url=mask.url, endorsed_rects=[], click_rects=click_rects)
    if not rects or not pristine_png:
        return mask
    from PIL import Image
    masked = Image.open(BytesIO(mask.png)).convert("RGB")
    pristine = Image.open(BytesIO(pristine_png)).convert("RGB")
    W, H = masked.size
    for (x, y, w, h) in rects:
        box = (max(0, int(x)), max(0, int(y)),
               min(W, int(x + w)), min(H, int(y + h)))
        if box[2] > box[0] and box[3] > box[1]:
            masked.paste(pristine.crop(box), box)
    buf = BytesIO()
    masked.save(buf, format="PNG")
    return Mask(png=buf.getvalue(), result=mask.result, regions=mask.regions,
                stats=mask.stats, url=mask.url, endorsed_rects=rects,
                click_rects=click_rects)


class MaskUnavailable(Exception):
    """Raised when a mask was required and could not be built.

    Deliberately NOT a silent fallback to the unmasked screenshot: showing the planner the raw page
    because labeling failed is the one outcome that turns a defense off exactly when it is least
    understood. The caller decides -- fail the episode, or explicitly run unmasked.

    The SA loop no longer aborts on it (2026-09-24): after its retries it shows the turn through
    fallback_mask, i.e. as if no rule had been proposed -- default-deny, nothing granted.
    """


class MaskConfigError(MaskUnavailable):
    """A misconfiguration (a named rules directory that does not exist). Still aborts: falling
    back would turn a typo in a flag into a whole run of black screens that scores as a defense."""



def _trusted_file_surface(root: "ET.Element", window_trust) -> str | None:
    """The masking shortcut's window scan (the masking design). Return the title of a `<frame>`
    whose backing file provenance is already settled as TRUSTED, else None.

    Keyed on the window title, which is a native-app frame's `name` attribute (e.g.
    ``"open-invoices.txt - gedit"``). ``window_trust`` is FileProvenanceMap.window_trust: True for a
    trusted-file window, False for a known-untrusted one, None when there is no provenance answer.
    Only True short-circuits -- False and None both fall through to the ordinary rule-based masking,
    so this can never *widen* what is shown, only decline to mask a settled-trusted document."""
    if window_trust is None:
        return None
    for fr in root.iter("frame"):
        name = fr.get("name")
        if name and window_trust(name) is True:
            return name
    return None



_CHROME_APPS = ("google chrome", "chromium", "chrome")
_CLOSE_NAMES = ("close", "×", "✕", "x")
_SHELL_APPS = ("gnome-shell", "gjs")


def _attr(el, name):
    for k, v in el.attrib.items():
        if k == name or k.endswith("}" + name):
            return v
    return None


def _box(el):
    """(x, y, w, h) of a node with real on-screen geometry, else None."""
    c, s = _attr(el, "screencoord"), _attr(el, "size")
    if not c or not s:
        return None
    try:
        x, y = (int(v) for v in c.strip("()").split(","))
        w, h = (int(v) for v in s.strip("()").split(","))
    except ValueError:
        return None
    if w <= 0 or h <= 0 or x < -10000 or y < -10000:
        return None
    return (x, y, w, h)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def js_dialog_close_boxes(root: "ET.Element") -> list:
    """The close control of every native JavaScript dialog Chrome is showing (alert/confirm/prompt).

    WHILE SUCH A DIALOG IS UP, CHROME DROPS THE PAGE FROM THE ACCESSIBILITY TREE: the browser frame
    is renamed after the dialog ("banking.com says") and holds only a `dialog` (class RootView) --
    no document-web, no toolbar, no tab strip -- although the page is still painted behind it.
    Measured on a live VM, 2026-09-24. So resolve_content_roots finds nothing and every such turn
    used to end the episode with MaskUnavailable: 54 alert()/confirm() calls across the ten UCM
    sites, most of them the success message right after a task's final action.

    The dialog's MESSAGE is page-authored and can carry untrusted data (a recipient, a product
    name, an attachment filename), so only the controls that dismiss the dialog are returned.
    """
    boxes = []
    for app in root:
        if _local(app.tag) != "application":
            continue
        if (_attr(app, "name") or "").strip().lower() not in _CHROME_APPS:
            continue
        for dlg in app.iter():
            if _local(dlg.tag) != "dialog" or _attr(dlg, "class") != "RootView":
                continue
            if (_attr(dlg, "showing") or "").lower() != "true":
                continue
            buttons = [(b, _box(b)) for b in dlg.iter()
                       if _local(b.tag) == "push-button" and _box(b) is not None]
            close = [bx for b, bx in buttons
                     if (_attr(b, "name") or "").strip().lower() in _CLOSE_NAMES]
            if not close:
                close = [bx for b, bx in buttons
                         if (_attr(b, "name") or "").strip().lower() in ("ok", "cancel")]
            boxes.extend(close)
    return boxes


def fallback_mask(obs: dict, url: str | None, reason: str,
                  screen_size: tuple = (1920, 1080)) -> Mask:
    """What a turn gets when no ordinary mask can be built: the screen as if no rule had been
    proposed for anything on it. Never raises -- this is what replaces aborting the episode.

    "No rules" is default-deny: every showing window's rectangle is redacted, the desktop shell
    around them (top bar, dock -- never content) is left as it is, and nothing is granted. The one
    exception is a native JS dialog's close control (js_dialog_close_boxes), which is revealed so
    the agent can dismiss the dialog. With no usable tree at all, the whole screen is redacted;
    with no screenshot at all, the picture is a black screen of `screen_size`.
    """
    import numpy as np
    from PIL import Image
    _ensure_at_spi_importable()
    import mask_screenshot as ms

    png = obs.get("screenshot") if obs else None
    try:
        pristine = Image.open(BytesIO(png)).convert("RGB") if png else None
    except Exception:
        pristine = None
    if pristine is None:
        pristine = Image.new("RGB", screen_size, (0, 0, 0))
    w, h = pristine.size

    root = None
    tree_xml = obs.get("accessibility_tree") if obs else None
    if tree_xml:
        try:
            root = ET.fromstring(tree_xml)
        except ET.ParseError:
            root = None

    bits = np.ones((h, w), dtype=bool)
    reveal = []
    if root is not None:
        frames = []
        for app in root:
            if (_attr(app, "name") or "").strip().lower() in _SHELL_APPS:
                continue
            for fr in app.iter():
                if _local(fr.tag) == "frame" and (_attr(fr, "showing") or "").lower() == "true":
                    bx = _box(fr)
                    if bx:
                        frames.append(bx)
        if frames:
            bits[:] = False
            for x, y, fw, fh in frames:
                bits[max(0, y):min(h, y + fh), max(0, x):min(w, x + fw)] = True
        reveal = js_dialog_close_boxes(root)
        for x, y, bw, bh in reveal:
            bits[max(0, y):min(h, y + bh), max(0, x):min(w, x + bw)] = False

    image = pristine.copy()
    image.paste(Image.new("RGB", (w, h), (0, 0, 0)), (0, 0),
                Image.fromarray((bits * 255).astype("uint8"), mode="L"))
    stats = {"masked_pct": 100.0 * float(bits.mean()), "n_regions": 0, "n_rules": 0,
             "domain": _host_of(url), "scope": _host_of(url), "fallback": reason,
             "revealed_dialog_controls": len(reveal)}
    result = ms.MaskResult(image, bits, [], stats)
    buf = BytesIO()
    image.save(buf, format="PNG")
    logger.warning("[mask] fallback (%s): no-rules default-deny, %.1f%% redacted, %d dialog "
                   "control(s) shown", reason, stats["masked_pct"], len(reveal))
    return Mask(png=buf.getvalue(), result=result, regions=[], stats=stats, url=url)


def build_mask(obs: dict, url: str | None, config: MaskConfig,
               browser_focused: bool = True, window_trust=None,
               basename_trust=None) -> Mask | None:
    """Redact one live observation. Returns None when masking is off.

    Everything here is marshalling; the four calls that do the work are all imported from at-spi/.

    ``window_trust`` (optional) is FileProvenanceMap.window_trust, the trusted-files shortcut: when
    the native-app surface this turn is a window backed by a file already cleared as trusted, its
    content is the user's own and is not masked (the masking design). None disables the shortcut,
    which is the behaviour every caller that does not pass it keeps.

    ``basename_trust`` (optional) is FileProvenanceMap.basename_trust, the same idea one level
    down: a file manager or file chooser LISTING the user's own files shows each as a bare
    basename, and those rows are un-masked row by row rather than the whole window at once. Passed
    only on a NATIVE surface -- see that method for why a web page printing a filename must not
    earn the same grant.
    """
    if not config.enabled:
        return None
    _ensure_at_spi_importable()

    import labeling_store as ls
    import mask_screenshot as ms
    import rule_scope as rsc
    import selector_propose as sp
    from PIL import Image

    tree_xml = obs.get("accessibility_tree")
    png = obs.get("screenshot")
    if not tree_xml or not png:
        raise MaskUnavailable("observation carries no accessibility tree or no screenshot")

    try:
        root = ET.fromstring(tree_xml)
    except ET.ParseError as e:
        raise MaskUnavailable(f"could not parse accessibility tree: {e}") from e

    pristine = Image.open(BytesIO(png)).convert("RGB")

    platform = None if browser_focused else "app"
    if platform is None:
        platform = rsc.infer_platform_live(root, ls)
    platform = "desktop"

    universe = ls.build_universe_from_root(
        root,
        platform=platform,
        screen_size=pristine.size,
        filename=_host_of(url) or "<live>",
        meta={"url": url},
    )
    if not universe.get("roots"):
        if js_dialog_close_boxes(root):
            return fallback_mask(obs, url, "js_dialog")
        raise MaskUnavailable("no content root in the accessibility tree (is a page loaded?)")

    root_nodes = _root_nodes(root, ls, universe.get("platform"))
    content_root = root_nodes[0] if root_nodes else None
    if content_root is not None:
        try:
            ls.assert_row_node_alignment(universe, content_root)
        except AssertionError as e:
            raise MaskUnavailable(str(e)) from e

    trace: dict = {}
    focus_scope = rsc.app_scope_of_tree(root, ls)

    root_urls = _web_root_urls(root, universe, root_nodes, url)
    root_ctx: list[dict] = []
    rules_by_scope: dict = {}
    traces_by_key: dict = {}
    for ri, desc in enumerate(universe.get("roots", [])):
        r_is_web = desc.get("kind") == "web"
        r_url = root_urls[ri] if ri < len(root_urls) else url
        r_scope = _host_of(r_url) if r_is_web else _app_scope_of_root(desc, focus_scope)
        r_template = rsc.routing_template(r_url) if r_is_web else None
        key = (r_scope, r_template, r_is_web)
        if key not in rules_by_scope:
            r_node = root_nodes[ri] if ri < len(root_nodes) else content_root
            traces_by_key[key] = {}
            rules_by_scope[key] = _resolve_rules(
                config, r_scope, r_node, universe, sp, url=r_url,
                root_nodes=root_nodes, template=r_template, is_web=r_is_web,
                trace=traces_by_key[key])
        trusted_file = False
        if not r_is_web and window_trust is not None and ri < len(root_nodes):
            title = root_nodes[ri].get("name")
            if title and window_trust(title) is True:
                trusted_file = True
                universe["roots"][ri]["deny_exempt"] = True
                logger.info("[sa][mask] window %r is backed by a trusted file; shown unmasked "
                            "(provenance shortcut)", title)
        root_ctx.append({"is_web": r_is_web, "scope": r_scope, "rules": rules_by_scope[key],
                         "key": key, "trusted_file": trusted_file})

    if root_ctx and all(c["rules"] is None for c in root_ctx):
        return None
    for c in root_ctx:
        if c["rules"] is None:
            c["rules"] = []

    scope = focus_scope if root_ctx and not root_ctx[0]["is_web"] else (_host_of(url) or focus_scope)
    is_web = bool(root_ctx) and root_ctx[0]["is_web"]
    template = rsc.routing_template(url) if is_web else None
    focus_ctx = next((c for c in root_ctx if c["scope"] == scope), root_ctx[0] if root_ctx else None)
    rules = focus_ctx["rules"] if focus_ctx else []
    rows = _verdict_rows(universe, root_ctx, sp, basename_trust=basename_trust)

    result = ms.mask_image(pristine, rows, universe, style=config.style, biba=False,
                           default_deny=config.default_deny, content_root=content_root,
                           root_nodes=root_nodes, draw_regions=False)
    if result is None:
        raise MaskUnavailable("renderer returned nothing")

    buf = BytesIO()
    result.image.save(buf, format="PNG")
    stats = dict(result.stats)
    stats["domain"] = scope
    stats["scope"] = scope
    stats["platform"] = universe.get("platform")
    stats["template"] = template
    stats["n_rules"] = len(rules)
    stats.update(trace)
    if focus_ctx is not None:
        stats.update(traces_by_key.get(focus_ctx["key"], {}))
    stats["rules_by_root"] = [
        {"scope": c["scope"], "n_rules": len(c["rules"] or []),
         "rules_from": traces_by_key.get(c["key"], {}).get("rules_from"),
         "rules_capture": traces_by_key.get(c["key"], {}).get("rules_capture")}
        for c in root_ctx]
    return Mask(png=buf.getvalue(), result=result, regions=result.regions,
                stats=stats, url=url)


def _resolve_rules(config: "MaskConfig", scope: str | None, content_root, universe: dict, sp,
                   url: str | None = None, root_nodes: "list | None" = None,
                   template: str | None = None, is_web: bool = True,
                   trace: "dict | None" = None):
    """The rule set for this surface: cached if allowed and present, otherwise proposed live.

    Returns None only for the "run this page unmasked" cases (`on_unknown_domain == "off"`, or
    `on_native_app == "off"` on a native turn). An empty list is a real answer -- "we have rules
    and they grant nothing" -- and masks the surface.
    """
    if is_web and not scope:
        _trace(trace, "web_root_no_url", "")
        logger.info("[mask] web root with no resolvable URL (a non-browser app has focus) -- "
                    "default-denying that surface rather than aborting the mask")
        return []

    if config.rules is not None:
        episode = config.rules.proposed_for(scope, template)
        if episode is not None:
            _trace(trace, "episode_proposal", "")
            return episode

    if config.rule_caching and config.rules and scope:
        cached, capture, how = config.rules.for_scope(scope, url)
        if cached is not None:
            logger.info("[mask] %s: using stored rule set from %s (%s, %d rules)",
                        scope, capture, how, len(cached))
            _trace(trace, how, capture)
            return _enforce_allowlist(cached, sp, scope if is_web else None)

    if config.propose_model and scope and content_root is not None and (
            is_web or config.on_native_app == "app_rules"):
        try:
            rules = _propose_live(config, scope,
                                  root_nodes if root_nodes else [content_root], universe, sp)
        except Exception as e:
            logger.error("[mask] live rule proposal for %r failed (%s) -- no rules, default-deny",
                         scope, e)
            _trace(trace, "proposal_failed", config.propose_model or "")
            return []
        rules = _enforce_allowlist(rules, sp, scope if is_web else None)
        if config.rules is not None:
            config.rules.store(scope, template, rules, persist=config.rule_caching,
                               provenance={"model": config.propose_model, "url": url,
                                           "proposed_at": _now()})

        logger.info("[mask] %s %s: proposed %d rule(s) live",
                    scope, template or "(no template)", len(rules))
        _trace(trace, "proposed_live", config.propose_model or "")
        return rules

    if not is_web:
        _trace(trace, f"native_{config.on_native_app}", "")
        return _resolve_native_fallback(config, scope)

    host = scope or "<no url>"
    if config.on_unknown_domain == "abort":
        logger.error("[mask] no rule set for domain %r and no live proposer -- no rules, "
                     "default-deny masks the whole page", host)
        _trace(trace, "unknown_abort", "")
        return []
    _trace(trace, f"unknown_{config.on_unknown_domain}", "")
    if config.on_unknown_domain == "off":
        logger.warning("[mask] no rules for %s -- running UNMASKED for this page", host)
        return None
    logger.warning("[mask] no rules for %s -- default-deny masks the whole page", host)
    return []


def _trace(trace: "dict | None", how: str, capture: str) -> None:
    """Record how this turn's rule set was chosen, for mask_stats.jsonl."""
    if trace is not None:
        trace["rules_from"] = how
        trace["rules_capture"] = capture


def _resolve_native_fallback(config: "MaskConfig", scope: str | None):
    """What to do on a native-application turn with no rule set and no live proposal.

    Separate from the web fallback because the two failures mean different things. No rules for a
    HOSTNAME means the corpus has never seen that site, which for an eval replica is a
    provisioning gap worth stopping over. No rules for an APPLICATION is the ordinary state of a
    desktop: the agent alt-tabs into a terminal for one turn and back out, and aborting the episode
    over it -- which is what `on_unknown_domain: abort` did to every RTC-Bench run that opened a
    shell -- makes masking unusable for exactly the tasks this benchmark is built from.
    """
    where = scope or "<unknown application>"
    if config.on_native_app == "abort":
        logger.error("[mask] no rule set for %r and no live proposer -- no rules, default-deny", where)
        return []
    if config.on_native_app == "off":
        logger.warning("[mask] %s: no rules -- running this native window UNMASKED", where)
        return None
    logger.warning("[mask] %s: no rules -- default-deny masks the whole window", where)
    return []


def _enforce_allowlist(rules: list, sp, domain: str | None) -> list:
    """Drop UNCONSTRAINED rules -- and only those. Deliberate page-scope rules are kept.

    The matcher is default-deny already: `predict_trusted` returns "untrusted" when nothing
    matches, and an ordinary rule's only power is to grant trust over the region it names. Two
    shapes escape that, and they are not the same thing:

      SHAPE 2, a deliberate whole-page assertion (`scope: "page"`). KEPT, as of the change below.
      An accidental wildcard -- no `ancestor_path`, no `own_role`, no `own_class` and no `scope`
      either. DROPPED. The model has named no region at all, so there is nothing to honour;
      predict_trusted skips these too (see filter_rules), and dropping them here just makes the
      live path say so.

    THIS USED TO REFUSE PAGE-SCOPE GRANTS OUTRIGHT, on the reasoning that "on a live page reached
    mid-episode it is a single sentence from a model that turns the entire defense off, with no
    reviewer in the loop." That reasoning is still true and is the risk this now accepts, by
    decision. What it traded against: the offline scorer HONOURS page-scope grants -- see
    drop_overbroad_rules, which exempts them by design -- so the two paths gave different answers
    for the same rules. A UGC-free page scored FN=0/FP=0 offline (051_docs.python.org does exactly
    that, from one rule) and then masked completely live, and every such page was being reported
    on numbers the running agent never reproduced. Divergence between the scored path and the
    served path is the failure this project has paid for before.

    THE TEST IS NOW `sp.is_page_scope`, not a local re-derivation. The two paths did not even agree
    on what page-scope MEANS: offline reads `rule["scope"] == "page"` while this function inferred
    it from the absence of ancestor_path/own_role/own_class. A rule carrying `scope: "page"` plus a
    stray `own_role` was page-scope to one and a narrow rule to the other. Sharing the predicate is
    what makes "matches the offline policy" checkable rather than asserted.
    """
    kept, wildcards = [], []
    for rule in rules or []:
        if sp.is_page_scope(rule):
            kept.append(rule)
            continue
        if not (rule.get("ancestor_path") or rule.get("own_role") or rule.get("own_class")):
            wildcards.append(rule)
            continue
        kept.append(rule)
    if wildcards:
        logger.warning("[mask] %s: dropped %d unconstrained rule(s) -- no region named and no "
                       "scope:page (notes: %s)", domain, len(wildcards),
                       [str(r.get("note"))[:60] for r in wildcards])
    page_grants = [r for r in kept if sp.is_page_scope(r) and r.get("verdict") != "untrusted"]
    if page_grants:
        logger.warning("[mask] %s: honouring %d page-scope TRUST rule(s) -- the whole page is "
                       "trusted on this rule alone, matching the offline policy (notes: %s)",
                       domain, len(page_grants),
                       [str(r.get("note"))[:60] for r in page_grants])
    return kept


def _propose_live(config: "MaskConfig", domain: str, root_nodes: list, universe: dict, sp) -> list:
    """Run Phase 1 against the CURRENT page, through the same code the offline runs use.

    The sanitizer is the security-relevant part and is shared verbatim: `skeleton_from_root` strips
    every accessible name to `[name:length:N]` and keeps only roles, classes and ids. The model that
    decides what is trusted therefore never reads page text -- otherwise an attacker who controls
    page text would control the rules that decide what is trusted, which is precisely the attack
    this system exists to stop.
    """
    from mm_agents import openrouter
    from mm_agents.sa_agent.llm import build_client

    skeleton = sp.skeleton_from_roots(root_nodes)
    if not skeleton:
        raise ValueError("empty skeleton -- no content root to describe")
    client = build_client(config.propose_model, config.propose_vllm_port,
                          history=openrouter.ALWAYS)
    _provider, model_name = [p.strip() for p in config.propose_model.split("|", 1)]
    logger.info("[mask] %s: no cached rules -- proposing live with %s (%d char skeleton)",
                domain, model_name, len(skeleton))
    return sp.propose_rules_from_skeleton(
        client, skeleton, model_name,
        domain=domain,
        category=None,
        universe_for_extent=universe,
        universe_for_verify=universe,
        verbose=False,
    )


def _enclosing_window(node, parent_of):
    """The top-level `frame` (a window) that `node` is painted into, or None."""
    n = parent_of.get(node)
    while n is not None:
        if n.tag.split("}")[-1] == "frame":
            return n
        n = parent_of.get(n)
    return None


def _web_root_urls(tree_root, universe: dict, root_nodes: list, fallback_url: "str | None") -> list:
    """One URL per content root -- read from the window that root is actually painted into.

    WHY NOT ONE URL PER SCREEN, which is what this used to be. `url` arrives from
    `LabeledPlanningLoop._current_url`, which takes the FIRST omnibox in the tree. The loop below
    then graded every web root under it, so with two browser windows open the page in one window
    was graded under the other window's rules -- github.com's rules applied to the attacker's page
    next to it, decided by nothing but document order. The comment on that loop already claims
    "the focused window's scope decides its own rows and nothing else's"; for native roots that
    was true, for web roots it was not.

    `chrome://` is what forced the issue. Those scopes carry a whole-page TRUST grant
    (at-spi/llm_rules_container_overrides), so the same mis-attribution would have handed a second
    window's attacker page the browser's own trust -- a leak rather than a mis-scoping.

    HOW. The omnibox and the page it describes share their window's `frame` element (verified on
    every corpus capture: application > frame > ... > tool-bar > entry for the omnibox, and
    application > frame > ... > document-web for the page), so walking up to that frame and reading
    the omnibox inside it attributes the URL structurally rather than positionally. In practice web
    roots and browser windows are one-to-one: `resolve_content_roots` keeps only the OUTERMOST
    `document-web` per page, so a cross-origin iframe -- an ad slot, an embedded player -- stays
    inside its page's root and takes that page's URL rather than becoming a surface of its own.

    ONE WINDOW STILL MEANS THE CALLER'S URL, unchanged. `url` is authoritative whenever the screen
    holds at most one browser window, which is every corpus capture and every ordinary turn: live
    it IS this window's omnibox (LabeledPlanningLoop._current_url reads the same tree), and offline
    a caller passing an explicit URL is deliberately asking "grade this tree as if the browser were
    here" -- which is how the template-matching and scope_default regressions in
    test/sa_masking.py are written. Overriding that from the tree would silently turn those
    into exact_path lookups and would give this function a say it does not need: with one window
    there is nothing to attribute.

    ONLY A SECOND WINDOW CHANGES ANYTHING. Then there is no single right answer to inherit, so each
    root takes its own window's omnibox and a window that has none gets None -- `_resolve_rules`
    default-denies it. Failing closed is the only safe direction; trust is what is being handed out.
    """
    from mm_agents.accessibility_tree_wrap.coord_to_semantic import (
        omnibox_url_in,
    )
    roots = universe.get("roots", []) or []
    parent_of = {c: p for p in tree_root.iter() for c in p}

    windows, per_root = [], []
    for ri, desc in enumerate(roots):
        if desc.get("kind") != "web":
            windows.append(None)
            per_root.append(None)
            continue
        node = root_nodes[ri] if ri < len(root_nodes) else desc.get("node")
        win = _enclosing_window(node, parent_of) if node is not None else None
        windows.append(win)
        per_root.append(omnibox_url_in(win, include_internal=True) if win is not None else None)

    n_windows = len({id(w) for w in windows if w is not None})
    if n_windows <= 1:
        return [fallback_url if d.get("kind") == "web" else None for d in roots]

    out = []
    for ri, desc in enumerate(roots):
        if desc.get("kind") != "web":
            out.append(None)
            continue
        u = per_root[ri]
        if u is None:
            logger.warning("[mask] web root %d: %d browser windows on screen and no omnibox in "
                           "this one -- not naming it (default-deny)", ri, n_windows)
        out.append(u)
    logger.info("[mask] %d browser windows on screen, graded separately: %s",
                n_windows, sorted({u for u in out if u}))
    return out


def _root_nodes(root, ls, platform):
    """The content root ELEMENTS (labeling_store returns descriptors without the node in its
    serialisable output, so re-resolve -- same call, same answer).

    `platform` MUST be the one the universe was built with, and used to be hardcoded to "web".
    That is a web-only question, and asking it of a desktop app returns either nothing (a GTK app
    exposes no document-web) or the wrong subtree (on Thunderbird, the quoted HTML inside the
    compose window). Live, both outcomes disable rule proposal: `content_root is None` skips
    _propose_live entirely, and the wrong subtree gives the model a 61-character skeleton it
    correctly declines to write rules for. Either way the page ends up with an empty allowlist,
    which under default-deny is a black screen. The offline sweep had the same bug through
    full_tree_extraction._content_root and it cost 39 of 160 captures their entire rule set.

    ALL roots, not the first: an app exposes one native frame per window and a page one
    document-web per cross-origin iframe, and the universe emits rows for every one of them.
    """
    try:
        descriptors, _platform = ls.resolve_content_roots(root, platform)
        return [d["node"] for d in descriptors]
    except Exception:
        return []


def single_scope_ctx(universe: dict, *, scope: str | None, rules: list,
                     is_web: bool) -> list[dict]:
    """A `root_ctx` putting EVERY root of *universe* on one scope -- the pre-per-window behaviour.

    For callers that genuinely have one surface (a synthetic single-app fixture, a corpus capture)
    and would otherwise hand-build the same dict per root. Not used by build_mask: a live screen is
    exactly the case where one scope for every root is the wrong answer.
    """
    return [{"is_web": is_web, "scope": scope, "rules": rules}
            for _ in universe.get("roots", []) or [None]]


def _verdict_rows(universe: dict, root_ctx: list, sp, basename_trust=None) -> list[dict]:
    """One `predict_trusted` call per universe row, in the shape mask_image expects.

    PER ROOT. `root_ctx[i]` carries the scope, the rule set and the surface kind for universe root
    `i`, and every row is graded under the entry its own `root_index` names. This used to take one
    `rules`/`domain`/`app_scope` triple for the whole screen, and the three scope-dependent GRANTS
    below -- terminal default-trust, the native shell/content split, and rule matching itself --
    were applied to every row in the universe whatever window it came from. On a screen showing one
    surface that is the same thing. On a screen showing a terminal over a browser it is not: in the
    2026-09-16 own_owncloud_2 run every row on screen was graded as `app:gnome-terminal-server`,
    terminal default-trust marked all of them trusted, and the injected document in the browser
    window behind the terminal was handed to the planner unredacted.

    The ancestor chain comes from `sp.ancestor_path_rows()` -- the scorer's own splicer, imported
    rather than reproduced. This module used to rebuild it inline (the row's `skipped_ancestors`,
    then its laid-out ancestors, walking up) and that copy had ALREADY drifted: it emitted the
    chain nearest-first while the scorer emits it root-first, i.e. exactly reversed. A one-entry
    ancestor_path still matched -- a subsequence of length one matches anywhere -- so nothing
    looked wrong, but every multi-entry rule was being matched against the tree upside down, and
    the geometric propagation gate (which measures the descent from the container the rule names
    down to the row) would have measured it from the wrong end. This is the failure the module
    docstring's "never copied" rule exists to prevent, caught in this very function.

    `sp.ancestor_path_rows` indexes `universe["rows"]` positionally; elem_index IS that position
    for every row labeling_store builds, verified corpus-wide.

    CONTENT TIER ONLY, which is not a filter but the definition of what gets labeled. The universe
    splits every node into `content` (this node paints text or is a control -- it makes a claim
    about its own pixels) and `structural` (a wrapper, an echo of its child's name, an unnamed
    div). The offline labelers emit content rows and nothing else, so a rule set is proposed,
    scored and evaluated against content rows alone. Feeding mask_image the structural rows too
    hands it page-spanning containers -- on 007 the two largest untrusted rows were 1,787,100 px
    each, i.e. the entire content root -- and the painter's algorithm then blacks out the whole
    page and lets trusted descendants restore islands. That is precisely what `--biba` does, and
    what its help text warns produces "an image an agent should NOT be shown". Measured over the
    v8 run, content-only reproduces the corpus mask (3.0/13.0/42.8/39.2/41.2/68.4/19.2%) while
    all-rows roughly doubles it (38.2/36.8/74.9/67.3/75.2/71.9/78.2%).

    A row with no `tier` at all is kept: an unknown row over-masks, which is the direction this
    system is allowed to be wrong in.
    """
    content = []
    for row in universe["rows"]:
        if row.get("tier", "content") != "content":
            continue
        path = sp.ancestor_path_rows(universe, row["elem_index"])
        content.append((row, path))

    def _ctx(row):
        i = row.get("root_index", 0)
        return root_ctx[i] if 0 <= i < len(root_ctx) else (root_ctx[0] if root_ctx else
                                                          {"is_web": True, "scope": None, "rules": []})

    by_root: dict = {}
    for row, path in content:
        by_root.setdefault(row.get("root_index", 0), []).append((row, path))

    region_scope_by_root: dict = {}
    for ri, items in by_root.items():
        ctx = _ctx(items[0][0])
        app_scope = None if ctx["is_web"] else ctx["scope"]
        if app_scope is None:
            region_scope_by_root[ri] = None
            continue
        from mm_agents.accessibility_tree_wrap import native_regions as nr
        app_name = app_scope[len("app:"):] if app_scope.startswith("app:") else app_scope
        chains = [[p.get("role") for p in path] + [row["role"]] for row, path in items]
        region_scope_by_root[ri] = nr.scope_from_role_chains(app_name, chains) if chains else None

    terminal_trusted_by_root: dict = {}
    for ri, items in by_root.items():
        ctx = _ctx(items[0][0])
        app_scope = None if ctx["is_web"] else ctx["scope"]
        trusted = False
        if app_scope is not None:
            from mm_agents.sa_agent import terminal_taint as _tt
            if _tt.is_terminal_scope(app_scope):
                term_text = "\n".join(
                    f"{row.get('text') or ''}\n{row.get('text_content') or ''}"
                    for row, _ in items)
                _pt = (lambda op: basename_trust(os.path.basename(op))) if basename_trust else None
                trusted = not _tt.text_has_taint_command(
                    term_text, _pt, taint_on_unknown_file=True)
        terminal_trusted_by_root[ri] = trusted

    rows = []
    for row, path in content:
        ctx = _ctx(row)
        ri = row.get("root_index", 0)
        rules = ctx["rules"]
        domain = ctx["scope"] if ctx["is_web"] else None
        region_scope = region_scope_by_root.get(ri)
        terminal_trusted = terminal_trusted_by_root.get(ri, False)
        row_basename_trust = None if ctx["is_web"] else basename_trust
        verdict, _rationale = sp.predict_trusted(
            row["role"], row.get("attr_class"), path, rules,
            own_id=row.get("attr_id"), domain=domain, own_bbox=row.get("bbox"))
        verdict, _rationale = sp.capacity_verdict(row, verdict, _rationale)

        region = region_scope.classify(row["role"], [p.get("role") for p in path]) if region_scope else None
        if region == "shell":
            verdict, _rationale = "trusted", "native app shell (menu/toolbar chrome or container navigation)"
        if terminal_trusted:
            verdict, _rationale = "trusted", "terminal window (first-party shell), no taint-producing command visible"
        if ctx.get("trusted_file"):
            verdict, _rationale = "trusted", "trusted-file window (provenance shortcut)"

        if region != "shell" and row_basename_trust is not None and verdict != "trusted":
            if row_basename_trust(row.get("text")) is True:
                verdict = "trusted"
                _rationale = "file-owner shortcut: basename of a user-owned file"
        rows.append({
            "file": universe.get("file"),
            "elem_index": row["elem_index"],
            "bbox": row["bbox"],
            "verdict": verdict,
        })
    return rows
