"""Tests for the dogfood migration (personas.py → Site Model rows).

Uses synthetic duck-typed personas (the migration never imports the harness),
so it asserts the lift logic without depending on the real personas.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import mongomock
import pytest

from qa_store.schema import DEFAULT_TENANT, Store
from qa_store.site_model import (
    get_site_target,
    list_flows_by_target,
    list_knowledge_by_target,
)
from qa_store.site_model_migration import (
    SLYREPLY_ADMIN_CREDENTIAL_REF,
    extract_from_personas,
    migrate_dogfood,
)


@dataclass
class _FakePersona:
    archetype: str = "generic"
    flows: list = field(default_factory=list)
    explore_system_prompt: str = ""
    report_system_prompt: str = ""
    uses_admin_login: bool = False


_SUPPRESSION = (
    "BY DESIGN — do not re-file. The /verify-pending page is intentional and "
    "not a bug; it's the magic-link gate."
)

_PERSONAS = {
    "newcomer": _FakePersona(
        archetype="low-tech",
        flows=["homepage", "signup"],
        explore_system_prompt=(
            "You are a newcomer.\n\n" + _SUPPRESSION + "\n\nGo explore."
        ),
    ),
    "admin": _FakePersona(
        archetype="staff",
        flows=["homepage", "admin-dashboard"],  # 'homepage' shared with newcomer
        uses_admin_login=True,
        report_system_prompt=(
            # Same suppression as newcomer → should dedup to one knowledge row.
            "Write a review.\n\n" + _SUPPRESSION + "\n\nDone."
        ),
    ),
}


@pytest.fixture
def store() -> Store:
    client = mongomock.MongoClient()
    s = Store(client=client, db_name="testease_test")
    s.site_targets.create_index(
        [("tenant_id", 1), ("target_id", 1)], unique=True,
    )
    s.test_flows.create_index(
        [("tenant_id", 1), ("target_id", 1), ("flow_id", 1)], unique=True,
    )
    s.site_knowledge.create_index(
        [("tenant_id", 1), ("target_id", 1), ("entry_id", 1)], unique=True,
    )
    return s


def test_extract_flows_unique_per_persona_and_dedups_suppressions():
    flows, knowledge, has_admin = extract_from_personas(_PERSONAS)

    # 4 flow rows (2 per persona), distinct flow_ids even for the shared slug.
    flow_ids = sorted(f["flow_id"] for f in flows)
    assert flow_ids == [
        "admin:admin-dashboard", "admin:homepage",
        "newcomer:homepage", "newcomer:signup",
    ]
    # The shared BY-DESIGN block dedups to ONE entry, applies_to both personas.
    assert len(knowledge) == 1
    assert sorted(knowledge[0]["applies_to"]) == ["admin", "newcomer"]
    assert has_admin is True


def test_migrate_creates_target_flows_and_knowledge(store):
    result = migrate_dogfood(store, personas=_PERSONAS)

    assert result["flows"] == 4
    assert result["knowledge"] == 1

    target = get_site_target(store, DEFAULT_TENANT, "slyreply")
    assert target["base_url"] == "https://slyreply.ai"
    # has_admin → credential_ref is a POINTER, never raw creds.
    assert target["auth"]["method"] == "form"
    assert target["auth"]["credential_ref"] == SLYREPLY_ADMIN_CREDENTIAL_REF

    flows = list_flows_by_target(store, DEFAULT_TENANT, "slyreply")
    assert len(flows) == 4
    assert all(f["generated_from"] == "template" for f in flows)

    knowledge = list_knowledge_by_target(store, DEFAULT_TENANT, "slyreply")
    assert len(knowledge) == 1
    assert knowledge[0]["kind"] == "by_design"
    assert knowledge[0]["authored_by"] == "migration"


def test_migrate_is_idempotent(store):
    migrate_dogfood(store, personas=_PERSONAS)
    migrate_dogfood(store, personas=_PERSONAS)  # re-run

    assert len(list_flows_by_target(store, DEFAULT_TENANT, "slyreply")) == 4
    assert len(list_knowledge_by_target(store, DEFAULT_TENANT, "slyreply")) == 1
    assert len(list(store.site_targets.find())) == 1
