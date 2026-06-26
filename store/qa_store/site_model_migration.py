"""Dogfood migration: lift slyreply's site knowledge out of ``personas.py``.

The harness used to hardcode, per persona: a ``flows`` list (the test plan) and
``BY DESIGN`` suppression blocks in the prompts ("don't re-file X, it's
intended"). For the Site Model those become per-(tenant, target) DATA. This
module turns a persona registry into Site-Model rows and upserts them for the
``slyreply`` target under ``DEFAULT_TENANT``.

Dependency direction: this module does NOT import the harness. ``personas.py``
lives in the harness package (which depends on qa-store, not the reverse), so
``extract_from_personas`` operates on duck-typed persona objects (anything with
``.id``, ``.archetype``, ``.flows``, ``.uses_admin_login`` and the prompt
strings). A thin runner in the harness imports ``PERSONAS`` and calls this.

Idempotent: every write is an upsert on a unique (tenant, target, entity_id)
key, so re-running reconciles in place — no duplicates.
"""

from __future__ import annotations

import hashlib
from typing import Any

from qa_store.schema import DEFAULT_TENANT, Store
from qa_store.site_model import (
    upsert_site_knowledge,
    upsert_site_target,
    upsert_test_flow,
)

# Pointer (NOT a raw secret) into the qa-store persona-credential vault where
# the slyreply admin login actually lives (qa_store.crypto / get_persona_
# credentials). site_targets.auth.credential_ref stores only this reference.
SLYREPLY_ADMIN_CREDENTIAL_REF = "qa-store:persona-credentials/slyreply-admin"


def _prompt_texts(persona: Any) -> list[str]:
    """The persona's prompt strings (where BY DESIGN blocks live)."""
    out: list[str] = []
    for attr in ("explore_system_prompt", "report_system_prompt"):
        val = getattr(persona, attr, "") or ""
        if isinstance(val, str) and val:
            out.append(val)
    return out


def _by_design_blocks(text: str) -> list[str]:
    """Paragraphs whose first content is ``BY DESIGN`` — the dedicated
    suppression blocks (skips inline mentions like '... IS BY DESIGN')."""
    blocks: list[str] = []
    for para in text.split("\n\n"):
        stripped = para.strip()
        if stripped.upper().startswith("BY DESIGN"):
            blocks.append(stripped)
    return blocks


def extract_from_personas(
    personas: dict[str, Any],
) -> tuple[list[dict], list[dict], bool]:
    """Turn a persona registry into (test_flows, site_knowledge, has_admin).

    - flows: one row per (persona, flow slug); flow_id ``{persona}:{slug}`` is
      unique, so the same slug under different personas is preserved with its
      own ``persona_archetype``.
    - knowledge: deduped BY-DESIGN blocks; ``applies_to`` collects the persona
      ids a block came from.
    """
    flows: list[dict] = []
    # entry_id -> {entry_id, body, applies_to(list)} ; deduped by body hash so a
    # suppression shared across personas is one row applying to several.
    knowledge: dict[str, dict] = {}
    has_admin = False

    for pid, persona in personas.items():
        if getattr(persona, "uses_admin_login", False):
            has_admin = True
        archetype = getattr(persona, "archetype", None)
        for slug in getattr(persona, "flows", []) or []:
            flows.append({
                "flow_id": f"{pid}:{slug}",
                "area": slug,
                "user_story": slug.replace("-", " "),
                "persona_archetype": archetype,
            })
        for text in _prompt_texts(persona):
            for body in _by_design_blocks(text):
                key = hashlib.sha1(
                    " ".join(body.split()).encode("utf-8"),
                ).hexdigest()[:12]
                entry = knowledge.setdefault(
                    key,
                    {"entry_id": f"bydesign-{key}", "body": body, "applies_to": []},
                )
                if pid not in entry["applies_to"]:
                    entry["applies_to"].append(pid)

    return flows, list(knowledge.values()), has_admin


def migrate_dogfood(
    store: Store,
    *,
    personas: dict[str, Any] | None = None,
    flows: list[dict] | None = None,
    suppressions: list[dict] | None = None,
    has_admin: bool = False,
    base_url: str = "https://slyreply.ai",
    tenant_id: str = DEFAULT_TENANT,
    target_id: str = "slyreply",
    display_name: str = "SlyReply",
) -> dict:
    """Create the ``slyreply`` target and upsert its lifted flows + by-design
    knowledge. Pass a ``personas`` dict to extract from it, or pre-extracted
    ``flows`` / ``suppressions``. Idempotent — safe to re-run."""
    if personas is not None:
        flows, suppressions, has_admin = extract_from_personas(personas)
    flows = flows or []
    suppressions = suppressions or []

    upsert_site_target(
        store,
        tenant_id=tenant_id,
        target_id=target_id,
        base_url=base_url,
        display_name=display_name,
        auth={
            "method": "form",
            # Pointer only — the real admin creds stay in the credential vault.
            "credential_ref": SLYREPLY_ADMIN_CREDENTIAL_REF if has_admin else None,
        },
        ownership={"method": "first_party", "status": "verified"},
        status="active",
    )
    for f in flows:
        upsert_test_flow(
            store,
            tenant_id=tenant_id,
            target_id=target_id,
            flow_id=f["flow_id"],
            area=f.get("area", ""),
            user_story=f.get("user_story", ""),
            persona_archetype=f.get("persona_archetype"),
            generated_from="template",
        )
    for s in suppressions:
        upsert_site_knowledge(
            store,
            tenant_id=tenant_id,
            target_id=target_id,
            entry_id=s["entry_id"],
            kind="by_design",
            body=s["body"],
            applies_to=s.get("applies_to", []),
            authored_by="migration",
        )
    return {
        "tenant_id": tenant_id,
        "target_id": target_id,
        "flows": len(flows),
        "knowledge": len(suppressions),
        "has_admin": has_admin,
    }
