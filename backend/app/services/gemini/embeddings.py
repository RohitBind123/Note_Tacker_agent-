"""Gemini embeddings client for the copilot retrieval layer (RAG).

Embeds transcript chunks (``RETRIEVAL_DOCUMENT``) for storage and copilot
questions (``RETRIEVAL_QUERY``) for lookup, using the model + dimensionality
from config (``GEMINI_EMBED_MODEL`` / ``EMBED_DIMENSIONS``) — never hardcoded.

Why we L2-normalise ourselves: ``gemini-embedding-001`` only returns
unit-length vectors at the full 3072 dims. At any reduced ``outputDimensionality``
(we use 768 to stay under pgvector's HNSW limit) the vectors are NOT normalised,
so cosine similarity would be distorted. We normalise every vector in pure
Python before it leaves this module, so callers can rely on cosine distance.

Each returned vector is asserted to be exactly ``EMBED_DIM`` long, so a model or
config drift fails loudly here rather than at the (fixed-width) DB column.
"""
from __future__ import annotations

import math

import httpx

from app.config import settings
from app.db.copilot_models import EMBED_DIM
from app.logging_config import get_logger
from app.services.http import request_with_retries

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
# Gemini's batchEmbedContents accepts up to 100 requests per call; chunk to stay
# safely under any server-side cap.
_BATCH_LIMIT = 100

TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_QUERY = "RETRIEVAL_QUERY"


def l2_normalize(vector: list[float]) -> list[float]:
    """Return a unit-length copy of ``vector`` (immutable: never mutates input).

    A zero vector (norm 0) is returned unchanged — there is no meaningful
    direction to normalise to, and dividing by zero would poison the row.
    """
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        return list(vector)
    return [component / norm for component in vector]


class GeminiEmbedder:
    """Thin async client over the Gemini embeddings REST API."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        dimensions: int | None = None,
    ) -> None:
        self._model = model or settings.gemini_embed_model
        self._key = api_key or settings.gemini_api_key
        self._base = (api_base or settings.gemini_api_base).rstrip("/")
        self._dims = dimensions or settings.embed_dimensions
        if not self._key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        if self._dims != EMBED_DIM:
            # The DB column is a fixed-width vector(EMBED_DIM). A mismatch would
            # only surface as an insert error far from the cause — fail here.
            raise RuntimeError(
                f"embed_dimensions ({self._dims}) != schema EMBED_DIM ({EMBED_DIM}); "
                "a re-embed migration is required to change the vector width"
            )

    def _request_body(self, text: str, task_type: str) -> dict:
        return {
            "model": f"models/{self._model}",
            "content": {"parts": [{"text": text}]},
            "taskType": task_type,
            "outputDimensionality": self._dims,
        }

    def _coerce_vector(self, values: list) -> list[float]:
        vector = [float(v) for v in values]
        if len(vector) != self._dims:
            raise RuntimeError(
                f"embedding length {len(vector)} != expected {self._dims}"
            )
        return l2_normalize(vector)

    async def embed_query(self, text: str) -> list[float]:
        """Embed a single copilot question (RETRIEVAL_QUERY), L2-normalised."""
        url = f"{self._base}/models/{self._model}:embedContent?key={self._key}"
        body = self._request_body(text, TASK_QUERY)
        resp = await request_with_retries("POST", url, json=body, timeout=_TIMEOUT)
        if resp.status_code != 200:
            log.error("gemini_embed_query_failed", status=resp.status_code, body=resp.text[:300])
            raise RuntimeError(
                f"gemini embed_query failed ({resp.status_code}): {resp.text[:200]}"
            )
        values = resp.json().get("embedding", {}).get("values")
        if not isinstance(values, list):
            raise RuntimeError("gemini embed_query returned no embedding.values")
        return self._coerce_vector(values)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed many transcript chunks (RETRIEVAL_DOCUMENT), L2-normalised.

        Returns one vector per input text, in order. Chunks the call into
        ``_BATCH_LIMIT``-sized requests so large transcripts stay within the
        server-side per-call cap.
        """
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH_LIMIT):
            window = texts[start : start + _BATCH_LIMIT]
            vectors.extend(await self._embed_batch(window))
        return vectors

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        url = f"{self._base}/models/{self._model}:batchEmbedContents?key={self._key}"
        body = {"requests": [self._request_body(t, TASK_DOCUMENT) for t in texts]}
        log.info("gemini_embed_batch_request", model=self._model, count=len(texts))
        resp = await request_with_retries("POST", url, json=body, timeout=_TIMEOUT)
        if resp.status_code != 200:
            log.error("gemini_embed_batch_failed", status=resp.status_code, body=resp.text[:300])
            raise RuntimeError(
                f"gemini embed_documents failed ({resp.status_code}): {resp.text[:200]}"
            )
        embeddings = resp.json().get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise RuntimeError(
                f"gemini embed_documents returned {len(embeddings or [])} vectors "
                f"for {len(texts)} inputs"
            )
        return [self._coerce_vector(item.get("values", [])) for item in embeddings]
