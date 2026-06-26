"""Tests for the harness Config dataclass and its env parsing.

Covers the Config-level invariants that matter after the QA org-API-key
retirement: Config carries neither an ``anthropic_api_key`` nor a
``load_persona_memory`` field any more (the persona-memory subsystem and
its env gate were retired), there is no LLM-backend selector — every run
bills Claude Code Max — and (#1822) the ``prices`` PriceTable field is
gone along with the per-run dollar conversion.
"""

from __future__ import annotations

import pytest

from qa_agents.config import Config


# ---------------------------------------------------------------------------
# Retired fields — the persona-memory subsystem and the QA org API key are
# gone, so Config must NOT carry their fields.
# ---------------------------------------------------------------------------
class TestRetiredFields:
    def test_no_anthropic_api_key_field(self) -> None:
        """The QA org API-key credential was removed; the only Anthropic
        auth left for QA is the Claude Code Max OAuth token (scrubbed in
        runner._options_env)."""
        config = Config.from_env()
        assert not hasattr(config, "anthropic_api_key")

    def test_no_load_persona_memory_field(self) -> None:
        """The persona-memory subsystem (and its QA_LOAD_PERSONA_MEMORY
        gate) was retired."""
        config = Config.from_env()
        assert not hasattr(config, "load_persona_memory")

    def test_no_prices_field(self) -> None:
        """#1822 — the PriceTable / QA_PRICE_* knobs were retired with the
        per-run dollar conversion; Config tracks no price data."""
        config = Config.from_env()
        assert not hasattr(config, "prices")

    def test_dataclass_builds_without_retired_fields(self) -> None:
        """A Config built directly (no from_env) needs neither retired
        field — every code path that constructs Config in tests stays
        green without them."""
        config = Config(
            persona="margaret",
            web_base_url="http://x",
            smtp_host="localhost",
            smtp_port=1025,
            mailpit_url="http://x",
            explore_model="model",
            report_model="model",
            max_turns=10,
            run_timeout_s=60,
            out_dir="/tmp",
            mongodb_url="",
            admin_email="",
            admin_password="",
            sink="file",
            run_id="",
            qa_store_url="",
            qa_store_db="",
            discord_webhook_url="",
            concurrency=1,
        )
        assert config.persona == "margaret"


# ---------------------------------------------------------------------------
# Max-only billing — there is no LLM backend selector. QA_LLM_BACKEND is no
# longer read by the harness; every run uses Claude Code Max (the
# ANTHROPIC_API_KEY scrub lives in runner._options_env, tested there).
# ---------------------------------------------------------------------------
class TestNoBackendSelector:
    def test_config_has_no_backend_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``backend`` Config field was removed; QA_LLM_BACKEND is
        ignored entirely."""
        monkeypatch.setenv("QA_LLM_BACKEND", "claude-code")
        config = Config.from_env()
        assert not hasattr(config, "backend")


# ---------------------------------------------------------------------------
# Multi-pod sharding knobs (#1821). A sharded run is one Job with N pods;
# k8s indexed Jobs expose the pod ordinal as JOB_COMPLETION_INDEX, and the
# operator sets QA_POD_COUNT to the parallelism. Both default so single-pod
# (the legacy default) behaviour is unchanged.
# ---------------------------------------------------------------------------
class TestPodSharding:
    def test_defaults_single_pod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env set ⇒ pod_index=0, pod_count=1 (the single-pod default)."""
        monkeypatch.delenv("JOB_COMPLETION_INDEX", raising=False)
        monkeypatch.delenv("QA_POD_COUNT", raising=False)
        config = Config.from_env()
        assert config.pod_index == 0
        assert config.pod_count == 1

    def test_reads_pod_index_and_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JOB_COMPLETION_INDEX", "2")
        monkeypatch.setenv("QA_POD_COUNT", "4")
        config = Config.from_env()
        assert config.pod_index == 2
        assert config.pod_count == 4

    def test_blank_env_falls_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty string (a Job template that sets the var to "") must
        not crash int parsing — it falls back to the default."""
        monkeypatch.setenv("JOB_COMPLETION_INDEX", "")
        monkeypatch.setenv("QA_POD_COUNT", "")
        config = Config.from_env()
        assert config.pod_index == 0
        assert config.pod_count == 1
