"""One-shot initialiser for the Site Model ``$vectorSearch`` indexes.

``connect()`` already ensures these on every boot (best-effort), but a fresh
``mongodb-atlas-local`` answers ``mongod`` pings well before its bundled
``mongot`` is ready to accept search-index creation — so the app's first
boot-time attempt can quietly no-op. This module is the deterministic path: it
polls until mongot accepts a ``listSearchIndexes`` round-trip, then ensures the
indexes, so an operator (or a compose init step) can run::

    python -m qa_store.init_vector_indexes

It exits 0 once the indexes exist (created or already-present), or non-zero if
mongot never became ready inside the timeout.

Env: ``QA_STORE_URL`` / ``QA_STORE_DB`` (same as ``connect()``);
``QA_VECTOR_INIT_TIMEOUT_S`` (default 120) bounds the readiness wait.
"""

from __future__ import annotations

import logging
import os
import sys
import time

from .schema import _VECTOR_INDEXES, connect, ensure_vector_indexes

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 3.0


def _search_engine_ready(store) -> bool:
    """True once mongot answers a ``listSearchIndexes`` without raising.

    On a plain mongod / mongomock this raises (no Atlas Search); during the
    atlas-local warm-up window it also raises until mongot is up.
    """
    attr = _VECTOR_INDEXES[0][0]  # any Site Model collection will do
    try:
        list(getattr(store, attr).list_search_indexes())
        return True
    except Exception:  # noqa: BLE001 — not ready / unsupported
        return False


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    timeout_s = float(os.environ.get("QA_VECTOR_INIT_TIMEOUT_S", "120"))

    store = connect()  # also runs the boot-time best-effort ensure
    deadline = time.monotonic() + timeout_s
    while not _search_engine_ready(store):
        if time.monotonic() >= deadline:
            log.error(
                "Atlas Search (mongot) not ready after %.0fs — is this "
                "mongodb-atlas-local or cloud Atlas? Plain mongod has no "
                "$vectorSearch.", timeout_s,
            )
            return 1
        log.info("waiting for Atlas Search engine (mongot) to accept indexes…")
        time.sleep(_POLL_INTERVAL_S)

    created = ensure_vector_indexes(store)
    if created:
        log.info("created vector indexes: %s", ", ".join(created))
    else:
        log.info("vector indexes already present — nothing to do")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
