"""Tests for resolve_target_mcp — granted capabilities → MCP servers + creds."""

from __future__ import annotations

import mongomock
from qa_store.capabilities import seed_capability_catalog, set_capability_status
from qa_store.schema import Store

from qa_agents.target_mcp import resolve_target_mcp


def _store() -> Store:
    s = Store(client=mongomock.MongoClient(), db_name="testdb")
    s.site_capabilities.create_index(
        [("tenant_id", 1), ("target_id", 1), ("capability_id", 1)], unique=True,
    )
    seed_capability_catalog(s)
    return s


def test_no_grants_is_empty():
    out = resolve_target_mcp(_store(), "acme")
    assert out == {"server_ids": [], "env": {}, "servers": []}


def test_granted_capability_lights_up_its_server_and_injects_cred(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    set_capability_status(
        store, target_id="acme", capability_id="openapi-spec",
        status="granted", token="https://acme.test/openapi.json",
    )
    out = resolve_target_mcp(store, "acme")
    assert out["server_ids"] == ["openapi"]
    # The vaulted URL is injected as the env var the harness already reads.
    assert out["env"] == {"QA_OPENAPI_URL": "https://acme.test/openapi.json"}
    assert out["servers"][0]["server_id"] == "openapi"
    assert out["servers"][0]["capabilities"] == ["openapi-spec"]
    assert "OpenAPI" in out["servers"][0]["display_name"]
    assert out["servers"][0]["friendly_name"] == "API probing tool"


def test_email_unlocked_with_inbox_cred(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    set_capability_status(
        store, target_id="acme", capability_id="sandbox-inbox",
        status="granted", token="http://mailpit.acme.test",
    )
    out = resolve_target_mcp(store, "acme")
    assert out["server_ids"] == ["email"]
    assert out["env"] == {"QA_MAILPIT_URL": "http://mailpit.acme.test"}


def test_only_granted_count_proposed_and_declined_ignored(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    set_capability_status(store, target_id="acme", capability_id="openapi-spec",
                          status="proposed")
    set_capability_status(store, target_id="acme", capability_id="sandbox-inbox",
                          status="declined")
    assert resolve_target_mcp(store, "acme") == {"server_ids": [], "env": {}, "servers": []}


def test_server_unlocked_without_env_cred(monkeypatch):
    # sandbox-sending unlocks the email server but feeds no env var — the
    # server still lights up; env stays empty.
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    set_capability_status(store, target_id="acme", capability_id="sandbox-sending",
                          status="granted")
    out = resolve_target_mcp(store, "acme")
    assert out["server_ids"] == ["email"]
    assert out["env"] == {}


def test_resolution_is_graceful_on_bad_store():
    # A store that explodes on read must yield the empty result, never raise.
    class Boom:
        def __getattr__(self, _):
            raise RuntimeError("no mongo here")

    assert resolve_target_mcp(Boom(), "acme") == {"server_ids": [], "env": {}, "servers": []}
