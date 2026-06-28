"""Tests for instance-level LLM backend config (qa_store.app_config)."""

from __future__ import annotations

import mongomock
import pytest

from qa_store.app_config import (
    DEFAULT_LLM_BACKEND,
    clear_llm_token,
    get_llm_config,
    get_llm_token,
    set_llm_config,
)
from qa_store.schema import Store


def _store() -> Store:
    return Store(client=mongomock.MongoClient(), db_name="testdb")


def test_defaults_to_claude_code_no_token():
    cfg = get_llm_config(_store())
    assert cfg == {"backend": DEFAULT_LLM_BACKEND, "credential_ref": None}
    assert DEFAULT_LLM_BACKEND == "claude-code"


def test_set_backend_without_token():
    store = _store()
    cfg = set_llm_config(store, backend="api")
    assert cfg["backend"] == "api"
    assert cfg["credential_ref"] is None


def test_set_token_is_vaulted_not_inline(monkeypatch):
    monkeypatch.delenv("QA_CREDENTIAL_KEY", raising=False)
    store = _store()
    cfg = set_llm_config(store, backend="claude-code", token="sk-oauth-xyz")
    # The config doc holds only a pointer — never the raw token.
    assert cfg["credential_ref"] == "vault://default/_config/llm-token"
    doc = store.qa_config.find_one({"key": "llm"})
    assert "sk-oauth-xyz" not in str(doc)
    # The value is retrievable only through the resolver (vault).
    assert get_llm_token(store) == "sk-oauth-xyz"


def test_changing_backend_keeps_existing_token():
    store = _store()
    set_llm_config(store, backend="claude-code", token="tok")
    cfg = set_llm_config(store, backend="api")  # no token passed
    assert cfg["backend"] == "api"
    assert get_llm_token(store) == "tok"  # preserved


def test_clear_token_removes_value_and_pointer():
    store = _store()
    set_llm_config(store, backend="claude-code", token="tok")
    cfg = clear_llm_token(store)
    assert cfg["credential_ref"] is None
    assert get_llm_token(store) is None
    assert cfg["backend"] == "claude-code"  # choice kept


def test_set_rejects_unknown_backend():
    with pytest.raises(ValueError):
        set_llm_config(_store(), backend="bedrock")


def test_config_target_not_a_listed_site():
    # The instance secret lives under the reserved _config pseudo-target; it
    # must not appear as a Site.
    from qa_store.site_model import list_site_targets

    store = _store()
    set_llm_config(store, backend="claude-code", token="tok")
    assert list_site_targets(store) == []
