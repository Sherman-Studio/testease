"""Tests for the loadgen MCP tool (internal-load-economist persona).

The blast tool fires real SMTP sends through ``send_smtp``; here we
monkeypatch that module-level function so no network is touched and we can
assert on call count, args, and the summary text. The core lives in
``run_blast`` (the SDK-decorated ``blast`` just delegates), so we test that
directly.
"""

from __future__ import annotations

import qa_agents.tools.loadgen as loadgen
from qa_agents.tools.loadgen import build_loadgen_server, run_blast


def _patch_send(monkeypatch, *, fail_indices: set[int] | None = None):
    """Replace send_smtp with a recorder. Returns the list of recorded
    call kwargs. ``fail_indices`` raises for those (0-based) call ordinals."""
    calls: list[dict] = []
    fail_indices = fail_indices or set()

    def fake_send_smtp(**kwargs):
        idx = len(calls)
        calls.append(kwargs)
        if idx in fail_indices:
            raise OSError("SMTP boom")
        return f"<qa-{idx}@test>"

    monkeypatch.setattr(loadgen, "send_smtp", fake_send_smtp)
    return calls


def _kw():
    return {"smtp_host": "mail", "smtp_port": 1025, "persona_from_address": "nadia@qa.example.com"}


# ---------------------------------------------------------------------------
# build contract
# ---------------------------------------------------------------------------
def test_build_loadgen_server_contract():
    server, tool_names = build_loadgen_server(**_kw())
    # create_sdk_mcp_server returns a dict {type,name,instance}.
    assert server["name"] == "loadgen"
    assert tool_names == ["blast"]


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------
async def test_blast_sends_count_messages(monkeypatch):
    calls = _patch_send(monkeypatch)
    out = await run_blast(**_kw(), args={"to": "support@slyreply.ai", "count": 5, "kind": "text"})
    assert len(calls) == 5
    text = out["content"][0]["text"]
    assert "5/5 sent" in text
    assert "0 failed" in text
    # Each send used the persona's authenticated from-address and the target.
    assert all(c["from_addr"] == "nadia@qa.example.com" for c in calls)
    assert all(c["to_addr"] == "support@slyreply.ai" for c in calls)
    # Sample message-ids are surfaced.
    assert "sample message-ids:" in text


async def test_blast_cycles_custom_prompt_pool(monkeypatch):
    calls = _patch_send(monkeypatch)
    await run_blast(
        **_kw(),
        args={"to": "u@slyreply.ai", "count": 4, "kind": "text",
              "prompt_pool": "alpha||beta"},
    )
    bodies = [c["body"] for c in calls]
    # 4 sends over a 2-item pool → alpha,beta,alpha,beta.
    assert bodies == ["alpha", "beta", "alpha", "beta"]


async def test_blast_image_kind_uses_image_pool(monkeypatch):
    calls = _patch_send(monkeypatch)
    await run_blast(**_kw(), args={"to": "art@slyreply.ai", "count": 2, "kind": "image"})
    # Image pool bodies read like image-generation requests.
    assert any("Generate" in c["body"] or "Draw" in c["body"] or "Create" in c["body"]
               for c in calls)


async def test_blast_attachment_kind_attaches_default_fixture(monkeypatch):
    calls = _patch_send(monkeypatch)
    out = await run_blast(**_kw(), args={"to": "inv@slyreply.ai", "count": 1, "kind": "attachment"})
    # The default fixture is resolved and passed to send_smtp.
    paths = calls[0]["attachments"]
    assert paths and paths[0].name == "sample-invoice.pdf"
    assert "sample-invoice.pdf" in out["content"][0]["text"]


# ---------------------------------------------------------------------------
# validation + failure reporting
# ---------------------------------------------------------------------------
async def test_blast_requires_to(monkeypatch):
    calls = _patch_send(monkeypatch)
    out = await run_blast(**_kw(), args={"count": 3})
    assert "ERROR" in out["content"][0]["text"]
    assert not calls


async def test_blast_rejects_nonpositive_count(monkeypatch):
    calls = _patch_send(monkeypatch)
    out = await run_blast(**_kw(), args={"to": "u@slyreply.ai", "count": 0})
    assert "ERROR" in out["content"][0]["text"]
    assert not calls


async def test_blast_caps_count(monkeypatch):
    calls = _patch_send(monkeypatch)
    out = await run_blast(**_kw(), args={"to": "u@slyreply.ai", "count": 9999})
    assert "exceeds the per-call cap" in out["content"][0]["text"]
    assert not calls


async def test_blast_bad_attachment_fails_cleanly(monkeypatch):
    calls = _patch_send(monkeypatch)
    out = await run_blast(
        **_kw(),
        args={"to": "u@slyreply.ai", "count": 2, "kind": "text",
              "attachments": "does-not-exist.zip"},
    )
    assert "ERROR resolving attachment" in out["content"][0]["text"]
    assert not calls  # nothing sent if the plan can't be built


async def test_blast_reports_partial_failures(monkeypatch):
    calls = _patch_send(monkeypatch, fail_indices={1, 3})
    out = await run_blast(**_kw(), args={"to": "u@slyreply.ai", "count": 5, "kind": "text"})
    assert len(calls) == 5
    text = out["content"][0]["text"]
    assert "3/5 sent" in text
    assert "2 failed" in text
    assert "error samples:" in text
    assert "SMTP boom" in text
