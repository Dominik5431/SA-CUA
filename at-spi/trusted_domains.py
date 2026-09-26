"""
trusted_domains.py -- consolidated allowlist of domains whose site operator is trusted (per
CLAUDE.md's threat model: the provider/builder of the site is trusted, third-party-uploaded
content on it is not). This is a domain-trust allowlist, not a content-trust list -- being on
this list only means the domain is eligible for the episode-trusted-domain chrome elevation in
mm_agents/sa_agent/screen_labels.py (_document_zone_integrity's CHROME_LIKE_ROLES rule) and,
planned next, the CMP/cookie-consent-banner allowlist discussed for that same module. It says
nothing about any specific piece of text on the domain, which is still classified individually
by region/authorship as always.

Keyed by bare hostname (no scheme/path), so a lookup for any domain the agent is currently on is a
plain dict access -- e.g. TRUSTED_DOMAINS["stackoverflow.com"]["category"], or
TRUSTED_DOMAINS.get(hostname) is None to test "is this domain trusted at all." Lookups are exact
hostname strings as they'd appear in a page URL/title (matching get_active_tab_hostname's output
elsewhere in the codebase) -- "www.example.com" and "example.com" are different keys if both occur
in the wild; several entries below already carry a domain's www.-prefixed *or* bare form,
whichever the source page actually used, not both.

Each value is a dict with:
  category        -- coarse content-type tag (qa, shopping, news, reference, docs, code, cloud,
                     gov, edu, research, travel, social_ugc, media_entertainment, blog_tutorial,
                     personal_blog, vendor, jobs, misc). Best-effort, not authoritative -- several
                     entries were bucketed by a keyword heuristic, not read individually; re-tag
                     freely on review.
  source          -- provenance, kept for auditability:
    "atspi_corpus"   -- already in at-spi/sites.py (hand-curated for the AT-SPI trust corpus)
    "benchmark_task" -- extracted from goals/ and evaluation_examples/ task content, i.e.
                        domains a task in this repo's own suite actually references. One
                        entry (pypi-resolve.org) was found and excluded during extraction --
                        it is not a real site, it's the malicious URL injected by
                        goals/adv/.../own_reddit_2_loose_*.json's own attack payload. Kept as
                        a cautionary note: this list must never be auto-populated without a
                        review pass, since adversarial task content lives in the same repo.
    "literature"      -- commonly-used real-world domains from other web-agent evaluation
                        papers (WebVoyager/Mind2Web-style), added from memory for broader
                        general-purpose coverage -- NOT verified against the papers'
                        appendices, so don't cite this list as a literature reference without
                        checking the primary sources first.
  cookie_provider -- CMP vendor detected for this domain's consent banner (e.g. "onetrust",
                     "sourcepoint", "fides", "bespoke" for a homegrown first-party banner, or
                     None if this domain has never been checked, or "none" if checked and no
                     consent banner was found). Populated by the CMP-detection pass planned in
                     at-spi/detect_consent_banners.py -- every entry starts at None here; not
                     filled in yet.
  cookie_accept_ids -- list of AT-SPI attr:id values for the banner's accept/dismiss control(s),
                     once known -- what an elevation rule would match against. Empty until the
                     detection pass runs.

benchmark_task and literature entries have NOT been individually reviewed the way
at-spi/sites.py's original 85 were -- treat this as a first-pass draft. Edit freely; this is a
plain hand-editable dict, not fetched from any external service.
"""

import json
import os

_RAW: list[tuple[str, str, str]] = [
    ("aws.amazon.com", "cloud", "atspi_corpus"),
    ("azure.microsoft.com", "cloud", "atspi_corpus"),
    ("cloud.google.com", "cloud", "atspi_corpus"),
    ("vercel.com", "cloud", "atspi_corpus"),
    ("www.digitalocean.com", "cloud", "atspi_corpus"),
    ("www.heroku.com", "cloud", "atspi_corpus"),
    ("crates.io", "code", "atspi_corpus"),
    ("github.com", "code", "atspi_corpus"),
    ("gitlab.com", "code", "atspi_corpus"),
    ("pkg.go.dev", "code", "atspi_corpus"),
    ("pypi.org", "code", "atspi_corpus"),
    ("www.npmjs.com", "code", "atspi_corpus"),
    ("developer.mozilla.org", "docs", "atspi_corpus"),
    ("docs.djangoproject.com", "docs", "atspi_corpus"),
    ("docs.docker.com", "docs", "atspi_corpus"),
    ("docs.python.org", "docs", "atspi_corpus"),
    ("go.dev", "docs", "atspi_corpus"),
    ("kubernetes.io", "docs", "atspi_corpus"),
    ("nodejs.org", "docs", "atspi_corpus"),
    ("reactjs.org", "docs", "atspi_corpus"),
    ("whatwg.org", "docs", "atspi_corpus"),
    ("www.iso.org", "docs", "atspi_corpus"),
    ("www.rfc-editor.org", "docs", "atspi_corpus"),
    ("www.rust-lang.org", "docs", "atspi_corpus"),
    ("www.w3.org", "docs", "atspi_corpus"),
    ("ocw.mit.edu", "edu", "atspi_corpus"),
    ("www.berkeley.edu", "edu", "atspi_corpus"),
    ("www.cam.ac.uk", "edu", "atspi_corpus"),
    ("www.coursera.org", "edu", "atspi_corpus"),
    ("www.edx.org", "edu", "atspi_corpus"),
    ("www.freecodecamp.org", "edu", "atspi_corpus"),
    ("www.harvard.edu", "edu", "atspi_corpus"),
    ("www.khanacademy.org", "edu", "atspi_corpus"),
    ("www.mit.edu", "edu", "atspi_corpus"),
    ("www.ox.ac.uk", "edu", "atspi_corpus"),
    ("www.stanford.edu", "edu", "atspi_corpus"),
    ("www.w3schools.com", "edu", "atspi_corpus"),
    ("europa.eu", "gov", "atspi_corpus"),
    ("www.cdc.gov", "gov", "atspi_corpus"),
    ("www.gov.uk", "gov", "atspi_corpus"),
    ("www.ietf.org", "gov", "atspi_corpus"),
    ("www.nasa.gov", "gov", "atspi_corpus"),
    ("www.nih.gov", "gov", "atspi_corpus"),
    ("www.nist.gov", "gov", "atspi_corpus"),
    ("www.unicode.org", "gov", "atspi_corpus"),
    ("www.usa.gov", "gov", "atspi_corpus"),
    ("www.weather.gov", "gov", "atspi_corpus"),
    ("getfedora.org", "homepage", "atspi_corpus"),
    ("ubuntu.com", "homepage", "atspi_corpus"),
    ("www.apache.org", "homepage", "atspi_corpus"),
    ("www.debian.org", "homepage", "atspi_corpus"),
    ("www.gnu.org", "homepage", "atspi_corpus"),
    ("www.mozilla.org", "homepage", "atspi_corpus"),
    ("www.python.org", "homepage", "atspi_corpus"),
    ("www.redhat.com", "homepage", "atspi_corpus"),
    ("duckduckgo.com", "misc", "atspi_corpus"),
    ("www.bing.com", "misc", "atspi_corpus"),
    ("www.openstreetmap.org", "misc", "atspi_corpus"),
    ("apnews.com", "news", "atspi_corpus"),
    ("www.bbc.com", "news", "atspi_corpus"),
    ("www.npr.org", "news", "atspi_corpus"),
    ("www.nytimes.com", "news", "atspi_corpus"),
    ("www.reuters.com", "news", "atspi_corpus"),
    ("www.theguardian.com", "news", "atspi_corpus"),
    ("www.wsj.com", "news", "atspi_corpus"),
    ("askubuntu.com", "qa", "atspi_corpus"),
    ("english.stackexchange.com", "qa", "atspi_corpus"),
    ("security.stackexchange.com", "qa", "atspi_corpus"),
    ("serverfault.com", "qa", "atspi_corpus"),
    ("softwareengineering.stackexchange.com", "qa", "atspi_corpus"),
    ("stackoverflow.com", "qa", "atspi_corpus"),
    ("superuser.com", "qa", "atspi_corpus"),
    ("unix.stackexchange.com", "qa", "atspi_corpus"),
    ("archive.org", "reference", "atspi_corpus"),
    ("en.wikipedia.org", "reference", "atspi_corpus"),
    ("simple.wikipedia.org", "reference", "atspi_corpus"),
    ("www.britannica.com", "reference", "atspi_corpus"),
    ("www.wolframalpha.com", "reference", "atspi_corpus"),
    ("arxiv.org", "research", "atspi_corpus"),
    ("scholar.google.com", "research", "atspi_corpus"),
    ("www.amazon.com", "shopping", "atspi_corpus"),
    ("www.bestbuy.com", "shopping", "atspi_corpus"),
    ("www.ebay.com", "shopping", "atspi_corpus"),
    ("www.etsy.com", "shopping", "atspi_corpus"),
    ("www.walmart.com", "shopping", "atspi_corpus"),

    ("devicetests.com", "blog_tutorial", "benchmark_task"),
    ("howlongtoread.com", "blog_tutorial", "benchmark_task"),
    ("in5stepstutorials.com", "blog_tutorial", "benchmark_task"),
    ("itsfoss.com", "blog_tutorial", "benchmark_task"),
    ("technical-tips.com", "blog_tutorial", "benchmark_task"),
    ("www.dedoimedo.com", "blog_tutorial", "benchmark_task"),
    ("www.howtogeek.com", "blog_tutorial", "benchmark_task"),
    ("www.ilovefreesoftware.com", "blog_tutorial", "benchmark_task"),
    ("www.maketecheasier.com", "blog_tutorial", "benchmark_task"),
    ("www.makeuseof.com", "blog_tutorial", "benchmark_task"),
    ("www.speedtest.net", "blog_tutorial", "benchmark_task"),
    ("www.thegeekdiary.com", "blog_tutorial", "benchmark_task"),
    ("academicpages.github.io", "code", "benchmark_task"),
    ("campbell-muscle-lab.github.io", "code", "benchmark_task"),
    ("docs.videolan.me", "code", "benchmark_task"),
    ("jalammar.github.io", "code", "benchmark_task"),
    ("kid3.kde.org", "code", "benchmark_task"),
    ("lilianweng.github.io", "code", "benchmark_task"),
    ("raw.githubusercontent.com", "code", "benchmark_task"),
    ("seekstar.github.io", "code", "benchmark_task"),
    ("wiki.videolan.org", "code", "benchmark_task"),
    ("deb.nodesource.com", "docs", "benchmark_task"),
    ("developer.apple.com", "docs", "benchmark_task"),
    ("help.libreoffice.org", "docs", "benchmark_task"),
    ("help.ubuntu.com", "docs", "benchmark_task"),
    ("marketplace.uipath.com", "docs", "benchmark_task"),
    ("picard.musicbrainz.org", "docs", "benchmark_task"),
    ("releases.ubuntu.com", "docs", "benchmark_task"),
    ("support.google.com", "docs", "benchmark_task"),
    ("support.mozilla.org", "docs", "benchmark_task"),
    ("www.adsigner.com", "docs", "benchmark_task"),
    ("www.libreofficehelp.com", "docs", "benchmark_task"),
    ("www.sync.blue", "docs", "benchmark_task"),
    ("www.vlchelp.com", "docs", "benchmark_task"),
    ("ai.stanford.edu", "edu", "benchmark_task"),
    ("colab.research.google.com", "edu", "benchmark_task"),
    ("cs50.harvard.edu", "edu", "benchmark_task"),
    ("experts.utexas.edu", "edu", "benchmark_task"),
    ("funbrain.com", "edu", "benchmark_task"),
    ("researchguides.case.edu", "edu", "benchmark_task"),
    ("tensorlab.cms.caltech.edu", "edu", "benchmark_task"),
    ("www.cs.utexas.edu", "edu", "benchmark_task"),
    ("www.eas.caltech.edu", "edu", "benchmark_task"),
    ("www.dmv.virginia.gov", "gov", "benchmark_task"),
    ("www.justice.gov", "gov", "benchmark_task"),
    ("www.recreation.gov", "gov", "benchmark_task"),
    ("www.surreycc.gov.uk", "gov", "benchmark_task"),
    ("store.steampowered.com", "media_entertainment", "benchmark_task"),
    ("www.dota2.com", "media_entertainment", "benchmark_task"),
    ("www.imdb.com", "media_entertainment", "benchmark_task"),
    ("www.nba.com", "media_entertainment", "benchmark_task"),
    ("www.nfl.com", "media_entertainment", "benchmark_task"),
    ("www.youtube.com", "media_entertainment", "benchmark_task"),
    ("webext.eu", "misc", "benchmark_task"),
    ("www.accuweather.com", "misc", "benchmark_task"),
    ("www.babycenter.com", "misc", "benchmark_task"),
    ("news.google.com", "news", "benchmark_task"),
    ("www.bbc.co.uk", "news", "benchmark_task"),
    ("www.cnn.com", "news", "benchmark_task"),
    ("www.laptopmag.com", "news", "benchmark_task"),
    ("www.nationalgeographic.com", "news", "benchmark_task"),
    ("jimfan.me", "personal_blog", "benchmark_task"),
    ("yukezhu.me", "personal_blog", "benchmark_task"),
    ("ask.libreoffice.org", "qa", "benchmark_task"),
    ("www.drugs.com", "reference", "benchmark_task"),
    ("www.geeksforgeeks.org", "reference", "benchmark_task"),
    ("www.mathsisfun.com", "reference", "benchmark_task"),
    ("www.wikidata.org", "reference", "benchmark_task"),
    ("www.wikihow.com", "reference", "benchmark_task"),
    ("aclanthology.org", "research", "benchmark_task"),
    ("anonymous.4open.science", "research", "benchmark_task"),
    ("blog.eleuther.ai", "research", "benchmark_task"),
    ("dblp.org", "research", "benchmark_task"),
    ("hal.science", "research", "benchmark_task"),
    ("paperswithcode.com", "research", "benchmark_task"),
    ("research.nvidia.com", "research", "benchmark_task"),
    ("shopping.google.com", "shopping", "benchmark_task"),
    ("www.cars.com", "shopping", "benchmark_task"),
    ("www.kohls.com", "shopping", "benchmark_task"),
    ("www.macys.com", "shopping", "benchmark_task"),
    ("www.rentalcars.com", "shopping", "benchmark_task"),
    ("bugartisan.medium.com", "social_ugc", "benchmark_task"),
    ("medium.com", "social_ugc", "benchmark_task"),
    ("nikki-ricks.medium.com", "social_ugc", "benchmark_task"),
    ("www.linkedin.com", "social_ugc", "benchmark_task"),
    ("www.quora.com", "social_ugc", "benchmark_task"),
    ("www.reddit.com", "social_ugc", "benchmark_task"),
    ("www.tiktok.com", "social_ugc", "benchmark_task"),
    ("www.zhihu.com", "social_ugc", "benchmark_task"),
    ("x.com", "social_ugc", "benchmark_task"),
    ("discussions.flightaware.com", "travel", "benchmark_task"),
    ("premier.ticketek.com.au", "travel", "benchmark_task"),
    ("www.airbnb.com", "travel", "benchmark_task"),
    ("www.budget.com", "travel", "benchmark_task"),
    ("www.delta.com", "travel", "benchmark_task"),
    ("www.flightaware.com", "travel", "benchmark_task"),
    ("www.lonelyplanet.com", "travel", "benchmark_task"),
    ("www.mbta.com", "travel", "benchmark_task"),
    ("www.qatarairways.com", "travel", "benchmark_task"),
    ("www.ryanair.com", "travel", "benchmark_task"),
    ("www.tripadvisor.com", "travel", "benchmark_task"),
    ("www.united.com", "travel", "benchmark_task"),
    ("devstreaming-cdn.apple.com", "vendor", "benchmark_task"),
    ("download.microsoft.com", "vendor", "benchmark_task"),
    ("huggingface.co", "vendor", "benchmark_task"),
    ("openai.com", "vendor", "benchmark_task"),
    ("pytorch.org", "vendor", "benchmark_task"),
    ("techcommunity.microsoft.com", "vendor", "benchmark_task"),
    ("www.apple.com", "vendor", "benchmark_task"),
    ("www.deepl.com", "vendor", "benchmark_task"),

    ("indeed.com", "jobs", "literature"),
    ("espn.com", "media_entertainment", "literature"),
    ("netflix.com", "media_entertainment", "literature"),
    ("spotify.com", "media_entertainment", "literature"),
    ("doordash.com", "misc", "literature"),
    ("google.com", "misc", "literature"),
    ("uber.com", "misc", "literature"),
    ("dictionary.cambridge.org", "reference", "literature"),
    ("yelp.com", "reference", "literature"),
    ("zillow.com", "reference", "literature"),
    ("target.com", "shopping", "literature"),
    ("booking.com", "travel", "literature"),
    ("expedia.com", "travel", "literature"),
    ("southwest.com", "travel", "literature"),
    ("ticketmaster.com", "travel", "literature"),
]

def _load_cookie_overrides() -> dict[str, dict]:
    """cookie_provider_overrides.json is written by detect_consent_banners.py --apply, kept as a
    separate sidecar (not merged into _RAW's source text) so re-running the detector never
    requires editing this hand-reviewable file -- just regenerate the sidecar and re-import.
    Missing/malformed sidecar -> no overrides, every domain keeps cookie_provider=None."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie_provider_overrides.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_COOKIE_OVERRIDES = _load_cookie_overrides()

TRUSTED_DOMAINS: dict[str, dict] = {
    domain: {
        "category": category,
        "source": source,
        "cookie_provider": _COOKIE_OVERRIDES.get(domain, {}).get("cookie_provider"),
        "cookie_accept_ids": _COOKIE_OVERRIDES.get(domain, {}).get("cookie_accept_ids", []),
    }
    for domain, category, source in _RAW
}
