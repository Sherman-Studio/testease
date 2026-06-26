"""Tests for the GridFS screenshot helpers (#860).

mongomock doesn't satisfy ``GridFSBucket``'s ``Database`` type check, so we
substitute a tiny in-memory ``FakeBucket`` that implements the four methods
``screenshots`` actually calls (``upload_from_stream``, ``open_download_stream``,
``delete``). That's enough to exercise:

* filename + metadata shape on upload
* string-oid → ObjectId conversion on read
* the ``NoFile`` swallow on delete

For end-to-end GridFS-against-real-Mongo coverage, the live harness writes
on every Atlas-sink run and the review UI streams the bytes back — that
loop catches schema regressions an in-memory fake can't.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from bson import ObjectId
from gridfs.errors import NoFile

from qa_store import screenshots
from qa_store.schema import Store


@dataclass
class _FakeStream:
    """Stand-in for the GridOut object the bucket hands back on read."""

    data: bytes

    def read(self) -> bytes:
        return self.data


@dataclass
class _FakeBucket:
    """In-memory GridFS-shaped stub.

    Only implements what ``qa_store.screenshots`` calls. Records every
    upload + delete so tests can inspect call history.
    """

    files: dict = field(default_factory=dict)
    uploads: list = field(default_factory=list)
    deletes: list = field(default_factory=list)

    def upload_from_stream(self, filename, data, metadata=None):
        oid = ObjectId()
        self.files[oid] = {"filename": filename, "data": data,
                           "metadata": metadata or {}}
        self.uploads.append({"filename": filename, "metadata": metadata or {},
                             "oid": oid})
        return oid

    def open_download_stream(self, oid):
        if oid not in self.files:
            raise NoFile(f"no file with id {oid}")
        return _FakeStream(self.files[oid]["data"])

    def delete(self, oid):
        self.deletes.append(oid)
        if oid not in self.files:
            raise NoFile(f"no file with id {oid}")
        del self.files[oid]


@pytest.fixture
def store(monkeypatch) -> Store:
    """A Store with `screenshots._bucket` patched to a FakeBucket.

    Patches the module-level helper rather than constructing a real bucket —
    each test gets a fresh FakeBucket via a closure so test isolation is
    clean.
    """
    import mongomock
    client = mongomock.MongoClient()
    s = Store(client=client, db_name="slyreply_qa_test")
    fake = _FakeBucket()
    monkeypatch.setattr(screenshots, "_bucket", lambda _store: fake)
    # Expose the fake on the store object so the tests can inspect it
    # without re-importing the module's patched symbol.
    s._fake_bucket = fake  # type: ignore[attr-defined]
    return s


def test_store_screenshot_returns_oid_and_records_metadata(store):
    oid = screenshots.store_screenshot(
        store, b"PNGDATA",
        run_id="run-1", persona_id="first-impression-critic", step_n=7,
    )
    assert isinstance(oid, ObjectId)
    assert len(store._fake_bucket.uploads) == 1  # type: ignore[attr-defined]
    upload = store._fake_bucket.uploads[0]  # type: ignore[attr-defined]
    # Filename embeds the triple — operator-readable in mongosh.
    assert upload["filename"] == "run-1/first-impression-critic/step-007.png"
    # Metadata round-trips so an orphan-cleanup job can scan files
    # without joining back to qa_run_steps.
    md = upload["metadata"]
    assert md["run_id"] == "run-1"
    assert md["persona_id"] == "first-impression-critic"
    assert md["step_n"] == 7
    assert md["content_type"] == "image/png"


def test_store_screenshot_uses_three_digit_padded_step(store):
    """Step number must be zero-padded to 3 so filenames sort correctly
    in mongosh / ls output (`step-002.png` before `step-010.png`)."""
    screenshots.store_screenshot(
        store, b"x",
        run_id="r", persona_id="p", step_n=2,
    )
    screenshots.store_screenshot(
        store, b"x",
        run_id="r", persona_id="p", step_n=10,
    )
    names = sorted(
        u["filename"] for u in store._fake_bucket.uploads  # type: ignore[attr-defined]
    )
    assert names == ["r/p/step-002.png", "r/p/step-010.png"]


def test_fetch_screenshot_returns_bytes(store):
    oid = screenshots.store_screenshot(
        store, b"PNG_PAYLOAD",
        run_id="r", persona_id="p", step_n=1,
    )
    assert screenshots.fetch_screenshot(store, oid) == b"PNG_PAYLOAD"


def test_fetch_screenshot_accepts_string_oid(store):
    """The review UI receives oids as URL fragments — string-in must work."""
    oid = screenshots.store_screenshot(
        store, b"data",
        run_id="r", persona_id="p", step_n=1,
    )
    assert screenshots.fetch_screenshot(store, str(oid)) == b"data"


def test_fetch_screenshot_raises_NoFile_on_missing(store):
    """The endpoint maps NoFile to HTTP 404 — caller MUST see the raise."""
    with pytest.raises(NoFile):
        screenshots.fetch_screenshot(store, ObjectId())


def test_delete_screenshot_removes(store):
    oid = screenshots.store_screenshot(
        store, b"x",
        run_id="r", persona_id="p", step_n=1,
    )
    screenshots.delete_screenshot(store, oid)
    with pytest.raises(NoFile):
        screenshots.fetch_screenshot(store, oid)


def test_delete_screenshot_is_noop_on_missing(store):
    """An orphan-cleanup pass that picks up a screenshot just deleted by
    something else must not crash."""
    # Should not raise.
    screenshots.delete_screenshot(store, ObjectId())


def test_delete_screenshot_accepts_string_oid(store):
    oid = screenshots.store_screenshot(
        store, b"x",
        run_id="r", persona_id="p", step_n=1,
    )
    screenshots.delete_screenshot(store, str(oid))
    assert oid not in store._fake_bucket.files  # type: ignore[attr-defined]
