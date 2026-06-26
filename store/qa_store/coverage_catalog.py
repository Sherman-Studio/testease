"""Coverage catalog of every user-facing action SlyReply supports.

A curated, PR-reviewed enumeration of the actions a real user (or admin) can
take on the site. Slice 4 of the coverage-matrix epic surfaces this catalog
as a categorised checkbox list on the QA-harness trigger UI; operators tick
items that become MANDATORY for the chosen persona's run. Unticked items
stay in the persona's free-rein space. The catalog is a static module on
purpose: additions require code review and a PR, not an admin form.

LIVES IN ``qa-store`` (not ``qa_agents``) on purpose:
  * The review UI's API server needs to read this catalog to render the
    trigger-page checklist. ``qa_agents`` brings the entire Claude Agent
    SDK as a transitive dep; importing it just for ~100 lines of static
    data would balloon the review-ui image by ~50 MB.
  * ``qa_store`` is already a shared lightweight dep of both the harness
    and the review-ui API. Adding the catalog here gives both packages a
    single source of truth at zero new infra cost.
  * The module has NO qa-store / pymongo dependency — it's pure
    stdlib + a frozen dataclass. The placement is about reuse, not
    coupling to the store.

See #857 (epic) / #859 (initial creation) / #861 (move + UI consumption)
for context.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoverageAction:
    """One enumerated user-facing action a persona can be asked to attempt."""

    id: str
    """Stable slug — ``category.subcategory_word``, e.g. ``billing.upgrade_to_pro``."""

    category: str
    """One of the values in :data:`CATEGORIES`."""

    human_description: str
    """Operator-readable sentence — how an operator thinks about the action."""

    persona_compat: tuple[str, ...]
    """Subset of :data:`KNOWN_PERSONA_IDS` who could plausibly attempt this."""

    requires_auth: bool
    """``True`` if the user must be signed in to perform the action."""

    expected_outcome: str
    """What success looks like, one sentence — how 'I did it' is recognised."""


# Every persona in the harness registry (qa_agents/personas.py). All twelve
# are part of the catalog's universe so persona_compat tuples can address any
# of them. Lens-per-persona (from RunControl.vue's PERSONA_LABELS map):
#
#   margaret  — reluctant newcomer, low tech
#   daniel    — chat-native creative, compares to ChatGPT / Claude
#   priya     — power user, reads the fine print + fair-use abuse
#   tomas     — day-one admin, operates the console end-to-end (admin-only)
#   helen     — UK in-house counsel, legal / vendor due diligence
#   riley     — ethical security researcher, bug-bounty mindset
#   maya      — mobile-native, every screen at 390×844 on iPhone
#   brendan   — recipient of the AI reply, the OTHER end of the round-trip
#   siobhan   — Microsoft 365 / Outlook operator, ATP, threading, M365 quirks
#   amir      — B2B buyer, custom domain, multi-seat, SSO, audit (pre-purchase)
#   dmitri    — would-be abuser, Trust & Safety surface, scammer lens
#   catalina  — attachment-heavy bookkeeper, PDFs/DOCX/XLSX/CSV at all tiers
KNOWN_PERSONA_IDS: frozenset[str] = frozenset(
    {
        "margaret", "daniel", "helen", "maya", "priya", "riley", "tomas",
        "brendan", "siobhan", "amir", "dmitri", "catalina",
    }
)


CATEGORIES: tuple[str, ...] = (
    "auth",
    "agents",
    "playground",
    "billing",
    "account",
    "contact",
    "docs",
    "admin",
)


# Shorthand persona-compat tuples used below. Source order in CATALOG matches
# CATEGORIES; within each section entries are ordered roughly by how a user
# would meet them.
#
# `_ALL_USER_PERSONAS` is "anyone who behaves like a typical signed-up user
# completing this flow" — siobhan (M365 lens) and catalina (attachment-heavy
# lens) fit this shape just like the original six. tomas is admin-only and
# brendan is reply-recipient-only, so neither is in this shorthand; amir
# (pre-purchase prospect) and dmitri (abuser) are deliberately separate
# because their participation in any given flow is a per-entry call.
_ALL_USER_PERSONAS = (
    "margaret", "daniel", "helen", "maya", "priya", "riley",
    "siobhan", "catalina",
)
_PAYING_PERSONAS = ("daniel", "maya", "priya", "siobhan", "catalina")
_TECHNICAL_PERSONAS = ("daniel", "priya", "riley", "siobhan")

# Pre-purchase prospect — heavy on pricing, billing, legal pages, docs,
# templates. Only ever evaluates; doesn't actually transact in sandbox runs.
_PROSPECTS = ("amir",)
# Abusers — actively try to register/use the product in adversarial ways.
# Slot into entries where the abuse path is meaningful (register flood,
# disposable email, fair-use trip, account deletion probing, etc.).
_ABUSERS = ("dmitri",)
# Reply recipient — the OTHER end of the email round-trip. brendan doesn't
# do any of the typical user actions; he's the inbox-side of agent replies.
# Only the agents.external_sender_receives_reply entry uses this.
_REPLY_RECIPIENTS = ("brendan",)


CATALOG: tuple[CoverageAction, ...] = (
    # ─── auth ───────────────────────────────────────────────────────────
    CoverageAction(
        id="auth.view_landing_page",
        category="auth",
        human_description="Open the public landing page and read what SlyReply is for.",
        # Amir is a B2B prospect — landing is his FIRST interaction; this is
        # arguably the most important entry for him to attempt.
        persona_compat=_ALL_USER_PERSONAS + _PROSPECTS,
        requires_auth=False,
        expected_outcome="Landing page renders with hero, value prop, and primary CTA visible.",
    ),
    CoverageAction(
        id="auth.register_new_account",
        category="auth",
        human_description="Sign up for a new account with a fresh email address and password.",
        # Dmitri attempts register-flood / disposable-email patterns; the
        # signup gate is the main surface he probes (issue #827 layered
        # defenses live here). Amir is excluded — he evaluates pre-purchase
        # via marketing/docs/legal, doesn't actually sign up in sandbox.
        persona_compat=_ALL_USER_PERSONAS + _ABUSERS,
        requires_auth=False,
        expected_outcome="Account is created; user is redirected to the verify-pending screen.",
    ),
    CoverageAction(
        id="auth.complete_email_verification",
        category="auth",
        human_description="Open the verification email and follow the link to confirm the address.",
        persona_compat=_ALL_USER_PERSONAS,
        requires_auth=False,
        expected_outcome=(
            "Verification link logs the user in and lands them on "
            "/profile/overview?verified=true."
        ),
    ),
    CoverageAction(
        id="auth.resend_verification_email",
        category="auth",
        human_description=(
            "From the verify-pending screen, ask SlyReply to resend the verification "
            "email."
        ),
        persona_compat=("margaret", "daniel", "priya", "riley"),
        requires_auth=False,
        expected_outcome="A second verification email arrives; the link still works.",
    ),
    CoverageAction(
        id="auth.login_existing_user",
        category="auth",
        human_description="Sign in with an existing email and password from the login page.",
        persona_compat=_ALL_USER_PERSONAS + ("tomas",),
        requires_auth=False,
        expected_outcome=(
            "Login succeeds; user lands on /profile/overview with a session cookie "
            "set."
        ),
    ),
    CoverageAction(
        id="auth.logout",
        category="auth",
        human_description="Sign out from the in-product menu and confirm the session is gone.",
        persona_compat=_ALL_USER_PERSONAS + ("tomas",),
        requires_auth=True,
        expected_outcome=(
            "User is returned to the landing or login page and protected routes "
            "redirect to /login."
        ),
    ),
    CoverageAction(
        id="auth.request_password_reset",
        category="auth",
        human_description="Trigger the forgot-password flow and request a reset email.",
        persona_compat=("margaret", "daniel", "priya", "riley"),
        requires_auth=False,
        expected_outcome=(
            "Generic confirmation shown; a reset email with a working link arrives in "
            "the inbox."
        ),
    ),
    CoverageAction(
        id="auth.complete_password_reset",
        category="auth",
        human_description="Follow the reset link, choose a new password, and sign back in with it.",
        persona_compat=("margaret", "daniel", "priya", "riley"),
        requires_auth=False,
        expected_outcome=(
            "The new password works on the next login; the old one no longer "
            "authenticates."
        ),
    ),


    # ─── agents ─────────────────────────────────────────────────────────
    CoverageAction(
        id="agents.browse_template_catalog",
        category="agents",
        human_description="Browse the public agent catalog and filter or search by category.",
        persona_compat=_ALL_USER_PERSONAS,
        requires_auth=False,
        expected_outcome="Catalog grid renders; filters and search narrow the visible templates.",
    ),
    CoverageAction(
        id="agents.view_template_detail",
        category="agents",
        human_description=(
            "Open an agent template's detail page and read its description and "
            "capability."
        ),
        persona_compat=_ALL_USER_PERSONAS,
        requires_auth=False,
        expected_outcome=(
            "Detail page shows the template's prompt summary, model tier, and "
            "Activate CTA."
        ),
    ),
    CoverageAction(
        id="agents.activate_template",
        category="agents",
        human_description=(
            "Pick a template from the catalog and activate it as one of the user's "
            "own agents."
        ),
        persona_compat=("margaret", "daniel", "maya", "priya", "riley"),
        requires_auth=True,
        expected_outcome="New agent appears in My Agents with a unique slug@slyreply.ai address.",
    ),
    CoverageAction(
        id="agents.create_custom",
        category="agents",
        human_description=(
            "Create a custom AI agent from scratch — name, description, system "
            "prompt, model, capability."
        ),
        persona_compat=("daniel", "maya", "priya", "riley"),
        requires_auth=True,
        expected_outcome=(
            "The new agent is saved, has a working slug@slyreply.ai address, and "
            "shows in the list."
        ),
    ),
    CoverageAction(
        id="agents.list_my_agents",
        category="agents",
        human_description=(
            "Open the My Agents page and see every agent the user owns, including "
            "the wildcard."
        ),
        persona_compat=_ALL_USER_PERSONAS,
        requires_auth=True,
        expected_outcome="All non-deleted agents render; the wildcard catch-all is always present.",
    ),
    CoverageAction(
        id="agents.edit_system_prompt",
        category="agents",
        human_description=(
            "Open an existing agent and rewrite its system prompt to change how it "
            "answers."
        ),
        persona_compat=("daniel", "priya", "riley"),
        requires_auth=True,
        expected_outcome=(
            "Save succeeds; subsequent playground or email replies reflect the new "
            "prompt."
        ),
    ),
    CoverageAction(
        id="agents.change_model",
        category="agents",
        human_description="Switch an agent to a different AI model from the model picker.",
        persona_compat=_TECHNICAL_PERSONAS,
        requires_auth=True,
        expected_outcome=(
            "Model field updates and the agent's next reply is generated by the "
            "chosen model."
        ),
    ),
    CoverageAction(
        id="agents.set_capability",
        category="agents",
        human_description=(
            "Change the agent's capability (text, vision, document-analysis, "
            "image-generation)."
        ),
        persona_compat=("daniel", "priya", "riley"),
        requires_auth=True,
        expected_outcome=(
            "Capability is persisted and only models matching that capability are "
            "offered going forward."
        ),
    ),
    CoverageAction(
        id="agents.customize_avatar",
        category="agents",
        human_description="Reroll or customize an agent's avatar from the avatar picker.",
        persona_compat=("daniel", "maya"),
        requires_auth=True,
        expected_outcome=(
            "The new avatar persists across the agents list, playground, and email "
            "signature surfaces."
        ),
    ),
    CoverageAction(
        id="agents.reset_to_template_default",
        category="agents",
        human_description="Reset a customized template-derived agent back to its template default.",
        persona_compat=("daniel", "priya"),
        requires_auth=True,
        expected_outcome=(
            "Customizations are dropped; the agent re-renders with the template's "
            "original prompt and config."
        ),
    ),
    CoverageAction(
        id="agents.delete_agent",
        category="agents",
        human_description="Delete an agent so its address stops answering email.",
        persona_compat=("daniel", "priya", "riley"),
        requires_auth=True,
        expected_outcome=(
            "Agent disappears from the list and email to its address no longer "
            "produces an AI reply."
        ),
    ),
    CoverageAction(
        id="agents.recreate_with_same_slug",
        category="agents",
        human_description=(
            "After deleting an agent, create a new agent reusing the same slug to "
            "confirm the name is recyclable."
        ),
        persona_compat=("priya", "riley"),
        requires_auth=True,
        expected_outcome=(
            "The slug is accepted and a new agent is created at the same "
            "slug@slyreply.ai address."
        ),
    ),
    CoverageAction(
        id="agents.toggle_wildcard_catchall",
        category="agents",
        human_description=(
            "Enable or disable the per-account wildcard catch-all agent from the "
            "agents page."
        ),
        persona_compat=("daniel", "priya", "riley"),
        requires_auth=True,
        expected_outcome=(
            "Toggle state is persisted; unmatched local-parts either route to the "
            "wildcard or bounce accordingly."
        ),
    ),
    CoverageAction(
        id="agents.send_email_round_trip",
        category="agents",
        human_description=(
            "Send a real email to a configured agent's address and receive the AI's "
            "reply."
        ),
        persona_compat=_ALL_USER_PERSONAS,
        requires_auth=True,
        expected_outcome=(
            "A reply arrives in mailpit within ~60 seconds, addressed back to the "
            "sender and threaded."
        ),
    ),
    CoverageAction(
        id="agents.reply_to_existing_thread",
        category="agents",
        human_description=(
            "Reply to a previous AI response to test second-hop conversation "
            "threading."
        ),
        persona_compat=("daniel", "priya", "riley"),
        requires_auth=True,
        expected_outcome=(
            "The next reply lands in the same thread (In-Reply-To/References chain "
            "preserved)."
        ),
    ),
    CoverageAction(
        id="agents.unknown_sender_dropped",
        category="agents",
        human_description=(
            "Email an agent from an address NOT registered to the account and "
            "confirm the message is silently dropped."
        ),
        # Dmitri tries this as part of his scammer-lens probing — does the
        # sender-is-auth model leak that an address has an account, or
        # silently swallow? Riley probes the same surface from a bug-bounty
        # angle.
        persona_compat=("priya", "riley") + _ABUSERS,
        requires_auth=False,
        expected_outcome="No reply is generated and no error is leaked back to the unknown sender.",
    ),
    CoverageAction(
        id="agents.external_sender_receives_reply",
        category="agents",
        human_description=(
            "As an external customer (not the account holder), email a configured "
            "agent and judge the reply you receive on quality, tone, and timing."
        ),
        # Brendan IS the reply recipient — the OTHER end of the round-trip
        # the entire product is built around. He's the only persona whose
        # job is "be the customer the agent answers" rather than "be the
        # SlyReply user who configured the agent". No other persona belongs
        # here; their lens is always operator-of-SlyReply, not reply-recipient.
        persona_compat=_REPLY_RECIPIENTS,
        requires_auth=False,
        expected_outcome=(
            "A reply arrives in Brendan's inbox within ~2 minutes, reflecting the "
            "agent's configured tone, addressing the original question, and "
            "threading correctly via In-Reply-To / References headers."
        ),
    ),


    # ─── playground ─────────────────────────────────────────────────────
    CoverageAction(
        id="playground.open",
        category="playground",
        human_description="Open the playground page and confirm the three-column layout renders.",
        persona_compat=_ALL_USER_PERSONAS,
        requires_auth=False,
        expected_outcome=(
            "Playground loads with the agent picker, chat panel, and config panel all "
            "visible."
        ),
    ),
    CoverageAction(
        id="playground.run_prompt_against_own_agent",
        category="playground",
        human_description=(
            "Pick one of the user's own agents and send a prompt through the "
            "playground."
        ),
        persona_compat=_ALL_USER_PERSONAS,
        requires_auth=True,
        expected_outcome=(
            "A reply renders in the chat panel within a few seconds, using the "
            "agent's system prompt."
        ),
    ),
    CoverageAction(
        id="playground.run_prompt_anonymous_demo",
        category="playground",
        human_description=(
            "Use the public playground as an unauthenticated visitor against the "
            "demo agent."
        ),
        persona_compat=("margaret", "daniel", "maya", "priya"),
        requires_auth=False,
        expected_outcome=(
            "Reply renders without login; the remaining-uses counter decrements "
            "correctly."
        ),
    ),
    CoverageAction(
        id="playground.switch_agent",
        category="playground",
        human_description=(
            "Switch between two different agents inside the playground without "
            "leaving the page."
        ),
        persona_compat=("daniel", "priya", "riley"),
        requires_auth=True,
        expected_outcome=(
            "The agent picker swaps cleanly; the next reply reflects the "
            "newly-selected agent's prompt."
        ),
    ),
    CoverageAction(
        id="playground.attach_image_for_vision",
        category="playground",
        human_description=(
            "Attach an image to a vision-capable agent's playground turn and ask it "
            "to describe the image."
        ),
        persona_compat=("daniel", "priya"),
        requires_auth=True,
        expected_outcome=(
            "The reply references the image content, proving the attachment reached "
            "the vision-capable model."
        ),
    ),
    CoverageAction(
        id="playground.generate_image",
        category="playground",
        human_description=(
            "Run an image-generation agent in the playground and view the resulting "
            "image inline."
        ),
        persona_compat=("daniel", "maya"),
        requires_auth=True,
        expected_outcome="A generated image renders in the chat panel and can be downloaded.",
    ),
    CoverageAction(
        id="playground.check_remaining_quota",
        category="playground",
        human_description=(
            "Read the playground 'uses remaining' indicator to understand the "
            "per-day cap."
        ),
        persona_compat=("daniel", "priya", "riley"),
        requires_auth=False,
        expected_outcome=(
            "A remaining-uses count is visible and decrements after each playground "
            "run."
        ),
    ),


    # ─── billing ────────────────────────────────────────────────────────
    CoverageAction(
        id="billing.view_pricing_page",
        category="billing",
        human_description=(
            "Open the public pricing page and read the Free / Pro / Power tier "
            "comparison."
        ),
        # Amir reads this with B2B-evaluation intent (seat math, custom-domain
        # availability per tier, contract terms) — pricing-page coverage from
        # a prospect lens is qualitatively different from a typical user's.
        persona_compat=_ALL_USER_PERSONAS + _PROSPECTS,
        requires_auth=False,
        expected_outcome=(
            "All three tiers render with their fair-use copy and per-tier feature "
            "lists."
        ),
    ),
    CoverageAction(
        id="billing.view_current_plan",
        category="billing",
        human_description=(
            "Open the billing page and read the current subscription tier and "
            "renewal date."
        ),
        persona_compat=_ALL_USER_PERSONAS,
        requires_auth=True,
        expected_outcome=(
            "Billing page shows the user's tier, renewal date (if any), and a clear "
            "upgrade or manage CTA."
        ),
    ),
    CoverageAction(
        id="billing.upgrade_to_pro",
        category="billing",
        human_description=(
            "Upgrade from Free to Pro through the Revolut checkout with test card "
            "4929 4205 7359 5709."
        ),
        persona_compat=_PAYING_PERSONAS,
        requires_auth=True,
        expected_outcome=(
            "Checkout returns to /profile/billing with subscription_tier=pro "
            "visible."
        ),
    ),
    CoverageAction(
        id="billing.upgrade_to_power",
        category="billing",
        human_description=(
            "Upgrade to the Power tier through the Revolut checkout with test card "
            "4929 4205 7359 5709."
        ),
        persona_compat=("daniel", "priya"),
        requires_auth=True,
        expected_outcome=(
            "Subscription tier flips to power and the higher fair-use ceiling is "
            "reflected on /profile/billing."
        ),
    ),
    CoverageAction(
        id="billing.downgrade_plan",
        category="billing",
        human_description="Downgrade from Pro (or Power) back to Free from the billing page.",
        persona_compat=("daniel", "priya"),
        requires_auth=True,
        expected_outcome=(
            "Plan changes; the lower tier's limits take effect from the next billing "
            "cycle."
        ),
    ),
    CoverageAction(
        id="billing.cancel_subscription",
        category="billing",
        human_description="Cancel an active paid subscription so it does not renew.",
        persona_compat=("daniel", "priya", "helen"),
        requires_auth=True,
        expected_outcome=(
            "UI shows 'cancels on <date>'; no renewal charge happens; access "
            "continues until the period end."
        ),
    ),
    CoverageAction(
        id="billing.reactivate_subscription",
        category="billing",
        human_description=(
            "Reactivate a subscription that was previously cancelled but is still "
            "inside its paid period."
        ),
        persona_compat=("daniel", "priya"),
        requires_auth=True,
        expected_outcome=(
            "Cancellation flag is cleared; subscription will renew normally at the "
            "period end."
        ),
    ),
    CoverageAction(
        id="billing.update_payment_method",
        category="billing",
        human_description=(
            "Replace the saved card with a new Revolut sandbox test card via the "
            "payment-method setup flow."
        ),
        persona_compat=("daniel", "priya"),
        requires_auth=True,
        expected_outcome=(
            "Billing page shows the last-4 of the new card; future invoices charge "
            "it."
        ),
    ),
    CoverageAction(
        id="billing.view_invoices",
        category="billing",
        human_description=(
            "Open the invoices list on the billing page and confirm past charges are "
            "listed."
        ),
        persona_compat=("daniel", "priya", "helen"),
        requires_auth=True,
        expected_outcome=(
            "A list of paid invoices renders with amount, date, and a 'Download PDF' "
            "link per row."
        ),
    ),
    CoverageAction(
        id="billing.download_invoice_pdf",
        category="billing",
        human_description="Download an invoice PDF for a past charge from the invoices list.",
        persona_compat=("priya", "helen"),
        requires_auth=True,
        expected_outcome=(
            "A valid PDF downloads (or opens in-browser) with the invoice number and "
            "totals."
        ),
    ),
    CoverageAction(
        id="billing.trip_fair_use_cooldown",
        category="billing",
        human_description=(
            "Send a deliberate burst of emails to trip the fair-use cooldown and "
            "observe the user-facing signal."
        ),
        persona_compat=("priya", "riley"),
        requires_auth=True,
        expected_outcome=(
            "Somewhere in the burst, replies stop and the user sees either a 'limit "
            "hit' message or a clear bounce."
        ),
    ),


    # ─── account ────────────────────────────────────────────────────────
    CoverageAction(
        id="account.view_profile",
        category="account",
        human_description=(
            "Open /profile/account and confirm the display name and registered "
            "emails render."
        ),
        persona_compat=_ALL_USER_PERSONAS,
        requires_auth=True,
        expected_outcome=(
            "Profile page shows the user's name, primary email, verified state, and "
            "registered-emails list."
        ),
    ),
    CoverageAction(
        id="account.update_display_name",
        category="account",
        human_description=(
            "Change the user's display name from /profile/account and confirm the "
            "new value persists."
        ),
        persona_compat=("daniel", "priya", "riley"),
        requires_auth=True,
        expected_outcome=(
            "Name is saved; subsequent page renders and email signatures show the new "
            "name."
        ),
    ),
    CoverageAction(
        id="account.change_password",
        category="account",
        human_description=(
            "Change the account password from /profile/account by entering the "
            "current and new password."
        ),
        persona_compat=("daniel", "priya", "riley"),
        requires_auth=True,
        expected_outcome="Password change confirmed; the next login requires the new password.",
    ),
    CoverageAction(
        id="account.add_registered_email",
        category="account",
        human_description="Add a second registered email to the account from /profile/account.",
        persona_compat=("daniel", "priya"),
        requires_auth=True,
        expected_outcome=(
            "A verification email arrives at the new address; once verified it "
            "appears in the registered-emails list."
        ),
    ),
    CoverageAction(
        id="account.verify_new_email",
        category="account",
        human_description=(
            "Verify a newly-added secondary email by following the verification "
            "link."
        ),
        persona_compat=("daniel", "priya"),
        requires_auth=True,
        expected_outcome=(
            "The new address is marked verified and can be used as an authenticated "
            "sender."
        ),
    ),
    CoverageAction(
        id="account.remove_registered_email",
        category="account",
        human_description="Remove a secondary registered email from the account.",
        persona_compat=("priya",),
        requires_auth=True,
        expected_outcome=(
            "The address disappears from the list; email from it is treated as "
            "unknown-sender afterwards."
        ),
    ),
    CoverageAction(
        id="account.export_data",
        category="account",
        human_description=(
            "Request the GDPR data export (Art. 20 portability) from "
            "/profile/account."
        ),
        persona_compat=("helen", "priya", "riley"),
        requires_auth=True,
        expected_outcome=(
            "An export download (JSON or zip) is produced containing the user's "
            "stored data."
        ),
    ),
    CoverageAction(
        id="account.record_consent",
        category="account",
        human_description=(
            "Update the recorded consent (T&Cs / privacy) version from "
            "/profile/account."
        ),
        persona_compat=("helen",),
        requires_auth=True,
        expected_outcome=(
            "The stored consent version updates and is visible in the consent UI / "
            "data export."
        ),
    ),
    CoverageAction(
        id="account.delete_account",
        category="account",
        human_description=(
            "Find the Delete Account flow and walk it as far as the final "
            "confirmation (without confirming)."
        ),
        # Helen probes for DSR/GDPR. Priya wants retention/deletion controls
        # at the fair-use surface. Dmitri probes deletion as part of the
        # abuse-lifecycle exploration (account-rotation patterns).
        persona_compat=("helen", "priya") + _ABUSERS,
        requires_auth=True,
        expected_outcome=(
            "The flow is discoverable and the final-confirmation step is reached and "
            "described clearly."
        ),
    ),
    CoverageAction(
        id="account.open_support_ticket",
        category="account",
        human_description=(
            "Open an in-product support ticket from /profile/support and send the "
            "first message."
        ),
        persona_compat=("margaret", "daniel", "priya"),
        requires_auth=True,
        expected_outcome=(
            "Ticket is created, visible in 'My tickets', and the admin notification "
            "fires."
        ),
    ),


    # ─── contact ────────────────────────────────────────────────────────
    CoverageAction(
        id="contact.submit_contact_form",
        category="contact",
        human_description="Submit the public /contact form with name, email, topic, and message.",
        # Amir reaches out via the sales-contact path for SSO / multi-seat /
        # custom-domain questions before deciding to purchase.
        persona_compat=("margaret", "daniel", "helen", "priya") + _PROSPECTS,
        requires_auth=False,
        expected_outcome="Form returns a success state; an admin notification email is dispatched.",
    ),
    CoverageAction(
        id="contact.view_legal_pages",
        category="contact",
        human_description=(
            "Open Privacy, Terms, DPA, Security, and Cookie pages from the footer "
            "and read them."
        ),
        # Amir's whole job is pre-purchase due diligence — Privacy, DPA, and
        # Security pages are his core read. Adding him here is what makes
        # this entry meaningful for B2B coverage.
        persona_compat=("helen", "priya", "riley") + _PROSPECTS,
        requires_auth=False,
        expected_outcome=(
            "Each linked legal page renders with substantive content and a "
            "last-updated marker."
        ),
    ),


    # ─── docs ───────────────────────────────────────────────────────────
    CoverageAction(
        id="docs.browse_index",
        category="docs",
        human_description="Open /docs and browse the documentation index by category.",
        persona_compat=_ALL_USER_PERSONAS,
        requires_auth=False,
        expected_outcome=(
            "Docs index lists categories and articles; clicking through reaches a "
            "readable doc page."
        ),
    ),
    CoverageAction(
        id="docs.read_single_doc",
        category="docs",
        human_description=(
            "Open a single docs article (e.g. 'How SlyReply Works') and confirm it "
            "renders end-to-end."
        ),
        persona_compat=_ALL_USER_PERSONAS,
        requires_auth=False,
        expected_outcome="The article renders with title, prose, and working in-page anchors.",
    ),


    # ─── admin ──────────────────────────────────────────────────────────
    CoverageAction(
        id="admin.login_as_admin",
        category="admin",
        human_description=(
            "Sign in as the seeded sandbox administrator on /login and reach the "
            "admin console."
        ),
        persona_compat=("tomas",),
        requires_auth=False,
        expected_outcome=(
            "Admin lands on /admin with the admin layout visible (not the user "
            "profile shell)."
        ),
    ),
    CoverageAction(
        id="admin.view_dashboard",
        category="admin",
        human_description=(
            "Open /admin and read the dashboard summary (signups, spend, abuse "
            "signals)."
        ),
        persona_compat=("tomas",),
        requires_auth=True,
        expected_outcome=(
            "Dashboard renders with the headline numbers and links into the deeper "
            "pages."
        ),
    ),
    CoverageAction(
        id="admin.search_user_by_email",
        category="admin",
        human_description=(
            "On /admin/users, find a specific user by email and open their detail "
            "page."
        ),
        persona_compat=("tomas",),
        requires_auth=True,
        expected_outcome=(
            "User detail page loads with their tier, registered emails, and recent "
            "activity."
        ),
    ),
    CoverageAction(
        id="admin.suspend_user",
        category="admin",
        human_description=(
            "Suspend a user from their detail page so they can no longer "
            "authenticate."
        ),
        persona_compat=("tomas",),
        requires_auth=True,
        expected_outcome="User is marked suspended; their next login attempt is refused.",
    ),
    CoverageAction(
        id="admin.adjust_app_settings",
        category="admin",
        human_description=(
            "Open /admin/settings and toggle a feature flag or change a numeric "
            "setting."
        ),
        persona_compat=("tomas",),
        requires_auth=True,
        expected_outcome=(
            "Setting persists and the rest of the app reflects the new value on the "
            "next request."
        ),
    ),
    CoverageAction(
        id="admin.review_fair_use_dashboard",
        category="admin",
        human_description="Open /admin/fair-use and identify a user who tripped fair use today.",
        persona_compat=("tomas",),
        requires_auth=True,
        expected_outcome=(
            "Page lists fair-use trips with timestamps and a user-linkable row per "
            "offender."
        ),
    ),
    CoverageAction(
        id="admin.inspect_inbound_logs",
        category="admin",
        human_description="Open /admin/inbound and find a blocked inbound email from a chosen day.",
        persona_compat=("tomas",),
        requires_auth=True,
        expected_outcome=(
            "Inbound list renders with status filtering; a blocked entry's reason is "
            "readable."
        ),
    ),
    CoverageAction(
        id="admin.read_audit_logs",
        category="admin",
        human_description=(
            "Open /admin/audit-logs and confirm the admin's own recent actions "
            "appear in the trail."
        ),
        persona_compat=("tomas",),
        requires_auth=True,
        expected_outcome=(
            "Audit-log page shows recent admin actions, including ones the current "
            "session just took."
        ),
    ),
)
