"""Load the target's by-design site_knowledge and render it for prompts.

Closes the code→data→used loop: the BY-DESIGN suppressions that were migrated
out of personas.py into qa-store (#2097) get loaded here, ONCE per run, and
injected into each persona's explore prompt so personas stop re-flagging
intentional behaviour.

LIST-based — no vectors/model/index. It reads ``site_knowledge`` rows for the
run's ``(DEFAULT_TENANT, target_id)`` with ``kind == by_design`` via the
qa-store DAO.

Graceful by design: if qa-store is unreachable (e.g. a local file-sink run with
no store), the collection is empty, or anything errors, it returns ``""`` and
the run proceeds exactly as today. A short server-selection timeout keeps a
storeless local run from hanging on the default 30s.
"""

from __future__ import annotations

import logging

from .config import Config

log = logging.getLogger(__name__)

# Heading + lead-in the personas see. The bodies themselves already start with
# "BY DESIGN —" (the migration lifted them verbatim), so they're self-labelling.
_HEADER = "## Known by-design behaviours for this site"
_INTRO = (
    "The following are intentional and already triaged — do NOT file them as "
    "findings/bugs:"
)
# The corpus is ~90 short entries; cap defensively so a future bulk import can't
# balloon the prompt. If ever exceeded, the block notes how many were omitted.
_MAX_ENTRIES = 200
# Don't wait the pymongo default (30s) when there's no store reachable.
_SERVER_SELECT_TIMEOUT_MS = 2000


def _format_block(bodies: list[str]) -> str:
    """Render the by-design bodies as a labelled, bulleted block (or "")."""
    if not bodies:
        return ""
    shown = bodies[:_MAX_ENTRIES]
    lines = [_HEADER, _INTRO, ""]
    for body in shown:
        lines.append(f"- {body}")
        lines.append("")
    if len(bodies) > _MAX_ENTRIES:
        lines.append(f"(+{len(bodies) - _MAX_ENTRIES} more by-design entries not shown)")
    return "\n".join(lines).rstrip() + "\n"


def load_by_design_block(config: Config, *, store: object | None = None) -> str:
    """Return the formatted by-design block for this run's target, or "".

    Call once per run (at run setup), then reuse for every persona — never
    per-persona-per-turn. ``store`` is injectable for tests; in production it's
    built from the run's qa-store config (short server-selection timeout so a
    storeless local run fails fast instead of hanging the default 30s).
    """
    own_client = None
    try:
        from qa_store.schema import DEFAULT_TENANT  # noqa: PLC0415
        from qa_store.site_model import list_knowledge_by_target  # noqa: PLC0415

        if store is None:
            from pymongo import MongoClient  # noqa: PLC0415
            from qa_store.schema import Store  # noqa: PLC0415

            own_client = MongoClient(
                config.qa_store_url,
                serverSelectionTimeoutMS=_SERVER_SELECT_TIMEOUT_MS,
            )
            store = Store(client=own_client, db_name=config.qa_store_db)
        rows = list_knowledge_by_target(store, DEFAULT_TENANT, config.target_id)
    except Exception:  # noqa: BLE001 — never break a run over enrichment
        log.warning(
            "by-design knowledge unavailable for target %r; running without it",
            config.target_id, exc_info=True,
        )
        return ""
    finally:
        if own_client is not None:
            own_client.close()

    bodies = [
        (r.get("body") or "").strip()
        for r in rows
        if r.get("kind") == "by_design" and (r.get("body") or "").strip()
    ]
    if bodies:
        log.info(
            "loaded %d by-design knowledge entr%s for target %r",
            len(bodies), "y" if len(bodies) == 1 else "ies", config.target_id,
        )
    return _format_block(bodies)
