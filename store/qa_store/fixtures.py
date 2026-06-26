"""Shared test-data fixtures every persona can reference.

A "fixture" is shared knowledge the personas couldn't reasonably
discover by exploring the site itself — payment test card numbers, the
mailhog/mailpit URL pattern, well-known invalid emails, regions that
trigger specific behaviour, etc. Without a central place, every persona
prompt ends up with copy-pasted hardcoded values.

Personas reference fixtures via prompt placeholders:

    "Use the declined card: {fixture_payment_cards_declined_card}"

The harness's ``render_explore_prompt`` flattens the fixture dict into
the .format() kwargs so any ``{fixture_X}`` placeholder resolves to
``fixtures.payment.cards.X``.

Payment fixtures are provider-NEUTRAL (keyed under ``cards``, not a
provider name) so personas don't bake a payment processor into their
prompts. The default card numbers below are Revolut SANDBOX test cards —
a worked example set; operators override them per app with their own
processor's published test cards. Where a processor has no test-clock,
subscription lifecycle error states (trialing → past_due → canceled,
declines) can be driven via QA-only backend hooks rather than the
processor itself.

A future slice will add a per-tenant YAML editor where operators
customise fixtures for their app. This module is the *default* set —
enough for most generic SaaS apps to work out of the box.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Default fixtures. Operator-customisable in Slice 3 (#1008); for now,
# these are the defaults shipped with the harness image.
# ---------------------------------------------------------------------------
DEFAULT_FIXTURES: dict[str, Any] = {
    # ---------------------------------------------------------------
    # Payment — Revolut sandbox test cards (provider-neutral ``cards``
    # key so persona prompts don't bake in a processor name).
    # Reference: https://developer.revolut.com/docs/guides/merchant/test-and-go-live/testing/test-cards
    #
    # NOTE: the 3DS-fail card only triggers a challenge when the order
    # amount is >= £25 / €30; below that threshold it succeeds. The
    # decline cards only DECLINE in the normal (non-pre-auth) flow —
    # under authorisation_type=pre_authorisation every card authorises
    # in sandbox. Subscription lifecycle error states (past_due,
    # canceled, trialing) are driven via the QA /advance hook because
    # Revolut has no test-clock and the checkout widget (#1977) isn't
    # built yet.
    # ---------------------------------------------------------------
    "payment": {
        "provider": "revolut",
        "mode": "sandbox",
        "cards": {
            # Visa, always succeeds — happy-path billing personas use this.
            "valid_card": "4929 4205 7359 5709",
            # Mastercard, always succeeds — alternate happy-path card.
            "valid_card_mastercard": "5281 4388 0180 4148",
            # Generic decline (do_not_honour) — Aiko's declined-payer persona.
            "declined_card": "2720 9988 3777 9594",
            # Insufficient funds — alternate decline reason.
            "insufficient_funds_card": "4929 5736 3812 5985",
            # Card expired — alternate decline reason.
            "expired_card": "4532 3367 4387 4205",
            # 3DS-fail card (needs order amount >= £25 / €30 to challenge).
            "requires_3ds_card": "4242 4242 4242 4242",
            # Challenge failed — 3DS authentication failure path.
            "challenge_failed_card": "5215 6741 1512 7070",
            # Stuck "processing" — never settles, for timeout-handling tests.
            "processing_stuck_card": "2223 0000 1047 9399",
            # Universal test cvc + expiry for the cards above (any 3-digit
            # CVV, any future MM/YY expiry are accepted in sandbox).
            "any_cvc": "123",
            "any_future_expiry": "12 / 34",
        },
    },

    # ---------------------------------------------------------------
    # Inboxes — webmail viewers personas use to verify signup / reset
    # emails arrived. The operator overrides per-tenant; default is
    # mailpit at /mailpit (a common convention).
    # ---------------------------------------------------------------
    "inboxes": {
        "default": {
            "name": "mailpit",
            "kind": "mailpit",
            # Path-relative by default — the renderer prepends base_url.
            # Tenants on a separate domain override with an absolute URL.
            "path": "/mailpit",
        },
    },

    # ---------------------------------------------------------------
    # Email patterns — well-known emails that should be REJECTED or
    # handled specially by a well-built signup flow.
    # ---------------------------------------------------------------
    "test_emails": {
        # Obviously-invalid formats personas use to probe validation.
        "missing_at": "broken-no-at-sign",
        "missing_tld": "user@",
        "missing_local": "@example.com",
        "with_spaces": "user with spaces@example.com",
        # Plus-addressing — used by adversarial-tester to probe quota
        # bypass. Personas substitute their own local part.
        "plus_addressing_pattern": "{local}+plus@{domain}",
        # Disposable / blocklist domains a well-built signup blocks.
        "disposable_domain": "mailinator.com",
        "disposable_domain_2": "tempmail.com",
        # Catch-all real-but-unmonitored — for the brand-edge persona.
        "fake_brand_domain": "spotify.example.com",
    },

    # ---------------------------------------------------------------
    # Slugs / reserved names — for the brand-edge tester.
    # ---------------------------------------------------------------
    "reserved_slugs": [
        "admin", "administrator", "support", "help", "api", "www",
        "root", "system", "test", "demo", "public", "private",
        "mail", "ftp", "ssh", "static", "assets", "cdn",
    ],

    # ---------------------------------------------------------------
    # Adversarial inputs — common XSS / injection probes a security-
    # curious tester runs. None of these should land as raw HTML in
    # the rendered page.
    # ---------------------------------------------------------------
    "adversarial_inputs": {
        "xss_script": "<script>alert(1)</script>",
        "xss_img_onerror": '<img src=x onerror=alert(1)>',
        "html_inject": "<b>bold</b>",
        "sql_inject": "'; DROP TABLE users;--",
        "long_string": "A" * 200,
        "emoji": "🤖 testing 🧪",
        "rtl": "اختبار",  # "test" in Arabic
        "cyrillic": "Тестирование",
        "leading_whitespace": "   leading spaces",
    },

    # ---------------------------------------------------------------
    # Network conditions — for the slow-connection persona. Not actually
    # enforced by the harness today (Playwright supports throttling but
    # we haven't wired it); these are documentation for the persona's
    # mental model. Slice 4 (#1004) could wire real throttling.
    # ---------------------------------------------------------------
    "network": {
        "slow_3g_kbps": "400",
        "fast_3g_kbps": "1600",
        "offline_mode": "true",
    },
}


def flat_placeholders(
    fixtures: dict[str, Any] | None = None,
    *,
    prefix: str = "fixture",
    separator: str = "_",
) -> dict[str, str]:
    """Flatten a nested fixture dict into ``{prefix_path_to_leaf: value}``.

    Used by ``render_explore_prompt`` so a prompt template can use
    ``{fixture_payment_cards_declined_card}`` and get the value out
    of ``DEFAULT_FIXTURES["payment"]["cards"]["declined_card"]``.

    Lists become comma-separated strings — adequate for the brand-edge
    tester's reserved-slugs list ("admin, administrator, support, ...").
    Nested dicts recurse. Non-scalar values (anything not str/int/
    float/bool/list) skip with no entry — keeps prompts safe from
    accidental "<repr at 0x...>" noise.

    Example:
        flat_placeholders({"a": {"b": "x"}}) → {"fixture_a_b": "x"}
    """
    fixtures = fixtures if fixtures is not None else DEFAULT_FIXTURES
    out: dict[str, str] = {}

    def _walk(node: Any, path: list[str]) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, path + [str(k)])
        elif isinstance(node, list):
            key = separator.join([prefix] + path)
            out[key] = ", ".join(str(x) for x in node)
        elif isinstance(node, (str, int, float, bool)):
            key = separator.join([prefix] + path)
            out[key] = str(node)
        # else: skip silently — non-scalar leaves shouldn't end up in
        # prompts.

    _walk(fixtures, [])
    return out


def load_fixtures(tenant_id: str | None = None) -> dict[str, Any]:
    """Load the fixtures dict for one tenant.

    Slice 1 of #1009 ships only the default set — Slice 3 (#1008) adds
    per-tenant overrides via a YAML editor backed by a ``qa_fixtures``
    collection. Today ``tenant_id`` is accepted for forward-compat but
    ignored; the default dict is returned in every case.

    Returning a NEW dict each call (deep copy via dict ops) so a caller
    that mutates the returned object doesn't poison the module-level
    constant for the next caller.
    """
    import copy  # noqa: PLC0415

    _ = tenant_id  # reserved for Slice 3
    return copy.deepcopy(DEFAULT_FIXTURES)


# ---------------------------------------------------------------------------
# Slice 3a of #1006 — the per-tenant FIXTURE CATALOG.
#
# A YAML file at ``fixtures/<tenant>.yaml`` describing the test
# environment the personas operate against: base URL, mail viewer,
# payment test cards, seeded accounts, guard-rails, glossary. The
# operator writes it once per app; the variant generator
# (``qa_store.variants``) reads slices of it to branch a canonical
# action into testable sad-paths.
#
# This is distinct from ``DEFAULT_FIXTURES`` above (the prompt-placeholder
# dict the harness flattens into ``{fixture_*}`` kwargs). The catalog is
# structured data the discovery loop reads; the placeholder dict is for
# prompt interpolation. They will converge in Slice 4 / #1004's editor;
# for now they're separate concerns shipped side by side.
# ---------------------------------------------------------------------------

# Directory shipped with the package — ``qa-store/fixtures/``.
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

# The minimal set a catalog MUST declare for the loader to accept it.
# Everything else is additive — a tenant can grow the schema without the
# loader caring. Kept narrow on purpose: a new app should onboard with
# "< 20 lines of operator-provided config" (the epic's success cri
# terion), so we don't demand more than app + base_url to be valid.
REQUIRED_CATALOG_KEYS = ("app", "base_url")

# ``${VAR}`` interpolation — matches ${NAME} where NAME is a typical env
# var identifier. Anything not matching is left verbatim.
_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class FixtureCatalogError(ValueError):
    """Raised when a fixture catalog is missing or malformed."""


def _interpolate_env(value: Any, env: dict[str, str]) -> Any:
    """Recursively replace ``${VAR}`` placeholders in a parsed YAML value.

    Strings are scanned for ``${VAR}`` and each match swapped for
    ``env[VAR]`` when the var is set. An UNSET var is left as the literal
    ``${VAR}`` placeholder — a missing secret stays visible (so the
    operator can spot it) rather than collapsing to an empty string that
    silently logs the persona in as nobody.

    Lists + dicts recurse. Non-string scalars pass through untouched.
    """
    if isinstance(value, str):
        def _sub(m: re.Match[str]) -> str:
            name = m.group(1)
            return env.get(name, m.group(0))
        return _ENV_PLACEHOLDER.sub(_sub, value)
    if isinstance(value, list):
        return [_interpolate_env(v, env) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate_env(v, env) for k, v in value.items()}
    return value


def load_fixture_catalog(
    tenant: str = "example",
    *,
    fixtures_dir: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Load + validate one tenant's fixture catalog YAML.

    Reads ``<fixtures_dir>/<tenant>.yaml``, parses it, interpolates
    ``${VAR}`` placeholders against ``env`` (defaults to ``os.environ``),
    and validates the required top-level keys are present.

    ``fixtures_dir`` defaults to the package's shipped ``fixtures/`` dir;
    tests override it to point at a temp file. ``env`` is injectable so
    tests can pin interpolation without mutating the process environment.

    Raises :class:`FixtureCatalogError` if the file is missing, not a
    mapping, or missing a required key. Slice 3b / #1004 will add a
    DB-backed editor returning the same shape from a ``qa_fixtures``
    collection; this file-based loader is the Slice 3a substrate.
    """
    import yaml  # noqa: PLC0415 — lazy: only the discovery loop needs YAML.

    base = Path(fixtures_dir) if fixtures_dir is not None else FIXTURES_DIR
    path = base / f"{tenant}.yaml"
    if not path.is_file():
        raise FixtureCatalogError(
            f"no fixture catalog for tenant {tenant!r} at {path}"
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover — defensive
        raise FixtureCatalogError(
            f"fixture catalog {path} is not valid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise FixtureCatalogError(
            f"fixture catalog {path} must be a YAML mapping, got "
            f"{type(raw).__name__}"
        )

    missing = [k for k in REQUIRED_CATALOG_KEYS if not raw.get(k)]
    if missing:
        raise FixtureCatalogError(
            f"fixture catalog {path} missing required key(s): "
            f"{', '.join(missing)}"
        )

    resolved_env = env if env is not None else dict(os.environ)
    return _interpolate_env(raw, resolved_env)
