"""Tests for AtlasReportSink — the Slice 6 store-backed report sink.

The qa-store is mocked with mongomock (pymongo only, no real Mongo). No network
and no Anthropic calls — the sink is exercised against a fabricated RunResult.
"""

from __future__ import annotations

import dataclasses

import mongomock
import pytest

from qa_agents.accounting import RunAccounting
from qa_agents.config import Config
from qa_agents.report import AtlasReportSink, FileReportSink, RunResult, build_sink, write_run
from qa_agents.tools.findings import Findings


@pytest.fixture(autouse=True)
def _mock_mongo(monkeypatch):
    """Make qa_store.connect() hand back a mongomock-backed Store."""
    import qa_store
    from qa_store.schema import Store

    def _fake_connect(url=None, db=None):
        client = mongomock.MongoClient()
        s = Store(client=client, db_name=db or "slyreply_qa_test")
        s.runs.create_index("run_id", unique=True)
        s.findings.create_index("finding_id", unique=True)
        return s

    # Patch the symbol the sink imports lazily.
    monkeypatch.setattr(qa_store, "connect", _fake_connect)
    return _fake_connect


def _config(**overrides) -> Config:
    base = Config(
        persona="first-impression-critic",
        web_base_url="http://localhost:5173",
        smtp_host="localhost",
        smtp_port=1025,
        mailpit_url="http://localhost:8025",
        explore_model="claude-sonnet-4-6",
        report_model="claude-opus-4-7",
        max_turns=60,
        run_timeout_s=1800,
        out_dir="./qa-runs",
        mongodb_url="mongodb://mongo/slyreply",
        admin_email="admin@x",
        admin_password="pw",
        sink="atlas",
        run_id="qa-testrun",
        qa_store_url="mongodb://localhost:27017",
        qa_store_db="slyreply_qa_test",
        discord_webhook_url="",
        concurrency=1,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _result(persona_id="first-impression-critic", review="## Would I use this?\n\nYes, cautiously.") -> RunResult:
    acc = RunAccounting()
    acc.record(
        "explore", "claude-sonnet-4-6",
        {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 200},
        num_turns=12,
    )
    acc.record(
        "report", "claude-opus-4-7",
        {"input_tokens": 3000, "output_tokens": 1200},
        num_turns=1,
    )
    findings = Findings()
    findings.add("confusion", "major", "What is a UID?", 'Page said "UID".')
    findings.add("worry", "blocker", "Privacy", "Can it read my mail?")
    return RunResult(
        run_id=f"{persona_id}-20260519T100000Z",
        persona_id=persona_id,
        persona_display_name=persona_id.title(),
        started_at="2026-05-19T10:00:00+00:00",
        finished_at="2026-05-19T10:20:00+00:00",
        accounting=acc,
        findings=findings,
        review_markdown=review,
    )


# ---------------------------------------------------------------------------
# build_sink selection.
# ---------------------------------------------------------------------------
def test_build_sink_file():
    assert isinstance(build_sink(_config(sink="file")), FileReportSink)


def test_build_sink_atlas():
    assert isinstance(build_sink(_config(sink="atlas")), AtlasReportSink)


def test_build_sink_rejects_unknown():
    with pytest.raises(ValueError):
        build_sink(_config(sink="bogus"))


# ---------------------------------------------------------------------------
# run id grouping.
# ---------------------------------------------------------------------------
def test_sink_uses_configured_run_id():
    sink = AtlasReportSink(_config(run_id="qa-fixed"))
    assert sink.run_id == "qa-fixed"


def test_sink_generates_run_id_when_unset():
    sink = AtlasReportSink(_config(run_id=""))
    assert sink.run_id.startswith("qa-")
    assert sink.run_id != ""


# ---------------------------------------------------------------------------
# write_summary persists the persona result into the shared run.
# ---------------------------------------------------------------------------
def test_write_run_persists_persona_into_shared_run():
    sink = AtlasReportSink(_config())
    write_run(_result("first-impression-critic"), sink)

    store = sink._ensure_store()
    run = store.runs.find_one({"run_id": "qa-testrun"})
    assert run is not None
    assert run["personas"] == ["first-impression-critic"]
    assert len(run["reviews"]) == 1
    assert run["reviews"][0]["persona"] == "first-impression-critic"
    assert "Would I use this?" in run["reviews"][0]["review_markdown"]
    assert run["reviews"][0]["verdict"] == "Yes, cautiously."
    # Findings landed.
    findings = list(store.findings.find({"run_id": "qa-testrun"}))
    assert len(findings) == 2
    assert {f["severity"] for f in findings} == {"major", "blocker"}


def test_multiple_personas_share_one_run():
    """The whole point of the Atlas sink — one run, many personas."""
    sink = AtlasReportSink(_config(run_id="qa-multi"))
    write_run(_result("first-impression-critic"), sink)
    write_run(_result("desktop-evaluator"), sink)

    store = sink._ensure_store()
    runs = list(store.runs.find())
    assert len(runs) == 1  # NOT one run per persona
    run = runs[0]
    assert sorted(run["personas"]) == ["desktop-evaluator", "first-impression-critic"]
    assert len(run["reviews"]) == 2
    assert store.findings.count_documents({"run_id": "qa-multi"}) == 4


def test_write_review_returns_atlas_locator():
    sink = AtlasReportSink(_config(run_id="qa-loc"))
    loc = sink.write_review(_result(), "ignored markdown")
    assert loc == "atlas://slyreply_qa_test/qa_runs/qa-loc"


def test_finish_stamps_run_totals():
    sink = AtlasReportSink(_config(run_id="qa-fin"))
    write_run(_result("first-impression-critic"), sink)
    write_run(_result("desktop-evaluator"), sink)
    sink.finish()

    store = sink._ensure_store()
    run = store.runs.find_one({"run_id": "qa-fin"})
    assert run["status"] == "reviewed"
    assert run["finished_at"] is not None
    # Two personas, each: 4000 input, 1700 output across phases.
    assert run["totals"]["input_tokens"] == 8000
    assert run["totals"]["output_tokens"] == 3400
    # #1822 — token counts only; the dollar fields are gone for new runs.
    assert "cost_usd" not in run["totals"]
    assert "real_cost_usd" not in run["totals"]


def test_finish_is_noop_when_no_personas_written():
    sink = AtlasReportSink(_config(run_id="qa-empty"))
    sink.finish()  # must not raise
    store = sink._ensure_store()
    assert store.runs.find_one({"run_id": "qa-empty"}) is None


def test_re_running_a_persona_replaces_its_slice():
    sink = AtlasReportSink(_config(run_id="qa-rerun"))
    write_run(_result("first-impression-critic", review="## Would I use this?\n\nFirst take."), sink)
    write_run(_result("first-impression-critic", review="## Would I use this?\n\nSecond take."), sink)
    store = sink._ensure_store()
    run = store.runs.find_one({"run_id": "qa-rerun"})
    assert len(run["reviews"]) == 1
    assert run["reviews"][0]["verdict"] == "Second take."


# ---------------------------------------------------------------------------
# Multi-pod finish barrier (#1821). A sharded run has N pods writing into one
# shared store under one run_id; expected_personas (the full roster) is the
# finish denominator. finish_if_last() must finalise EXACTLY ONCE — the pod
# that writes the last expected persona and wins the atomic claim.
# ---------------------------------------------------------------------------
@pytest.fixture
def shared_store(monkeypatch):
    """A single mongomock-backed Store every sink in the test shares.

    The autouse _mock_mongo fixture hands each connect() call a FRESH client,
    which is right for single-sink tests but wrong for a multi-pod sim where
    two pods must see each other's writes. This fixture pins ONE client so
    every AtlasReportSink built in the test lands in the same database.
    """
    import qa_store
    from qa_store.schema import Store

    client = mongomock.MongoClient()

    def _one_connect(url=None, db=None):
        s = Store(client=client, db_name=db or "slyreply_qa_test")
        return s

    monkeypatch.setattr(qa_store, "connect", _one_connect)
    return Store(client=client, db_name="slyreply_qa_test")


def _pod_sink(run_id, expected):
    """Build a sink as one pod of a sharded run would: shared run_id + the
    full expected-persona roster (the finish denominator)."""
    return AtlasReportSink(
        _config(run_id=run_id, pod_count=2), expected_personas=expected
    )


class TestMultiPodFinishBarrier:
    EXPECTED = ["desktop-evaluator", "first-impression-critic"]

    def test_first_pod_does_not_finish(self, shared_store):
        """Pod A writes its persona then calls finish_if_last(): the other
        expected persona hasn't filed, so the barrier stays closed."""
        pod_a = _pod_sink("qa-2pod", self.EXPECTED)
        write_run(_result("first-impression-critic"), pod_a)
        assert pod_a.finish_if_last() is False

        run = shared_store.runs.find_one({"run_id": "qa-2pod"})
        assert run["status"] == "new"
        assert run["finished_at"] is None
        # The denominator was seeded as the FULL roster, not just pod A's one.
        assert sorted(run["expected_personas"]) == sorted(self.EXPECTED)

    def test_last_pod_finishes_exactly_once(self, shared_store):
        """The second pod's write completes the roster; its finish_if_last()
        wins the claim and stamps the run reviewed — exactly one finalisation
        across the simulated 2-pod sequence."""
        pod_a = _pod_sink("qa-2pod", self.EXPECTED)
        write_run(_result("first-impression-critic"), pod_a)
        won_a = pod_a.finish_if_last()

        pod_b = _pod_sink("qa-2pod", self.EXPECTED)
        write_run(_result("desktop-evaluator"), pod_b)
        won_b = pod_b.finish_if_last()

        # Exactly one pod finalised.
        assert [won_a, won_b] == [False, True]

        run = shared_store.runs.find_one({"run_id": "qa-2pod"})
        assert run["status"] == "reviewed"
        assert run["finished_at"] is not None
        # Totals summed across BOTH pods' personas.
        assert run["totals"]["input_tokens"] == 8000
        assert run["totals"]["output_tokens"] == 3400

    def test_only_one_claim_wins_under_race(self, shared_store):
        """Both pods have written and both observe all-reviewed at the same
        instant (a race); the atomic claim must still let only one finalise."""
        pod_a = _pod_sink("qa-race", self.EXPECTED)
        pod_b = _pod_sink("qa-race", self.EXPECTED)
        write_run(_result("first-impression-critic"), pod_a)
        write_run(_result("desktop-evaluator"), pod_b)

        # Both call finish_if_last() after every persona is in — the all-
        # reviewed gate is open for BOTH, so the claim is the tiebreaker.
        results = [pod_a.finish_if_last(), pod_b.finish_if_last()]
        assert results.count(True) == 1
        assert results.count(False) == 1

        run = shared_store.runs.find_one({"run_id": "qa-race"})
        assert run["status"] == "reviewed"

    def test_loser_pod_can_be_gated_for_no_discord(self, shared_store):
        """The contract the orchestrator relies on: a pod that is NOT last
        gets False from finish_if_last(), which the orchestrator uses to
        suppress the Discord alert. Assert the False here directly."""
        pod_a = _pod_sink("qa-gate", self.EXPECTED)
        write_run(_result("first-impression-critic"), pod_a)
        # pod_a is first of two — it must report False (no finalise, no alert).
        assert pod_a.finish_if_last() is False


class TestFinalizeStranded:
    EXPECTED = ["desktop-evaluator", "first-impression-critic"]

    def test_finalize_stamps_stranded_run(self, shared_store):
        """A run where one pod crashed before writing its slice: only one of
        two expected personas filed, so no pod ever won the barrier and the
        run is stuck at 'new'. finalize_stranded writes a placeholder for the
        missing persona and stamps the run reviewed."""
        pod_a = _pod_sink("qa-stranded", self.EXPECTED)
        write_run(_result("first-impression-critic"), pod_a)
        # Pod B (desktop-evaluator) never ran. The run is stuck:
        assert pod_a.finish_if_last() is False
        run = shared_store.runs.find_one({"run_id": "qa-stranded"})
        assert run["status"] == "new"

        # Reaper finalises it.
        reaper = AtlasReportSink(_config(run_id="qa-stranded"))
        reaper.finalize_stranded("qa-stranded")

        run = shared_store.runs.find_one({"run_id": "qa-stranded"})
        assert run["status"] == "reviewed"
        assert run["finished_at"] is not None
        # A placeholder review now exists for the missing persona.
        personas = {r["persona"] for r in run["reviews"]}
        assert personas == set(self.EXPECTED)
        missing_review = next(
            r for r in run["reviews"] if r["persona"] == "desktop-evaluator"
        )
        assert missing_review["verdict"] == "stranded"
        assert "never reported" in missing_review["review_markdown"].lower()
        # Totals reflect the one real persona that did report.
        assert run["totals"]["input_tokens"] == 4000

    def test_finalize_noop_on_unknown_run(self, shared_store):
        """An unknown run id is a no-op, not a crash."""
        reaper = AtlasReportSink(_config(run_id="qa-nope"))
        reaper.finalize_stranded("qa-nope")  # must not raise
        assert shared_store.runs.find_one({"run_id": "qa-nope"}) is None

    def test_finalize_when_nothing_missing(self, shared_store):
        """If every expected persona actually filed (race where the reaper
        runs after a late completion), finalize just stamps — no spurious
        placeholder rows."""
        pod_a = _pod_sink("qa-complete", self.EXPECTED)
        write_run(_result("first-impression-critic"), pod_a)
        write_run(_result("desktop-evaluator"), pod_a)

        reaper = AtlasReportSink(_config(run_id="qa-complete"))
        reaper.finalize_stranded("qa-complete")

        run = shared_store.runs.find_one({"run_id": "qa-complete"})
        assert run["status"] == "reviewed"
        assert len(run["reviews"]) == 2
        assert all(r["verdict"] != "stranded" for r in run["reviews"])
