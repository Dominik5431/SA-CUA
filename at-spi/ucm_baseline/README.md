# UCM as a baseline on this corpus

[Untrusted Content Masking][ucm] identifies user-generated regions of a web page with CSS
selectors and hides them behind placeholders before an agent sees the page. Its
`src/automatic_boundary_detection/` experiment does that automatically: an LLM is shown a
*sanitized* copy of the page — every text node replaced by `[text:length:N]` — and proposes CSS
selectors from structure alone. That is the closest published thing to what `labelers/` does
here, so it is the baseline.

This package runs it. It contains no reimplementation of the method: the sanitizer, the proposal
loop and the element resolution are imported from the sibling checkout and called unchanged.

    python -m ucm_baseline.run_ucm all --model "vllm|Qwen/Qwen3.8-27B" --vllm_port 8001
    python -m ucm_baseline.compare --run ucm_runs/<id> --rules llm_rules_container_v14

## What runs, and what had to be built

| | |
|---|---|
| `generic_site.py` | A site profile in UCM's `sites/<name>/site.py` shape, but domain-agnostic. |
| `llm_shim.py` | An `anthropic.Anthropic`-shaped object over `mm_agents.sa_agent.llm.build_client`, so their loop can run against the same model our labeler runs against. |
| `ucm_import.py` | Loads their `pipeline/*.py` out of the checkout. `UCM_REPO` overrides the path. |
| `match.py` | Runs a selector config against a saved snapshot, reusing their `GET_ELEMENTS_JS`. |
| `bridge.py` | xpath → screen rectangle → AT-SPI row. |
| `run_ucm.py` | `propose` → `match` → `score`. |
| `compare.py` | Puts a run beside one of our rule sets on identical rows. |

Their code that executes verbatim: `analyze_with_llm` (the whole multi-turn conversation, JSON
extraction, salvage path, dedupe), `generate_config` (including the `confidence == "low"` drop),
`GET_ELEMENTS_JS` and `get_elements_with_ids`. The UCM working tree is never written to.

## The five things that were not out of the box

**1. Scoring.** UCM's own metric is signature-F1 against its own hand labels, which says nothing
about our ground truth. `bridge.py` maps each matched element to its screen rectangle via
`dom_geom/`, then masks every AT-SPI row *contained* in one — containment rather than identity
because `reveal.js` hides a matched element's children too. `selector_propose.score_file` grew a
`page=` argument so the resulting verdicts go through *the same scorer* our rules do; there is no
second implementation to drift.

**2. One generic site profile.** UCM ships three, hand-tuned; booking's prompt is ~2,500 words of
property-card anatomy. Eighteen hand-tuned prompts would measure our prompt engineering. So every
host gets identical words with its name substituted. The prompt keeps UCM's skeleton — sanitized
framing, the provenance question, ALL-USERS-ARE-UNTRUSTED, the granularity rule, the
no-bare-hashed-class rule, the platform-computed-values carve-out, the JSON contract — and drops
every site-specific enumeration.

**3. The model.** Their loop hardcodes `claude-sonnet-4-5`. The shim redirects it without editing
their code; the discarded model name is recorded in `meta.json` under `requested_models`.

**4. Frames.** `document.querySelectorAll` does not enter an iframe, so subframe content is
invisible to UCM. Reproduced rather than papered over, and counted per capture as
`frames_ignored`.

**5. Coverage.** 115 of the 160 captures have a DOM. The other 45 are native applications
(Thunderbird, VS Code, LibreOffice, nautilus, a terminal, …) where a CSS-selector method has
nothing to run on. They are excluded, not scored as failures.

## Protocol

UCM proposes one selector set per *site* from a few of that site's pages. Same here: 3 captures
per host (`--pages_per_host`), spread across the host's index range rather than taken from the
front, because captures are numbered in collection order and the first three of a host are three
views of one page type. The remaining captures of each host are kept and reported separately:

* **in-sample** — evaluated on the pages the selectors were written from. UCM's own protocol.
* **held-out** — the same selectors on an unseen page of the same site.

## Things worth knowing before reading a number

* **UCM is a denylist**, ours is an allowlist. It masks only what someone wrote a selector for;
  everything unnamed stays visible. So `fn_free` — an untrusted row left unmasked whose text
  could carry an instruction — is the column to read, not F1.
* **Snapshot replay is not free.** Serializing and re-parsing normalises invalid markup (ownCloud
  nests `<a>` in `<a>`, GitLab nests `<p>` in `<p>`), which renumbers xpaths: agreement with
  `dom_geom` is 1.000 on wikipedia/github/booking/stackoverflow and 0.888 on one GitLab page. An
  xpath that does not resolve falls back to a `(tag, id, class, textPreview)` key, accepted only
  when unique on both sides. The per-run report prints how many elements took each route.
* **Nine captures have derived rather than measured geometry** (8 RocketChat/ownCloud whose
  calibration fell 1–4 votes short of quorum, 1 Reddit page annotated after an inherited
  calibration). Their origin is `[70, 114]`, which 105 of the 107 measured captures agree on.
* **Some snapshots redirect on load.** `025_www.booking.com` navigates away via an inline script;
  the matcher allows exactly one document commit and aborts everything else, which also cuts the
  network so the DOM stays as captured.

## Tests

    python at-spi/tests/test_ucm_baseline.py      # UCM_SKIP_BROWSER=1 to skip the two slow ones

Sixteen tests. The load-bearing ones: the sanitizer erases every word of page text; UCM's real
`analyze_with_llm` runs against a scripted Anthropic-shaped client and still dedupes and filters;
derived screen rectangles reproduce the stored ones exactly; reloading a snapshot reproduces the
recorded xpaths; `score_file(page=…)` is the same scorer.

One of those found a real bug before the run: doubling `{{num_pages}}`/`{{html_content_str}}` in
the prompt template does not raise — `str.format` turns `{{x}}` into the literal `{x}` — it just
sends the model a prompt with no page in it, and the model answers, reasonably, that it found no
untrusted regions.

[ucm]: ../../../untrusted-content-masking
