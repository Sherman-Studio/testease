"""Tests for the qa_run_logs collection helpers (#902 / #903)."""

from __future__ import annotations

from datetime import UTC, datetime

import mongomock
import pytest

from qa_store.schema import (
    RUN_LOG_KINDS,
    RUN_LOG_TTL_SECONDS,
    Store,
    _ensure_indexes,
    append_run_log,
    list_run_logs_for_persona,
)


@pytest.fixture
def store() -> Store:
    """Mongomock-backed Store with the real index setup applied."""
    client = mongomock.MongoClient()
    s = Store(client=client, db_name="slyreply_qa_test")
    _ensure_indexes(s)
    return s


class TestAppendRunLog:
    def test_persists_minimal_row(self, store):
        append_run_log(
            store,
            run_id="r1",
            persona_id="first-impression-critic",
            seq=1,
            kind="text",
            content="hello",
        )
        rows = list(store.run_logs.find())
        assert len(rows) == 1
        row = rows[0]
        assert row["run_id"] == "r1"
        assert row["persona_id"] == "first-impression-critic"
        assert row["seq"] == 1
        assert row["kind"] == "text"
        assert row["content"] == "hello"
        assert row["metadata"] == {}
        assert row["turn"] is None
        assert row["phase"] == ""
        assert "ts" in row

    def test_persists_full_row(self, store):
        ts = datetime.now(UTC)
        append_run_log(
            store,
            run_id="r1",
            persona_id="desktop-evaluator",
            seq=5,
            kind="result",
            content="turn done",
            phase="explore",
            turn=3,
            metadata={"cost": 0.012, "tokens": 1234},
            ts=ts,
        )
        row = store.run_logs.find_one()
        assert row["phase"] == "explore"
        assert row["turn"] == 3
        assert row["metadata"] == {"cost": 0.012, "tokens": 1234}
        # mongomock stores tz-aware datetimes but returns naive ones;
        # compare on the second so the test doesn't depend on driver
        # round-trip semantics.
        assert row["ts"].replace(tzinfo=None).replace(microsecond=0) == \
            ts.replace(tzinfo=None).replace(microsecond=0)

    def test_accepts_unknown_kind(self, store):
        """Validation is informational, not gatekeeping — a future
        runner edit that emits a new kind shouldn't lose log lines."""
        append_run_log(
            store, run_id="r1", persona_id="m", seq=1,
            kind="future_kind", content="x",
        )
        assert store.run_logs.find_one()["kind"] == "future_kind"

    def test_known_kinds_all_persist(self, store):
        for i, kind in enumerate(RUN_LOG_KINDS, start=1):
            append_run_log(
                store, run_id="r1", persona_id="m", seq=i,
                kind=kind, content=kind,
            )
        assert store.run_logs.count_documents({}) == len(RUN_LOG_KINDS)


class TestListRunLogsForPersona:
    def test_orders_by_seq_ascending(self, store):
        # Insert out-of-order to confirm sort is by seq, not insert order.
        for seq in (3, 1, 2):
            append_run_log(
                store, run_id="r1", persona_id="m", seq=seq,
                kind="text", content=f"line {seq}",
            )
        rows = list_run_logs_for_persona(store, "r1", "m")
        assert [r["seq"] for r in rows] == [1, 2, 3]

    def test_filters_by_run_id_and_persona_id(self, store):
        # Three docs across two personas and two runs.
        append_run_log(store, run_id="r1", persona_id="m", seq=1, kind="text", content="a")
        append_run_log(store, run_id="r1", persona_id="d", seq=1, kind="text", content="b")
        append_run_log(store, run_id="r2", persona_id="m", seq=1, kind="text", content="c")
        rows = list_run_logs_for_persona(store, "r1", "m")
        assert [r["content"] for r in rows] == ["a"]

    def test_returns_empty_when_no_logs(self, store):
        assert list_run_logs_for_persona(store, "no-such-run", "ghost") == []

    def test_respects_limit(self, store):
        for seq in range(1, 10):
            append_run_log(
                store, run_id="r1", persona_id="m", seq=seq,
                kind="text", content=str(seq),
            )
        rows = list_run_logs_for_persona(store, "r1", "m", limit=3)
        assert len(rows) == 3
        # Earliest seqs (sort ascending) come first.
        assert [r["seq"] for r in rows] == [1, 2, 3]

    def test_strips_internal_id(self, store):
        append_run_log(
            store, run_id="r1", persona_id="m", seq=1,
            kind="text", content="x",
        )
        row = list_run_logs_for_persona(store, "r1", "m")[0]
        assert "_id" not in row


class TestIndexes:
    """Pin the indexes the access patterns assume — a future refactor
    that drops one would silently slow cross-run queries to a scan."""

    def test_ttl_index_exists(self, store):
        info = store.run_logs.index_information()
        assert "ts_ttl" in info
        # mongomock preserves the expireAfterSeconds option on create_index.
        # Real Mongo returns it; mongomock may strip it in older versions —
        # tolerate both by checking presence loosely.
        opts = info["ts_ttl"]
        if "expireAfterSeconds" in opts:
            assert opts["expireAfterSeconds"] == RUN_LOG_TTL_SECONDS

    def test_run_persona_seq_index_exists(self, store):
        info = store.run_logs.index_information()
        assert "run_persona_seq" in info

    def test_persona_recent_index_exists(self, store):
        info = store.run_logs.index_information()
        assert "persona_recent" in info
