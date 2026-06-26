"""Harness-side wrapper around qa-store persona credentials (#1105 Slice 1.1).

Slice 1.0 (#1110) shipped the data plane: ``qa_personas.credentials``
sub-doc, qa-store helpers (set/get/clear), and the operator-visible
API. This module is the harness's read+write surface — it caches the
credential bundle in-process for the run's duration so the setup
phase doesn't round-trip Mongo on every prompt build, and so the
recorder hook has a clean place to call ``save_after_signup`` from.

Design notes:

- One ``CredentialBundle`` per (persona_id, run) — the cache is keyed
  by persona_id but cleared between runs by the
  :func:`reset_run_cache` call the orchestrator fires at run start.
- ``save_after_signup`` is the recorder's entry point. It writes to
  qa-store AND warms the cache so a subsequent ``load_for_persona``
  inside the same run sees the credentials without re-reading Mongo.
- All qa-store calls are wrapped in try/except: a Mongo blip during
  setup must NOT crash the persona run. Failure paths fall back to
  the legacy signup-every-run behaviour cleanly.
- ``QA_CREDENTIAL_KEY`` is read by ``qa_store.crypto``; this module
  is agnostic to whether passwords come back encrypted or plaintext.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CredentialBundle:
    """Decrypted, harness-ready view of a persona's credentials.

    ``password`` is the decrypted plaintext when available, ``None``
    if the persona never signed up OR decryption failed (rotated key
    without re-encryption, corrupt ciphertext). Callers MUST handle
    the None case — the standard fallback is "run the scripted
    signup flow instead of login".
    """
    email: str
    password: str | None
    verified: bool
    session_jwt: str | None
    last_rotation_n: int


# In-process cache: persona_id -> CredentialBundle.
# Sized assuming a single persona per process (the harness runs one
# persona at a time); the cap is defensive against orchestrator
# changes that might run multiple personas concurrently.
_CACHE: dict[str, CredentialBundle | None] = {}


def reset_run_cache() -> None:
    """Clear the in-process cache. Called by the orchestrator at run
    start so a long-lived harness process (e.g. ``--all``) doesn't
    leak credentials between personas.

    Idempotent: clearing an already-empty cache is a no-op.
    """
    _CACHE.clear()


def load_for_persona(
    store: Any, persona_id: str, *, force_refresh: bool = False,
) -> CredentialBundle | None:
    """Fetch credentials for ``persona_id`` from qa-store, cached.

    Returns ``None`` when the persona has never been signed up OR
    when the qa-store read fails — both shape into "no usable
    credentials; fall back to signup" at the caller.

    ``store`` is a ``qa_store.schema.Store`` instance; lazy-imported
    inside so the harness package can import this module without
    pulling qa-store at module-load time.
    """
    if not force_refresh and persona_id in _CACHE:
        return _CACHE[persona_id]
    try:
        from qa_store import get_persona_credentials  # noqa: PLC0415
        raw = get_persona_credentials(store, persona_id)
    except Exception:  # noqa: BLE001
        log.exception(
            "credentials: load failed for %s — falling back to signup",
            persona_id,
        )
        _CACHE[persona_id] = None
        return None
    if raw is None:
        _CACHE[persona_id] = None
        return None
    bundle = CredentialBundle(
        email=raw.get("email", ""),
        password=raw.get("password_plain"),
        verified=bool(raw.get("verified", False)),
        session_jwt=raw.get("session_jwt"),
        last_rotation_n=int(raw.get("last_rotation_n", 0)),
    )
    _CACHE[persona_id] = bundle
    return bundle


def save_after_signup(
    store: Any,
    persona_id: str,
    *,
    email: str,
    password: str,
    verified: bool = False,
) -> None:
    """Persist credentials after a successful signup, warm the cache.

    Recorder hook entry-point: when the setup phase's scripted signup
    succeeds (or — Slice 1.2 follow-up — when the AI persona drives
    its own signup flow), call this with the email+password the
    persona used. The cache update lets a subsequent
    :func:`load_for_persona` inside the same run pick up the saved
    state without a Mongo round-trip.

    Failures are logged + swallowed so a Mongo blip during the
    post-signup write doesn't crash the persona's actual exploration
    phase. The persona continues; next run just won't find
    credentials and falls back to signup again.
    """
    try:
        from qa_store import set_persona_credentials  # noqa: PLC0415
        set_persona_credentials(
            store, persona_id,
            email=email,
            password_plain=password,
            verified=verified,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "credentials: save failed for %s (email=%s) — credentials "
            "will not persist; next run will signup again",
            persona_id, email,
        )
        return
    # Warm the cache so subsequent in-run reads see the new bundle.
    _CACHE[persona_id] = CredentialBundle(
        email=email,
        password=password,
        verified=verified,
        session_jwt=None,
        last_rotation_n=0,
    )


def save_resume_token(
    store: Any,
    persona_id: str,
    *,
    token: str,
    expires_at: Any | None = None,
) -> None:
    """Persist a single-use session-restore token (#1257 slice 2).

    Called by the scripted prelude after a successful login when the
    backend's ``POST /api/auth/internal/issue-resume-token`` returns a
    token. The token is what later runs (slice 3) hand to the persona
    as a ``{persona_resume_url}`` placeholder so the persona's first
    action is ``mcp__playwright__browser_navigate`` to
    ``{base}/auth/restore?token=…`` — the browser arrives already
    logged in, no UI login form needed.

    Failures are logged + swallowed for the same reason as
    :func:`save_after_signup`: a Mongo blip during the post-login
    write must not crash the persona's actual exploration phase.
    Next run will see no token and fall back to the slice-1.1 UI
    login path (which still works fine; the resume URL is just an
    optimisation).
    """
    try:
        from qa_store import record_persona_resume_token  # noqa: PLC0415
        record_persona_resume_token(
            store, persona_id, token=token, expires_at=expires_at,
        )
    except KeyError:
        log.warning(
            "credentials: save_resume_token called for %s but the persona "
            "has no saved credentials; call save_after_signup first",
            persona_id,
        )
        return
    except Exception:  # noqa: BLE001
        log.exception(
            "credentials: save_resume_token failed for %s", persona_id,
        )


def load_resume_token(store: Any, persona_id: str) -> dict | None:
    """Return the persona's current resume token + expiry, or None.

    None covers all of: persona unknown, no credentials yet, no token
    saved, or token expired. The caller (#1257 slice 3) treats every
    None case the same way — render the prompt without the resume
    URL placeholder, fall back to the password-typing path.
    """
    try:
        from qa_store import get_persona_resume_token  # noqa: PLC0415
        return get_persona_resume_token(store, persona_id)
    except Exception:  # noqa: BLE001
        log.exception(
            "credentials: load_resume_token failed for %s — treating as no token",
            persona_id,
        )
        return None


def record_session(
    store: Any,
    persona_id: str,
    *,
    jwt: str,
    jwt_expires_at: Any | None = None,
) -> None:
    """Refresh the persona's session JWT cookie after a successful
    login or page reload.

    Slice 1.1 doesn't yet inject JWTs (the next slice wires the
    ``/__test/__set-cookie`` helper that makes the JWT path useful).
    Until then this is a no-op-friendly write — the harness CAN
    call it after a login but nothing currently consumes the result
    other than the operator-visible status endpoint.

    Failures are logged + swallowed for the same reason as
    :func:`save_after_signup`.
    """
    try:
        from qa_store import record_persona_session  # noqa: PLC0415
        record_persona_session(
            store, persona_id, jwt=jwt, jwt_expires_at=jwt_expires_at,
        )
    except KeyError:
        # Persona has no prior credentials — session-refresh has
        # nothing to attach to. Caller should have called
        # save_after_signup first; log as a soft warning.
        log.warning(
            "credentials: record_session called for %s but the persona "
            "has no saved credentials; ignoring",
            persona_id,
        )
        return
    except Exception:  # noqa: BLE001
        log.exception(
            "credentials: record_session failed for %s", persona_id,
        )
        return
    # Update the cache too — but only the JWT half; preserve the
    # password (which record_persona_session also preserves).
    cached = _CACHE.get(persona_id)
    if cached is not None:
        _CACHE[persona_id] = CredentialBundle(
            email=cached.email,
            password=cached.password,
            verified=cached.verified,
            session_jwt=jwt,
            last_rotation_n=cached.last_rotation_n,
        )


def clear_for_persona(store: Any, persona_id: str) -> None:
    """Wipe persona credentials. Used by the ``clear_credentials_then_signup``
    setup-action DSL value and by operator-side resets.

    Failures swallowed: a Mongo blip doesn't crash the persona run.
    The cache is cleared regardless so the same process doesn't
    re-use stale credentials within the run.
    """
    _CACHE.pop(persona_id, None)
    try:
        from qa_store import clear_persona_credentials  # noqa: PLC0415
        clear_persona_credentials(store, persona_id)
    except KeyError:
        # Already-cleared / unknown persona — clearing is idempotent.
        return
    except Exception:  # noqa: BLE001
        log.exception(
            "credentials: clear failed for %s", persona_id,
        )
