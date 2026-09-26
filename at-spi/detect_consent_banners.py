#!/usr/bin/env python3
"""
detect_consent_banners.py -- scans captured AT-SPI XML for a real consent/cookie banner and, if
one is found, whether it carries a recognizable third-party CMP (Consent Management Platform)
fingerprint or is a bespoke first-party implementation. Fills in trusted_domains.py's
cookie_provider / cookie_accept_ids fields; never invents/guesses beyond what's literally in the
captured markup.

Why this is safe to automate (see the conversation this came out of): under this project's threat
model the domain itself is trusted -- only third-party-*uploaded* content on it is untrusted -- so
a consent banner's own id/class strings are not attacker-reachable the way free text is; they're
part of the trusted operator's own template. What the detector finds is therefore a real
structural fact about a trusted domain, not something that needs adversarial-robustness treatment
the way build_benchmark.py/label_benchmark.py's UGC-content labeling does.

Detection, per captured XML file:
  1. Look for a push-button/link/toggle-button whose accessible name matches consent-control
     vocabulary (accept all, reject all, i agree, allow all, manage cookies, cookie
     settings/preferences, consent) -- same vocabulary already used ad hoc earlier in this
     project's manual corpus review.
  2. If none found: cookie_provider = "none" (this capture shows no consent banner at all --
     could be genuinely absent, or the page never rendered one during this particular visit;
     re-checked on every recapture, not assumed permanent).
  3. If found: look at that element's own attr:id/attr:class AND nearby container ids (the
     dialog/section usually wrapping it) for a known CMP vendor's real id/class convention (not
     the vendor's brand name --
     e.g. Sourcepoint uses "sp_message_*" not "sourcepoint", Civic UK Cookie Control uses "ccc"
     not "civiccomputing"). If one matches: cookie_provider = that vendor. Otherwise:
     cookie_provider = "bespoke" (a real banner exists, but with no recognizable vendor fingerprint
     -- e.g. eBay's "gdpr-banner-accept", AWS's "awsccc-cb-c").
  4. cookie_accept_ids = the attr:id of every matched accept/agree/allow-type control (not
     manage/settings-type controls, which only open a preferences dialog rather than dismissing
     the banner) -- what a future elevation rule would match against.

Usage:
  python at-spi/detect_consent_banners.py                    # scan every captured xml, print a report
  python at-spi/detect_consent_banners.py --apply             # also write results into trusted_domains.py
  python at-spi/detect_consent_banners.py --xml_dir at-spi/xml --manifest at-spi/manifest.json
"""
import argparse
import ast
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
XML_DIR = os.path.join(HERE, "xml")
MANIFEST_PATH = os.path.join(HERE, "manifest.json")
TRUSTED_DOMAINS_PATH = os.path.join(HERE, "trusted_domains.py")

_ACCEPT_NAME_RE = re.compile(
    r'name="[^"]*(accept all|accept cookies|reject all|i agree|allow all)[^"]*"', re.IGNORECASE
)
_CONSENT_NAME_RE = re.compile(
    r'name="[^"]*(accept all|accept cookies|reject all|i agree|allow all|manage cookies|'
    r'cookie settings|cookie preferences|consent)[^"]*"',
    re.IGNORECASE,
)
_ELEMENT_RE = re.compile(r"<([a-z-]+)\s+([^>]*)>", re.IGNORECASE)
_ID_RE = re.compile(r'attr:id="([^"]*)"')

_CMP_FINGERPRINTS: list[tuple[str, re.Pattern]] = [
    ("onetrust", re.compile(r"onetrust", re.IGNORECASE)),
    ("sourcepoint", re.compile(r"sp_message_(container|iframe)", re.IGNORECASE)),
    ("fides", re.compile(r"fides-(banner|accept|reject|modal)", re.IGNORECASE)),
    ("civic_uk_cookie_control", re.compile(r"\bccc-icon\b|\bccc\b", re.IGNORECASE)),
    ("trustarc", re.compile(r"trustarc", re.IGNORECASE)),
    ("cookielaw", re.compile(r"cookielaw", re.IGNORECASE)),
    ("cmpbox", re.compile(r"cmpbox", re.IGNORECASE)),
    ("cookie_notice", re.compile(r"cookie-notice", re.IGNORECASE)),
    ("usercentrics", re.compile(r"usercentrics", re.IGNORECASE)),
    ("cookiebot", re.compile(r"cookiebot", re.IGNORECASE)),
    ("didomi", re.compile(r"didomi", re.IGNORECASE)),
    ("quantcast", re.compile(r"quantcast", re.IGNORECASE)),
    ("iubenda", re.compile(r"iubenda", re.IGNORECASE)),
    ("termly", re.compile(r"termly", re.IGNORECASE)),
    ("osano", re.compile(r"osano", re.IGNORECASE)),
    ("cassie", re.compile(r"cassie", re.IGNORECASE)),
    ("consentmanager", re.compile(r"consentmanager", re.IGNORECASE)),
    ("evidon", re.compile(r"evidon", re.IGNORECASE)),
]


def detect(xml_text: str) -> dict:
    """Returns {"cookie_provider": str, "cookie_accept_ids": list[str]}."""
    consent_elements = []
    accept_ids = []
    for m in _ELEMENT_RE.finditer(xml_text):
        tag, attrs = m.group(1), m.group(2)
        if tag not in ("push-button", "link", "toggle-button"):
            continue
        if not _CONSENT_NAME_RE.search(attrs):
            continue
        own_id_m = _ID_RE.search(attrs)
        own_id = own_id_m.group(1) if own_id_m else None
        consent_elements.append((attrs, own_id))
        if _ACCEPT_NAME_RE.search(attrs) and own_id:
            accept_ids.append(own_id)

    if not consent_elements:
        return {"cookie_provider": "none", "cookie_accept_ids": []}

    haystacks = [attrs for attrs, _ in consent_elements]
    for m in re.finditer(r'<(dialog|section|internal-frame)\s+([^>]*)>', xml_text, re.IGNORECASE):
        haystacks.append(m.group(2))

    for vendor, pattern in _CMP_FINGERPRINTS:
        if any(pattern.search(h) for h in haystacks):
            return {"cookie_provider": vendor, "cookie_accept_ids": sorted(set(accept_ids))}

    return {"cookie_provider": "bespoke", "cookie_accept_ids": sorted(set(accept_ids))}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xml_dir", default=XML_DIR)
    ap.add_argument("--manifest", default=MANIFEST_PATH)
    ap.add_argument("--trusted_domains", default=TRUSTED_DOMAINS_PATH)
    ap.add_argument("--apply", action="store_true", help="Write results into trusted_domains.py")
    args = ap.parse_args()

    with open(args.manifest, encoding="utf-8") as f:
        manifest = json.load(f)

    results: dict[str, dict] = {}
    for row in manifest:
        if row.get("status") != "ok":
            continue
        xml_path = os.path.join(args.xml_dir, row["filename"])
        if not os.path.exists(xml_path):
            continue
        with open(xml_path, encoding="utf-8") as f:
            xml_text = f.read()
        results[row["domain"]] = detect(xml_text)

    by_provider: dict[str, list[str]] = {}
    for domain, r in results.items():
        by_provider.setdefault(r["cookie_provider"], []).append(domain)

    print(f"Scanned {len(results)} captures.\n")
    for provider in sorted(by_provider, key=lambda p: (p == "none", p)):
        domains = sorted(by_provider[provider])
        print(f"{provider:28s} {len(domains):3d}  {', '.join(domains[:6])}{' ...' if len(domains) > 6 else ''}")

    if not args.apply:
        print("\n(dry run -- pass --apply to write cookie_provider_overrides.json)")
        return

    overrides_path = os.path.join(os.path.dirname(args.trusted_domains), "cookie_provider_overrides.json")
    with open(overrides_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, sort_keys=True)
    print(f"\nWrote {len(results)} domains' cookie_provider/cookie_accept_ids to {overrides_path}")


if __name__ == "__main__":
    main()
