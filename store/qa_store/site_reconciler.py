"""Site Model embedding reconciler.

Backfills + keeps current the embeddings the retriever reads: ``body_embedding``
on ``site_knowledge`` and ``description_embedding`` on ``site_surfaces``. Like
Sherman's KB reconciler, it re-embeds an entry only when its source text
changed, detected by comparing ``content_sha(text)`` against the stored
fingerprint (``embedded_body_sha`` / ``embedded_sha``). This decouples writers
(the admin editor, the crawler) from embedding — they just write the text; the
reconciler fills the vectors out-of-band on a cadence.

Synchronous (pymongo). Bounded per call (``batch_limit`` is a shared budget
across both collections), idempotent (a pass with nothing stale does no
writes), and per-entry-failure-tolerant (one bad embed is logged + retried next
pass). Optionally scoped to a single ``(tenant_id, target_id)``.

This is the LIBRARY. Wiring it onto a schedule inside the harness/review-ui
runtime — and baking the fastembed model into those images — is a later slice.
"""

from __future__ import annotations

import logging
from typing import Any

from qa_store.embeddings import EmbeddingProvider
from qa_store.schema import Store
from qa_store.site_model import content_sha

log = logging.getLogger(__name__)

DEFAULT_BATCH_LIMIT = 50


def _reconcile_collection(
    coll: Any,
    scope: dict,
    embeddings: EmbeddingProvider,
    *,
    text_field: str,
    emb_field: str,
    sha_field: str,
    id_field: str,
    limit: int,
) -> int:
    """Embed up to ``limit`` docs in ``coll`` whose ``text_field`` changed
    since last embedding. Returns how many were embedded."""
    if limit <= 0:
        return 0
    embedded = 0
    projection = {text_field: 1, sha_field: 1, id_field: 1}
    for doc in coll.find(scope, projection):
        if embedded >= limit:
            break
        text = doc.get(text_field) or ""
        want = content_sha(text)
        if doc.get(sha_field) == want:
            continue  # already up to date
        try:
            vector = embeddings.embed(text)
        except Exception:  # noqa: BLE001 — one bad entry retries next pass
            log.warning(
                "site-reconcile: embed failed for %s=%s, skipping",
                id_field, doc.get(id_field), exc_info=True,
            )
            continue
        coll.update_one(
            {"_id": doc["_id"]},
            {"$set": {emb_field: vector, sha_field: want}},
        )
        embedded += 1
    return embedded


def reconcile_site_embeddings(
    store: Store,
    *,
    embeddings: EmbeddingProvider,
    tenant_id: str | None = None,
    target_id: str | None = None,
    batch_limit: int = DEFAULT_BATCH_LIMIT,
) -> int:
    """(Re-)embed stale site_knowledge bodies + site_surfaces descriptions.

    ``batch_limit`` is a shared budget across both collections (knowledge
    first, then surfaces with whatever's left). Pass ``tenant_id`` /
    ``target_id`` to scope a pass to one target. Returns the total embedded."""
    scope: dict = {}
    if tenant_id is not None:
        scope["tenant_id"] = tenant_id
    if target_id is not None:
        scope["target_id"] = target_id

    total = _reconcile_collection(
        store.site_knowledge, scope, embeddings,
        text_field="body", emb_field="body_embedding",
        sha_field="embedded_body_sha", id_field="entry_id",
        limit=batch_limit,
    )
    total += _reconcile_collection(
        store.site_surfaces, scope, embeddings,
        text_field="description", emb_field="description_embedding",
        sha_field="embedded_sha", id_field="surface_id",
        limit=batch_limit - total,
    )
    if total:
        log.info("site-reconcile: embedded %d entr%s",
                 total, "y" if total == 1 else "ies")
    return total
