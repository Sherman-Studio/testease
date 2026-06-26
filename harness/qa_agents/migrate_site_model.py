"""Runner: lift slyreply's site knowledge from ``personas.py`` into the Site
Model (qa-store).

Lives in the harness because that's where ``personas.py`` is. It imports the
``PERSONAS`` registry + qa-store and calls the qa-store migration, keeping the
dependency direction one-way (harness → qa-store). Idempotent — re-run to
reconcile in place.

    python -m qa_agents.migrate_site_model

Reads QA_STORE_URL / QA_STORE_DB from the environment (DB-name-agnostic — it
writes to whatever DB qa-store is configured for).
"""

from __future__ import annotations

import logging

from qa_store import connect
from qa_store.site_model_migration import migrate_dogfood

from qa_agents.personas import PERSONAS

log = logging.getLogger(__name__)


def main() -> int:
    store = connect()  # QA_STORE_URL / QA_STORE_DB from env
    try:
        result = migrate_dogfood(store, personas=PERSONAS)
    finally:
        store.close()
    log.info("site-model dogfood migration complete: %s", result)
    print(result)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
