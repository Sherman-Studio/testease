"""Tests for token accounting — the per-phase tally and run totals.

#1822 — the per-run dollar conversion (PriceTable, estimate_cost,
cost_usd / real_cost_usd / cost_is_estimated) was retired: every run
bills the operator's flat-rate Claude Code Max subscription, so the
accounting stops at raw token counts.
"""

from __future__ import annotations

from qa_agents.accounting import RunAccounting


# --------------------------------------------------------------------------
# RunAccounting — recording and totals.
# --------------------------------------------------------------------------
def test_record_tallies_tokens_and_turns():
    acc = RunAccounting()
    phase = acc.record(
        phase="explore",
        model="claude-sonnet-4-6",
        usage={"input_tokens": 100, "output_tokens": 50},
        num_turns=7,
    )
    assert phase.input_tokens == 100
    assert phase.output_tokens == 50
    assert phase.num_turns == 7
    assert phase.total_tokens == 150


def test_record_coerces_object_usage():
    class Usage:
        input_tokens = 10
        output_tokens = 20
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 5

    acc = RunAccounting()
    phase = acc.record(
        phase="explore",
        model="claude-sonnet-4-6",
        usage=Usage(),
    )
    assert phase.input_tokens == 10
    assert phase.output_tokens == 20
    assert phase.cache_read_input_tokens == 5
    assert phase.total_tokens == 35


def test_record_empty_usage_is_zero():
    acc = RunAccounting()
    phase = acc.record(phase="explore", model="claude-sonnet-4-6", usage=None)
    assert phase.total_tokens == 0


def test_run_totals_sum_both_phases():
    acc = RunAccounting()
    acc.record(
        "explore",
        "claude-sonnet-4-6",
        {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 200},
        num_turns=12,
    )
    acc.record(
        "report",
        "claude-opus-4-7",
        {"input_tokens": 2000, "output_tokens": 800},
        num_turns=1,
    )
    assert acc.total_input_tokens == 3000
    assert acc.total_output_tokens == 1300
    assert acc.total_cache_tokens == 200
    assert acc.total_tokens == 4500
    assert acc.total_turns == 13


def test_as_dict_shape():
    acc = RunAccounting()
    acc.record("explore", "claude-sonnet-4-6", {"input_tokens": 100})
    d = acc.as_dict()
    assert d["total_input_tokens"] == 100
    assert d["total_tokens"] == 100
    assert len(d["phases"]) == 1
    assert d["phases"][0]["phase"] == "explore"


# ---------------------------------------------------------------------------
# #1822 — the dollar fields are GONE for new runs. Old run documents that
# carry cost_usd / real_cost_usd are tolerated by the readers; new
# accounting payloads must not write them.
# ---------------------------------------------------------------------------
def test_as_dict_carries_no_dollar_fields():
    acc = RunAccounting()
    acc.record(
        "report", "claude-opus-4-7", {"output_tokens": 500_000},
    )
    d = acc.as_dict()
    assert "total_cost_usd" not in d
    assert "real_cost_usd" not in d
    assert "cost_is_estimated" not in d
    assert "cost_usd" not in d["phases"][0]
    assert "cost_is_estimated" not in d["phases"][0]


# ---------------------------------------------------------------------------
# backend tagging on RunAccounting. Runs are Max-only: the default backend
# is "claude-code". The field is retained — the Atlas sink + review-UI key
# off it to render the Max pill.
# ---------------------------------------------------------------------------
class TestBackendTagging:
    def test_default_backend_is_claude_code(self):
        acc = RunAccounting()
        assert acc.backend == "claude-code"

    def test_as_dict_carries_backend(self):
        acc = RunAccounting()
        acc.record("report", "claude-opus-4-7", {"output_tokens": 500_000})
        d = acc.as_dict()
        assert d["backend"] == "claude-code"
