"""Heuristic site explorer (v1) — bootstrap a target's Site Model from its HTML.

The product's north star is an LLM agent that drives a real browser to probe a
site. This is the deterministic first cut: fetch the registered site's homepage,
derive its Surfaces / Flows / Knowledge, and generate a starter questionnaire —
**no Claude token required**. It advances the target lifecycle
``registered → exploring → awaiting-answers``. The LLM-powered, browser-driving
explorer (on the operator's Claude subscription) layers on top of this later.

Best-effort: if the fetch fails (unreachable / timeout), it still writes a
generic starter model + questionnaire so onboarding always proceeds. Discovery
is GET-only — read the public page, never act on the site (the product's
"discovery, not intrusion" principle).
"""

from __future__ import annotations

import logging
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from qa_store.capabilities import (
    CAPABILITY_CATALOG,
    list_site_capabilities,
    set_capability_status,
)
from qa_store.schema import DEFAULT_TENANT, Store
from qa_store.site_model import (
    get_site_target,
    set_target_lifecycle,
    upsert_site_knowledge,
    upsert_site_surface,
    upsert_test_flow,
)
from qa_store.site_questions import upsert_site_question

log = logging.getLogger(__name__)

# The explorer *proposes* (never grants) capabilities whose ``proposed_when`` tag
# matches an affordance it detected — turning the 30-item catalog into a short,
# site-tailored "suggested" list so a newcomer isn't staring at the whole wall.
# Earn-trust: it never auto-proposes above L3 (no read-only DB / infra access);
# the operator reaches for those deliberately. Discovery proposes, the human grants.
_MAX_AUTO_PROPOSE_LEVEL = 3
_DETECTED_TO_SIGNALS = {
    "login": {"login_form"},
    "signup": {"login_form", "email"},  # new accounts need a test login + an inbox
    "checkout": {"payments"},
    "api": {"api"},
}

_USER_AGENT = "TestEase-Explorer/0.1 (+https://github.com/Sherman-Studio/testease)"
_FLOW_KEYWORDS = {
    "signup": [
        "sign up", "signup", "register", "get started", "try free", "create account", "join",
    ],
    "login": ["log in", "login", "sign in", "signin"],
    "checkout": ["pricing", "plans", "upgrade", "checkout", "subscribe", "billing", "buy "],
    "api": ["/api", "developer", "api docs", "api reference"],
}


class _Scan(HTMLParser):
    """Collect the title, forms (and whether each has a password field), and
    anchor (href, text) pairs from a page."""

    def __init__(self) -> None:
        super().__init__()
        self._title_parts: list[str] = []
        self._in_title = False
        self._title_done = False  # capture only the FIRST <title> (the document
        # head one) — pages embed extra <title> elements inside inline SVG icons.
        self.forms: list[dict] = []
        self._cur_form: dict | None = None
        self.links: list[tuple[str, str]] = []
        self._a_href: str | None = None
        self._a_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title" and not self._title_done:
            self._in_title = True
        elif tag == "form":
            self._cur_form = {"has_password": False, "action": a.get("action", "")}
        elif tag == "input":
            if self._cur_form is not None and (a.get("type") or "").lower() == "password":
                self._cur_form["has_password"] = True
        elif tag == "a" and a.get("href"):
            self._a_href = a["href"]
            self._a_text = []

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True
        elif tag == "form" and self._cur_form is not None:
            self.forms.append(self._cur_form)
            self._cur_form = None
        elif tag == "a" and self._a_href is not None:
            self.links.append((self._a_href, " ".join(self._a_text).strip()))
            self._a_href, self._a_text = None, []

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
        elif self._a_href is not None:
            self._a_text.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self._title_parts).split())[:200]


def _fetch(base_url: str) -> str | None:
    try:
        with httpx.Client(
            follow_redirects=True, timeout=8.0, headers={"User-Agent": _USER_AGENT},
        ) as client:
            r = client.get(base_url)
            return r.text if r.status_code < 400 else None
    except Exception:  # noqa: BLE001 — network is best-effort
        log.warning("explorer: fetch failed for %s", base_url, exc_info=True)
        return None


def _detect(scan: _Scan) -> set[str]:
    detected: set[str] = set()
    if any(f["has_password"] for f in scan.forms):
        detected.add("login")
    for href, text in scan.links:
        hay = f"{href} {text}".lower()
        for flow, kws in _FLOW_KEYWORDS.items():
            if any(k in hay for k in kws):
                detected.add(flow)
    return detected


def _propose_capabilities(
    store: Store, tenant_id: str, target_id: str, detected: set[str],
) -> int:
    """Propose the catalog capabilities relevant to what was detected. Idempotent
    and non-destructive: skips anything the operator (or a prior pass) has already
    acted on, and never proposes above ``_MAX_AUTO_PROPOSE_LEVEL``. Returns how
    many fresh proposals it made."""
    signals: set[str] = set()
    for d in detected:
        signals |= _DETECTED_TO_SIGNALS.get(d, set())
    if not signals:
        return 0
    acted = {
        c["capability_id"]
        for c in list_site_capabilities(store, tenant_id, target_id)
        if c.get("status") in {"granted", "declined", "not_applicable", "proposed"}
    }
    n = 0
    for cap in CAPABILITY_CATALOG:
        if (
            cap.get("proposed_when") in signals
            and cap["level"] <= _MAX_AUTO_PROPOSE_LEVEL
            and cap["capability_id"] not in acted
        ):
            set_capability_status(
                store, tenant_id=tenant_id, target_id=target_id,
                capability_id=cap["capability_id"], status="proposed",
                proposed_by="explorer",
            )
            n += 1
    return n


def explore_target(
    store: Store, target_id: str, *, tenant_id: str = DEFAULT_TENANT,
) -> dict | None:
    """Bootstrap the Site Model for ``target_id`` from its homepage. Returns a
    summary, or ``None`` if the target doesn't exist."""
    target = get_site_target(store, tenant_id, target_id)
    if target is None:
        return None
    base_url = (target.get("base_url") or "").strip()
    set_target_lifecycle(store, tenant_id, target_id, "exploring")

    html = _fetch(base_url)
    title = ""
    detected: set[str] = set()
    n_forms = n_links = 0

    surfaces: list[tuple[str, str, str, str]] = []  # (id, path, kind, description)
    if html:
        scan = _Scan()
        try:
            scan.feed(html)
        except Exception:  # noqa: BLE001 — tolerate malformed HTML
            log.warning("explorer: HTML parse error for %s", base_url, exc_info=True)
        title = scan.title
        n_forms, n_links = len(scan.forms), len(scan.links)
        detected = _detect(scan)
        surfaces.append(("home", "/", "page", f"Homepage{f' — {title}' if title else ''}"))
        for i, f in enumerate(scan.forms[:8]):
            is_auth = f["has_password"]
            surfaces.append((
                f"form-{i + 1}", f.get("action") or "/",
                "auth_flow" if is_auth else "form",
                "Login / auth form (has a password field)" if is_auth else "Form",
            ))
    else:
        surfaces.append((
            "home", "/", "page",
            "Homepage (could not be fetched — add surfaces manually or re-explore).",
        ))

    for sid, path, kind, desc in surfaces:
        upsert_site_surface(
            store, tenant_id=tenant_id, target_id=target_id, surface_id=sid,
            kind=kind, path=path, description=desc,
        )

    # Flows — from what was detected, with a baseline so it's never empty.
    flow_specs = {
        "signup": ("auth", "A new visitor signs up and verifies their account.",
                   "mobile-signup-visitor"),
        "login": ("auth", "An existing user signs in and reaches their account.",
                  "desktop-evaluator"),
        "checkout": ("billing", "A user upgrades / checks out and pays.", "declined-payer"),
        "api": ("api", "A developer creates a token and calls the API.", "api-poker"),
    }
    flows = [f for f in ("signup", "login", "checkout", "api") if f in detected]
    if not flows:
        flows = ["first-impression"]
        flow_specs["first-impression"] = (
            "general", "A first-time visitor forms an impression of the landing page.",
            "first-impression-critic",
        )
    for fid in flows:
        area, story, arch = flow_specs[fid]
        upsert_test_flow(
            store, tenant_id=tenant_id, target_id=target_id, flow_id=fid,
            area=area, user_story=story, persona_archetype=arch, generated_from="explorer",
        )

    # Knowledge — one starter note describing the pass.
    host = urlparse(base_url).netloc or base_url
    if html:
        seen = f"Saw {n_forms} form(s) and {n_links} link(s)"
        det = ", ".join(sorted(detected)) if detected else "no obvious affordances"
        named = f" (“{title}”)" if title else ""
        summary = (
            f"Bootstrapped by the heuristic explorer from {host}'s homepage{named}. "
            f"{seen}; detected: {det}. Refine this model, or re-explore with the "
            "LLM agent for a deeper pass."
        )
    else:
        summary = (
            f"The homepage at {base_url} could not be fetched during exploration. "
            "Add surfaces/flows manually, or re-explore once it's reachable."
        )
    upsert_site_knowledge(
        store, tenant_id=tenant_id, target_id=target_id, entry_id="kb-explorer-pass",
        kind="guidance", body=summary, authored_by="explorer",
    )

    # Questionnaire — always ask the auth + scope basics; add by detection.
    # (question_id, text, kind, category, rationale, options, required)
    questions: list[tuple] = [
        ("login-username", "Which account should the personas sign in with?",
         "free_text", "auth", "Personas log in rather than signing up fresh.", [], True),
        ("login-password", "What's the password for that account?",
         "secret", "auth", "Stored encrypted in the vault — only a pointer is kept.", [], True),
    ]
    if "signup" in detected:
        questions.append((
            "allow-signup", "Should personas also create brand-new accounts via signup?",
            "boolean", "auth", "Signup was detected on the site.", [], False))
    if "checkout" in detected:
        questions.append((
            "payment-mode", "Is checkout a demo flow or does it take real payment?",
            "choice", "payments",
            "Pricing/checkout was detected — gates whether billing personas run freely.",
            ["demo (no charge)", "real payment"], True))
    if "api" in detected:
        questions.append((
            "api-base", "If there's a public API, what's its base URL?",
            "url", "api", "API/docs links were detected.", [], False))
    questions.append((
        "avoid-paths", "Any paths or actions the personas must NOT take?",
        "free_text", "scope", "Guard-rails for this site.", [], False))

    for order, (qid, text, kind, cat, rationale, options, required) in enumerate(questions):
        upsert_site_question(
            store, tenant_id=tenant_id, target_id=target_id, question_id=qid,
            text=text, kind=kind, category=cat, rationale=rationale,
            options=options, required=required, order=order, generated_by="explorer",
        )

    n_proposed = _propose_capabilities(store, tenant_id, target_id, detected)

    set_target_lifecycle(store, tenant_id, target_id, "awaiting-answers")
    return {
        "lifecycle": "awaiting-answers",
        "fetched": bool(html),
        "title": title,
        "detected": sorted(detected),
        "counts": {
            "surfaces": len(surfaces),
            "flows": len(flows),
            "knowledge": 1,
            "questions": len(questions),
            "capabilities_proposed": n_proposed,
        },
    }
