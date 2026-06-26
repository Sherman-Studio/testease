"""Inject migrated by-design site_knowledge into persona prompts (code→data→used).

LIST-based (no vectors). Verifies: with seeded by-design knowledge the explore
prompt carries the block + bodies; with none it's unchanged; a DB error is
swallowed and the run proceeds; and bodies with literal braces don't break the
prompt's str.format (the block is appended AFTER formatting).
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import mongomock
from qa_store.schema import DEFAULT_TENANT, Store

from qa_agents.config import Config
from qa_agents.personas import get_persona, render_explore_prompt
from qa_agents.site_knowledge import _format_block, load_by_design_block

_PERSONA_ID = "first-impression-critic"


def _config(**overrides) -> Config:
    base = Config(
        persona=_PERSONA_ID, web_base_url="http://frontend", smtp_host="s",
        smtp_port=1025, mailpit_url="http://m", explore_model="m",
        report_model="m", max_turns=10, run_timeout_s=60, out_dir="./o",
        mongodb_url="mongodb://x", admin_email="a@x", admin_password="pw",
        sink="file", run_id="t", qa_store_url="mongodb://localhost:27017",
        qa_store_db="testdb", discord_webhook_url="", concurrency=1,
        target_id="example",
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _store_with(rows: list[dict]) -> Store:
    store = Store(client=mongomock.MongoClient(), db_name="testdb")
    for r in rows:
        store.site_knowledge.insert_one(r)
    return store


def _row(entry_id, body, *, kind="by_design", target="example"):
    return {
        "tenant_id": DEFAULT_TENANT, "target_id": target,
        "entry_id": entry_id, "kind": kind, "body": body,
    }


# ── load_by_design_block ──
def test_loads_only_by_design_bodies():
    store = _store_with([
        _row("k1", "BY DESIGN — /verify-pending is intentional."),
        _row("k2", "just some guidance", kind="guidance"),
    ])
    block = load_by_design_block(_config(), store=store)
    assert "Known by-design behaviours" in block
    assert "/verify-pending is intentional" in block
    assert "just some guidance" not in block  # non-by_design excluded


def test_empty_knowledge_returns_empty_string():
    assert load_by_design_block(_config(), store=_store_with([])) == ""


def test_scoped_to_run_target():
    store = _store_with([_row("k1", "another target's note", target="other")])
    assert load_by_design_block(_config(target_id="example"), store=store) == ""


def test_db_error_is_swallowed():
    bad = MagicMock()
    bad.site_knowledge.find.side_effect = RuntimeError("mongo down")
    assert load_by_design_block(_config(), store=bad) == ""


def test_format_block_empty_is_empty():
    assert _format_block([]) == ""


# ── render_explore_prompt injection ──
def test_prompt_includes_block_when_present():
    block = _format_block(["BY DESIGN — X is intentional."])
    prompt = render_explore_prompt(
        get_persona(_PERSONA_ID), "http://frontend", by_design_block=block,
    )
    assert "Known by-design behaviours" in prompt
    assert "X is intentional" in prompt


def test_prompt_unchanged_without_block():
    p = get_persona(_PERSONA_ID)
    base = render_explore_prompt(p, "http://frontend")
    with_empty = render_explore_prompt(p, "http://frontend", by_design_block="")
    assert base == with_empty
    assert "Known by-design behaviours" not in base


def test_block_with_literal_braces_does_not_break_format():
    # A body with braces must NOT be passed through str.format — it's appended
    # after formatting, so the braces survive verbatim and nothing raises.
    block = _format_block(["BY DESIGN — the {placeholder} in the copy is literal."])
    prompt = render_explore_prompt(
        get_persona(_PERSONA_ID), "http://frontend", by_design_block=block,
    )
    assert "{placeholder}" in prompt
