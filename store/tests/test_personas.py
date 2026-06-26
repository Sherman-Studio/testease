"""Tests for the qa_personas CRUD helpers + seed (#988 + #1000).

Covers:
  * create_persona — happy path, DuplicateKeyError on persona_id collision
  * upsert_persona — insert path + update path
  * get_persona / list_personas — empty state, default sort, hidden filter
  * update_persona — partial patch, unknown id KeyError, immutable fields
  * delete_persona — non-default deletion, ValueError on default rows,
    False on missing rows
  * seed_default_personas — insert-only semantics (operator edits survive),
    ImportError surfaced when harness package missing
  * reset_default_personas — destructive reset overwrites operator edits

mongomock is used end-to-end — the unique index on persona_id is honoured
so the DuplicateKeyError path exercises real behaviour.
"""

from __future__ import annotations

import mongomock
import pytest
from pymongo.errors import DuplicateKeyError

from qa_store.schema import (
    Store,
    create_persona,
    delete_persona,
    get_persona,
    list_personas,
    update_persona,
    upsert_persona,
)


@pytest.fixture
def store() -> Store:
    client = mongomock.MongoClient()
    s = Store(client=client, db_name="slyreply_qa_test")
    # Mirror the production unique-index so collision tests cover the
    # real path.
    s.personas.create_index("persona_id", unique=True)
    return s


def _persona_doc(**overrides) -> dict:
    """A minimal but valid persona doc — overrides whatever the test cares about."""
    base = {
        "persona_id": "alice",
        "display_name": "Alice Tester",
        "registered_email": "alice@example.com",
        "explore_system_prompt": "explore",
        "report_system_prompt": "report",
        "flows": ["signup", "billing"],
        "uses_admin_login": False,
        "setup_actions": None,
        "browser_locale": None,
        "color_token": "teal",
        "avatar_seed": "alice",
        "is_default": False,
        "hidden": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# create_persona
# ---------------------------------------------------------------------------
class TestCreatePersona:
    def test_inserts_doc_with_timestamps(self, store):
        result = create_persona(store, _persona_doc())
        assert result["persona_id"] == "alice"
        assert result["display_name"] == "Alice Tester"
        assert "created_at" in result
        assert "updated_at" in result
        # _id stripped from API surface (round-trips as JSON elsewhere)
        assert "_id" not in result

    def test_duplicate_persona_id_raises(self, store):
        create_persona(store, _persona_doc())
        with pytest.raises(DuplicateKeyError):
            create_persona(store, _persona_doc())

    def test_default_flag_preserved(self, store):
        result = create_persona(store, _persona_doc(is_default=True))
        assert result["is_default"] is True


# ---------------------------------------------------------------------------
# upsert_persona
# ---------------------------------------------------------------------------
class TestUpsertPersona:
    def test_inserts_when_missing(self, store):
        upsert_persona(store, _persona_doc())
        assert get_persona(store, "alice")["display_name"] == "Alice Tester"

    def test_overwrites_when_present(self, store):
        create_persona(store, _persona_doc())
        upsert_persona(store, _persona_doc(display_name="Alice 2"))
        assert get_persona(store, "alice")["display_name"] == "Alice 2"

    def test_preserves_created_at_on_overwrite(self, store):
        original = create_persona(store, _persona_doc())
        upsert_persona(store, _persona_doc(display_name="Alice 2"))
        updated = get_persona(store, "alice")
        assert updated["created_at"] == original["created_at"]
        # updated_at should advance — at minimum, the upsert wrote a new one
        assert updated["updated_at"] >= original["updated_at"]


# ---------------------------------------------------------------------------
# list_personas
# ---------------------------------------------------------------------------
class TestListPersonas:
    def test_empty(self, store):
        assert list_personas(store) == []

    def test_default_sort_default_first_then_alpha(self, store):
        create_persona(store, _persona_doc(persona_id="zoe", display_name="Zoe", is_default=False))
        create_persona(store, _persona_doc(persona_id="bob", display_name="Bob", is_default=True))
        create_persona(store, _persona_doc(persona_id="amy", display_name="Amy", is_default=True))
        names = [p["display_name"] for p in list_personas(store)]
        # Defaults first (Amy, Bob alphabetical) then non-default (Zoe)
        assert names == ["Amy", "Bob", "Zoe"]

    def test_hidden_excluded_by_default(self, store):
        create_persona(store, _persona_doc(persona_id="alice", hidden=False))
        create_persona(store, _persona_doc(persona_id="bob", hidden=True))
        ids = {p["persona_id"] for p in list_personas(store)}
        assert ids == {"alice"}

    def test_include_hidden_returns_all(self, store):
        create_persona(store, _persona_doc(persona_id="alice", hidden=False))
        create_persona(store, _persona_doc(persona_id="bob", hidden=True))
        ids = {p["persona_id"] for p in list_personas(store, include_hidden=True)}
        assert ids == {"alice", "bob"}


# ---------------------------------------------------------------------------
# get_persona
# ---------------------------------------------------------------------------
class TestGetPersona:
    def test_returns_doc(self, store):
        create_persona(store, _persona_doc())
        assert get_persona(store, "alice")["display_name"] == "Alice Tester"

    def test_unknown_returns_none(self, store):
        assert get_persona(store, "ghost") is None


# ---------------------------------------------------------------------------
# update_persona
# ---------------------------------------------------------------------------
class TestUpdatePersona:
    def test_partial_patch(self, store):
        create_persona(store, _persona_doc())
        updated = update_persona(store, "alice", {"display_name": "Alice Edited"})
        assert updated["display_name"] == "Alice Edited"
        # Other fields untouched
        assert updated["registered_email"] == "alice@example.com"

    def test_unknown_id_raises(self, store):
        with pytest.raises(KeyError):
            update_persona(store, "ghost", {"display_name": "Ghost"})

    def test_persona_id_in_patch_is_ignored(self, store):
        """persona_id is the identity — patch attempts must be silently dropped."""
        create_persona(store, _persona_doc())
        updated = update_persona(store, "alice", {
            "persona_id": "bob",
            "display_name": "Renamed",
        })
        # The row still lives at alice
        assert updated["persona_id"] == "alice"
        assert get_persona(store, "alice")["display_name"] == "Renamed"
        assert get_persona(store, "bob") is None

    def test_is_default_in_patch_is_ignored(self, store):
        """is_default is set at creation; UI can't toggle it via PATCH."""
        create_persona(store, _persona_doc(is_default=False))
        updated = update_persona(store, "alice", {"is_default": True})
        assert updated["is_default"] is False

    def test_explicit_null_clears_nullable_field(self, store):
        create_persona(store, _persona_doc(browser_locale="en-GB"))
        updated = update_persona(store, "alice", {"browser_locale": None})
        assert updated["browser_locale"] is None

    def test_hidden_flag_settable(self, store):
        create_persona(store, _persona_doc(is_default=True))
        updated = update_persona(store, "alice", {"hidden": True})
        assert updated["hidden"] is True


# ---------------------------------------------------------------------------
# delete_persona
# ---------------------------------------------------------------------------
class TestDeletePersona:
    def test_deletes_non_default(self, store):
        create_persona(store, _persona_doc(is_default=False))
        assert delete_persona(store, "alice") is True
        assert get_persona(store, "alice") is None

    def test_refuses_default(self, store):
        """Default personas are protected from hard-delete — use hidden=True."""
        create_persona(store, _persona_doc(is_default=True))
        with pytest.raises(ValueError, match="default persona"):
            delete_persona(store, "alice")
        # Row still exists
        assert get_persona(store, "alice") is not None

    def test_missing_returns_false(self, store):
        assert delete_persona(store, "ghost") is False


# ---------------------------------------------------------------------------
# seed_default_personas + reset_default_personas
#
# These tests need qa_agents on the path. The harness package only imports
# `dataclasses` at module level (no Claude SDK), so it's importable in unit
# tests too. If the dev environment doesn't have it, the seed test is
# skipped via the fixture below.
# ---------------------------------------------------------------------------
@pytest.fixture
def harness_available() -> bool:
    try:
        import qa_agents.personas  # noqa: F401
        return True
    except ImportError:
        return False


class TestSeedDefaultPersonas:
    def test_inserts_all_defaults_on_clean_db(self, store, harness_available):
        if not harness_available:
            pytest.skip("qa_agents.personas not on path")
        from qa_store.seed_personas import seed_default_personas

        n = seed_default_personas(store)
        assert n == 25
        rows = list_personas(store)
        assert len(rows) == 25
        # Every default is flagged as such
        assert all(r["is_default"] for r in rows)
        # Each has a non-empty colour token + avatar seed
        assert all(r["color_token"] for r in rows)
        assert all(r["avatar_seed"] for r in rows)

    def test_re_seed_inserts_nothing(self, store, harness_available):
        """The insert-only contract — operator edits must survive boot."""
        if not harness_available:
            pytest.skip("qa_agents.personas not on path")
        from qa_store.seed_personas import seed_default_personas

        seed_default_personas(store)
        n = seed_default_personas(store)
        assert n == 0

    def test_operator_edit_survives_re_seed(self, store, harness_available):
        """The bug we're fixing: a re-seed must NOT overwrite UI edits."""
        if not harness_available:
            pytest.skip("qa_agents.personas not on path")
        from qa_store.seed_personas import seed_default_personas

        seed_default_personas(store)
        # Operator edits Margaret's display_name via the UI
        update_persona(store, "first-impression-critic", {"display_name": "Marg Edited"})
        # Pod restarts → seed runs again
        seed_default_personas(store)
        # The edit must still be there
        assert get_persona(store, "first-impression-critic")["display_name"] == "Marg Edited"

    def test_import_error_when_harness_missing(self, store, monkeypatch):
        """Document the contract: ImportError bubbles up so the caller can log it."""
        import sys
        # Hide qa_agents temporarily so the lazy import fails
        monkeypatch.setitem(sys.modules, "qa_agents", None)
        monkeypatch.setitem(sys.modules, "qa_agents.personas", None)
        from qa_store.seed_personas import seed_default_personas

        with pytest.raises((ImportError, TypeError)):
            # TypeError can wrap an ImportError on some Python versions when
            # sys.modules has a None entry — either is acceptable here, the
            # caller treats both as "harness not available".
            seed_default_personas(store)


class TestResetDefaultPersonas:
    def test_overwrites_operator_edits(self, store, harness_available):
        """The destructive counterpart to seed — explicit operator action."""
        if not harness_available:
            pytest.skip("qa_agents.personas not on path")
        from qa_store.seed_personas import (
            reset_default_personas,
            seed_default_personas,
        )

        seed_default_personas(store)
        original_name = get_persona(store, "first-impression-critic")["display_name"]

        update_persona(store, "first-impression-critic", {"display_name": "Marg Edited"})
        assert get_persona(store, "first-impression-critic")["display_name"] == "Marg Edited"

        n = reset_default_personas(store)
        assert n == 25
        assert get_persona(store, "first-impression-critic")["display_name"] == original_name
