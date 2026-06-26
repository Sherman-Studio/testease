"""Unit tests for the Faker identity MCP server (#1023)."""

from __future__ import annotations

import re

import pytest

from qa_agents.tools.identity import (
    Identity,
    _slugify_name_for_email,
    build_identity_server,
    generate_identity,
    resolve_faker_locale,
)


# ---------------------------------------------------------------------------
# resolve_faker_locale — pure mapping logic.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "region,language,expected",
    [
        ("GB", "en", "en_GB"),
        ("US", "en", "en_US"),
        ("IE", "en", "en_IE"),
        ("DE", "de", "de_DE"),
        ("JP", "ja", "ja_JP"),
        ("FR", "fr", "fr_FR"),
        # Lowercase region / uppercase language are normalised.
        ("gb", "EN", "en_GB"),
    ],
)
def test_resolve_locale_returns_specific_when_supported(region, language, expected):
    assert resolve_faker_locale(region=region, language=language) == expected


def test_resolve_locale_falls_back_to_language_default_when_region_unsupported():
    """region="XX" is not in the catalog; fall back to the language's
    culturally-default region rather than en_US (which would lose locale
    realism)."""
    assert resolve_faker_locale(region="XX", language="ja") == "ja_JP"
    assert resolve_faker_locale(region="XX", language="de") == "de_DE"


def test_resolve_locale_language_only_picks_default_region():
    """Persona that only set language → use the language-default region."""
    assert resolve_faker_locale(region=None, language="ja") == "ja_JP"
    assert resolve_faker_locale(region=None, language="fr") == "fr_FR"
    assert resolve_faker_locale(region=None, language="en") == "en_US"


def test_resolve_locale_unknown_language_falls_back_to_en_us():
    """Klingon (klingon-en) is not a Faker locale. Fall through to en_US
    rather than crash — the persona still gets a usable identity."""
    assert resolve_faker_locale(region="US", language="kl") == "en_US"
    assert resolve_faker_locale(region=None, language=None) == "en_US"
    assert resolve_faker_locale(region="", language="") == "en_US"


# ---------------------------------------------------------------------------
# _slugify_name_for_email — ASCII-safe local-part generator.
# ---------------------------------------------------------------------------
def test_slugify_plain_ascii_name():
    assert _slugify_name_for_email("Maya Patel") == "maya.patel"


def test_slugify_strips_diacritics():
    """A French / German name with accents must still produce ASCII."""
    assert _slugify_name_for_email("Émilie Schröder") == "emilie.schroder"


def test_slugify_cjk_name_falls_back_to_user():
    """田中 太郎 contains no ASCII letters — generator returns the safe
    sentinel ``user`` so the caller's random suffix still produces a
    valid email."""
    assert _slugify_name_for_email("田中 太郎") == "user"


def test_slugify_empty_or_whitespace_returns_user():
    assert _slugify_name_for_email("") == "user"
    assert _slugify_name_for_email("   ") == "user"


# ---------------------------------------------------------------------------
# generate_identity — locale-aware payload generation.
# ---------------------------------------------------------------------------
def test_generate_identity_returns_complete_payload():
    identity = generate_identity(
        region="GB",
        language="en",
        email_domain="testease.example.com",
        seed=42,
    )
    assert isinstance(identity, Identity)
    assert identity.name
    assert identity.first_name
    assert identity.last_name
    assert identity.email.endswith("@testease.example.com")
    assert identity.phone
    assert identity.street_address
    assert identity.city
    assert identity.postcode
    assert identity.country
    assert identity.locale == "en_GB"


def test_generate_identity_email_is_well_formed_address():
    """The generated email must pass a basic shape check — local + @ +
    domain — so signup forms with an HTML5 ``type=email`` validator
    accept it."""
    identity = generate_identity(
        region="JP",
        language="ja",
        email_domain="testease.example.com",
        seed=99,
    )
    assert re.match(
        r"^[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+$",
        identity.email,
    ), f"email {identity.email!r} does not match the basic shape regex"


def test_generate_identity_email_has_random_suffix_for_uniqueness():
    """Two runs of the same persona must NOT produce the same email —
    even with the same seed, the suffix uses :mod:`secrets` for
    process-level entropy so cross-run collisions are vanishingly
    unlikely."""
    a = generate_identity(
        region="GB", language="en", email_domain="x.test", seed=1
    )
    b = generate_identity(
        region="GB", language="en", email_domain="x.test", seed=1
    )
    # Names will be the same (seed=1 fixes Faker's RNG) but emails
    # differ because the suffix is from secrets.token_hex, not Faker.
    assert a.name == b.name
    assert a.email != b.email


def test_generate_identity_ja_jp_produces_japanese_phone():
    """Sanity check that locale wiring actually flows through to Faker.
    Japanese phone numbers contain the country code or "0" prefix and
    digits/dashes — they do NOT look like US ``(555) 123-4567``."""
    identity = generate_identity(
        region="JP",
        language="ja",
        email_domain="x.test",
        seed=7,
    )
    assert identity.locale == "ja_JP"
    # Japan Faker phone outputs include either +81 or a leading 0.
    assert re.search(r"\+81|^0", identity.phone), (
        f"expected JP-style phone, got {identity.phone!r}"
    )


def test_identity_summary_is_human_readable():
    """The summary string is what the agent sees in the tool result.
    Must contain the fields the persona needs to paste into the form."""
    identity = generate_identity(
        region="GB", language="en", email_domain="x.test", seed=3
    )
    summary = identity.summary()
    assert identity.name in summary
    assert identity.email in summary
    assert identity.phone in summary
    assert identity.locale in summary


def test_identity_as_dict_roundtrips_all_fields():
    identity = generate_identity(
        region="DE", language="de", email_domain="x.test", seed=5
    )
    payload = identity.as_dict()
    for key in (
        "name", "first_name", "last_name", "email", "phone",
        "street_address", "city", "postcode", "country", "locale",
    ):
        assert key in payload
        assert payload[key] == getattr(identity, key)


# ---------------------------------------------------------------------------
# build_identity_server — factory contract.
# ---------------------------------------------------------------------------
def test_build_identity_server_returns_server_and_tool_names():
    """Matches the build_email_server / build_findings_server shape: a
    (server, tool-names) tuple. The runner uses the tool-names list to
    populate allowed_tools."""
    server, tool_names = build_identity_server(
        persona_region="GB",
        persona_language="en",
        email_domain="testease.example.com",
    )
    assert server is not None
    assert tool_names == ["generate_identity"]
