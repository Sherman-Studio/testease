"""Embedding providers for the Site Model vector layer.

Self-contained in the product data layer — this mirrors the *pattern* of
Sherman's embedding seam (BYOK, local default, pluggable Protocol) but does
NOT import sherman-ai and shares no database with it. qa-store is pymongo and
**synchronous**, so these providers are sync too (``fastembed`` is sync;
pymongo's ``aggregate`` runs ``$vectorSearch`` server-side).

The **default is a LOCAL, no-key model** — ``LocalEmbeddingProvider``
(``fastembed`` running ``BAAI/bge-small-en-v1.5``, 384-d). It runs in-process
on CPU, so a self-hosted Test Ease needs no embedding key — only whatever token
runs the agent. ``OpenAIEmbeddingProvider`` is an opt-in hosted adapter.
``MockEmbeddingProvider`` keeps the retriever/reconciler unit-testable without
a model.

Selection is **env-driven**: ``make_embedding_provider()`` reads
``QA_EMBEDDING_PROVIDER`` (``local`` | ``openai`` | ``mock``; default
``local``). The selected provider's output dimension MUST match
``numDimensions`` on the Atlas vector indexes — so the index bootstrap sizes
itself via ``embedding_dim_for()`` rather than a hardcoded 384, keeping the two
in lockstep when the provider changes.

NOTE (runtime wiring, deferred): whatever image runs the reconciler must have
the fastembed model **baked into it at build time** (read-only-root containers
can't download it at runtime) — same as Sherman's Dockerfile. That image change
lands with the harness/review-ui wiring slice, not here.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Protocol, runtime_checkable

# Dimension of the DEFAULT (local) provider — bge-small-en-v1.5 is 384-d, which
# is what the site_knowledge_vector / site_surfaces_vector indexes are sized for.
DEFAULT_EMBEDDING_DIM = 384

DEFAULT_LOCAL_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
LOCAL_EMBEDDING_DIM = 384

# Opt-in hosted model — 1536-d, so selecting it would require re-creating the
# Atlas indexes at numDimensions=1536 and re-embedding the corpus.
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
OPENAI_EMBEDDING_DIM = 1536


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Anything that turns text into a fixed-dimension vector (synchronously)."""

    def embed(self, text: str) -> list[float]: ...

    @property
    def dim(self) -> int: ...


class LocalEmbeddingProvider:
    """DEFAULT provider: a local fastembed model, no API key.

    Runs ``BAAI/bge-small-en-v1.5`` (384-d) on CPU via ``fastembed`` (ONNX).
    The ``fastembed`` import + model load are lazy (cached on first ``embed``)
    so importing this module stays cheap; ``model`` is injectable so unit tests
    pass a fake and never download weights. ``cache_dir`` defaults to the env
    ``QA_FASTEMBED_CACHE`` (the baked-image path; see the module note).
    """

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_LOCAL_EMBEDDING_MODEL,
        dim: int = LOCAL_EMBEDDING_DIM,
        cache_dir: str | None = None,
        model: Any | None = None,
    ) -> None:
        self._model_name = model_name
        self._dim = dim
        self._cache_dir = cache_dir or os.environ.get("QA_FASTEMBED_CACHE")
        self._model = model

    @property
    def dim(self) -> int:
        return self._dim

    def _ensure_model(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding  # lazy — heavy import

            self._model = TextEmbedding(
                model_name=self._model_name, cache_dir=self._cache_dir,
            )
        return self._model

    def embed(self, text: str) -> list[float]:
        model = self._ensure_model()
        # fastembed's .embed() yields one vector (np array) per input text.
        vectors = list(model.embed([text or ""]))
        return [float(x) for x in vectors[0]]


class OpenAIEmbeddingProvider:
    """OPT-IN hosted provider (``text-embedding-3-small``, 1536-d).

    The user brings ``OPENAI_API_KEY``. The ``openai`` SDK is imported lazily;
    ``client`` is injectable so tests pass a fake without a key or a network
    call. Selecting this requires the Atlas indexes at numDimensions=1536.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_OPENAI_EMBEDDING_MODEL,
        dim: int = OPENAI_EMBEDDING_DIM,
        client: Any | None = None,
    ) -> None:
        self._model = model
        self._dim = dim
        if client is not None:
            self._client = client
            return
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError(
                "OpenAIEmbeddingProvider needs an OpenAI key — pass api_key "
                "or set OPENAI_API_KEY",
            )
        from openai import OpenAI  # lazy

        self._client = OpenAI(api_key=key)

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, text: str) -> list[float]:
        resp = self._client.embeddings.create(model=self._model, input=text or "")
        return list(resp.data[0].embedding)


class MockEmbeddingProvider:
    """Deterministic test/dev embedder — hashes text into a stable vector.

    Same string → same vector, different strings → different vectors. Good
    enough to exercise reconciler/pipeline plumbing without a live model.
    """

    def __init__(self, dim: int = DEFAULT_EMBEDDING_DIM) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, text: str) -> list[float]:
        normalised = (text or "").strip().lower()
        seed = hashlib.sha256(normalised.encode("utf-8")).digest()
        out: list[float] = []
        block = seed
        while len(out) < self._dim:
            for b in block:
                out.append((b - 127.5) / 127.5)
                if len(out) >= self._dim:
                    break
            block = hashlib.sha256(block).digest()
        return out


# ---------------------------------------------------------------------------
# Provider selection — QA_EMBEDDING_PROVIDER (default: local, no key).
# ---------------------------------------------------------------------------
# The env name every entry point reads to pick a provider. Default ``local``
# keeps a self-hosted Test Ease key-free (fastembed runs in-process). The
# selected provider's dimension MUST match ``numDimensions`` on the Atlas
# vector indexes, so boot-time index creation sizes itself via
# ``embedding_dim_for`` (see qa_store.schema.ensure_vector_indexes callers).
DEFAULT_EMBEDDING_PROVIDER = "local"

# Output dimension per provider — the source of truth the index bootstrap and
# the reconciler both consult so they can never drift apart.
_PROVIDER_DIMS: dict[str, int] = {
    "local": LOCAL_EMBEDDING_DIM,
    "openai": OPENAI_EMBEDDING_DIM,
    "mock": DEFAULT_EMBEDDING_DIM,
}


def _selected_provider(provider: str | None) -> str:
    name = (
        provider
        if provider is not None
        else os.environ.get("QA_EMBEDDING_PROVIDER", DEFAULT_EMBEDDING_PROVIDER)
    ).strip().lower()
    if name not in _PROVIDER_DIMS:
        raise ValueError(
            f"unknown QA_EMBEDDING_PROVIDER {name!r}; expected one of "
            f"{', '.join(sorted(_PROVIDER_DIMS))}",
        )
    return name


def embedding_dim_for(provider: str | None = None) -> int:
    """The output dimension of the selected provider.

    The Atlas vector indexes must be sized to this (``ensure_vector_indexes``
    takes ``dim``), so the index bootstrap stays in lockstep with whatever
    ``QA_EMBEDDING_PROVIDER`` selects. ``provider=None`` reads the env.
    """
    return _PROVIDER_DIMS[_selected_provider(provider)]


def make_embedding_provider(provider: str | None = None) -> EmbeddingProvider:
    """Construct the embedding provider selected by ``QA_EMBEDDING_PROVIDER``.

    - ``local`` (DEFAULT) → ``LocalEmbeddingProvider`` (fastembed bge-small,
      384-d, in-process, **no key**).
    - ``openai`` → ``OpenAIEmbeddingProvider`` (text-embedding-3-small, 1536-d);
      needs ``OPENAI_API_KEY`` and the ``qa-store[vector]`` extra. NB: the Atlas
      indexes must already be 1536-d — switching providers after indexes exist
      means dropping + recreating them and re-embedding the corpus.
    - ``mock`` → ``MockEmbeddingProvider`` (deterministic; dev/tests only).

    ``provider=None`` reads the env. Heavy SDKs (fastembed / openai) are
    imported lazily inside the provider, so this stays cheap until first use.
    Tests that need to inject a fake model/client construct the provider class
    directly rather than going through this factory.
    """
    name = _selected_provider(provider)
    if name == "local":
        return LocalEmbeddingProvider()
    if name == "openai":
        return OpenAIEmbeddingProvider()
    return MockEmbeddingProvider()
