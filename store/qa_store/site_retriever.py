"""Semantic retrieval over the Site Model (site_knowledge + site_surfaces).

Runs ``$vectorSearch`` against the Atlas indexes the schema slice defined
(``site_knowledge_vector`` on ``body_embedding``; ``site_surfaces_vector`` on
``description_embedding``). Synchronous — pymongo's ``aggregate`` runs the
search server-side.

Mirrors the shape of Sherman's retriever, but self-contained in the product
data layer (no sherman import). The crucial difference: retrieval is **scoped
per (tenant, target)** via the ``$vectorSearch.filter`` on the indexed
``tenant_id`` + ``target_id`` filter fields — one tenant's target can never
surface another's rows. ``kind`` is an optional extra filter.

Threshold-gated and error-swallowing: retrieval is enrichment for a run, so a
missing index / Atlas hiccup logs and returns ``[]`` rather than breaking.
"""

from __future__ import annotations

import logging
from typing import Any

from qa_store.embeddings import EmbeddingProvider
from qa_store.schema import Store

log = logging.getLogger(__name__)

KB_INDEX_NAME = "site_knowledge_vector"
SURFACES_INDEX_NAME = "site_surfaces_vector"
KB_EMBEDDED_FIELD = "body_embedding"
SURFACES_EMBEDDED_FIELD = "description_embedding"

DEFAULT_TOP_K = 3
# Atlas searchScore below which a match is treated as noise (cosine-ish).
DEFAULT_MIN_SCORE = 0.65
# Truncate excerpts so a "related context" block stays cheap in a prompt.
EXCERPT_MAX_CHARS = 600


def _vector_search_pipeline(
    *,
    index: str,
    path: str,
    vector: list[float],
    scope_filter: dict,
    top_k: int,
) -> list[dict]:
    """Build the ``$vectorSearch`` + ``$project`` pipeline. ``scope_filter``
    is the per-(tenant, target[, kind]) MQL filter applied INSIDE the
    ``$vectorSearch`` stage so scoping happens during the ANN search, not
    after."""
    return [
        {
            "$vectorSearch": {
                "index": index,
                "path": path,
                "queryVector": vector,
                "numCandidates": max(top_k * 10, 100),
                "limit": top_k,
                "filter": scope_filter,
            },
        },
        {
            "$project": {
                "_id": 0,
                "entry_id": 1,
                "surface_id": 1,
                "kind": 1,
                "title": 1,
                "path": 1,
                "body": 1,
                "description": 1,
                "applies_to": 1,
                "tenant_id": 1,
                "target_id": 1,
                "searchScore": {"$meta": "vectorSearchScore"},
            },
        },
    ]


def _truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n].rstrip() + "…"


def _run(
    coll: Any,
    pipeline: list[dict],
    *,
    min_score: float,
    id_field: str,
    excerpt_field: str,
) -> list[dict]:
    """Execute the aggregation, drop sub-threshold matches, normalise to
    citation dicts. Failures are logged + swallowed (enrichment, not
    load-bearing)."""
    try:
        docs = list(coll.aggregate(pipeline))
    except Exception:  # noqa: BLE001 — retrieval must not break a run
        log.warning("site vector search failed", exc_info=True)
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for doc in docs:
        score = float(doc.get("searchScore", 0.0))
        if score < min_score:
            continue
        source_id = str(doc.get(id_field) or "")
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        out.append({
            "source_id": source_id,
            "kind": doc.get("kind"),
            "title": doc.get("title") or doc.get("path") or "",
            "excerpt": _truncate(str(doc.get(excerpt_field) or ""), EXCERPT_MAX_CHARS),
            "score": score,
            "applies_to": list(doc.get("applies_to") or []),
            "tenant_id": doc.get("tenant_id"),
            "target_id": doc.get("target_id"),
        })
    return out


def search_site_knowledge(
    store: Store,
    tenant_id: str,
    target_id: str,
    query: str,
    *,
    embeddings: EmbeddingProvider,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    kind: str | None = None,
) -> list[dict]:
    """Top-``top_k`` site_knowledge entries semantically similar to ``query``,
    scoped to ``(tenant_id, target_id)`` (and ``kind`` if given)."""
    if top_k <= 0 or not query.strip():
        return []
    scope: dict = {"tenant_id": tenant_id, "target_id": target_id}
    if kind is not None:
        scope["kind"] = kind
    vector = embeddings.embed(query)
    pipeline = _vector_search_pipeline(
        index=KB_INDEX_NAME, path=KB_EMBEDDED_FIELD, vector=vector,
        scope_filter=scope, top_k=top_k,
    )
    return _run(
        store.site_knowledge, pipeline, min_score=min_score,
        id_field="entry_id", excerpt_field="body",
    )


def search_site_surfaces(
    store: Store,
    tenant_id: str,
    target_id: str,
    query: str,
    *,
    embeddings: EmbeddingProvider,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    kind: str | None = None,
) -> list[dict]:
    """Top-``top_k`` site_surfaces semantically similar to ``query``, scoped to
    ``(tenant_id, target_id)`` (and surface ``kind`` if given)."""
    if top_k <= 0 or not query.strip():
        return []
    scope: dict = {"tenant_id": tenant_id, "target_id": target_id}
    if kind is not None:
        scope["kind"] = kind
    vector = embeddings.embed(query)
    pipeline = _vector_search_pipeline(
        index=SURFACES_INDEX_NAME, path=SURFACES_EMBEDDED_FIELD, vector=vector,
        scope_filter=scope, top_k=top_k,
    )
    return _run(
        store.site_surfaces, pipeline, min_score=min_score,
        id_field="surface_id", excerpt_field="description",
    )


__all__ = [
    "DEFAULT_MIN_SCORE",
    "DEFAULT_TOP_K",
    "KB_INDEX_NAME",
    "SURFACES_INDEX_NAME",
    "search_site_knowledge",
    "search_site_surfaces",
]
