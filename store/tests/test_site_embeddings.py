"""Tests for the Site Model embedding providers (sync)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from qa_store.embeddings import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_OPENAI_EMBEDDING_MODEL,
    LOCAL_EMBEDDING_DIM,
    OPENAI_EMBEDDING_DIM,
    EmbeddingProvider,
    LocalEmbeddingProvider,
    MockEmbeddingProvider,
    OpenAIEmbeddingProvider,
    embedding_dim_for,
    make_embedding_provider,
)


# ── Mock ──
def test_mock_dim_and_determinism():
    p = MockEmbeddingProvider()
    assert p.dim == DEFAULT_EMBEDDING_DIM == 384
    assert len(p.embed("hello")) == 384
    assert p.embed("hello world") == p.embed("hello world")
    assert p.embed("a") != p.embed("b")


def test_mock_satisfies_protocol():
    assert isinstance(MockEmbeddingProvider(), EmbeddingProvider)


# ── Local (default) with an injected fake model ──
class _FakeFastembed:
    def __init__(self, vec):
        self._vec = vec
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return iter([list(self._vec) for _ in texts])


def test_local_wraps_sync_model():
    fake = _FakeFastembed([0.25] * LOCAL_EMBEDDING_DIM)
    p = LocalEmbeddingProvider(model=fake, dim=LOCAL_EMBEDDING_DIM)
    v = p.embed("smtp latency")
    assert len(v) == 384
    assert all(isinstance(x, float) for x in v)
    assert p.dim == 384
    assert fake.calls == [["smtp latency"]]


def test_local_handles_empty_text():
    fake = _FakeFastembed([0.0])
    LocalEmbeddingProvider(model=fake, dim=1).embed(None)  # type: ignore[arg-type]
    assert fake.calls == [[""]]


def test_local_satisfies_protocol():
    assert isinstance(
        LocalEmbeddingProvider(model=_FakeFastembed([0.0])), EmbeddingProvider,
    )


# ── OpenAI (opt-in) with an injected fake client ──
def test_openai_calls_api_with_right_model():
    client = MagicMock()
    client.embeddings.create = MagicMock(
        return_value=SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])]),
    )
    p = OpenAIEmbeddingProvider(client=client, dim=2)
    assert p.embed("hello") == [0.1, 0.2]
    kwargs = client.embeddings.create.call_args.kwargs
    assert kwargs["model"] == DEFAULT_OPENAI_EMBEDDING_MODEL
    assert kwargs["input"] == "hello"


def test_openai_requires_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OpenAI key"):
        OpenAIEmbeddingProvider()


# ── Opt-in real fastembed (downloads weights) ──
@pytest.mark.skipif(
    os.environ.get("QA_VECTOR_E2E") != "1",
    reason="downloads the real fastembed model — opt in with QA_VECTOR_E2E=1",
)
def test_local_real_model_returns_384d():
    v = LocalEmbeddingProvider().embed("the relay connection pool is exhausted")
    assert len(v) == 384
    assert all(isinstance(x, float) for x in v)


# ── Provider selection (QA_EMBEDDING_PROVIDER) ──
def test_make_provider_defaults_to_local(monkeypatch):
    """No env set ⇒ the key-free local provider (the product default)."""
    monkeypatch.delenv("QA_EMBEDDING_PROVIDER", raising=False)
    assert isinstance(make_embedding_provider(), LocalEmbeddingProvider)


def test_make_provider_reads_env(monkeypatch):
    monkeypatch.setenv("QA_EMBEDDING_PROVIDER", "mock")
    assert isinstance(make_embedding_provider(), MockEmbeddingProvider)


def test_make_provider_routes_to_openai(monkeypatch):
    """Routing to the OpenAI provider is provable without the openai SDK: its
    __init__ checks for a key BEFORE the lazy ``import openai``, so selecting it
    with no key raises the provider's own ValueError — confirming the factory
    built an OpenAIEmbeddingProvider, not some other one."""
    monkeypatch.setenv("QA_EMBEDDING_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OpenAIEmbeddingProvider needs an OpenAI key"):
        make_embedding_provider()


def test_make_provider_explicit_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("QA_EMBEDDING_PROVIDER", "openai")
    # Explicit wins, so no OPENAI_API_KEY needed.
    assert isinstance(make_embedding_provider("mock"), MockEmbeddingProvider)


def test_make_provider_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("QA_EMBEDDING_PROVIDER", "  MOCK ")
    assert isinstance(make_embedding_provider(), MockEmbeddingProvider)


def test_make_provider_unknown_raises(monkeypatch):
    monkeypatch.setenv("QA_EMBEDDING_PROVIDER", "voyage")
    with pytest.raises(ValueError, match="unknown QA_EMBEDDING_PROVIDER"):
        make_embedding_provider()


def test_embedding_dim_for_tracks_provider(monkeypatch):
    monkeypatch.delenv("QA_EMBEDDING_PROVIDER", raising=False)
    assert embedding_dim_for() == LOCAL_EMBEDDING_DIM == 384
    assert embedding_dim_for("openai") == OPENAI_EMBEDDING_DIM == 1536
    assert embedding_dim_for("mock") == DEFAULT_EMBEDDING_DIM


def test_embedding_dim_for_unknown_raises():
    with pytest.raises(ValueError, match="unknown QA_EMBEDDING_PROVIDER"):
        embedding_dim_for("nope")
