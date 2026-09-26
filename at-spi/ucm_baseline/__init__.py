"""UCM (untrusted-content-masking) run as a baseline on this corpus.

The point of this package is that it contains NO reimplementation of UCM's method. The three
pieces that constitute the method -- the sanitizer, the LLM proposal loop, and the
querySelectorAll+getXPath element resolution -- are imported from the UCM checkout and called
unchanged. What lives here is only what UCM does not have and cannot have:

  generic_site.py   a site profile in UCM's own shape, but domain-agnostic. UCM ships three
                    hand-tuned ones; a corpus of 18 hosts cannot use them, and writing 18 more
                    would be measuring our prompt engineering, not their method.
  llm_shim.py       an Anthropic-API-shaped adapter over whatever endpoint this project already
                    talks to, so UCM's loop can run against the same model our own labeler runs
                    against. Their loop code is untouched; only the transport differs.
  match.py          runs a selector config against a saved snapshot, reusing UCM's own
                    GET_ELEMENTS_JS so the xpaths it emits are the same strings dom_geom recorded.
  bridge.py         xpath -> screen rectangle -> AT-SPI row. The join UCM has no need for and
                    therefore never built; dom_capture.py built the geometry half of it already.
  run_ucm.py        the CLI tying those together.
"""
