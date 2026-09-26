"""Seam S3: deterministic HTTP.

`crosscheck` (and later `registry`, `k_of_n`, Tier-C) fetch from the network. Without pinned
responses the benchmark measures network weather instead of the endorser. Fixtures also let
us author *hostile* responses -- a genuine GitHub search endpoint whose top result is an
attacker-controlled repo -- which is otherwise not testable at all.

Fixture files live in fixtures/http/<sha1_12(url)>.json and carry the URL verbatim so the
directory stays greppable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional


def fixture_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


class FixtureMiss(Exception):
    def __init__(self, url: str, key: str):
        super().__init__(f"no HTTP fixture for {url} (expected fixtures/http/{key}.json)")
        self.url = url
        self.key = key


class FixtureHTTP:
    """Drop-in replacement for hybrid_tools.fetch_url_text(url, max_chars=..., ...).

    In `record` mode a miss falls through to the real fetcher and is written to disk; otherwise a
    miss raises, because a silent live fetch would make a "deterministic" run non-deterministic.
    """

    def __init__(self, fixtures_dir: str | Path, record: bool = False):
        self.dir = Path(fixtures_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.record = record
        self.hits: list[str] = []
        self.misses: list[str] = []
        self.override: Optional[str] = None

    def path_for(self, url: str) -> Path:
        return self.dir / f"{fixture_key(url)}.json"

    def get(self, url: str) -> Optional[dict]:
        p = self.path_for(url)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def put(self, url: str, ok: bool, text: str, synthetic: bool = False, note: str = "",
            status: Optional[int] = None) -> Path:
        p = self.path_for(url)
        rec = {"url": url, "ok": ok, "text": text, "synthetic": synthetic, "note": note}
        if status is not None:
            rec["status"] = status
        p.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        return p

    def fetch_url_json(self, url: str, timeout: int = 15, max_bytes: int = 400_000):
        """Deterministic stand-in for ``hybrid_tools.fetch_url_json`` (the ``registry`` verifier).

        Fixtures carry an optional ``status``; without one it is inferred as 200/404 from ``ok``,
        so the pre-existing files keep working. The status has to be *stored* rather than derived
        because 404 and "unreachable" are different answers here -- one says the value is wrong,
        the other says nothing at all -- and a fixture format that could not tell them apart
        could not express the `registry_miss` case.
        """
        rec = self.get(url)
        if rec is None:
            self.misses.append(url)
            if not self.record:
                raise FixtureMiss(url, fixture_key(url))
            from mm_agents.sa_agent.hybrid_tools import fetch_url_json as real
            status, data, err = real(url, timeout=timeout)
            self.put(url, status == 200, json.dumps(data) if data is not None else err,
                     synthetic=False, note=f"recorded live (status {status})", status=status)
            return status, data, err
        self.hits.append(url)
        status = int(rec.get("status") or (200 if rec.get("ok") else 404))
        if status != 200:
            return status, None, f"HTTP {status} from '{url}'"
        try:
            return status, json.loads(str(rec["text"])), ""
        except Exception as e:
            return status, None, f"Response from '{url}' is not valid JSON: {e}"

    def fetch_url_text(self, url: str, max_chars: int = 4000, timeout: int = 15,
                       max_retries: int = 3) -> tuple[bool, str]:
        if self.override:
            p = self.dir / f"{self.override}.json"
            if not p.exists():
                raise FixtureMiss(url, self.override)
            rec = json.loads(p.read_text(encoding="utf-8"))
            self.hits.append(f"{url} [fixture:{self.override}]")
            return bool(rec["ok"]), str(rec["text"])[:max_chars]
        rec = self.get(url)
        if rec is not None:
            self.hits.append(url)
            return bool(rec["ok"]), str(rec["text"])[:max_chars]
        self.misses.append(url)
        if not self.record:
            raise FixtureMiss(url, fixture_key(url))
        from mm_agents.sa_agent.hybrid_tools import fetch_url_text as real_fetch
        ok, text = real_fetch(url, max_chars=max_chars, timeout=timeout, max_retries=max_retries)
        self.put(url, ok, text, synthetic=False, note="recorded live")
        return ok, text
