"""Tests for the Site Model embedding reconciler (mongomock + MockEmbeddings).

The reconciler uses find/update_one (not $vectorSearch), so mongomock works.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import mongomock
import pytest

from qa_store.embeddings import MockEmbeddingProvider
from qa_store.schema import Store
from qa_store.site_model import content_sha
from qa_store.site_reconciler import reconcile_site_embeddings

_EMB = MockEmbeddingProvider(dim=16)


@pytest.fixture
def store() -> Store:
    return Store(client=mongomock.MongoClient(), db_name="testease_test")


def _ins_kb(store, entry_id, body, *, sha=None, tenant="t", target="tg"):
    store.site_knowledge.insert_one({
        "tenant_id": tenant, "target_id": target, "entry_id": entry_id,
        "kind": "by_design", "body": body,
        "body_embedding": None, "embedded_body_sha": sha,
    })


def _ins_surface(store, surface_id, desc, *, sha=None):
    store.site_surfaces.insert_one({
        "tenant_id": "t", "target_id": "tg", "surface_id": surface_id,
        "kind": "page", "description": desc,
        "description_embedding": None, "embedded_sha": sha,
    })


def _kb(store, entry_id):
    return store.site_knowledge.find_one({"entry_id": entry_id}, {"_id": 0})


def test_backfills_both_collections(store):
    _ins_kb(store, "k1", "the relay pool")
    _ins_surface(store, "s1", "the pricing page")

    n = reconcile_site_embeddings(store, embeddings=_EMB)

    assert n == 2
    k = _kb(store, "k1")
    assert len(k["body_embedding"]) == 16
    assert k["embedded_body_sha"] == content_sha("the relay pool")
    s = store.site_surfaces.find_one({"surface_id": "s1"}, {"_id": 0})
    assert len(s["description_embedding"]) == 16
    assert s["embedded_sha"] == content_sha("the pricing page")


def test_skips_fresh(store):
    _ins_kb(store, "k1", "already embedded", sha=content_sha("already embedded"))
    assert reconcile_site_embeddings(store, embeddings=_EMB) == 0
    assert _kb(store, "k1")["body_embedding"] is None


def test_re_embeds_on_edit(store):
    _ins_kb(store, "k1", "edited body", sha="stale-sha")
    assert reconcile_site_embeddings(store, embeddings=_EMB) == 1
    assert _kb(store, "k1")["embedded_body_sha"] == content_sha("edited body")


def test_bounded_by_batch_limit(store):
    for i in range(3):
        _ins_kb(store, f"k{i}", f"body {i}")
    assert reconcile_site_embeddings(store, embeddings=_EMB, batch_limit=2) == 2
    assert reconcile_site_embeddings(store, embeddings=_EMB, batch_limit=2) == 1
    assert reconcile_site_embeddings(store, embeddings=_EMB) == 0


def test_idempotent(store):
    _ins_kb(store, "k1", "some body")
    assert reconcile_site_embeddings(store, embeddings=_EMB) == 1
    assert reconcile_site_embeddings(store, embeddings=_EMB) == 0


def test_empty_is_noop(store):
    assert reconcile_site_embeddings(store, embeddings=_EMB) == 0


def test_scope_to_target(store):
    _ins_kb(store, "k1", "body a", target="tg")
    _ins_kb(store, "k2", "body b", target="other")
    n = reconcile_site_embeddings(store, embeddings=_EMB, target_id="tg")
    assert n == 1
    assert _kb(store, "k1")["body_embedding"] is not None
    assert _kb(store, "k2")["body_embedding"] is None  # other target untouched


def test_one_bad_embed_is_skipped(store):
    _ins_kb(store, "k1", "first")
    _ins_kb(store, "k2", "second")
    failing = MagicMock()
    failing.embed = MagicMock(side_effect=[RuntimeError("boom"), [0.0] * 16])
    n = reconcile_site_embeddings(store, embeddings=failing)
    assert n == 1  # only the good one counted; the bad one retries next pass
