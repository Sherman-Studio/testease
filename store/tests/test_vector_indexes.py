"""Tests for ``ensure_vector_indexes`` — the Site Model ``$vectorSearch`` index
bootstrap.

mongomock can't run ``$vectorSearch`` *or* the Atlas Search index admin
commands, so (like ``test_site_retriever``) these mock the search-index API on
the collections directly and assert: the index model shape we send mongot,
idempotency (skip what already exists), the dim override, and that an
Atlas-Search-less deployment degrades quietly instead of raising.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock

from qa_store.schema import (
    _VECTOR_FILTER_FIELDS,
    _VECTOR_INDEXES,
    DEFAULT_VECTOR_DIM,
    ensure_vector_indexes,
)


def _fake_store(existing: dict[str, list[str]] | None = None) -> types.SimpleNamespace:
    """A Store stand-in whose Site Model collections expose the search-index
    admin API. ``existing`` maps collection attr → already-present index names.
    """
    existing = existing or {}
    store = types.SimpleNamespace()
    for attr, _name, _path in _VECTOR_INDEXES:
        coll = MagicMock(name=attr)
        coll.list_search_indexes.return_value = [
            {"name": n} for n in existing.get(attr, [])
        ]
        setattr(store, attr, coll)
    return store


def _created_model(coll: MagicMock):
    """The SearchIndexModel passed to ``create_search_index`` on ``coll``."""
    (model,), _ = coll.create_search_index.call_args
    return model.document  # pymongo SearchIndexModel → server document


# ── creation ────────────────────────────────────────────────────────────────
def test_creates_both_indexes_when_missing():
    store = _fake_store()
    created = ensure_vector_indexes(store)
    assert created == ["site_knowledge_vector", "site_surfaces_vector"]
    store.site_knowledge.create_search_index.assert_called_once()
    store.site_surfaces.create_search_index.assert_called_once()


def test_knowledge_index_shape():
    store = _fake_store()
    ensure_vector_indexes(store)
    doc = _created_model(store.site_knowledge)
    assert doc["name"] == "site_knowledge_vector"
    assert doc["type"] == "vectorSearch"
    fields = doc["definition"]["fields"]
    vector = next(f for f in fields if f["type"] == "vector")
    assert vector["path"] == "body_embedding"
    assert vector["numDimensions"] == DEFAULT_VECTOR_DIM == 384
    assert vector["similarity"] == "cosine"
    # tenant/target/kind are declared as filter fields so $vectorSearch can
    # scope per (tenant, target[, kind]) during the ANN search.
    filters = {f["path"] for f in fields if f["type"] == "filter"}
    assert filters == set(_VECTOR_FILTER_FIELDS)


def test_surfaces_index_targets_description_embedding():
    store = _fake_store()
    ensure_vector_indexes(store)
    doc = _created_model(store.site_surfaces)
    assert doc["name"] == "site_surfaces_vector"
    vector = next(f for f in doc["definition"]["fields"] if f["type"] == "vector")
    assert vector["path"] == "description_embedding"


def test_dim_override_for_openai_provider():
    """Selecting the 1536-d OpenAI provider recreates the indexes at the new
    dimension — ``dim`` flows straight through to ``numDimensions``."""
    store = _fake_store()
    ensure_vector_indexes(store, dim=1536)
    for attr, _name, _path in _VECTOR_INDEXES:
        doc = _created_model(getattr(store, attr))
        vector = next(f for f in doc["definition"]["fields"] if f["type"] == "vector")
        assert vector["numDimensions"] == 1536


# ── idempotency ─────────────────────────────────────────────────────────────
def test_skips_indexes_that_already_exist():
    store = _fake_store(
        existing={
            "site_knowledge": ["site_knowledge_vector"],
            "site_surfaces": ["site_surfaces_vector"],
        }
    )
    created = ensure_vector_indexes(store)
    assert created == []
    store.site_knowledge.create_search_index.assert_not_called()
    store.site_surfaces.create_search_index.assert_not_called()


def test_creates_only_the_missing_one():
    store = _fake_store(existing={"site_knowledge": ["site_knowledge_vector"]})
    created = ensure_vector_indexes(store)
    assert created == ["site_surfaces_vector"]
    store.site_knowledge.create_search_index.assert_not_called()
    store.site_surfaces.create_search_index.assert_called_once()


# ── graceful degradation ────────────────────────────────────────────────────
def test_no_atlas_search_engine_is_swallowed():
    """A plain mongod / mongomock / not-yet-ready mongot raises on
    ``list_search_indexes``; ensure must skip quietly (retrieval is
    enrichment), not propagate — so a bare connect() never fails for it."""
    store = _fake_store()
    store.site_knowledge.list_search_indexes.side_effect = RuntimeError("no mongot")
    store.site_surfaces.list_search_indexes.side_effect = RuntimeError("no mongot")
    created = ensure_vector_indexes(store)  # must not raise
    assert created == []
    store.site_knowledge.create_search_index.assert_not_called()


def test_create_failure_is_swallowed_and_does_not_block_siblings():
    """A genuine create error is logged, not raised, and the other index is
    still attempted. (Re-check after the failure still shows it absent.)"""
    store = _fake_store()
    store.site_knowledge.create_search_index.side_effect = RuntimeError("boom")
    created = ensure_vector_indexes(store)  # must not raise
    assert created == ["site_surfaces_vector"]
    store.site_surfaces.create_search_index.assert_called_once()


def test_concurrent_peer_creation_is_benign():
    """If a peer ensurer (app startup + the init one-shot both booting) creates
    the index between our list and our create, the create raises but the
    re-check shows it present — we treat it as a benign win, not us, and don't
    re-raise. ``list`` returns empty first (so we try), then the peer's index."""
    store = _fake_store()
    store.site_knowledge.list_search_indexes.side_effect = [
        [],  # pre-create check: not there yet
        [{"name": "site_knowledge_vector"}],  # post-failure re-check: peer made it
    ]
    store.site_knowledge.create_search_index.side_effect = RuntimeError("duplicate")
    created = ensure_vector_indexes(store)  # must not raise
    # We didn't create site_knowledge (the peer did) — only site_surfaces is ours.
    assert created == ["site_surfaces_vector"]
