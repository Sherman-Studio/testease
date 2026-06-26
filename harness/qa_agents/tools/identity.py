"""Identity MCP server — locale-aware fake persona identities (#1023).

Wraps the ``faker`` Python library as an in-process MCP server exposing
one tool: ``generate_identity``. The persona calls it once at the start
of a signup-shaped flow to get a name / email / phone / address that
looks plausible for the persona's region + language, then uses those
values in the actual signup form.

Why this exists
---------------

Pre-#1023 every persona had a hardcoded ``{persona_email}`` placeholder
like ``maya@testease.example.com``. That has two costs:

1. **Cross-run collisions.** Re-running Maya against the same staging
   site fails on "email already registered" once the persona has signed
   up once, because the email is static.
2. **No locale realism.** A Japanese tenant testing a Japanese signup
   flow sees "Maya Patel" type into the name field — not what a real
   ``ja_JP`` user looks like.

How the email routes
--------------------

Mailpit (the dev mail sink) catches everything to the persona's
registered email DOMAIN. To keep ``wait_for_email`` working, the
generated email reuses ``persona.registered_email``'s domain — only the
local-part is fresh. So a Maya whose registered_email is
``maya@testease.example.com`` might generate
``"name": "Yuki Tanaka", "email": "yuki.tanaka.4f2a@testease.example.com"``
which still lands in the Mailpit inbox the harness reads from.

Per-run persistence (#1023 acceptance item 3, "Run-detail UI shows the
per-run identity") is deferred to a follow-up — it needs a qa-store
schema addition + a review-ui change. The tool already returns the
identity to the agent, who can mention it in findings.
"""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass

from claude_agent_sdk import create_sdk_mcp_server, tool

# ---------------------------------------------------------------------------
# Locale resolution — pure helpers, unit-tested directly.
# ---------------------------------------------------------------------------

# Locales we explicitly support. Faker has more, but this is the set we've
# eyeballed for sensible name + address output. An unrecognised
# `language_region` falls back to the language's default region; an
# unrecognised language falls back to en_US.
_SUPPORTED_LOCALES = frozenset(
    {
        "en_US",
        "en_GB",
        "en_IE",
        "en_AU",
        "en_CA",
        "en_NZ",
        "fr_FR",
        "fr_CA",
        "de_DE",
        "de_AT",
        "de_CH",
        "es_ES",
        "es_MX",
        "it_IT",
        "pt_BR",
        "pt_PT",
        "nl_NL",
        "nl_BE",
        "ja_JP",
        "ko_KR",
        "zh_CN",
        "zh_TW",
        "sv_SE",
        "no_NO",
        "da_DK",
        "fi_FI",
        "pl_PL",
        "ru_RU",
    }
)

# When the operator picks a language without a region, fall back to a
# culturally-default region for that language so output is internally
# consistent (Japanese names with Japanese phone numbers, not US ones).
_LANGUAGE_DEFAULT_REGION = {
    "en": "US",
    "fr": "FR",
    "de": "DE",
    "es": "ES",
    "it": "IT",
    "pt": "BR",
    "nl": "NL",
    "ja": "JP",
    "ko": "KR",
    "zh": "CN",
    "sv": "SE",
    "no": "NO",
    "da": "DK",
    "fi": "FI",
    "pl": "PL",
    "ru": "RU",
}


def resolve_faker_locale(region: str | None, language: str | None) -> str:
    """Map (region, language) BCP-47 fragments to a Faker locale string.

    Faker uses ``language_REGION`` underscored locales (``en_GB``,
    ``ja_JP``). The persona model uses BCP-47 fragments (``"en"``,
    ``"GB"``) stored separately. This is the canonical mapping.

    Resolution order:

    1. ``{language}_{region}`` — if supported, use it.
    2. ``{language}_{default_region_for_language}`` — culturally-default
       region for the language. Lets ``language="ja", region=None``
       still produce Japanese output rather than English with
       Japanese-shaped placeholders.
    3. ``en_US`` — last-resort fallback.

    All three branches return a value in :data:`_SUPPORTED_LOCALES`, so
    the caller can construct ``Faker(locale)`` without a try/except.
    """
    lang = (language or "").strip().lower() or None
    reg = (region or "").strip().upper() or None

    if lang and reg:
        candidate = f"{lang}_{reg}"
        if candidate in _SUPPORTED_LOCALES:
            return candidate

    if lang and lang in _LANGUAGE_DEFAULT_REGION:
        candidate = f"{lang}_{_LANGUAGE_DEFAULT_REGION[lang]}"
        if candidate in _SUPPORTED_LOCALES:
            return candidate

    return "en_US"


def _slugify_name_for_email(name: str) -> str:
    """Make an ASCII-safe email local-part out of a faker-generated name.

    Strips diacritics, drops non-alphanumerics, lowercases. CJK names
    (``田中 太郎``) end up as an empty string — caller adds the random
    suffix so the address is still unique and well-formed.
    """
    import unicodedata

    normalised = unicodedata.normalize("NFKD", name or "")
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    parts = [p for p in ascii_only.lower().split() if p]
    return ".".join(parts) if parts else "user"


# ---------------------------------------------------------------------------
# Identity payload — what the tool returns to the agent.
# ---------------------------------------------------------------------------
@dataclass
class Identity:
    """A faker-generated identity for one persona-run.

    All fields are strings the persona can paste straight into a signup
    form. ``locale`` is included so the agent can mention it in findings
    ("I signed up as Yuki Tanaka — ja_JP locale, JP region").
    """

    name: str
    first_name: str
    last_name: str
    email: str
    phone: str
    street_address: str
    city: str
    postcode: str
    country: str
    locale: str

    def as_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        """Compact agent-readable rendering of the identity."""
        return (
            f"name: {self.name}\n"
            f"email: {self.email}\n"
            f"phone: {self.phone}\n"
            f"address: {self.street_address}, {self.city} {self.postcode}, "
            f"{self.country}\n"
            f"locale: {self.locale}"
        )


def generate_identity(
    *,
    region: str | None,
    language: str | None,
    email_domain: str,
    seed: int | None = None,
) -> Identity:
    """Generate a single faker-backed identity.

    ``email_domain`` is the persona's existing registered-email domain
    (e.g. ``testease.example.com``) — reusing it keeps generated mail
    flowing through the same Mailpit catch-all the harness already
    reads from.

    ``seed`` is for deterministic tests. Production callers leave it
    None so every run gets a fresh identity.
    """
    from faker import Faker

    locale = resolve_faker_locale(region=region, language=language)
    fake = Faker(locale)
    if seed is not None:
        fake.seed_instance(seed)

    first = fake.first_name()
    last = fake.last_name()
    full_name = f"{first} {last}"

    # Local-part is the slug + a short random suffix so two runs of the
    # same persona never collide on the signup form's email-uniqueness
    # check. 6 hex chars = 16M possibilities, enough for the use case.
    suffix = secrets.token_hex(3)
    local = _slugify_name_for_email(full_name)
    email = f"{local}.{suffix}@{email_domain}"

    return Identity(
        name=full_name,
        first_name=first,
        last_name=last,
        email=email,
        phone=fake.phone_number(),
        street_address=fake.street_address(),
        city=fake.city(),
        postcode=fake.postcode(),
        country=fake.current_country(),
        locale=locale,
    )


# ---------------------------------------------------------------------------
# SDK tool factory — mirrors build_email_server / build_findings_server.
# ---------------------------------------------------------------------------
def build_identity_server(
    *,
    persona_region: str | None,
    persona_language: str | None,
    email_domain: str,
):
    """Build the in-process MCP server exposing ``generate_identity``.

    ``persona_region`` / ``persona_language`` come from the Persona
    dataclass at run start. ``email_domain`` is parsed from
    ``persona.registered_email`` by the runner so the agent doesn't
    have to know it.
    """

    @tool(
        "generate_identity",
        "Generate a plausible identity to use for THIS signup (name, "
        "email, phone, address) — locale-appropriate for the region and "
        "language you've been given. Call this ONCE at the start of a "
        "signup flow, then use the returned values in the signup form. "
        "The email routes back to your inbox so wait_for_email will see "
        "verification mail addressed to it.",
        {},
    )
    async def generate_identity_tool(_args: dict) -> dict:
        identity = generate_identity(
            region=persona_region,
            language=persona_language,
            email_domain=email_domain,
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": identity.summary(),
                }
            ]
        }

    server = create_sdk_mcp_server(
        name="identity",
        tools=[generate_identity_tool],
    )
    return server, ["generate_identity"]
