"""
quarantine.py  —  quarantined constrained reads of untrusted screen content
============================================================================

FIDES's dual-LLM primitive, adapted to CUA. The privileged planner must never
receive raw ``Zone.DOCUMENT`` text (that is the injection channel). To use an
untrusted region it issues a *typed* query answered by a lower-privilege
Quarantined LLM (here the sa_agent Q-VLM) with constrained decoding — a strict
Pydantic schema. The returned typed value inherits the region's label (it stays
UNTRUSTED until deliberately endorsed), but the schema bounds what the
untrusted channel can express into the planner's control flow.

The model client is imported lazily so importing this module (e.g. in the offline
harness) does not require the model SDKs.
"""

from __future__ import annotations

import json
from typing import Optional, Type, TypeVar

from .labels import MetaValue, ProductLabel

try:
    from pydantic import BaseModel
except Exception:
    BaseModel = object

S = TypeVar("S", bound="BaseModel")


class QuarantinedReader:
    """Reads untrusted regions via constrained extraction. Inject a ``client`` for
    tests/offline use; otherwise a sa_agent Q-VLM client is built lazily."""

    def __init__(self, model: Optional[str] = None, client=None):
        self._model = model
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            from mm_agents.sa_agent.llm import build_client
            if self._model is None:
                raise ValueError("QuarantinedReader needs a model name or a client")
            self._client = build_client(self._model)
        return self._client

    def extract(
        self,
        region: MetaValue[str],
        schema: Type[S],
        instruction: str,
    ) -> MetaValue[S]:
        """Extract a typed value from an untrusted region's content.

        The result carries the *region's* label (taint is preserved across the
        quarantine boundary); endorsement is a separate, explicit step.
        """
        client = self._ensure_client()
        raw = self._query(client, region.value, schema, instruction)
        parsed = schema(**raw) if isinstance(raw, dict) else raw
        return MetaValue(parsed, region.label)

    def _query(self, client, content: str, schema: Type[S], instruction: str) -> dict:
        json_schema = schema.model_json_schema()
        prompt = (
            "You are a quarantined extractor. Read the CONTENT and return ONLY a "
            f"JSON object matching this schema:\n{json.dumps(json_schema)}\n\n"
            f"Task: {instruction}\n\nCONTENT:\n{content}"
        )
        text = client.complete(prompt)
        return json.loads(text)
