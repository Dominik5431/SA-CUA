# www.bbc.com -- pulled from the corpus, kept as an example

These eleven captures were `at-spi/`'s **fully-trusted control** slot (indices 038-043 standard,
138-142 the `consent_banner` variants of 038-042). They are no longer corpus input. The slot is now
filled by `www.nytimes.com` section fronts at the same indices -- see the block in `at-spi/sites.py`.

Everything a live capture produces is here in the same layout as the corpus, so these can be
re-scored at any time by pointing the tooling at this directory:

    xml/  dom/  dom_geom/  screenshots/  llm_rules_container_v11/
    labels/ground_truth_fulltree.json    1904 derived rows for these eleven files
    labels/ground_truth_fulltree.jsonl   empty -- these captures were never hand-labeled
    labels/dom_excluded.json             the DOM-oracle exclusion set for these eleven files
    manifest.json                        the eleven manifest rows, as they stood when pulled

## Why keep them

Because of the `aria-hidden` channel, which they demonstrate more cleanly than anything left in
the corpus. BBC's ad banner is painted, is read by a sighted user, and is dropped from the AT-SPI
tree entirely -- Chrome does not emit `aria-hidden="true"` subtrees at all, so no rule, no
geometry check and no DOM-free heuristic can reach it. It is not a labeling mistake that can be
argued with; the element the label would attach to does not exist in the artifact. Kept as the
standing worked example of that.

The captures are also a reminder of the second-order problem: because BBC renders its consent
banner through Sourcepoint *in an iframe*, all six standard captures originally came back with a
live `sp_message` banner still in the tree, indistinguishable from a page that never had one --
which is what motivated the every-frame consent traversal in `collect_atspi_trees.py`.
