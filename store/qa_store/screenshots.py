"""GridFS storage for Playwright screenshot captures (#860, Slice 3).

The QA persona harness intercepts every
``mcp__playwright__browser_take_screenshot`` tool result during a run and
ships the PNG bytes here. The review UI's Transcript tab fetches them back
to render inline thumbnails next to each step.

Why GridFS, not an embedded blob on the step doc:

* Mongo's per-document cap is 16 MB. A typical Playwright PNG is 100-500 KB,
  so a heavy persona with 50 screenshots in one run is already 25 MB — past
  the cap before you account for the rest of the step record.
* GridFS chunks blobs across the ``qa_screenshots.chunks`` collection and
  serves them efficiently as streams (the review UI's image endpoint pipes
  bytes straight through; nothing decodes the whole PNG in memory).
* It's the standard PyMongo path — no new infra, no new auth, just two
  more collections in the same Atlas cluster the rest of qa-store lives on.

Per-image size sanity check (from the issue body): ~200 KB avg × ~30 PNGs
per persona × 7 personas × ~50 runs/month ≈ 2 GB/month, well inside Atlas
free-tier-plus.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bson import ObjectId
from gridfs import GridFSBucket

if TYPE_CHECKING:
    from .schema import Store

# Dedicated bucket name so the chunks (`qa_screenshots.chunks`) and files
# (`qa_screenshots.files`) collections are obviously screenshot data when an
# operator pokes around in mongosh. Default GridFS bucket name (`fs`) would
# collide with any other GridFS user in the same database.
_BUCKET = "qa_screenshots"


def _bucket(store: Store) -> GridFSBucket:
    """Return a GridFSBucket bound to ``store``'s database.

    Cheap — GridFSBucket is a thin wrapper over a couple of collection
    handles; building it per call avoids a stale-handle pitfall when the
    Store's underlying connection is recycled across worker forks.
    """
    return GridFSBucket(store.db, bucket_name=_BUCKET)


def store_screenshot(
    store: Store,
    data: bytes,
    *,
    run_id: str,
    persona_id: str,
    step_n: int,
    content_type: str = "image/png",
) -> ObjectId:
    """Write ``data`` to GridFS and return the resulting ObjectId.

    ``run_id`` / ``persona_id`` / ``step_n`` are persisted on the GridFS
    file metadata so an admin can grep them out via mongosh without
    cross-referencing back to ``qa_run_steps``. The Transcript-tab read
    path only needs the oid, but keeping the metadata makes orphan cleanup
    (a future job that deletes screenshots whose step disappeared) a
    one-collection scan instead of a join.

    Filename uses the same triple so it's human-readable in mongosh:
    ``{run_id}/{persona_id}/step-{step_n:03d}.png``.
    """
    filename = f"{run_id}/{persona_id}/step-{step_n:03d}.png"
    return _bucket(store).upload_from_stream(
        filename,
        data,
        metadata={
            "run_id": run_id,
            "persona_id": persona_id,
            "step_n": int(step_n),
            "content_type": content_type,
        },
    )


def fetch_screenshot(store: Store, oid: ObjectId | str) -> bytes:
    """Read the full PNG by ObjectId.

    Returns the raw bytes; the review UI's endpoint streams them straight
    out with ``Content-Type: image/png``. Raises ``gridfs.NoFile`` if the
    oid doesn't exist — the endpoint translates that to 404. Accepts a
    string oid for caller convenience (the API receives oids as URL
    fragments).
    """
    if isinstance(oid, str):
        oid = ObjectId(oid)
    return _bucket(store).open_download_stream(oid).read()


def delete_screenshot(store: Store, oid: ObjectId | str) -> None:
    """Delete one screenshot blob.

    Used by the orphan-cleanup path (a future job that prunes screenshots
    whose step has been deleted) and by tests. ``gridfs.NoFile`` is
    swallowed — deleting a missing screenshot is a no-op, not an error.
    """
    from gridfs.errors import NoFile

    if isinstance(oid, str):
        oid = ObjectId(oid)
    try:
        _bucket(store).delete(oid)
    except NoFile:
        pass
