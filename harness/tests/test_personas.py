"""Structural tests for the relaunched 25-archetype catalog (#1009).

The pre-relaunch test_personas.py had ~1400 lines asserting on SlyReply-
specific prompt CONTENT (Margaret's first-impression copy, Daniel's
ChatGPT comparisons, Helen's UK-solicitor vocabulary, etc.). All of
that content was deleted in #1009 — the new catalog ships generic
archetype prompts that describe what each persona DOES, not what they
say. So this file now verifies the SHAPE of the catalog and the
rendering machinery, not the prompt prose itself.
"""

from __future__ import annotations

import pytest

from qa_agents.personas import (
    PERSONAS,
    Persona,
    get_persona,
    groups,
    personas_in_group,
    render_explore_prompt,
)

# Hand-picked anchor ids — used in dispatch tests. Kept short so a future
# rename of one persona doesn't churn every test in the file.
_KNOWN_IDS = (
    "mobile-signup-visitor",
    "desktop-evaluator",
    "happy-path-signup",
    "declined-payer",
    "first-impression-critic",
    "adversarial-tester",
    "keyboard-only",
)


def test_registry_holds_expected_persona_count():
    # #1009 shipped the catalog with 25 generic archetypes.
    # #1024 added Pia (perf-budget-evaluator).
    # #1026 added Asha (api-poker).
    # #1115 added Cătălina (attachment-aggressor) + Aurora (image-aggressor).
    # #1020 added Lara (trial-expirer), re-integrated post-#1149 revert.
    # Avery added a comprehensive-explorer for depth-over-breadth runs
    # — the first persona designed for the full run-duration budget,
    # uses signup_or_login to leverage the Slice 1.1 credentials path.
    # #1109 slice 3 added Elowen (attachment-explorer) — exercises the
    # threading + forwarding + attachment-integrity MCP tools added in
    # slices 1+2 (#1236, #1242).
    # #616 target-market rework: retired Diego (oauth-seeker — no social
    # sign-in exists) and Yuki (team-admin — no teams exist), and added 9
    # personas (Margaret, Daniel, Brendan, Siobhan ported from PERSONAS.md
    # + 5 new not-so-savvy/small-business users). 32 - 2 + 9 = 39.
    # + Nadia (internal-load-economist) — the first `internal` group
    #   persona (staff load & cost-economics test). 39 + 1 = 40.
    # + #1971 Renata (payments-matrix) — exhaustive Revolut payment-scenario
    #   tester. 40 + 1 = 41.
    # Bump this when the catalog grows; the assertion is a tripwire for
    # accidental persona deletions.
    assert len(PERSONAS) == 41

    # The retired personas must be gone (regression guard for #616).
    assert "team-admin" not in PERSONAS
    assert "oauth-seeker" not in PERSONAS


def test_registry_ids_are_unique():
    # PERSONAS is a dict, so ids are unique by construction — but verify
    # that each persona's .id matches its key (a typo in the dataclass
    # would silently shadow another persona).
    for pid, persona in PERSONAS.items():
        assert persona.id == pid, f"key {pid!r} != persona.id {persona.id!r}"


@pytest.mark.parametrize("persona_id", _KNOWN_IDS)
def test_known_ids_resolve(persona_id):
    p = get_persona(persona_id)
    assert isinstance(p, Persona)
    assert p.id == persona_id


def test_get_persona_unknown_raises_with_helpful_message():
    with pytest.raises(KeyError) as exc:
        get_persona("nobody")
    # The error message names valid ids so an operator typo is fixable.
    assert "mobile-signup-visitor" in str(exc.value)


@pytest.mark.parametrize("persona_id", sorted(PERSONAS))
def test_every_persona_has_required_fields(persona_id):
    p = get_persona(persona_id)
    assert p.id == persona_id
    assert p.display_name
    assert p.archetype
    assert p.registered_email and "@" in p.registered_email
    assert p.explore_system_prompt
    assert p.report_system_prompt
    assert p.flows  # at least one


@pytest.mark.parametrize("persona_id", sorted(PERSONAS))
def test_every_persona_defaults_to_inactive(persona_id):
    # The relaunch contract: a fresh tenant has zero personas activated.
    # The operator opts in via the Personas page (#1009 Personas.vue).
    assert get_persona(persona_id).is_active is False


@pytest.mark.parametrize("persona_id", sorted(PERSONAS))
def test_every_persona_has_region_and_language(persona_id):
    # Both default to None on Persona, but the seeded catalog sets them
    # explicitly so the Personas page shows useful defaults.
    p = get_persona(persona_id)
    assert p.region, f"{persona_id} missing region"
    assert p.language, f"{persona_id} missing language"


# ---------------------------------------------------------------------------
# Persona grouping (#616) — target / core / technical rotations.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("persona_id", sorted(PERSONAS))
def test_every_persona_has_a_valid_group(persona_id):
    assert get_persona(persona_id).group in {"target", "core", "technical", "internal"}


def test_target_group_holds_the_market_personas():
    # The point of the rework: the not-so-savvy / small-business users are
    # the default-emphasis "target" group, and the flagship is there.
    target = personas_in_group("target")
    assert "margaret" in target
    for pid in ("brendan", "sole-trader-mobile", "esl-shop-owner",
                "marketplace-seller", "solo-professional",
                "community-volunteer", "daniel", "siobhan"):
        assert pid in target, pid


def test_technical_archetypes_are_grouped_opt_in():
    # a11y / security / perf etc. stay in the catalog but out of the
    # default target rotation.
    technical = personas_in_group("technical")
    for pid in ("keyboard-only", "screen-reader", "adversarial-tester",
                "perf-budget-evaluator", "api-poker", "unicode-tester"):
        assert pid in technical, pid


def test_groups_partition_the_whole_catalog():
    g = groups()
    assert set(g) == {"target", "core", "technical", "internal"}
    flattened = sorted(pid for ids in g.values() for pid in ids)
    assert flattened == sorted(PERSONAS)


def test_internal_group_holds_the_staff_personas():
    # The internal group is SlyReply staff (load + cost economics), kept
    # out of the customer rotation. Nadia is the flagship.
    internal = personas_in_group("internal")
    assert "internal-load-economist" in internal
    assert get_persona("internal-load-economist").group == "internal"


def test_internal_persona_gets_the_insider_preamble():
    # Internal-group personas render with _INTERNAL_PREAMBLE, NOT the
    # customer "you are a real user" preamble. Pin the swap so a renderer
    # refactor can't silently hand a staff persona the wrong frame.
    rendered = render_explore_prompt(
        get_persona("internal-load-economist"), "http://frontend",
        admin_email="admin@x", admin_password="pw",
    )
    assert "INTERNAL QA" in rendered
    assert "SlyReply INTERNAL STAFF" in rendered
    # The privileged load tool must be named so the persona knows it exists.
    assert "mcp__loadgen__blast" in rendered
    # The customer preamble's "act like an ordinary user" framing must NOT
    # reach a staff persona — that's the whole reason internal is separate.
    assert "ACT LIKE A USER, NOT A SCRIPT" not in rendered


def test_personas_in_group_rejects_unknown_group():
    with pytest.raises(KeyError):
        personas_in_group("nonsense")


def test_display_name_carries_character_and_archetype():
    # "Maya — mobile signup visitor": light character + functional handle.
    # The em-dash is the convention; assert on at least one persona to
    # lock the format in.
    maya = get_persona("mobile-signup-visitor")
    assert "—" in maya.display_name
    assert "Maya" in maya.display_name


def test_browser_locale_composes_region_and_language():
    p = Persona(
        id="x", display_name="X", archetype="x",
        registered_email="x@example.com",
        explore_system_prompt="x", report_system_prompt="x",
        region="GB", language="en",
    )
    assert p.browser_locale == "en-GB"


def test_browser_locale_falls_back_when_one_field_missing():
    only_lang = Persona(
        id="x", display_name="X", archetype="x",
        registered_email="x@example.com",
        explore_system_prompt="x", report_system_prompt="x",
        language="en",
    )
    only_region = Persona(
        id="y", display_name="Y", archetype="y",
        registered_email="y@example.com",
        explore_system_prompt="y", report_system_prompt="y",
        region="GB",
    )
    neither = Persona(
        id="z", display_name="Z", archetype="z",
        registered_email="z@example.com",
        explore_system_prompt="z", report_system_prompt="z",
    )
    assert only_lang.browser_locale == "en"
    assert only_region.browser_locale == "GB"
    assert neither.browser_locale is None


# ---------------------------------------------------------------------------
# render_explore_prompt — substitution, preamble, fixtures, blocks.
# ---------------------------------------------------------------------------
def _any_persona() -> Persona:
    return get_persona("mobile-signup-visitor")


def test_render_substitutes_base_url_and_persona_email():
    p = _any_persona()
    rendered = render_explore_prompt(p, "http://frontend", inbox_url="http://mail")
    assert "http://frontend" in rendered
    assert p.registered_email in rendered
    # No stray placeholders.
    assert "{base_url}" not in rendered
    assert "{persona_email}" not in rendered
    assert "{inbox_url}" not in rendered


def test_render_prepends_harness_preamble():
    rendered = render_explore_prompt(_any_persona(), "http://frontend")
    # The preamble has a unique opening line that's easy to anchor on.
    assert rendered.startswith("HOW THIS SESSION WORKS")
    # And its anti-debugging guardrail must survive rendering.
    assert "INFRASTRUCTURE FAULT" in rendered.upper()


def test_preamble_biases_toward_actionable_findings():
    """The bug-bias steering line must reach every persona. The user
    reported in 2026-05-28 that Jordan's run filed mostly praise +
    observations and zero actionable findings — a 'failed run from
    the operator's perspective'. Pin the steering language so a
    future preamble refactor can't accidentally drop it."""
    rendered = render_explore_prompt(_any_persona(), "http://frontend")
    # The headline framing — must be loud (uppercase) and explicit.
    assert "OPERATOR HIRED YOU TO FIND THINGS TO FIX" in rendered
    # The audit instruction — the persona must self-check before wrap.
    assert "audit your finding list" in rendered
    # The concrete antipattern that triggered the change. Asserted as
    # two short phrases since the rendered preamble line-wraps and
    # ``in`` does a substring match — no false negatives on wrap.
    assert "0 actionable findings" in rendered
    assert "failed run from the operator" in rendered


@pytest.mark.parametrize("persona_id", sorted(PERSONAS))
def test_render_pins_registered_email_in_preamble(persona_id):
    # Same constraint as the old test_harness_preamble_pins_registered_email
    # check — the harness MUST tell the model exactly which from-address
    # send_email will use, or signup with a free-floating email silently
    # breaks the round-trip.
    p = get_persona(persona_id)
    rendered = render_explore_prompt(p, "http://frontend")
    assert p.registered_email in rendered, persona_id


@pytest.mark.parametrize("persona_id", sorted(PERSONAS))
def test_render_leaves_no_unresolved_placeholders(persona_id):
    rendered = render_explore_prompt(
        get_persona(persona_id),
        "http://frontend",
        inbox_url="http://mail",
        admin_email="admin@x", admin_password="pw",
    )
    # A leftover "{something}" usually means a prompt added a placeholder
    # that render_explore_prompt's kwargs don't know about. Catch it.
    assert "{" not in rendered or "}" not in rendered, (
        f"{persona_id}: rendered prompt contains unresolved placeholder "
        f"(look for braces)"
    )


def test_render_injects_fixture_placeholders():
    # Tomas's prompt uses {fixture_payment_cards_valid_card}; rendering
    # must resolve it from qa_store.fixtures.DEFAULT_FIXTURES (Revolut
    # sandbox happy-path Visa).
    rendered = render_explore_prompt(get_persona("upgrade-buyer"), "http://x")
    assert "4929 4205 7359 5709" in rendered, (
        "fixture placeholder for the happy-path card didn't resolve"
    )


def test_render_injects_declined_card_fixture():
    # Aiko's prompt uses {fixture_payment_cards_declined_card} (Revolut
    # sandbox do_not_honour / generic decline).
    rendered = render_explore_prompt(get_persona("declined-payer"), "http://x")
    assert "2720 9988 3777 9594" in rendered


# ---------------------------------------------------------------------------
# Mandatory-action block (#861 carried forward).
# ---------------------------------------------------------------------------
class TestMandatoryActionBlock:
    def test_no_block_when_no_mandatory_ids(self):
        rendered = render_explore_prompt(_any_persona(), "http://x")
        assert "MUST-ATTEMPT" not in rendered

    def test_no_block_when_mandatory_ids_is_empty_tuple(self):
        rendered = render_explore_prompt(
            _any_persona(), "http://x", mandatory_action_ids=()
        )
        assert "MUST-ATTEMPT" not in rendered

    def test_unknown_id_is_silently_dropped(self):
        # Defensive: the trigger UI pre-validates against CATALOG, so an
        # unknown id reaching the renderer is a should-not-happen. The
        # renderer just drops it (no exception).
        rendered = render_explore_prompt(
            _any_persona(), "http://x",
            mandatory_action_ids=("made.up_action_id",),
        )
        assert "made.up" not in rendered


# ---------------------------------------------------------------------------
# Persona dataclass — validation.
# ---------------------------------------------------------------------------
class TestPersonaValidation:
    def test_valid_setup_action_accepted(self):
        # setup_actions optional + restricted to a known set.
        p = Persona(
            id="x", display_name="X", archetype="x",
            registered_email="x@example.com",
            explore_system_prompt="x", report_system_prompt="x",
            setup_actions="signup",
        )
        assert p.setup_actions == "signup"

    def test_unknown_setup_action_raises(self):
        with pytest.raises(ValueError, match="setup_actions"):
            Persona(
                id="x", display_name="X", archetype="x",
                registered_email="x@example.com",
                explore_system_prompt="x", report_system_prompt="x",
                setup_actions="bogus",
            )

    def test_setup_actions_none_default(self):
        # Most shipped personas default to None — they discover signup
        # themselves via the AI prompt so that the signup flow itself
        # is part of what's being tested. The exceptions are personas
        # whose job EXPLICITLY assumes a logged-in starting state —
        # those use the Slice 1.1 scripted ``signup_or_login`` path
        # (#1111) so credentials persist across runs.
        _scripted_setup_personas = {
            "comprehensive-explorer",
            # #1253 — Jordan is a recurring re-verifier of prior findings,
            # which requires the same account across runs. Both this and
            # the duplicate invariant in test_setup_actions.py share the
            # allowlist — keep them in sync.
            "desktop-evaluator",
        }
        for p in PERSONAS.values():
            if p.id in _scripted_setup_personas:
                assert p.setup_actions == "signup_or_login", (
                    f"{p.id}: scripted-setup persona must use "
                    f"signup_or_login (got {p.setup_actions!r})"
                )
            else:
                assert p.setup_actions is None, (
                    f"{p.id}: expected setup_actions=None in the relaunched "
                    f"catalog (all generic archetypes self-discover)"
                )


def test_flow_checklist_numbered_from_one():
    p = _any_persona()
    checklist = p.flow_checklist()
    assert checklist.strip().startswith("1.")
    assert f"{len(p.flows)}." in checklist


# ---------------------------------------------------------------------------
# {persona_resume_url} preamble placeholder (#1257 slice 3).
#
# The harness preamble always carries the resume URL line; it's the
# value that switches between a real URL and the "(none — …)" sentinel.
# Personas inherit the line via the shared preamble, so no per-persona
# prompt edit is needed.
# ---------------------------------------------------------------------------
class TestResumeUrlPlaceholder:
    def test_empty_resume_url_renders_no_url_sentinel(self):
        """When the harness has no saved token (most personas, every
        first-ever run), the preamble must NOT show a URL — it must
        show a clear instruction to fall back to email + password."""
        rendered = render_explore_prompt(_any_persona(), "http://frontend")
        # Sentinel must be present so the AI knows what to do.
        assert "(none" in rendered
        # And the resume-URL section header must still mention what
        # to do IF the URL is present — even for the no-URL case the
        # AI should learn the contract.
        assert "Account-restore URL" in rendered

    def test_real_url_renders_inline_and_keeps_directive(self):
        """When a token IS saved, the placeholder becomes the full URL
        and the navigate-first directive is in scope."""
        rendered = render_explore_prompt(
            _any_persona(), "http://frontend",
            resume_url="http://frontend/auth/restore?token=abc123",
        )
        assert "http://frontend/auth/restore?token=abc123" in rendered
        # The directive must tell the AI to navigate there FIRST, in
        # the imperative — not "may" or "could".
        assert "NAVIGATE HERE FIRST" in rendered
        # And the 410-Gone fallback should be there too, so a stale
        # token doesn't strand the persona without recovery instructions.
        assert "410" in rendered

    def test_real_url_replaces_the_value_not_the_explanation(self):
        """A populated URL must show up on the Account-restore URL line
        itself — not be left to inference from the surrounding copy.
        The instructional paragraph below the context block legitimately
        mentions the "(none — …)" sentinel as part of explaining what
        the line might say; we only care that the URL *value* is the
        URL, not the sentinel."""
        rendered = render_explore_prompt(
            _any_persona(), "http://frontend",
            resume_url="http://frontend/auth/restore?token=xyz",
        )
        # The line "Account-restore URL ...:\n    {persona_resume_url}"
        # must carry the real URL right after the line label, not the
        # no-token sentinel.
        # We assert by finding the line and confirming the URL appears
        # in the indented value immediately below.
        lines = rendered.splitlines()
        for i, line in enumerate(lines):
            if "Account-restore URL" in line:
                # The next non-empty line is the indented value.
                next_lines = [
                    line2.strip()
                    for line2 in lines[i + 1:i + 3]
                    if line2.strip()
                ]
                assert next_lines, "no value line below the URL header"
                assert "http://frontend/auth/restore" in next_lines[0]
                break
        else:
            raise AssertionError("Account-restore URL line missing")

    def test_whitespace_only_url_is_treated_as_empty(self):
        """A caller passing whitespace (defensive) should not produce a
        broken URL line. Whitespace collapses to the sentinel."""
        rendered = render_explore_prompt(
            _any_persona(), "http://frontend", resume_url="   ",
        )
        assert "(none" in rendered

    @pytest.mark.parametrize("persona_id", sorted(PERSONAS))
    def test_every_persona_renders_resume_url_line(self, persona_id):
        """The resume URL line is on the shared preamble, so it must
        appear in every persona's rendered prompt — not just Jordan
        and Avery. Other personas without saved tokens see the
        sentinel; they ignore it. Pin this so a refactor that moves
        the line into per-persona blocks gets caught."""
        rendered = render_explore_prompt(get_persona(persona_id), "http://x")
        assert "Account-restore URL" in rendered, persona_id
