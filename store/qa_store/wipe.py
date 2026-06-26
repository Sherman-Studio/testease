"""Admin script — drop every per-run + per-persona collection.

The relaunch (#1009) needs a clean slate: the old 12 SlyReply-specific
personas, their 50+ runs, the screenshot bytes in GridFS, the
discovered_actions distilled from those runs — all GONE. New catalog
gets seeded on next pod boot; new runs populate everything from zero.

REVERSIBLE ONLY VIA ATLAS SNAPSHOT. There is no soft-delete here.

Run it from inside the qa-review pod where it has the Mongo
connection string in env:

    kubectl -n slyreply-qa exec deploy/qa-review -- \\
        python -m qa_store.wipe --confirm

The ``--confirm`` flag is required. Without it the script lists what
WOULD be dropped and exits cleanly — useful for verifying you're on
the right database first.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .schema import Store, connect

# Collections this script drops. Order doesn't matter for Mongo (drops
# are atomic per collection), but the grouping is documented so a
# reviewer can sanity-check the blast radius before running.
_RUN_DATA_COLLECTIONS = [
    # The runs themselves + their associated data
    "qa_runs",
    "qa_findings",
    "qa_run_steps",
    "qa_run_logs",
    # Slice 1 of #1006 — distilled discoveries per run
    "qa_discovered_actions",
    "qa_discovered_tools",
    "qa_discovered_branches",
    # Slice 3 of #1006 — sad-path variants generated from canonicals
    "qa_action_variants",
    # Saved scenarios — they reference old persona ids, so they'd be
    # broken pointers after the catalog swap. Cleaner to drop them.
    "qa_scenarios",
]

# qa_personas gets dropped + re-seeded with the new catalog on the next
# qa-review pod restart. Treated separately so a reviewer can see the
# distinction: run-DATA is being thrown away; the persona CATALOG is
# being REPLACED.
_PERSONA_COLLECTION = "qa_personas"

# GridFS bucket — Mongo splits it into two collections under the hood
# (one for files metadata, one for chunks). Both must be dropped.
_GRIDFS_BUCKET = "qa_screenshots"

# Audit collection. DELIBERATELY excluded from the wipe drop list so a
# history of wipes survives across resets. The /admin nuclear-button
# flow (#1146) reads from this to show "validation traces" — the
# pattern of wipe → run 1 → run 2 → run 3 across resets the operator
# uses to sanity-check the lifecycle epic.
_AUDIT_COLLECTION = "qa_admin_audit"


log = logging.getLogger(__name__)


def _gridfs_collections(bucket: str) -> list[str]:
    """The two physical collections Mongo uses for a GridFS bucket."""
    return [f"{bucket}.files", f"{bucket}.chunks"]


def list_drop_targets(store: Store) -> list[tuple[str, int]]:
    """Enumerate the collections that wipe() would drop, with row counts.

    Read-only — safe to call any time. Used by the no-confirm path so
    the operator can see exactly what's at stake before passing
    ``--confirm``.
    """
    targets: list[tuple[str, int]] = []
    db = store.db
    all_names = set(db.list_collection_names())
    for name in (
        _RUN_DATA_COLLECTIONS
        + [_PERSONA_COLLECTION]
        + _gridfs_collections(_GRIDFS_BUCKET)
    ):
        if name in all_names:
            count = db[name].estimated_document_count()
            targets.append((name, count))
        else:
            targets.append((name, 0))
    return targets


def wipe_for_relaunch(store: Store) -> dict[str, int]:
    """Drop every collection listed above. Idempotent.

    Returns ``{collection_name: rows_before_drop}`` so the caller can
    log the blast radius. Missing collections (already-dropped or
    never-existed) report 0 and are skipped silently.

    This is the destructive entry point — callers MUST gate on operator
    confirmation. The CLI (``__main__`` below) enforces ``--confirm``.
    """
    db = store.db
    all_names = set(db.list_collection_names())
    dropped: dict[str, int] = {}

    for name in (
        _RUN_DATA_COLLECTIONS
        + [_PERSONA_COLLECTION]
        + _gridfs_collections(_GRIDFS_BUCKET)
    ):
        if name not in all_names:
            log.info("wipe: %s already missing, skipping", name)
            dropped[name] = 0
            continue
        count = db[name].estimated_document_count()
        db.drop_collection(name)
        log.info("wipe: dropped %s (%d rows)", name, count)
        dropped[name] = count

    return dropped


def _format_table(rows: list[tuple[str, int]]) -> str:
    """Pretty-print the targets table for the dry-run output."""
    if not rows:
        return "(no targets)"
    width = max(len(name) for name, _ in rows)
    lines = [f"  {'collection'.ljust(width)}  rows"]
    lines.append(f"  {'-' * width}  ----")
    total = 0
    for name, count in rows:
        lines.append(f"  {name.ljust(width)}  {count:>4}")
        total += count
    lines.append(f"  {'-' * width}  ----")
    lines.append(f"  {'TOTAL'.ljust(width)}  {total:>4}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m qa_store.wipe",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help=(
            "Actually drop the collections. Without this flag, the script "
            "lists what WOULD be dropped and exits cleanly (dry-run)."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s: %(message)s",
    )

    store = connect(
        os.environ.get("QA_STORE_URL"),
        os.environ.get("QA_STORE_DB"),
    )

    targets = list_drop_targets(store)
    print(f"Target database: {store.db_name}")
    print(_format_table(targets))

    if not args.confirm:
        print("\nDry run — pass --confirm to actually drop these.")
        return 0

    print("\nDropping...")
    dropped = wipe_for_relaunch(store)
    print(f"\nDone. Dropped {sum(dropped.values())} rows across "
          f"{sum(1 for v in dropped.values() if v > 0)} non-empty "
          f"collections.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
