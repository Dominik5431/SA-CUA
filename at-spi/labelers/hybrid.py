#!/usr/bin/env python3
"""
hybrid.py -- helper library: the deterministic cookie-consent allowlist.

THIS USED TO BE A LABELER. It ran a precedence chain (zone -> cross-origin -> cookie allowlist ->
biba.py's critical-path derivation -> rule_based.py's category default) over extraction.py's
interactable-only rows and wrote labels/hybrid.json. That labeler went with the gen-1 universe,
along with the two label files it read. What survives is the one step nothing else duplicates:

    _cookie_verdict(own_id, domain)   imported by selector_propose.py, where it is the second
                                      layer of predict_trusted() -- checked before any LLM rule,
                                      so a pre-vetted consent control never costs a proposal.

Not run directly any more -- there is no main().
"""
import os
import sys

_AT_SPI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_AT_SPI_DIR))
sys.path.insert(0, _AT_SPI_DIR)

from trusted_domains import TRUSTED_DOMAINS


def _cookie_verdict(own_id: "str | None", domain: "str | None") -> "dict | None":
    """Step 3: deterministic, no LLM. Returns a full output row's trust fields if this element's
    own id matches a pre-vetted cookie-consent control for its domain, else None (fall through)."""
    if not own_id or not domain:
        return None
    info = TRUSTED_DOMAINS.get(domain)
    if not info or not info.get("cookie_provider"):
        return None
    if own_id not in info.get("cookie_accept_ids", []):
        return None
    return {
        "verdict": "trusted",
        "confidence": "high",
        "rationale": f"pre-vetted cookie-consent control (cookie_provider={info['cookie_provider']!r}) "
                     f"on trusted domain {domain!r}, per detect_consent_banners.py",
        "source": "cookie_allowlist",
    }
