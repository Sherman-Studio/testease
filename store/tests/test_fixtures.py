"""Tests for the fixture catalog loader (Slice 3a of #1006).

Two tiers:
  * TestShippedCatalog — the real ``fixtures/example.yaml`` parses and
    carries the keys the variant generator + harness depend on.
  * TestLoaderContract — env interpolation, required-key validation,
    missing-file + malformed-file errors, against temp YAML files.

The prompt-placeholder dict (DEFAULT_FIXTURES / flat_placeholders /
load_fixtures) is tested by the harness's test_personas.py; this file
is the new catalog half only.
"""

from __future__ import annotations

import pytest

from qa_store.fixtures import (
    REQUIRED_CATALOG_KEYS,
    FixtureCatalogError,
    load_fixture_catalog,
)


# ---------------------------------------------------------------------------
# The shipped, site-agnostic example catalog (the default tenant).
# ---------------------------------------------------------------------------
class TestShippedCatalog:
    def test_example_yaml_parses(self):
        cat = load_fixture_catalog("example", env={})
        assert isinstance(cat, dict)

    def test_default_tenant_is_the_example_catalog(self):
        # The default tenant arg loads the shipped example — no site name.
        assert load_fixture_catalog(env={})["app"] == "example"

    def test_has_required_keys(self):
        cat = load_fixture_catalog("example", env={})
        for key in REQUIRED_CATALOG_KEYS:
            assert cat.get(key), f"missing required key {key!r}"
        assert cat["app"] == "example"
        assert cat["base_url"].startswith("https://")

    def test_carries_payment_test_cards_for_billing_variants(self):
        cat = load_fixture_catalog("example", env={})
        ids = {c["id"] for c in cat["payment_test_cards"]}
        # The cards the variant generator branches billing on.
        assert {"valid", "declined-generic", "requires-3ds"} <= ids

    def test_carries_auth_inputs_for_auth_variants(self):
        cat = load_fixture_catalog("example", env={})
        assert "weak_password" in cat["auth_test_inputs"]
        assert "duplicate_email" in cat["auth_test_inputs"]

    def test_unset_secret_left_as_literal_placeholder(self):
        # With no env, ${TESTEASE_ADMIN_EMAIL} stays visible rather than
        # collapsing to an empty string.
        cat = load_fixture_catalog("example", env={})
        admin = next(a for a in cat["accounts"] if a["id"] == "admin")
        assert admin["email"] == "${TESTEASE_ADMIN_EMAIL}"

    def test_secret_interpolated_when_env_present(self):
        cat = load_fixture_catalog(
            "example",
            env={
                "TESTEASE_ADMIN_EMAIL": "admin@sandbox.test",
                "TESTEASE_ADMIN_PASSWORD": "s3cr3t",
            },
        )
        admin = next(a for a in cat["accounts"] if a["id"] == "admin")
        assert admin["email"] == "admin@sandbox.test"
        assert admin["password"] == "s3cr3t"


# ---------------------------------------------------------------------------
# Loader contract — temp YAML files.
# ---------------------------------------------------------------------------
def _write(tmp_path, name, text):
    p = tmp_path / f"{name}.yaml"
    p.write_text(text, encoding="utf-8")
    return tmp_path


class TestLoaderContract:
    def test_env_interpolation_nested(self, tmp_path):
        d = _write(
            tmp_path, "acme",
            "app: acme\nbase_url: https://acme.test\n"
            "accounts:\n  - id: a\n    secret: ${ACME_SECRET}\n",
        )
        cat = load_fixture_catalog("acme", fixtures_dir=d, env={"ACME_SECRET": "xyz"})
        assert cat["accounts"][0]["secret"] == "xyz"

    def test_partial_string_interpolation(self, tmp_path):
        d = _write(
            tmp_path, "acme",
            "app: acme\nbase_url: https://${HOST}/app\n",
        )
        cat = load_fixture_catalog("acme", fixtures_dir=d, env={"HOST": "acme.test"})
        assert cat["base_url"] == "https://acme.test/app"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FixtureCatalogError, match="no fixture catalog"):
            load_fixture_catalog("nope", fixtures_dir=tmp_path, env={})

    def test_non_mapping_raises(self, tmp_path):
        d = _write(tmp_path, "acme", "- just\n- a\n- list\n")
        with pytest.raises(FixtureCatalogError, match="must be a YAML mapping"):
            load_fixture_catalog("acme", fixtures_dir=d, env={})

    def test_missing_required_key_raises(self, tmp_path):
        d = _write(tmp_path, "acme", "app: acme\n")  # no base_url
        with pytest.raises(FixtureCatalogError, match="base_url"):
            load_fixture_catalog("acme", fixtures_dir=d, env={})

    def test_additive_keys_preserved(self, tmp_path):
        d = _write(
            tmp_path, "acme",
            "app: acme\nbase_url: https://acme.test\n"
            "some_future_key:\n  nested: value\n",
        )
        cat = load_fixture_catalog("acme", fixtures_dir=d, env={})
        assert cat["some_future_key"] == {"nested": "value"}
