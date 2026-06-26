"""Seed the ``qa_personas`` collection from the harness's PERSONAS dict.

Designed to be called on every FastAPI startup (idempotent — insert-only:
a row for a given ``persona_id`` is only written when no row exists yet,
so an operator's UI edits to a default persona survive subsequent boots).

After #1009's relaunch, the catalog is 25 generic archetypes (Maya the
mobile signup visitor, Jordan the desktop evaluator, etc) — all starting
with ``is_active=False``. The operator activates the subset they want
for their tenant from the Personas page. Region + language defaults
come from the persona definition; the operator customises both via the
detail editor.

The destructive ``reset_default_personas`` path overwrites every row
with the harness's current PERSONAS values — operator UI edits are
lost. It's the "factory reset" button.
"""

from __future__ import annotations

from .schema import PERSONA_COLOR_TOKENS, Store, create_persona, upsert_persona


def _color_for(index: int) -> str:
    """Pick a deterministic colour token for a persona by ordinal position.

    25 personas, 12 colour tokens — they cycle. Same index always picks
    the same colour, so a re-seed produces a stable palette. Personas
    later in the catalog reuse colours from earlier; that's fine because
    the Personas card layout shows the name + archetype alongside, the
    colour is just a visual rhythm cue, not an identity.
    """
    return PERSONA_COLOR_TOKENS[index % len(PERSONA_COLOR_TOKENS)]


def _build_doc(persona, *, ordinal: int) -> dict:
    """Translate a ``qa_agents.personas.Persona`` into the qa_personas
    document shape. Pulled out so seed + reset share the same mapping.

    ``ordinal`` is the persona's position in the registry — drives the
    color cycle. Order matters so passing it explicitly keeps the
    seed/reset paths in sync.
    """
    return {
        "persona_id": persona.id,
        "display_name": persona.display_name,
        "archetype": persona.archetype,
        "registered_email": persona.registered_email,
        "explore_system_prompt": persona.explore_system_prompt,
        "report_system_prompt": persona.report_system_prompt,
        "flows": list(persona.flows),
        "uses_admin_login": persona.uses_admin_login,
        "setup_actions": persona.setup_actions,
        # #1009 — region + language replace browser_locale. We persist
        # ALL three: region, language, and the composite (the property
        # the harness reads at run time). Persisting the composite means
        # an old harness that still reads browser_locale keeps working.
        "region": persona.region,
        "language": persona.language,
        "browser_locale": persona.browser_locale,
        "color_token": _color_for(ordinal),
        # Use the persona id as the avatar seed — stable across re-runs,
        # human-meaningful in URLs.
        "avatar_seed": persona.id,
        "is_default": True,
        # #1009 — all new catalog rows start INACTIVE. Operator activates
        # the relevant subset per-tenant from the Personas page. This is
        # the major behavioural change from the pre-relaunch catalog,
        # which auto-included every persona in trigger-time runs.
        "is_active": persona.is_active,
        "hidden": False,
    }


def seed_default_personas(store: Store) -> int:
    """Insert the seeded personas if they don't already exist.

    Returns the number of NEW rows inserted (0 on a re-run against an
    already-seeded DB). Existing rows are left untouched — an operator
    who edits a persona's prompt via the UI does NOT lose that edit on
    the next pod restart. The "factory reset" path lives in
    ``reset_default_personas``.
    """
    # ``qa_agents.personas`` only imports ``dataclasses`` at module level
    # (no Claude SDK, no Playwright). Install the harness with --no-deps
    # in the review-ui image and this import succeeds without dragging
    # the heavy runtime in. If qa-agents isn't on the path at all (some
    # test contexts), the seed is skipped — callers handle ImportError.
    from pymongo.errors import DuplicateKeyError
    from qa_agents.personas import PERSONAS  # type: ignore[import]

    inserted = 0
    for ordinal, persona in enumerate(PERSONAS.values()):
        doc = _build_doc(persona, ordinal=ordinal)
        try:
            create_persona(store, doc)
            inserted += 1
        except DuplicateKeyError:
            # Row already exists — keep the operator's edits. This is the
            # whole point of insert-only seeding.
            pass
    return inserted


def reset_default_personas(store: Store) -> int:
    """Overwrite all seeded personas with the catalog's current values.

    Destructive — every default persona's UI edits are lost. Used by the
    UI's "Reset to defaults" action and by tests that want a known
    starting state. Returns the number of personas processed.
    """
    from qa_agents.personas import PERSONAS  # type: ignore[import]

    for ordinal, persona in enumerate(PERSONAS.values()):
        upsert_persona(store, _build_doc(persona, ordinal=ordinal))
    return len(PERSONAS)
