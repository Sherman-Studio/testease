"""Tests for the Site Model retriever.

$vectorSearch can't run on mongomock, so these mock the ``aggregate`` cursor
directly to assert pipeline shape, the per-(tenant, target) scoping filter,
threshold gating, and citation normalisation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from qa_store.embeddings import MockEmbeddingProvider
from qa_store.site_retriever import (
    KB_INDEX_NAME,
    SURFACES_INDEX_NAME,
    search_site_knowledge,
    search_site_surfaces,
)

_EMB = MockEmbeddingProvider(dim=8)


def _store_with(collection_attr: str, docs: list[dict]):
    coll = MagicMock()
    coll.aggregate = MagicMock(return_value=list(docs))
    store = SimpleNamespace(**{collection_attr: coll})
    return store, coll


def _vector_stage(coll) -> dict:
    pipeline = coll.aggregate.call_args.args[0]
    return pipeline[0]["$vectorSearch"]


def test_knowledge_pipeline_scoped_and_shaped():
    docs = [{
        "entry_id": "k1", "kind": "by_design", "body": "check the relay pool",
        "applies_to": ["p1"], "searchScore": 0.91,
        "tenant_id": "acme", "target_id": "slyreply",
    }]
    store, coll = _store_with("site_knowledge", docs)

    out = search_site_knowledge(
        store, "acme", "slyreply", "relay pool", embeddings=_EMB, top_k=3,
    )

    assert [c["source_id"] for c in out] == ["k1"]
    assert out[0]["kind"] == "by_design"
    assert out[0]["excerpt"] == "check the relay pool"
    vs = _vector_stage(coll)
    assert vs["index"] == KB_INDEX_NAME
    assert vs["path"] == "body_embedding"
    assert len(vs["queryVector"]) == 8  # == embeddings.dim
    assert vs["filter"] == {"tenant_id": "acme", "target_id": "slyreply"}


def test_threshold_gates_low_scores():
    docs = [
        {"entry_id": "k1", "body": "x", "searchScore": 0.9},
        {"entry_id": "k2", "body": "y", "searchScore": 0.4},  # below 0.65
    ]
    store, _ = _store_with("site_knowledge", docs)
    out = search_site_knowledge(store, "t", "tg", "q", embeddings=_EMB)
    assert [c["source_id"] for c in out] == ["k1"]


def test_kind_filter_added_when_given():
    store, coll = _store_with("site_knowledge", [])
    search_site_knowledge(
        store, "t", "tg", "q", embeddings=_EMB, kind="by_design",
    )
    assert _vector_stage(coll)["filter"] == {
        "tenant_id": "t", "target_id": "tg", "kind": "by_design",
    }


def test_empty_query_short_circuits_without_aggregating():
    store, coll = _store_with("site_knowledge", [])
    assert search_site_knowledge(store, "t", "tg", "  ", embeddings=_EMB) == []
    coll.aggregate.assert_not_called()


def test_aggregate_failure_is_swallowed():
    coll = MagicMock()
    coll.aggregate = MagicMock(side_effect=RuntimeError("atlas down"))
    store = SimpleNamespace(site_knowledge=coll)
    assert search_site_knowledge(store, "t", "tg", "q", embeddings=_EMB) == []


def test_surfaces_use_surface_index_and_field():
    docs = [{
        "surface_id": "s1", "kind": "page", "path": "/pricing",
        "description": "the pricing page", "searchScore": 0.8,
    }]
    store, coll = _store_with("site_surfaces", docs)
    out = search_site_surfaces(store, "t", "tg", "price", embeddings=_EMB)
    assert out[0]["source_id"] == "s1"
    assert out[0]["title"] == "/pricing"
    vs = _vector_stage(coll)
    assert vs["index"] == SURFACES_INDEX_NAME
    assert vs["path"] == "description_embedding"
    assert vs["filter"] == {"tenant_id": "t", "target_id": "tg"}
