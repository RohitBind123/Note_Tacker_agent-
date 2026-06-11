"""Unit tests for the Gemini embeddings client (Batch 3).

Real round-trips against the live embeddings API are exercised in the E2E batch;
here we pin the wire shape (request body + response parsing), the pure
L2-normalisation, and the fail-loud dimension guard.
"""
import math

import httpx
import pytest
import respx

from app.db.copilot_models import EMBED_DIM
from app.services.gemini.embeddings import (
    GeminiEmbedder,
    l2_normalize,
)

BASE = "https://generativelanguage.googleapis.com/v1beta"


def _embedder() -> GeminiEmbedder:
    return GeminiEmbedder(model="gemini-embedding-001", api_key="test-key", api_base=BASE)


def _unit_len(vector: list[float]) -> float:
    return math.sqrt(sum(c * c for c in vector))


# --- pure normalisation ---


def test_l2_normalize_makes_unit_length():
    out = l2_normalize([3.0, 4.0])  # 3-4-5 triangle
    assert out == [0.6, 0.8]
    assert _unit_len(out) == pytest.approx(1.0)


def test_l2_normalize_does_not_mutate_input():
    src = [3.0, 4.0]
    l2_normalize(src)
    assert src == [3.0, 4.0]


def test_l2_normalize_zero_vector_is_passed_through():
    # No meaningful direction; must not divide by zero.
    assert l2_normalize([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


# --- dimension guard ---


def test_embedder_rejects_mismatched_dimensions():
    with pytest.raises(RuntimeError, match="re-embed migration"):
        GeminiEmbedder(api_key="k", dimensions=EMBED_DIM + 1)


# --- query embedding ---


@respx.mock
async def test_embed_query_normalises_and_sends_query_task():
    raw = [3.0, 4.0] + [0.0] * (EMBED_DIM - 2)
    route = respx.route(method="POST", url__startswith=f"{BASE}/models/").mock(
        return_value=httpx.Response(200, json={"embedding": {"values": raw}})
    )
    vector = await _embedder().embed_query("what did we decide?")
    assert len(vector) == EMBED_DIM
    assert _unit_len(vector) == pytest.approx(1.0)
    # request asked for the right task type + output dimensionality
    sent = route.calls.last.request
    assert b"RETRIEVAL_QUERY" in sent.content
    assert b"embedContent" in str(sent.url).encode()
    assert b"outputDimensionality" in sent.content


@respx.mock
async def test_embed_query_rejects_wrong_length_vector():
    # API returns a short vector -> we fail loudly rather than store a bad row.
    respx.route(method="POST", url__startswith=f"{BASE}/models/").mock(
        return_value=httpx.Response(200, json={"embedding": {"values": [1.0, 2.0, 3.0]}})
    )
    with pytest.raises(RuntimeError, match="!= expected"):
        await _embedder().embed_query("q")


# --- document (batch) embedding ---


@respx.mock
async def test_embed_documents_returns_one_vector_per_input():
    vec_a = [1.0, 0.0] + [0.0] * (EMBED_DIM - 2)
    vec_b = [0.0, 2.0] + [0.0] * (EMBED_DIM - 2)
    route = respx.route(method="POST", url__startswith=f"{BASE}/models/").mock(
        return_value=httpx.Response(
            200,
            json={"embeddings": [{"values": vec_a}, {"values": vec_b}]},
        )
    )
    vectors = await _embedder().embed_documents(["chunk one", "chunk two"])
    assert len(vectors) == 2
    assert all(len(v) == EMBED_DIM for v in vectors)
    assert all(_unit_len(v) == pytest.approx(1.0) for v in vectors)
    sent = route.calls.last.request
    assert b"batchEmbedContents" in str(sent.url).encode()
    assert b"RETRIEVAL_DOCUMENT" in sent.content


async def test_embed_documents_empty_input_skips_call():
    # No respx route registered -> a network call would raise. Empty in, empty out.
    assert await _embedder().embed_documents([]) == []


@respx.mock
async def test_embed_documents_count_mismatch_is_error():
    vec = [1.0] + [0.0] * (EMBED_DIM - 1)
    respx.route(method="POST", url__startswith=f"{BASE}/models/").mock(
        return_value=httpx.Response(200, json={"embeddings": [{"values": vec}]})
    )
    with pytest.raises(RuntimeError, match="returned 1 vectors for 2"):
        await _embedder().embed_documents(["a", "b"])
