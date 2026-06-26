"""Scripted persona-setup actions — signup + tier-upgrade prelude (#837).

Most personas need to be SIGNED UP (and sometimes UPGRADED to Pro / Power)
before the AI's exploration phase is even useful. Doing that via the Sonnet
explore loop costs ~22 turns / ~$0.66 per persona (see the daniel transcript
in qa-20260523T100324Z) — pure deterministic UI driving with no AI judgment.

This module exposes ``run_setup(action, *, page, mailpit, persona, web_base_url)``
which the runner calls BEFORE spawning the AI ``query()`` loop. ``page`` is
the same Playwright (async) Page object the AI would otherwise drive via the
Playwright MCP; ``mailpit`` is the existing :class:`qa_agents.tools.email.MailpitClient`
already used by ``wait_for_email``. We deliberately avoid spinning up a
parallel browser stack — the contract is "same clients the AI uses".

The scripted flows match the live frontend's ``data-testid`` selectors, which
are the test contract for these surfaces (see ``frontend/src/views/Register.vue``
and ``frontend/src/views/profile/ProfileBilling.vue``). When a selector moves
the test will break here — that's the point.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .personas import Persona
    from .tools.email import MailpitClient

# What ``run_setup`` accepts (besides ``None``, which is a no-op).
# #1105 Slice 1.1 added the last three (credential-aware variants).
VALID_SETUP_ACTIONS = (
    "signup",
    "signup_then_pro",
    "signup_then_power",
    "signup_or_login",
    "signup_fresh",
    "clear_credentials_then_signup",
)

# Per-persona signup test password. Plain-text on purpose — every signup
# persona uses the same value; the password is the test fixture's concern,
# not the persona's identity (which is the email address). Anything that
# passes the registration form's validation is fine; this satisfies the
# frontend's "min 8 chars" rule with room to spare.
_TEST_PASSWORD = "QaHarnessPassw0rd!"

# How long to wait for the verification email to arrive in Mailpit. Sized
# the same as the Sonnet-loop default (120s) so a slow inbound aiosmtpd
# round-trip doesn't time out the scripted flow.
_VERIFICATION_TIMEOUT_S = 120.0
# Mailpit poll interval — mirrors wait_for_email's 2s interval.
_VERIFICATION_POLL_INTERVAL_S = 2.0

# Regex used to extract the verification URL from the email body. The
# verification email currently embeds a single ``/verify?token=…`` link;
# this matches it without coupling to any surrounding copy.
_VERIFY_URL_RE = re.compile(r"https?://[^\s<>\"]+/verify[^\s<>\"]*", re.IGNORECASE)


class _PageProto(Protocol):
    """The async-Playwright Page surface this module uses.

    Declared as a Protocol so tests can pass any stub with the right shape
    without depending on the real ``playwright`` package.
    """

    async def goto(self, url: str, **kwargs: Any) -> Any: ...
    def locator(self, selector: str) -> Any: ...
    async def wait_for_selector(self, selector: str, **kwargs: Any) -> Any: ...
    # `request` is Playwright's APIRequestContext — used by
    # `_scripted_upgrade` for the QA billing provision hook call (#2007)
    # and by `_assert_tier_after_setup` for the readback.
    @property
    def request(self) -> Any: ...


async def run_setup(
    action: str | None,
    *,
    page: _PageProto,
    mailpit: MailpitClient,
    persona: Persona,
    web_base_url: str,
    store: Any | None = None,
) -> None:
    """Run the scripted setup for ``action``.

    ``None`` is a no-op — the AI gets control at ``/`` and drives the
    form itself, as it has always done. Any other value runs the
    named flow:

    - ``signup``: register + verify + dismiss-onboarding. Saves
      credentials at the end (lifecycle-aware default, #1105 Slice 1.1).
    - ``signup_then_pro`` / ``signup_then_power``: signup, then
      upgrade to the named tier.
    - ``signup_or_login``: try login first if credentials exist; else
      signup. Recommended for personas that should persist across runs.
    - ``signup_fresh``: always signup, but DON'T save credentials.
      Use to test the signup flow itself.
    - ``clear_credentials_then_signup``: one-shot reset + signup.

    ``store`` is the qa-store ``Store`` instance — required for
    credential-aware variants, optional for the legacy signup-only
    flows. When omitted on a credential-aware variant the function
    logs a warning and falls back to ``signup`` (no save).

    Unknown values raise ``ValueError`` — the dataclass validator
    also rejects them at construction time, so this is belt-and-braces
    for runtime callers that bypass the dataclass (e.g. a hand-built
    test persona).
    """
    if action is None:
        return

    # Lazy-import credentials so this module doesn't pull qa-store on
    # legacy paths that don't use it.
    if action in ("signup_or_login", "signup_fresh", "clear_credentials_then_signup", "signup"):
        from . import credentials as _creds  # noqa: PLC0415
    else:
        _creds = None  # type: ignore[assignment]

    if action == "signup":
        # Legacy alias — same flow as today, but lifecycle-aware: if
        # credentials already exist, try login first (saves the signup
        # tax on a returning persona). The save-after-signup hook still
        # fires so a fresh signup populates credentials going forward.
        await _credential_aware_signup(
            page, mailpit, persona,
            web_base_url=web_base_url,
            store=store,
            try_login_first=store is not None,
            save_after=store is not None,
        )
        return
    if action == "signup_then_pro":
        await _credential_aware_signup(
            page, mailpit, persona,
            web_base_url=web_base_url,
            store=store,
            try_login_first=store is not None,
            save_after=store is not None,
        )
        await _scripted_upgrade(page, plan="pro", web_base_url=web_base_url)
        # #974 — hard assert the upgrade actually took, otherwise the
        # persona runs as Free and reports as Pro (the entire history
        # of QA before this fix).
        await _assert_tier_after_setup(
            page, web_base_url=web_base_url, expected="pro",
        )
        return
    if action == "signup_then_power":
        await _credential_aware_signup(
            page, mailpit, persona,
            web_base_url=web_base_url,
            store=store,
            try_login_first=store is not None,
            save_after=store is not None,
        )
        await _scripted_upgrade(page, plan="power", web_base_url=web_base_url)
        await _assert_tier_after_setup(
            page, web_base_url=web_base_url, expected="power",
        )
        return
    if action == "signup_or_login":
        if store is None:
            log_no_store("signup_or_login", persona.id)
            await _scripted_signup(page, mailpit, persona, web_base_url=web_base_url)
            return
        await _credential_aware_signup(
            page, mailpit, persona,
            web_base_url=web_base_url,
            store=store,
            try_login_first=True,
            save_after=True,
        )
        return
    if action == "signup_fresh":
        # Operator-explicit "test the signup flow, don't pollute
        # lifecycle state". Skips credential save AND login attempt.
        await _scripted_signup(page, mailpit, persona, web_base_url=web_base_url)
        return
    if action == "clear_credentials_then_signup":
        if store is None:
            log_no_store("clear_credentials_then_signup", persona.id)
            await _scripted_signup(page, mailpit, persona, web_base_url=web_base_url)
            return
        _creds.clear_for_persona(store, persona.id)
        await _credential_aware_signup(
            page, mailpit, persona,
            web_base_url=web_base_url,
            store=store,
            try_login_first=False,
            save_after=True,
        )
        return
    valid = ", ".join(VALID_SETUP_ACTIONS)
    raise ValueError(
        f"Unknown setup_actions value: {action!r}. Valid: None, {valid}."
    )


def log_no_store(action: str, persona_id: str) -> None:
    """Log a warning when a credential-aware setup_action is invoked
    without a store reference — happens in local dev / test paths
    that bypass the Atlas wiring. The action falls through to plain
    signup; the operator's audit log shows why."""
    import logging  # noqa: PLC0415
    logging.getLogger(__name__).warning(
        "[%s] setup_actions=%r requested but no qa-store handle was "
        "passed; falling back to plain signup (no credential save)",
        persona_id, action,
    )


async def _credential_aware_signup(
    page: _PageProto,
    mailpit: MailpitClient,
    persona: Persona,
    *,
    web_base_url: str,
    store: Any | None,
    try_login_first: bool,
    save_after: bool,
) -> None:
    """The lifecycle-aware signup wrapper (#1105 Slice 1.1).

    Two-branch decision tree:
      1. If ``try_login_first`` AND credentials exist for this persona,
         drive the /login form with the saved email+password. On
         success, land at /profile/overview and return — saved the
         entire signup+verify tax.
      2. Otherwise (no creds, or login failed), fall through to the
         regular ``_scripted_signup``. On success, if ``save_after``
         is True, persist the email+password back to qa-store via
         the credentials module.

    Failure paths are silent: a login that fails just falls through to
    signup; a credential-save that fails logs a warning but lets the
    persona continue with the explore phase.
    """
    from . import credentials as _creds  # noqa: PLC0415

    if try_login_first and store is not None:
        bundle = _creds.load_for_persona(store, persona.id)
        if bundle is not None and bundle.password:
            try:
                await _scripted_login(
                    page, persona,
                    email=bundle.email,
                    password=bundle.password,
                    web_base_url=web_base_url,
                )
                # #1257 slice 2 — opportunistically request a fresh
                # resume token for the NEXT run. Each login refreshes
                # the token, so a persona who runs daily has a valid
                # restore URL every day.
                await _request_and_save_resume_token(
                    store, persona, web_base_url=web_base_url,
                )
                return
            except Exception:  # noqa: BLE001
                import logging  # noqa: PLC0415
                logging.getLogger(__name__).warning(
                    "[%s] login with saved credentials failed; "
                    "falling back to signup",
                    persona.id, exc_info=True,
                )

    await _scripted_signup(page, mailpit, persona, web_base_url=web_base_url)
    if save_after and store is not None:
        _creds.save_after_signup(
            store, persona.id,
            email=persona.registered_email,
            password=_TEST_PASSWORD,
            verified=True,  # the signup script clicks the verify link
        )
        # #1257 slice 2 — same as the login branch above; first-run
        # signup also gets a resume token so the SECOND run can land
        # logged in via the restore URL.
        await _request_and_save_resume_token(
            store, persona, web_base_url=web_base_url,
        )


async def _request_and_save_resume_token(
    store: Any,
    persona: Persona,
    *,
    web_base_url: str,
) -> None:
    """Ask the backend for a fresh resume token and persist it (#1257).

    Calls ``POST /api/auth/internal/issue-resume-token`` with the
    ``X-Resume-Token-Secret`` header set from ``$RESUME_TOKEN_ISSUE_SECRET``.
    On success the (token, expires_at) pair is saved to the persona's
    credentials sub-doc via ``credentials.save_resume_token``.

    Best-effort, never raises:
      - Missing env var ($RESUME_TOKEN_ISSUE_SECRET unset) → log + return.
        The harness still works without this; the persona will drive
        the UI login form next run.
      - Endpoint 404 (sandbox not deployed yet / prod refusal) → log + return.
      - Network failure → log + return.

    The contract is "we tried; if it worked, the next run starts logged
    in; if it didn't, the next run does what today's run did".
    """
    import logging  # noqa: PLC0415
    import os  # noqa: PLC0415
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    from . import credentials as _creds  # noqa: PLC0415

    log = logging.getLogger(__name__)
    secret = os.environ.get("RESUME_TOKEN_ISSUE_SECRET", "").strip()
    if not secret:
        log.info(
            "[%s] RESUME_TOKEN_ISSUE_SECRET not set; skipping resume-token "
            "request — next run will drive the UI login form",
            persona.id,
        )
        return

    url = f"{web_base_url.rstrip('/')}/api/auth/internal/issue-resume-token"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                headers={"X-Resume-Token-Secret": secret},
                json={"email": persona.registered_email},
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "[%s] resume-token issue endpoint unreachable (%s: %s); "
            "next run will drive the UI login form",
            persona.id, type(exc).__name__, exc,
        )
        return

    if resp.status_code != 200:
        # 404 covers all the refusal modes (prod / missing secret / bad
        # email). The harness doesn't need to distinguish — log the
        # status for diagnostics and fall through.
        log.warning(
            "[%s] resume-token issue returned %s; next run will drive "
            "the UI login form",
            persona.id, resp.status_code,
        )
        return

    try:
        body = resp.json()
        token = body["token"]
        expires_in = int(body.get("expires_in_seconds", 0))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "[%s] resume-token issue response malformed: %s",
            persona.id, exc,
        )
        return

    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    _creds.save_resume_token(
        store, persona.id, token=token, expires_at=expires_at,
    )
    log.info(
        "[%s] resume token saved (expires %s)",
        persona.id, expires_at.isoformat(),
    )


async def _scripted_login(
    page: _PageProto,
    persona: Persona,
    *,
    email: str,
    password: str,
    web_base_url: str,
) -> None:
    """Drive /login with the persona's saved credentials.

    Raises if the login form rejects the credentials OR the post-
    login redirect doesn't land on /profile/overview within the
    timeout. The caller catches and falls back to scripted_signup.

    Matches the data-testids in ``frontend/src/views/Login.vue``:
    - ``login-email``, ``login-password``, ``login-submit``.
    """
    base = web_base_url.rstrip("/")

    await page.goto(f"{base}/login")
    await page.wait_for_selector("[data-testid=login-form]")
    await page.locator("[data-testid=login-email]").fill(email)
    await page.locator("[data-testid=login-password]").fill(password)
    await page.locator("[data-testid=login-submit]").click()

    # Successful login redirects to /profile/overview. Wait for that
    # surface to land before returning so the explore phase sees the
    # post-auth state. If the URL doesn't move within the timeout,
    # the login failed (bad password, account locked, etc.) — raise
    # so the caller falls back to signup.
    await page.wait_for_selector(
        "[data-testid=profile-overview], main >> text=/sign in/i",
        timeout=10_000,
    )
    if "/login" in (page.url if hasattr(page, "url") else ""):
        raise RuntimeError(
            f"login attempt for {persona.id} stayed on /login — "
            "credentials likely invalid"
        )


# ---------------------------------------------------------------------------
# Signup — register form + verification mail click + onboarding dismiss.
# ---------------------------------------------------------------------------
async def _scripted_signup(
    page: _PageProto,
    mailpit: MailpitClient,
    persona: Persona,
    *,
    web_base_url: str,
) -> None:
    """Drive /register, wait for the verification mail, click the link, then
    dismiss the onboarding tour if it shows.

    On exit the page is at ``/profile/overview`` (post-signup landing). The
    AI's explore loop then takes over from there.
    """
    base = web_base_url.rstrip("/")
    fence = time.time()

    # 1. Navigate to /register.
    await page.goto(f"{base}/register")
    await page.wait_for_selector("[data-testid=register-form]")

    # 2. Fill name / email / password — the v-model fields in Register.vue.
    #    The frontend doesn't (yet) give each input its own testid, but the
    #    labels are stable and Playwright's `label=` selector picks them out
    #    reliably. We also fall back to positional input selectors inside the
    #    form for the rare case where the label markup gets reshuffled.
    await page.locator(
        "[data-testid=register-form] input[type=text]"
    ).fill(persona.display_name or persona.id.title())
    await page.locator(
        "[data-testid=register-form] input[type=email]"
    ).fill(persona.registered_email)
    await page.locator(
        "[data-testid=register-form] input[type=password]"
    ).fill(_TEST_PASSWORD)

    # 3. Tick the terms / consent checkbox (data-testid=consent-checkbox).
    await page.locator("[data-testid=consent-checkbox]").check()

    # 4. Submit (data-testid=register-submit).
    await page.locator("[data-testid=register-submit]").click()

    # 5. Poll Mailpit for the verification email addressed to the persona,
    #    fenced at the moment we hit the form (so a stale mail can't satisfy us).
    link = await _wait_for_verification_link(
        mailpit, to_address=persona.registered_email, since=fence
    )

    # 6. Navigate to the verification link inside the email body.
    await page.goto(link)

    # 7. Dismiss the onboarding tour if it's showing.
    await _dismiss_onboarding_if_present(page)

    # 8. Land on /profile/overview so the AI starts at the post-signup
    #    surface, consistent with the issue's spec.
    await page.goto(f"{base}/profile/overview")


# ---------------------------------------------------------------------------
# Upgrade — QA billing provision hook (Stripe→Revolut migration).
#
# History: the original version clicked the Upgrade button and "waited
# for the success state" — a no-op that left personas as Free in MongoDB
# (#974). The next version drove the public Stripe API directly
# (SetupIntent confirm with pm_card_visa, then /billing/activate). Both
# are gone: SlyReply now uses Revolut, and Revolut has NO test-clock and
# the checkout widget (#1977) isn't built yet, so there is no public API
# the harness can drive to mint a subscription deterministically.
#
# The new approach uses the QA-only backend endpoint added in #2007:
#
#   POST /api/qa/billing/provision  {"tier": "pro"|"power"}
#       → deterministic upgrade, no widget, no processor round-trip.
#         Flips subscription_tier in MongoDB and returns the new state.
#
# The endpoint requires the ``X-QA-Token`` header (the same secret the
# harness already injects on every browser request via QA_TOKEN — see
# runner._playwright_mcp_config) and authenticates as the persona's own
# session. It only exists outside production.
#
# Result: subscription_tier=plan in MongoDB after this runs. The
# post-setup tier assertion in ``run_setup`` verifies it as a hard error.
# ---------------------------------------------------------------------------


async def _scripted_upgrade(
    page: _PageProto,
    *,
    plan: str,
    web_base_url: str,
) -> None:
    """Drive the upgrade to ``plan`` via the QA billing provision hook.

    POSTs ``/api/qa/billing/provision`` with the persona's auth (the
    Playwright request context carries the session cookie) and the
    ``X-QA-Token`` header read from ``$QA_TOKEN``. Asserts a 200 and that
    the returned ``subscription_tier`` matches ``plan``.

    Raises ``RuntimeError`` if the endpoint returns a non-OK response or
    the wrong tier — the harness's outer error handler then attributes
    the failure to setup, not to the persona's AI run.
    """
    import os  # noqa: PLC0415

    if plan not in ("pro", "power"):
        raise ValueError(
            f"Unknown upgrade plan: {plan!r}. Valid: 'pro', 'power'."
        )
    base = web_base_url.rstrip("/")

    headers: dict[str, str] = {}
    qa_token = os.environ.get("QA_TOKEN", "").strip()
    if qa_token:
        headers["X-QA-Token"] = qa_token

    provision_resp = await page.request.post(
        f"{base}/api/qa/billing/provision",
        headers=headers,
        data={"tier": plan},
    )
    if not provision_resp.ok:
        body = await provision_resp.text()
        raise RuntimeError(
            f"POST /api/qa/billing/provision failed: "
            f"{provision_resp.status} {body} — the QA billing hook (#2007) "
            "must be deployed and X-QA-Token (QA_TOKEN) must be set."
        )
    provision_body = await provision_resp.json()
    actual = provision_body.get("subscription_tier")
    if actual != plan:
        raise RuntimeError(
            f"/api/qa/billing/provision returned subscription_tier={actual!r}, "
            f"expected {plan!r}: {provision_body!r}"
        )

    # Navigate to /profile/billing so the AI explore loop starts on a
    # familiar surface (and gets to see the post-upgrade UI state).
    await page.goto(f"{base}/profile/billing")


async def _assert_tier_after_setup(
    page: _PageProto,
    *,
    web_base_url: str,
    expected: str,
) -> None:
    """Hard assert /users/me reports the expected subscription tier (#974).

    Converts a previously-silent setup-actions miss (where the upgrade
    appeared to succeed but MongoDB stayed Free) into a loud RuntimeError.
    Without this, the persona's explore loop runs against the wrong tier
    and produces a report framed for a tier the persona is not actually on.
    """
    base = web_base_url.rstrip("/")
    resp = await page.request.get(f"{base}/api/users/me")
    if not resp.ok:
        raise RuntimeError(
            f"Post-setup tier check failed: GET /api/users/me returned "
            f"{resp.status} — can't verify subscription_tier landed."
        )
    me = await resp.json()
    actual = me.get("subscription_tier")
    if actual != expected:
        raise RuntimeError(
            f"Post-setup tier assertion failed: expected "
            f"subscription_tier={expected!r}, got {actual!r}. "
            f"The QA billing provision hook almost certainly didn't take "
            f"— see #974 / #2007."
        )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
async def _wait_for_verification_link(
    mailpit: MailpitClient,
    *,
    to_address: str,
    since: float,
    timeout_s: float = _VERIFICATION_TIMEOUT_S,
    poll_interval_s: float = _VERIFICATION_POLL_INTERVAL_S,
) -> str:
    """Poll Mailpit until a verification email addressed to ``to_address``
    arrives, then return the verification URL embedded in its body.

    ``since`` is the unix timestamp BEFORE we triggered the signup — used to
    fence out any stale mail left in the sink. Raises ``RuntimeError`` if the
    mail never lands within ``timeout_s``, or if a mail lands but doesn't
    contain a ``/verify`` URL.
    """
    target = to_address.lower()
    deadline = time.monotonic() + timeout_s
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            for raw in mailpit.list_messages():
                # Match recipient — Mailpit's "To" is a list of {Address,…}.
                tos = raw.get("To") or []
                addrs = []
                for t in tos:
                    if isinstance(t, dict):
                        a = t.get("Address") or t.get("address")
                    else:
                        a = t
                    if a:
                        addrs.append(str(a).lower())
                if target not in addrs:
                    continue
                msg_id = str(raw.get("ID") or raw.get("id") or "")
                full = mailpit.get_message(msg_id) if msg_id else raw
                # Combine list + full views so we can look at either Snippet
                # or Text without caring which shape the sink returns.
                body_candidates = [
                    str(full.get("Text") or ""),
                    str(full.get("Body") or ""),
                    str(raw.get("Snippet") or ""),
                ]
                for body in body_candidates:
                    match = _VERIFY_URL_RE.search(body)
                    if match:
                        return match.group(0)
                raise RuntimeError(
                    f"Verification email to {to_address} did not contain a "
                    "/verify URL in its body"
                )
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 — defensive against Mailpit blips
            last_error = repr(exc)
        await asyncio.sleep(poll_interval_s)
    tail = f" (last Mailpit error: {last_error})" if last_error else ""
    raise RuntimeError(
        f"Verification email to {to_address} did not arrive within "
        f"{timeout_s}s.{tail}"
    )


async def _dismiss_onboarding_if_present(page: _PageProto) -> None:
    """Click the onboarding-tour dismiss control IF the tour is showing.

    The onboarding tour modal exposes ``data-testid=onboarding-dismiss`` on
    its close control. If the element isn't visible we silently move on —
    a persona who didn't see a tour didn't need to dismiss one.
    """
    locator = page.locator("[data-testid=onboarding-dismiss]")
    try:
        visible = await locator.is_visible()
    except Exception:  # noqa: BLE001 - .is_visible() may not exist on every stub
        visible = False
    if visible:
        await locator.click()
