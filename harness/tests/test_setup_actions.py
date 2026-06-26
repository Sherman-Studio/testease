"""Tests for the optional scripted persona-setup actions (issue #837).

These tests exercise the ``setup_actions`` field on ``Persona`` and the
``run_setup`` dispatcher that the runner invokes before spawning the AI
explore loop. The flows themselves drive Playwright + Mailpit via the
exact clients the AI uses, so the tests stub both and assert the order
of operations.

#1009: pre-relaunch the harness shipped 12 SlyReply-specific personas,
five of which carried ``setup_actions="signup"`` / ``"signup_then_pro"``.
The relaunched catalog ships 25 generic archetypes that all default to
``setup_actions=None`` — they discover signup themselves. So this file
now builds local Persona fixtures inside each test that needs setup
wiring, instead of importing named constants.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, field
from unittest.mock import MagicMock

import pytest

from qa_agents import runner as runner_mod
from qa_agents import setup_actions as setup_actions_mod
from qa_agents.personas import (
    PERSONAS,
    Persona,
    get_persona,
)
from qa_agents.setup_actions import (
    VALID_SETUP_ACTIONS,
    _scripted_signup,
    _scripted_upgrade,
    run_setup,
)


# ---------------------------------------------------------------------------
# Local Persona fixtures — the relaunched catalog has no personas with
# setup_actions set, so tests that exercise the scripted prelude
# construct one here. Each fixture is a tiny in-memory archetype with
# just enough fields to satisfy the dataclass.
# ---------------------------------------------------------------------------
def _make_persona(
    *,
    persona_id: str = "fixture",
    display_name: str = "Fixture — persona",
    registered_email: str = "fixture@example.com",
    setup_actions: str | None = None,
) -> Persona:
    return Persona(
        id=persona_id,
        display_name=display_name,
        archetype="test archetype",
        registered_email=registered_email,
        explore_system_prompt="x",
        report_system_prompt="r",
        flows=["one"],
        setup_actions=setup_actions,
    )


# ---------------------------------------------------------------------------
# Stub Playwright Page — captures every call in order so the test can assert
# the harness drove the expected scripted sequence.
# ---------------------------------------------------------------------------
class _StubLocator:
    def __init__(self, page, selector: str) -> None:
        self._page = page
        self._selector = selector

    async def click(self, **kwargs) -> None:
        self._page.calls.append(("locator.click", self._selector, kwargs))

    async def fill(self, value: str, **kwargs) -> None:
        self._page.calls.append(("locator.fill", self._selector, value))

    async def check(self, **kwargs) -> None:
        self._page.calls.append(("locator.check", self._selector))

    async def is_visible(self, **kwargs) -> bool:
        self._page.calls.append(("locator.is_visible", self._selector))
        return self._page.visible_selectors.get(self._selector, False)


class _StubResponse:
    """Playwright APIResponse-shaped stub for `_scripted_upgrade` tests (#974)."""

    def __init__(
        self,
        *,
        status: int = 200,
        json_body: dict | None = None,
        text_body: str = "",
    ) -> None:
        self.status = status
        self._json = json_body if json_body is not None else {}
        self._text = text_body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    async def json(self) -> dict:
        return self._json

    async def text(self) -> str:
        return self._text


class _StubAPIRequestContext:
    """Playwright APIRequestContext-shaped stub.

    Records every GET / POST so tests can assert the sequence of API
    calls `_scripted_upgrade` made. Pre-registered responses match by
    (method, url-substring); the first matching entry wins. Tests
    override the default happy-path responses to simulate failures.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._responses: list[tuple[str, str, _StubResponse]] = []

    def register(
        self,
        method: str,
        url_substring: str,
        response: _StubResponse,
    ) -> None:
        # Most-recently-registered wins, so tests can override defaults
        # by registering a new response on top.
        self._responses.insert(0, (method.upper(), url_substring, response))

    def _lookup(self, method: str, url: str) -> _StubResponse:
        for m, sub, resp in self._responses:
            if m == method.upper() and sub in url:
                return resp
        return _StubResponse(
            status=404, text_body=f"_StubAPIRequestContext: no stub for {method} {url}",
        )

    async def get(self, url: str, **kwargs) -> _StubResponse:
        self.calls.append(("GET", url, kwargs))
        return self._lookup("GET", url)

    async def post(self, url: str, **kwargs) -> _StubResponse:
        self.calls.append(("POST", url, kwargs))
        return self._lookup("POST", url)


def _seed_happy_path_request_stub(
    request: _StubAPIRequestContext,
    *,
    tier: str = "pro",
) -> None:
    """Pre-register the QA billing provision call ``_scripted_upgrade``
    makes on its happy path (Stripe→Revolut migration, #2007), plus the
    /users/me tier-assertion read. Tests overlay failures on top for
    negative paths."""
    request.register(
        "POST", "/api/qa/billing/provision",
        _StubResponse(json_body={"subscription_tier": tier, "subscription_status": "active"}),
    )
    request.register(
        "GET", "/api/users/me",
        _StubResponse(json_body={"subscription_tier": tier}),
    )


class _StubPage:
    """Async Playwright-Page-shaped stub.

    Implements the surface area ``setup_actions._scripted_*`` actually
    touches: ``goto``, ``locator(selector).{click,fill,check,is_visible}``,
    ``wait_for_url``, ``wait_for_selector``, ``url`` reads, AND the
    ``request`` APIRequestContext used for the QA billing provision
    upgrade flow (#2007).
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.visible_selectors: dict[str, bool] = {}
        self.current_url: str = "about:blank"
        self.request = _StubAPIRequestContext()

    async def goto(self, url: str, **kwargs) -> None:
        self.calls.append(("goto", url))
        self.current_url = url

    def locator(self, selector: str) -> _StubLocator:
        self.calls.append(("locator", selector))
        return _StubLocator(self, selector)

    async def wait_for_url(self, url_pattern, **kwargs) -> None:
        self.calls.append(("wait_for_url", str(url_pattern)))

    async def wait_for_selector(self, selector: str, **kwargs) -> None:
        self.calls.append(("wait_for_selector", selector))

    async def wait_for_timeout(self, ms: int) -> None:
        self.calls.append(("wait_for_timeout", ms))

    async def evaluate(self, expression: str) -> object:
        self.calls.append(("evaluate", expression))
        return None

    async def screenshot(self, **kwargs) -> bytes:
        self.calls.append(("screenshot", kwargs))
        return b""


# ---------------------------------------------------------------------------
# Stub Mailpit client — emulates the wait/get loop used by ``_scripted_signup``
# for the verification mail.
# ---------------------------------------------------------------------------
@dataclass
class _StubMailpit:
    """Mailpit-API-shaped stub."""

    queued_message: dict | None = None
    queued_full: dict | None = None
    list_calls: int = 0
    get_calls: list[str] = field(default_factory=list)

    def list_messages(self, limit: int = 50) -> list[dict]:
        self.list_calls += 1
        if self.queued_message is None:
            return []
        return [self.queued_message]

    def get_message(self, message_id: str) -> dict:
        self.get_calls.append(message_id)
        return self.queued_full or {}


def _verification_email(to_addr: str, link: str) -> tuple[dict, dict]:
    """A Mailpit-shaped list-entry + single-message pair carrying ``link``."""
    list_entry = {
        "ID": "msg-verify-001",
        "To": [{"Address": to_addr}],
        "From": {"Address": "no-reply@slyreply.ai"},
        "Subject": "Verify your account",
        "Snippet": f"Click here: {link}",
        "Created": "2026-05-22T10:00:00Z",
    }
    full = {
        "ID": "msg-verify-001",
        "To": [{"Address": to_addr}],
        "From": {"Address": "no-reply@slyreply.ai"},
        "Subject": "Verify your account",
        "Text": (
            "Welcome!\n\n"
            f"Please verify your account by clicking this link:\n{link}\n\n"
            "Thanks."
        ),
        "Created": "2026-05-22T10:00:00Z",
    }
    return list_entry, full


# ---------------------------------------------------------------------------
# Persona dataclass — the ``setup_actions`` field and its validation.
# ---------------------------------------------------------------------------
def test_persona_default_setup_actions_is_none():
    """Backwards-compat: a Persona without setup_actions defaults to None."""
    p = _make_persona()
    assert p.setup_actions is None


def test_persona_setup_actions_accepts_known_values():
    # #1105 Slice 1.1 added the last three (credential-aware variants).
    for value in (
        None,
        "signup",
        "signup_then_pro",
        "signup_then_power",
        "signup_or_login",
        "signup_fresh",
        "clear_credentials_then_signup",
    ):
        p = _make_persona(setup_actions=value)
        assert p.setup_actions == value


def test_persona_setup_actions_value_is_validated():
    """Constructing a Persona with an unknown value raises ValueError."""
    with pytest.raises(ValueError) as exc:
        _make_persona(setup_actions="signup_then_business")
    msg = str(exc.value)
    assert "setup_actions" in msg
    assert "signup_then_business" in msg
    # The error lists the valid values so the next developer knows.
    assert "signup" in msg


def test_persona_remains_frozen_after_validation():
    """``frozen=True`` must still apply — we only added a validator."""
    p = _make_persona(setup_actions="signup")
    with pytest.raises((FrozenInstanceError, AttributeError)):
        p.setup_actions = "signup_then_pro"  # type: ignore[misc]


def test_relaunched_catalog_has_no_setup_actions_wired():
    """#1009 — every shipped persona defaults to setup_actions=None. The
    generic archetypes are expected to discover signup themselves; the
    scripted prelude is a per-tenant override, not a catalog default.

    Exceptions:
      - Avery (``comprehensive-explorer``): role explicitly assumes a
        logged-in starting state.
      - Jordan (``desktop-evaluator``, #1253): re-verifier of prior
        findings, which requires the same account across runs.

    The duplicate invariant in test_personas.py
    (``test_setup_actions_none_default``) shares this allowlist —
    keep them in sync.
    """
    _scripted_setup_personas = {"comprehensive-explorer", "desktop-evaluator"}
    for p in PERSONAS.values():
        if p.id in _scripted_setup_personas:
            assert p.setup_actions == "signup_or_login", (
                f"{p.id!r}: scripted-setup persona must use "
                f"signup_or_login (got {p.setup_actions!r})"
            )
        else:
            assert p.setup_actions is None, (
                f"{p.id!r}: shipped catalog should keep setup_actions=None"
            )


def test_valid_setup_actions_constant_matches_dataclass_validator():
    """VALID_SETUP_ACTIONS is what the Persona dataclass accepts (minus None).
    #1105 Slice 1.1 added the last three (credential-aware variants)."""
    assert set(VALID_SETUP_ACTIONS) == {
        "signup",
        "signup_then_pro",
        "signup_then_power",
        "signup_or_login",
        "signup_fresh",
        "clear_credentials_then_signup",
    }


# ---------------------------------------------------------------------------
# run_setup — dispatch and validation.
# ---------------------------------------------------------------------------
async def test_run_setup_none_is_a_noop():
    """A persona with setup_actions=None must skip the scripted flow."""
    page = _StubPage()
    mailpit = _StubMailpit()
    p = _make_persona()

    # Passing None should return cleanly without touching either client.
    await run_setup(
        None,
        page=page,
        mailpit=mailpit,
        persona=p,
        web_base_url="http://web",
    )

    assert page.calls == []
    assert mailpit.list_calls == 0


async def test_run_setup_unknown_value_raises_value_error():
    page = _StubPage()
    mailpit = _StubMailpit()
    p = _make_persona(setup_actions="signup")
    with pytest.raises(ValueError) as exc:
        await run_setup(
            "signup_then_business",
            page=page,
            mailpit=mailpit,
            persona=p,
            web_base_url="http://web",
        )
    assert "signup_then_business" in str(exc.value)
    # Nothing was driven before the validation failed.
    assert page.calls == []
    assert mailpit.list_calls == 0


# ---------------------------------------------------------------------------
# _scripted_signup — drives the register form and clicks the verify link.
# ---------------------------------------------------------------------------
async def test_scripted_signup_drives_form_and_verifies(monkeypatch):
    """signup: navigate → fill name/email/password → consent → submit → verify."""
    p = _make_persona(
        persona_id="signup-fixture",
        registered_email="signup_fixture@example.com",
        setup_actions="signup",
    )
    page = _StubPage()
    # Pre-load Mailpit with the verification mail so the polling exits on first
    # tick — keeps the test fast.
    list_entry, full = _verification_email(
        p.registered_email, "http://web/verify?token=ABC123"
    )
    mailpit = _StubMailpit(queued_message=list_entry, queued_full=full)
    # Don't actually sleep between Mailpit polls.
    monkeypatch.setattr(setup_actions_mod.asyncio, "sleep", _no_sleep)

    await _scripted_signup(
        page,
        mailpit,
        p,
        web_base_url="http://web",
    )

    # The browser visited /register, /verify?token=…, and /profile/overview.
    nav_urls = [c[1] for c in page.calls if c[0] == "goto"]
    assert any(u.endswith("/register") for u in nav_urls), nav_urls
    assert any("/verify" in u and "ABC123" in u for u in nav_urls), nav_urls
    assert any(u.endswith("/profile/overview") for u in nav_urls), nav_urls

    # The form was filled with the persona's registered email and consented.
    fills = [c for c in page.calls if c[0] == "locator.fill"]
    filled_values = [c[2] for c in fills]
    assert p.registered_email in filled_values
    # consent checkbox checked + submit clicked.
    assert any(c[0] == "locator.check" for c in page.calls), page.calls
    assert any(
        c[0] == "locator.click" and "register-submit" in c[1] for c in page.calls
    ), page.calls

    # Mailpit was polled for the verification email.
    assert mailpit.list_calls >= 1
    assert mailpit.get_calls, mailpit.get_calls


async def test_scripted_signup_waits_for_verification_email(monkeypatch):
    """If the verification mail isn't there yet, signup polls until it lands."""
    p = _make_persona(
        persona_id="poll-fixture",
        registered_email="poll_fixture@example.com",
        setup_actions="signup",
    )
    page = _StubPage()
    list_entry, full = _verification_email(
        p.registered_email, "http://web/verify?token=Z9"
    )

    class _DelayedMailpit(_StubMailpit):
        def __init__(self) -> None:
            super().__init__()

        def list_messages(self, limit: int = 50) -> list[dict]:
            self.list_calls += 1
            if self.list_calls < 3:
                return []  # not yet
            return [list_entry]

        def get_message(self, message_id: str) -> dict:
            self.get_calls.append(message_id)
            return full

    mailpit = _DelayedMailpit()
    monkeypatch.setattr(setup_actions_mod.asyncio, "sleep", _no_sleep)

    await _scripted_signup(
        page, mailpit, p, web_base_url="http://web"
    )

    assert mailpit.list_calls >= 3, "signup should poll until mail arrives"
    nav_urls = [c[1] for c in page.calls if c[0] == "goto"]
    assert any("Z9" in u for u in nav_urls), nav_urls


async def test_scripted_signup_dismisses_onboarding_tour(monkeypatch):
    """After verification, signup dismisses any onboarding tour modal if present."""
    p = _make_persona(
        persona_id="tour-fixture",
        registered_email="tour_fixture@example.com",
        setup_actions="signup",
    )
    page = _StubPage()
    # Pretend the onboarding-dismiss control IS visible after signup.
    page.visible_selectors = {
        "[data-testid=onboarding-dismiss]": True,
    }
    list_entry, full = _verification_email(
        p.registered_email, "http://web/verify?token=ONB"
    )
    mailpit = _StubMailpit(queued_message=list_entry, queued_full=full)
    monkeypatch.setattr(setup_actions_mod.asyncio, "sleep", _no_sleep)

    await _scripted_signup(page, mailpit, p, web_base_url="http://web")

    # The dismiss button was clicked once.
    dismiss_clicks = [
        c
        for c in page.calls
        if c[0] == "locator.click" and "onboarding-dismiss" in c[1]
    ]
    assert len(dismiss_clicks) == 1, page.calls


async def test_scripted_signup_skips_dismiss_when_tour_absent(monkeypatch):
    """If no onboarding tour is showing, signup must not error."""
    p = _make_persona(
        persona_id="notour-fixture",
        registered_email="notour_fixture@example.com",
        setup_actions="signup",
    )
    page = _StubPage()
    page.visible_selectors = {}
    list_entry, full = _verification_email(
        p.registered_email, "http://web/verify?token=NOTOUR"
    )
    mailpit = _StubMailpit(queued_message=list_entry, queued_full=full)
    monkeypatch.setattr(setup_actions_mod.asyncio, "sleep", _no_sleep)

    # Must not raise even though no tour is present.
    await _scripted_signup(page, mailpit, p, web_base_url="http://web")

    dismiss_clicks = [
        c
        for c in page.calls
        if c[0] == "locator.click" and "onboarding-dismiss" in c[1]
    ]
    assert dismiss_clicks == []


# ---------------------------------------------------------------------------
# _scripted_upgrade — QA billing provision hook (Stripe→Revolut migration).
#
# Revolut has no test-clock and the checkout widget (#1977) isn't built,
# so there is no public payment API the harness can drive. Instead the
# upgrade is a single POST to /api/qa/billing/provision (#2007), carrying
# the persona's session + the X-QA-Token header read from $QA_TOKEN. A
# non-200 or a wrong returned tier is a loud RuntimeError so a broken
# sandbox is never mistaken for a successful upgrade.
# ---------------------------------------------------------------------------
async def test_scripted_upgrade_pro_posts_to_provision_hook(monkeypatch):
    monkeypatch.setenv("QA_TOKEN", "qa-secret-token")
    page = _StubPage()
    _seed_happy_path_request_stub(page.request, tier="pro")

    await _scripted_upgrade(page, plan="pro", web_base_url="http://web")

    methods_and_urls = [(c[0], c[1]) for c in page.request.calls]
    # The one upgrade call: the QA billing provision hook.
    assert ("POST", "http://web/api/qa/billing/provision") in methods_and_urls
    # No Stripe / public-payment-API call is made any more.
    assert not any("stripe" in u.lower() for _, u in methods_and_urls)
    # The provision POST carries the right tier AND the X-QA-Token header.
    provision_kwargs = next(
        c[2] for c in page.request.calls
        if c[0] == "POST" and "/api/qa/billing/provision" in c[1]
    )
    assert provision_kwargs["data"] == {"tier": "pro"}
    assert provision_kwargs["headers"]["X-QA-Token"] == "qa-secret-token"
    # End-state navigation so the AI loop lands on a familiar surface.
    nav_urls = [c[1] for c in page.calls if c[0] == "goto"]
    assert any(u.endswith("/profile/billing") for u in nav_urls), nav_urls


async def test_scripted_upgrade_power_passes_power_tier(monkeypatch):
    monkeypatch.setenv("QA_TOKEN", "qa-secret-token")
    page = _StubPage()
    _seed_happy_path_request_stub(page.request, tier="power")

    await _scripted_upgrade(page, plan="power", web_base_url="http://web")

    provision_kwargs = next(
        c[2] for c in page.request.calls
        if c[0] == "POST" and "/api/qa/billing/provision" in c[1]
    )
    assert provision_kwargs["data"]["tier"] == "power"


async def test_scripted_upgrade_omits_header_when_no_qa_token(monkeypatch):
    """No QA_TOKEN env → no X-QA-Token header injected (the backend will
    then 403, but that's the backend's job to enforce, not the harness's)."""
    monkeypatch.delenv("QA_TOKEN", raising=False)
    page = _StubPage()
    _seed_happy_path_request_stub(page.request, tier="pro")

    await _scripted_upgrade(page, plan="pro", web_base_url="http://web")

    provision_kwargs = next(
        c[2] for c in page.request.calls
        if c[0] == "POST" and "/api/qa/billing/provision" in c[1]
    )
    assert "X-QA-Token" not in provision_kwargs["headers"]


async def test_scripted_upgrade_rejects_unknown_plan():
    page = _StubPage()
    with pytest.raises(ValueError):
        await _scripted_upgrade(page, plan="enterprise", web_base_url="http://web")


# Failure modes are loud RuntimeErrors so a broken sandbox is never
# mistaken for a successful upgrade.
async def test_scripted_upgrade_raises_when_provision_fails(monkeypatch):
    monkeypatch.setenv("QA_TOKEN", "qa-secret-token")
    page = _StubPage()
    _seed_happy_path_request_stub(page.request, tier="pro")
    page.request.register(
        "POST", "/api/qa/billing/provision",
        _StubResponse(status=403, text_body="missing X-QA-Token"),
    )
    with pytest.raises(RuntimeError, match="/api/qa/billing/provision failed"):
        await _scripted_upgrade(page, plan="pro", web_base_url="http://web")


async def test_scripted_upgrade_raises_when_provision_returns_wrong_tier(monkeypatch):
    monkeypatch.setenv("QA_TOKEN", "qa-secret-token")
    page = _StubPage()
    _seed_happy_path_request_stub(page.request, tier="pro")
    # Backend accepted the call but flipped the wrong (or no) tier.
    page.request.register(
        "POST", "/api/qa/billing/provision",
        _StubResponse(json_body={"subscription_tier": "free"}),
    )
    with pytest.raises(RuntimeError, match="subscription_tier=.*free.*expected.*pro"):
        await _scripted_upgrade(page, plan="pro", web_base_url="http://web")


# ---------------------------------------------------------------------------
# _assert_tier_after_setup — hard tier assertion, the second half of #974.
# Without this the harness would still silently miss a future regression
# in the upgrade flow even though _scripted_upgrade now raises on common
# failures. Belt-and-braces.
# ---------------------------------------------------------------------------
async def test_assert_tier_passes_when_tier_matches():
    from qa_agents.setup_actions import _assert_tier_after_setup
    page = _StubPage()
    page.request.register(
        "GET", "/api/users/me",
        _StubResponse(json_body={"subscription_tier": "pro"}),
    )
    # Returns None on success; should not raise.
    await _assert_tier_after_setup(
        page, web_base_url="http://web", expected="pro",
    )


async def test_assert_tier_raises_when_tier_mismatches():
    from qa_agents.setup_actions import _assert_tier_after_setup
    page = _StubPage()
    page.request.register(
        "GET", "/api/users/me",
        _StubResponse(json_body={"subscription_tier": "free"}),
    )
    with pytest.raises(RuntimeError, match="expected.*pro.*got.*free"):
        await _assert_tier_after_setup(
            page, web_base_url="http://web", expected="pro",
        )


async def test_assert_tier_raises_when_users_me_fails():
    from qa_agents.setup_actions import _assert_tier_after_setup
    page = _StubPage()
    page.request.register(
        "GET", "/api/users/me",
        _StubResponse(status=401, text_body="unauthorized"),
    )
    with pytest.raises(RuntimeError, match="GET /api/users/me"):
        await _assert_tier_after_setup(
            page, web_base_url="http://web", expected="pro",
        )


# ---------------------------------------------------------------------------
# run_setup dispatch — signup_then_pro / signup_then_power chain both steps.
# ---------------------------------------------------------------------------
async def test_signup_then_pro_includes_upgrade_and_tier_assertion(monkeypatch):
    """signup_then_pro runs signup, the pro-upgrade provision hook, and the
    post-setup tier assertion. The tier-assertion call is the regression
    guard for #974 — without it, future setup_actions misses revert to
    silent."""
    monkeypatch.setenv("QA_TOKEN", "qa-secret-token")
    p = _make_persona(
        persona_id="pro-buyer-fixture",
        registered_email="pro_buyer@example.com",
        setup_actions="signup_then_pro",
    )
    page = _StubPage()
    _seed_happy_path_request_stub(page.request, tier="pro")
    list_entry, full = _verification_email(
        p.registered_email, "http://web/verify?token=PRO"
    )
    mailpit = _StubMailpit(queued_message=list_entry, queued_full=full)
    monkeypatch.setattr(setup_actions_mod.asyncio, "sleep", _no_sleep)

    await run_setup(
        "signup_then_pro",
        page=page,
        mailpit=mailpit,
        persona=p,
        web_base_url="http://web",
    )

    # Signup happened (the navigation pieces).
    nav_urls = [c[1] for c in page.calls if c[0] == "goto"]
    assert any(u.endswith("/register") for u in nav_urls), nav_urls
    assert any("verify" in u for u in nav_urls), nav_urls
    # Upgrade ran — the QA billing provision hook.
    api_methods_urls = [(c[0], c[1]) for c in page.request.calls]
    assert ("POST", "http://web/api/qa/billing/provision") in api_methods_urls
    # Tier assertion ran AFTER the upgrade (#974).
    assert ("GET", "http://web/api/users/me") in api_methods_urls


async def test_signup_then_power_passes_power_tier_and_asserts(monkeypatch):
    monkeypatch.setenv("QA_TOKEN", "qa-secret-token")
    p = _make_persona(
        persona_id="power-buyer-fixture",
        registered_email="power_buyer@example.com",
        setup_actions="signup_then_power",
    )
    page = _StubPage()
    _seed_happy_path_request_stub(page.request, tier="power")
    list_entry, full = _verification_email(
        p.registered_email, "http://web/verify?token=POW"
    )
    mailpit = _StubMailpit(queued_message=list_entry, queued_full=full)
    monkeypatch.setattr(setup_actions_mod.asyncio, "sleep", _no_sleep)

    await run_setup(
        "signup_then_power",
        page=page,
        mailpit=mailpit,
        persona=p,
        web_base_url="http://web",
    )

    provision_kwargs = next(
        c[2] for c in page.request.calls
        if c[0] == "POST" and "/api/qa/billing/provision" in c[1]
    )
    assert provision_kwargs["data"]["tier"] == "power"
    # Tier assertion ran.
    assert any(
        c[0] == "GET" and "/api/users/me" in c[1] for c in page.request.calls
    ), page.request.calls


async def test_signup_then_pro_raises_when_post_setup_tier_assertion_fails(monkeypatch):
    """Even if every upgrade API returned OK, if /users/me reports the
    wrong tier the harness must raise — that's the whole point of #974's
    second-half guard."""
    monkeypatch.setenv("QA_TOKEN", "qa-secret-token")
    p = _make_persona(
        persona_id="silent-fail-fixture",
        registered_email="silent_fail@example.com",
        setup_actions="signup_then_pro",
    )
    page = _StubPage()
    _seed_happy_path_request_stub(page.request, tier="pro")
    # Override just the /users/me response to simulate a backend bug
    # where the upgrade silently failed to flip the tier.
    page.request.register(
        "GET", "/api/users/me",
        _StubResponse(json_body={"subscription_tier": "free"}),
    )
    list_entry, full = _verification_email(
        p.registered_email, "http://web/verify?token=PRO"
    )
    mailpit = _StubMailpit(queued_message=list_entry, queued_full=full)
    monkeypatch.setattr(setup_actions_mod.asyncio, "sleep", _no_sleep)

    with pytest.raises(RuntimeError, match="expected.*pro.*got.*free"):
        await run_setup(
            "signup_then_pro",
            page=page,
            mailpit=mailpit,
            persona=p,
            web_base_url="http://web",
        )


# ---------------------------------------------------------------------------
# Order-of-operations: signup runs in the canonical sequence and only then
# does the upgrade step start.
# ---------------------------------------------------------------------------
async def test_setup_actions_signup_flow_runs_before_ai_in_canonical_order(monkeypatch):
    """The harness drives /register → form → submit → wait → verify → tour
    in that order, before any AI work."""
    p = _make_persona(
        persona_id="order-fixture",
        registered_email="order_fixture@example.com",
        setup_actions="signup",
    )
    page = _StubPage()
    list_entry, full = _verification_email(
        p.registered_email, "http://web/verify?token=ORDER"
    )
    mailpit = _StubMailpit(queued_message=list_entry, queued_full=full)
    monkeypatch.setattr(setup_actions_mod.asyncio, "sleep", _no_sleep)

    await run_setup(
        "signup",
        page=page,
        mailpit=mailpit,
        persona=p,
        web_base_url="http://web",
    )

    # Pull out the high-level milestones in observation order.
    milestones: list[str] = []
    for call in page.calls:
        if call[0] == "goto" and call[1].endswith("/register"):
            milestones.append("nav-register")
        elif call[0] == "locator.fill" and call[2] == p.registered_email:
            milestones.append("fill-email")
        elif call[0] == "locator.click" and "register-submit" in call[1]:
            milestones.append("submit")
        elif call[0] == "goto" and "verify" in call[1]:
            milestones.append("nav-verify")
        elif call[0] == "goto" and call[1].endswith("/profile/overview"):
            milestones.append("nav-overview")

    # Mailpit polling happens after submit, before the verify nav.
    assert milestones.index("nav-register") < milestones.index("fill-email")
    assert milestones.index("fill-email") < milestones.index("submit")
    assert milestones.index("submit") < milestones.index("nav-verify")
    assert milestones.index("nav-verify") < milestones.index("nav-overview")


# ---------------------------------------------------------------------------
# Runner integration: run_explore_phase must invoke run_setup (when a
# persona declares one) BEFORE spawning the AI ``query()`` loop.
# ---------------------------------------------------------------------------
def _patch_runner_servers(monkeypatch):
    """Stub the MCP server factories used inside run_explore_phase."""
    monkeypatch.setattr(
        runner_mod,
        "build_email_server",
        lambda **kwargs: (object(), ["send_email", "wait_for_email", "get_email"]),
    )
    monkeypatch.setattr(
        runner_mod,
        "build_findings_server",
        # #1115 follow-up — accept the optional live_writer kwarg the
        # runner now passes through.
        lambda findings, *, live_writer=None: (object(), ["note_finding"]),
    )


def _runner_config(persona_id: str = "happy-path-signup"):
    from qa_agents.config import Config

    return Config(
        persona=persona_id,
        web_base_url="http://frontend",
        smtp_host="smtp-inbound",
        smtp_port=1025,
        mailpit_url="http://mailpit:8025",
        explore_model="claude-sonnet-4-6",
        report_model="claude-opus-4-7",
        max_turns=10,
        run_timeout_s=300,
        out_dir="./qa-runs",
        mongodb_url="mongodb://mongodb/slyreply",
        admin_email="admin@x",
        admin_password="pw",
        sink="file",
        run_id="qa-setup-test",
        qa_store_url="mongodb://localhost:27017",
        qa_store_db="slyreply_qa_test",
        discord_webhook_url="",
        concurrency=1,
    )


async def test_runner_invokes_run_setup_before_ai_query(monkeypatch):
    """When the persona has setup_actions, run_explore_phase invokes
    run_setup() BEFORE the first ``query()`` call.

    #1009: the shipped catalog has no personas with setup_actions wired
    (all generic archetypes self-discover signup), so we build a fixture
    persona here with setup_actions='signup' to exercise the dispatch.
    """
    from qa_agents.accounting import RunAccounting
    from qa_agents.tools.findings import Findings

    _patch_runner_servers(monkeypatch)

    fixture = _make_persona(
        persona_id="runner-fixture",
        registered_email="runner_fixture@example.com",
        setup_actions="signup",
    )

    order: list[str] = []

    async def _fake_run_setup(action, *, persona, config):
        order.append(f"setup({action})")
        assert action == "signup"
        assert persona.id == "runner-fixture"

    async def _fake_query(prompt, options):
        order.append("query")
        if False:
            yield None  # pragma: no cover

    monkeypatch.setattr(runner_mod, "_run_setup_for_persona", _fake_run_setup)
    monkeypatch.setattr(runner_mod, "query", _fake_query)

    await runner_mod.run_explore_phase(
        fixture,
        _runner_config(persona_id="runner-fixture"),
        Findings(),
        RunAccounting(),
    )

    assert order, "expected at least one event"
    # Setup must precede the first AI query.
    assert order[0].startswith("setup("), order


async def test_runner_skips_run_setup_when_setup_actions_is_none(monkeypatch):
    """No setup_actions → run_setup is NOT called; AI gets control immediately.

    Any shipped persona qualifies because the relaunched catalog defaults
    every archetype to setup_actions=None.
    """
    from qa_agents.accounting import RunAccounting
    from qa_agents.tools.findings import Findings

    _patch_runner_servers(monkeypatch)

    setup_calls: list[str] = []

    async def _fake_run_setup(action, *, persona, config):
        setup_calls.append(f"setup({action})")

    async def _fake_query(prompt, options):
        if False:
            yield None  # pragma: no cover

    monkeypatch.setattr(runner_mod, "_run_setup_for_persona", _fake_run_setup)
    monkeypatch.setattr(runner_mod, "query", _fake_query)

    # Any shipped persona — all have setup_actions=None in the relaunch.
    await runner_mod.run_explore_phase(
        get_persona("happy-path-signup"),
        _runner_config(),
        Findings(),
        RunAccounting(),
    )

    assert setup_calls == [], setup_calls


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
async def _no_sleep(_seconds: float) -> None:
    """Replace asyncio.sleep so polling loops complete instantly."""
    return None


# ---------------------------------------------------------------------------
# #1105 Slice 1.1 — credential-aware setup variants.
# ---------------------------------------------------------------------------
async def test_signup_or_login_without_store_falls_back_to_signup(monkeypatch, caplog):
    """When no store is passed, ``signup_or_login`` can't read credentials
    so it has no choice but to run a plain signup. The fallback is
    explicit (logged), not silent."""
    import logging as _logging

    from qa_agents import setup_actions as sa_mod

    page = _StubPage()
    mailpit = _StubMailpit()
    p = _make_persona(setup_actions="signup_or_login")
    signup_calls = []

    async def _stub_signup(page, mailpit, persona, *, web_base_url):
        signup_calls.append(persona.id)
    monkeypatch.setattr(sa_mod, "_scripted_signup", _stub_signup)

    caplog.set_level(_logging.WARNING, logger="qa_agents.setup_actions")
    await run_setup(
        "signup_or_login",
        page=page,
        mailpit=mailpit,
        persona=p,
        web_base_url="http://web",
        store=None,
    )
    assert signup_calls == [p.id]
    assert any(
        "no qa-store handle was passed" in r.getMessage()
        for r in caplog.records
    )


async def test_signup_fresh_skips_credential_save(monkeypatch):
    """``signup_fresh`` is the operator's opt-out: signup runs, but
    credentials are NOT persisted. Distinguishes "I'm testing the signup
    flow" from "I'm onboarding this persona for a long lifecycle"."""
    from qa_agents import credentials as _creds_mod
    from qa_agents import setup_actions as sa_mod

    page = _StubPage()
    mailpit = _StubMailpit()
    p = _make_persona(setup_actions="signup_fresh")
    saved = []

    async def _stub_signup(page, mailpit, persona, *, web_base_url):
        pass
    def _stub_save(*args, **kwargs):
        saved.append(kwargs.get("email"))
    monkeypatch.setattr(sa_mod, "_scripted_signup", _stub_signup)
    monkeypatch.setattr(_creds_mod, "save_after_signup", _stub_save)

    # Pass a sentinel store; the action must NOT call save_after_signup
    # even when one is available.
    await run_setup(
        "signup_fresh",
        page=page,
        mailpit=mailpit,
        persona=p,
        web_base_url="http://web",
        store=object(),  # any non-None sentinel
    )
    assert saved == [], saved


async def test_signup_or_login_with_credentials_drives_login(monkeypatch):
    """When credentials exist, ``signup_or_login`` must call the
    scripted login path with the saved email + password and NOT call
    the signup script."""
    from qa_agents import credentials as _creds_mod
    from qa_agents import setup_actions as sa_mod

    page = _StubPage()
    mailpit = _StubMailpit()
    p = _make_persona(setup_actions="signup_or_login")

    login_args = {}
    async def _stub_login(page, persona, *, email, password, web_base_url):
        login_args["email"] = email
        login_args["password"] = password
    async def _stub_signup(page, mailpit, persona, *, web_base_url):
        # Should NOT be called when login succeeds.
        login_args["signup_called"] = True

    monkeypatch.setattr(sa_mod, "_scripted_login", _stub_login)
    monkeypatch.setattr(sa_mod, "_scripted_signup", _stub_signup)

    # Mock the credentials module to return a bundle.
    monkeypatch.setattr(
        _creds_mod, "load_for_persona",
        lambda store, pid, force_refresh=False: _creds_mod.CredentialBundle(
            email="maya+r1@x.com",
            password="hunter22",
            verified=True,
            session_jwt=None,
            last_rotation_n=0,
        ),
    )

    await run_setup(
        "signup_or_login",
        page=page,
        mailpit=mailpit,
        persona=p,
        web_base_url="http://web",
        store=object(),
    )
    assert login_args.get("email") == "maya+r1@x.com"
    assert login_args.get("password") == "hunter22"
    assert "signup_called" not in login_args


async def test_signup_or_login_login_failure_falls_back_to_signup(monkeypatch, caplog):
    """A login that raises should NOT crash setup — it falls through
    to the scripted signup. The persona still gets a session, just via
    the slower signup path. Logged for the operator to investigate."""
    import logging as _logging

    from qa_agents import credentials as _creds_mod
    from qa_agents import setup_actions as sa_mod

    page = _StubPage()
    mailpit = _StubMailpit()
    p = _make_persona(setup_actions="signup_or_login")
    signup_calls = []

    async def _stub_login(page, persona, *, email, password, web_base_url):
        raise RuntimeError("login form rejected credentials")
    async def _stub_signup(page, mailpit, persona, *, web_base_url):
        signup_calls.append(persona.id)
    monkeypatch.setattr(sa_mod, "_scripted_login", _stub_login)
    monkeypatch.setattr(sa_mod, "_scripted_signup", _stub_signup)
    monkeypatch.setattr(
        _creds_mod, "load_for_persona",
        lambda store, pid, force_refresh=False: _creds_mod.CredentialBundle(
            email="m@x.com", password="pw", verified=True,
            session_jwt=None, last_rotation_n=0,
        ),
    )
    monkeypatch.setattr(_creds_mod, "save_after_signup", lambda *a, **kw: None)

    caplog.set_level(_logging.WARNING, logger="qa_agents.setup_actions")
    await run_setup(
        "signup_or_login",
        page=page,
        mailpit=mailpit,
        persona=p,
        web_base_url="http://web",
        store=object(),
    )
    assert signup_calls == [p.id], "signup must run after login failure"
    assert any(
        "login with saved credentials failed" in r.getMessage()
        for r in caplog.records
    )


async def test_clear_credentials_then_signup_clears_first(monkeypatch):
    """``clear_credentials_then_signup`` runs clear, THEN signup. The
    operator uses this to reset a persona that's gotten into a bad
    state without manually hitting the API endpoint first."""
    from qa_agents import credentials as _creds_mod
    from qa_agents import setup_actions as sa_mod

    order = []

    def _stub_clear(store, pid):
        order.append(("clear", pid))
    async def _stub_signup(page, mailpit, persona, *, web_base_url):
        order.append(("signup", persona.id))
    def _stub_save(*args, **kwargs):
        order.append(("save", kwargs.get("email")))

    monkeypatch.setattr(_creds_mod, "clear_for_persona", _stub_clear)
    monkeypatch.setattr(sa_mod, "_scripted_signup", _stub_signup)
    monkeypatch.setattr(_creds_mod, "save_after_signup", _stub_save)
    # No prior credentials → signup path; save fires at the end.
    monkeypatch.setattr(
        _creds_mod, "load_for_persona",
        lambda store, pid, force_refresh=False: None,
    )

    page = _StubPage()
    mailpit = _StubMailpit()
    p = _make_persona(setup_actions="clear_credentials_then_signup")

    await run_setup(
        "clear_credentials_then_signup",
        page=page,
        mailpit=mailpit,
        persona=p,
        web_base_url="http://web",
        store=object(),
    )
    # Order matters: clear before signup, save after.
    assert order == [
        ("clear", p.id),
        ("signup", p.id),
        ("save", p.registered_email),
    ]


# ---------------------------------------------------------------------------
# #1257 slice 2 — _request_and_save_resume_token
# ---------------------------------------------------------------------------
class TestRequestAndSaveResumeToken:
    """The post-login / post-signup hook that asks the backend for a
    resume token, persists it, and lets the persona's NEXT run start
    already logged in.

    All failure paths are silent: secret unset, endpoint 4xx, network
    blip — the harness logs and continues so the persona's explore
    phase still runs.
    """

    def _persona(self):
        return _make_persona(setup_actions="signup_or_login")

    @pytest.mark.asyncio
    async def test_skips_without_secret_env(self, monkeypatch):
        """Most importantly: a harness deployment that hasn't been
        configured with RESUME_TOKEN_ISSUE_SECRET MUST NOT call the
        endpoint. The persona's setup phase continues; only the
        next-run-already-logged-in optimisation is skipped."""
        monkeypatch.delenv("RESUME_TOKEN_ISSUE_SECRET", raising=False)

        called = {"post": False}

        class _StubClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def post(self, *_args, **_kwargs):
                called["post"] = True

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient",
                            lambda **_: _StubClient())
        await setup_actions_mod._request_and_save_resume_token(
            store=MagicMock(),
            persona=self._persona(),
            web_base_url="http://web",
        )
        assert called["post"] is False

    @pytest.mark.asyncio
    async def test_happy_path_saves_token_to_store(self, monkeypatch):
        """200 from the backend → save_resume_token gets the token +
        a tz-aware expires_at. The expiry is computed from the
        response's expires_in_seconds + now(UTC)."""
        from datetime import UTC, datetime

        monkeypatch.setenv("RESUME_TOKEN_ISSUE_SECRET", "test-secret")

        captured_post = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"token": "tok-abc", "expires_in_seconds": 3600}

        class _StubClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def post(self, url, *, headers, json):
                captured_post["url"] = url
                captured_post["headers"] = headers
                captured_post["json"] = json
                return _Resp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient",
                            lambda **_: _StubClient())

        saved = {}

        def _fake_save(_store, persona_id, *, token, expires_at):
            saved["persona_id"] = persona_id
            saved["token"] = token
            saved["expires_at"] = expires_at

        # Patch the credentials module the setup_actions function
        # imports lazily.
        import qa_agents.credentials as _creds
        monkeypatch.setattr(_creds, "save_resume_token", _fake_save)

        await setup_actions_mod._request_and_save_resume_token(
            store=MagicMock(),
            persona=self._persona(),
            web_base_url="http://web",
        )

        assert captured_post["url"] == "http://web/api/auth/internal/issue-resume-token"
        assert captured_post["headers"]["X-Resume-Token-Secret"] == "test-secret"
        assert captured_post["json"] == {"email": self._persona().registered_email}
        assert saved["token"] == "tok-abc"
        # Expiry must be tz-aware UTC and roughly now+1h.
        assert saved["expires_at"].tzinfo is UTC
        delta = (saved["expires_at"] - datetime.now(UTC)).total_seconds()
        assert 3500 < delta < 3700

    @pytest.mark.asyncio
    async def test_non_200_does_not_save(self, monkeypatch):
        monkeypatch.setenv("RESUME_TOKEN_ISSUE_SECRET", "test-secret")

        class _Resp:
            status_code = 404

            @staticmethod
            def json():
                return {"detail": "not found"}

        class _StubClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def post(self, *_a, **_k):
                return _Resp()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient",
                            lambda **_: _StubClient())

        saved = {}
        import qa_agents.credentials as _creds
        monkeypatch.setattr(_creds, "save_resume_token",
                            lambda *_a, **_k: saved.update({"called": True}))

        await setup_actions_mod._request_and_save_resume_token(
            store=MagicMock(),
            persona=self._persona(),
            web_base_url="http://web",
        )
        assert "called" not in saved

    @pytest.mark.asyncio
    async def test_network_exception_does_not_crash(self, monkeypatch):
        """A timeout / unreachable backend must not propagate. The
        helper logs and returns; the persona's setup phase keeps going."""
        monkeypatch.setenv("RESUME_TOKEN_ISSUE_SECRET", "test-secret")

        class _StubClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def post(self, *_a, **_k):
                raise RuntimeError("connection reset")

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient",
                            lambda **_: _StubClient())

        # No exception expected.
        await setup_actions_mod._request_and_save_resume_token(
            store=MagicMock(),
            persona=self._persona(),
            web_base_url="http://web",
        )
