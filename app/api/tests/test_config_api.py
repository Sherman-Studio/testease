"""API tests for the BYOK LLM-backend config endpoints."""

from __future__ import annotations

import mongomock
import pytest
from fastapi.testclient import TestClient
from qa_store.schema import Store

from qa_review_api.app import create_app
from qa_review_api.settings import Settings


@pytest.fixture
def store() -> Store:
    s = Store(client=mongomock.MongoClient(), db_name="testease_test")
    s.qa_config.create_index([("tenant_id", 1), ("key", 1)], unique=True)
    s.site_secrets.create_index(
        [("tenant_id", 1), ("target_id", 1), ("ref", 1)], unique=True,
    )
    return s


def _client(store) -> TestClient:
    settings = Settings(
        qa_store_url="mongodb://x", qa_store_db="testease_test",
        github_token="", github_repo="",
    )
    return TestClient(create_app(settings=settings, store=store, seed_personas=False))


def test_defaults_claude_code_unconfigured(store, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = _client(store).get("/api/config/llm")
    assert r.status_code == 200
    b = r.json()
    assert b["backend"] == "claude-code"
    assert b["env_var"] == "CLAUDE_CODE_OAUTH_TOKEN"
    assert b["token_configured"] is False
    assert b["token_source"] is None
    assert {x["id"] for x in b["backends"]} == {"claude-code", "api"}


def test_env_token_reported_as_configured(store, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "from-env")
    b = _client(store).get("/api/config/llm").json()
    assert b["token_configured"] is True
    assert b["token_source"] == "env"


def test_put_token_is_vaulted_and_never_echoed(store, monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    c = _client(store)
    r = c.put("/api/config/llm", json={"backend": "claude-code", "token": "sk-oauth-secret"})
    assert r.status_code == 200
    assert "sk-oauth-secret" not in r.text          # never echoed
    assert r.json()["token_configured"] is True
    assert r.json()["token_source"] == "vault"
    # GET still never returns the token.
    assert "sk-oauth-secret" not in c.get("/api/config/llm").text


def test_vault_token_takes_precedence_over_env(store, monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "from-env")
    c = _client(store)
    c.put("/api/config/llm", json={"backend": "claude-code", "token": "from-ui"})
    assert c.get("/api/config/llm").json()["token_source"] == "vault"


def test_switch_backend_keeps_token(store, monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = _client(store)
    c.put("/api/config/llm", json={"backend": "claude-code", "token": "tok"})
    # Switch to api without re-supplying a token — the claude-code token stays
    # vaulted, but the api backend's env var is what's now checked.
    b = c.put("/api/config/llm", json={"backend": "api"}).json()
    assert b["backend"] == "api"
    assert b["env_var"] == "ANTHROPIC_API_KEY"


def test_bad_backend_422(store):
    r = _client(store).put("/api/config/llm", json={"backend": "bedrock"})
    assert r.status_code == 422


def test_clear_token(store, monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    c = _client(store)
    c.put("/api/config/llm", json={"backend": "claude-code", "token": "tok"})
    b = c.delete("/api/config/llm/token").json()
    assert b["token_configured"] is False
    assert b["token_source"] is None
