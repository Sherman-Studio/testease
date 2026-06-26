"""Tests for :mod:`qa_store.coverage_catalog`.

The catalog is data, but it is data the Slice 4 coverage-matrix UI consumes
directly — a regression here (a typo'd persona id, a duplicate entry id, an
empty category) silently breaks the trigger page. These tests assert the
shape invariants so a bad PR can never merge silently.

Moved from ``qa_agents.coverage_catalog`` to ``qa_store.coverage_catalog``
in #861 so the review-ui API can read the catalog without dragging the
Claude Agent SDK into its image (see qa_store/__init__.py header).
"""

from __future__ import annotations

import re

import pytest

from qa_store.coverage_catalog import (
    CATALOG,
    CATEGORIES,
    KNOWN_PERSONA_IDS,
    CoverageAction,
)

# Allowed shape: lowercase letters/digits/underscore, dotted segments,
# first segment must match a known category — e.g. ``billing.upgrade_to_pro``.
_ID_REGEX = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


def test_catalog_is_a_tuple_of_coverage_actions() -> None:
    assert isinstance(CATALOG, tuple), "CATALOG must be a tuple (immutable)"
    assert CATALOG, "CATALOG must not be empty"
    for entry in CATALOG:
        assert isinstance(entry, CoverageAction), (
            f"every CATALOG entry must be a CoverageAction, got {type(entry).__name__}"
        )


def test_every_action_id_is_unique() -> None:
    ids = [entry.id for entry in CATALOG]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"duplicate action ids in CATALOG: {duplicates}"


def test_every_action_id_is_stable_slug() -> None:
    bad = [entry.id for entry in CATALOG if not _ID_REGEX.match(entry.id)]
    assert not bad, (
        f"action ids must match category.subcategory_word pattern; offenders: {bad}"
    )


def test_every_action_id_first_segment_matches_a_known_category() -> None:
    bad: list[str] = []
    for entry in CATALOG:
        prefix = entry.id.split(".", 1)[0]
        if prefix not in CATEGORIES:
            bad.append(entry.id)
    assert not bad, (
        f"action id's first segment must be in CATEGORIES; offenders: {bad}"
    )


def test_every_action_category_is_in_categories_constant() -> None:
    bad = [
        entry.id
        for entry in CATALOG
        if entry.category not in CATEGORIES
    ]
    assert not bad, (
        f"every entry's .category must be in CATEGORIES; offenders: {bad}"
    )


def test_every_action_id_category_matches_category_field() -> None:
    """The id's prefix and the .category field must agree (no cross-wiring)."""
    bad: list[str] = []
    for entry in CATALOG:
        prefix = entry.id.split(".", 1)[0]
        if prefix != entry.category:
            bad.append(f"{entry.id} (category={entry.category})")
    assert not bad, f"id-prefix vs .category mismatch: {bad}"


def test_every_action_persona_compat_is_subset_of_known_ids() -> None:
    bad: list[str] = []
    for entry in CATALOG:
        unknown = set(entry.persona_compat) - KNOWN_PERSONA_IDS
        if unknown:
            bad.append(f"{entry.id}: unknown persona ids {sorted(unknown)}")
    assert not bad, "persona_compat must be ⊆ KNOWN_PERSONA_IDS: " + "; ".join(bad)


def test_every_action_persona_compat_is_non_empty() -> None:
    """An entry with no plausible persona is dead code in the matrix UI."""
    bad = [entry.id for entry in CATALOG if not entry.persona_compat]
    assert not bad, (
        f"every entry's persona_compat must list at least one persona; offenders: {bad}"
    )


def test_every_persona_appears_in_at_least_one_action() -> None:
    seen: set[str] = set()
    for entry in CATALOG:
        seen.update(entry.persona_compat)
    missing = sorted(KNOWN_PERSONA_IDS - seen)
    assert not missing, (
        f"these personas never appear in any catalog entry: {missing}"
    )


def test_every_category_in_categories_constant_has_at_least_one_action() -> None:
    by_category = {c: 0 for c in CATEGORIES}
    for entry in CATALOG:
        by_category[entry.category] = by_category.get(entry.category, 0) + 1
    empty = sorted(c for c, n in by_category.items() if n == 0)
    assert not empty, f"these categories have no entries: {empty}"


def test_catalog_size_within_bounds() -> None:
    """Sanity check on scope — too few = under-covered, too many = unreviewable."""
    assert 40 <= len(CATALOG) <= 100, (
        f"CATALOG has {len(CATALOG)} entries; expected 40 ≤ n ≤ 100 "
        "(re-evaluate the bounds in #859 if the catalog has legitimately grown)."
    )


def test_every_entry_has_non_empty_strings() -> None:
    bad: list[str] = []
    for entry in CATALOG:
        for field_name in ("id", "human_description", "expected_outcome", "category"):
            value = getattr(entry, field_name)
            if not isinstance(value, str) or not value.strip():
                bad.append(f"{entry.id or '<no-id>'}.{field_name}")
    assert not bad, f"these fields are empty or non-string: {bad}"


def test_requires_auth_is_boolean() -> None:
    bad = [
        entry.id
        for entry in CATALOG
        if not isinstance(entry.requires_auth, bool)
    ]
    assert not bad, f"requires_auth must be bool; offenders: {bad}"


def test_admin_actions_compat_only_tomas() -> None:
    """The admin category is operator-side and currently only tomas covers it."""
    bad: list[str] = []
    for entry in CATALOG:
        if entry.category != "admin":
            continue
        non_tomas = set(entry.persona_compat) - {"tomas"}
        if non_tomas:
            bad.append(f"{entry.id}: non-admin personas in compat {sorted(non_tomas)}")
    assert not bad, (
        "admin-category entries should currently only list 'tomas': " + "; ".join(bad)
    )


def test_tomas_only_appears_on_admin_and_auth_login() -> None:
    """Tomás logs in as admin — he should not be wired into user-side flows.

    The single allowed user-side action is ``auth.login_existing_user`` (he
    logs in, just not via signup). Every other tomas appearance must be in
    the admin category.
    """
    allowed_non_admin_ids = {"auth.login_existing_user", "auth.logout"}
    bad: list[str] = []
    for entry in CATALOG:
        if "tomas" not in entry.persona_compat:
            continue
        if entry.category == "admin":
            continue
        if entry.id in allowed_non_admin_ids:
            continue
        bad.append(entry.id)
    assert not bad, (
        "tomas should only appear in admin entries (plus the shared login row): "
        f"unexpected appearances on {bad}"
    )


@pytest.mark.parametrize("entry", CATALOG, ids=lambda e: e.id)
def test_entry_is_hashable_and_frozen(entry: CoverageAction) -> None:
    """frozen=True means entries can be used in sets / as dict keys."""
    assert hash(entry) is not None
    with pytest.raises((AttributeError, Exception)):
        entry.id = "mutated.attempt"  # type: ignore[misc]
