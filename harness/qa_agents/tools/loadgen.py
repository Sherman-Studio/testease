"""In-process MCP server for the internal load-test persona (Nadia).

The ``blast`` tool fires a batch of emails at ONE SlyReply UID address
through the REAL inbound SMTP pipeline — the same ``send_smtp`` path the
email tool uses, so every message actually reaches ``process_inbound``,
generates real provider spend, and counts against the tenant's fair-use
budget. This is how the ``internal-load-economist`` persona drives the
volume a single browser-clicking agent could never reach (dozens/hour vs.
hundreds/minute), which is the whole point of the economics run.

Why a separate server from ``qa_agents.tools.email``: the email tool is
shaped for a *customer* persona testing one round-trip at a time and is
in the default-enabled catalog set. ``loadgen`` is a privileged,
insider-only capability (``default_enabled=False``,
``persona_compat=["internal-load-economist"]``) so it never wires into a
normal customer persona's toolset.

Sends are sync (``smtplib``) so each is run in a worker thread via
``asyncio.to_thread`` and fanned out under a small concurrency cap — the
QA tenant is exempted from the per-minute/per-IP transport throttles (the
run sets ``QA_TOKEN``), so the only ceiling we want to hit is fair-use,
not the SMTP front door.
"""

from __future__ import annotations

import asyncio

from .email import _resolve_attachment_path, send_smtp

# Per-call ceiling. A single blast() is one tool call; keeping it bounded
# means the persona pages through volume in observable chunks (and the
# transcript stays legible) rather than firing an unbounded flood the
# operator can't reason about. To go bigger, the persona calls blast again.
_MAX_BLAST = 200

# How many sends are in flight at once. Generous (the tenant is throttle-
# exempt) but not unbounded — we don't want to exhaust the SMTP server's
# connection limiter, which would turn a fair-use test into a transport
# test. The drain still completes _MAX_BLAST sends; this only caps fan-out.
_BLAST_CONCURRENCY = 10

# Built-in prompt pools, one per ``kind``. Used when the persona doesn't
# supply its own ``prompt_pool``. Deliberately varied so identical bodies
# don't let any caching shortcut the provider cost we're trying to measure.
_DEFAULT_POOLS: dict[str, list[str]] = {
    "text": [
        "Can you summarise the key points a customer should know about our refund policy?",
        "Draft a friendly reply to a customer asking where their order is.",
        "Write a short out-of-office auto-reply for the holidays.",
        "A client asked for a quote on 200 units — draft a professional response.",
        "Reply to a complaint about a late delivery, apologetic but not over-promising.",
        "Explain our 30-day return window in two sentences.",
    ],
    "image": [
        "Generate a flat-vector logo of a friendly fox holding a coffee mug.",
        "Create a minimalist hero image for a coffee subscription landing page.",
        "Draw a simple line-art icon set: cart, truck, gift box.",
        "Produce a watercolour-style banner of a mountain sunrise.",
        "Generate a product mockup of a matte-black water bottle on a wooden desk.",
    ],
    "attachment": [
        "What's the total on this invoice and when is it due?",
        "Pull the line items out of the attached receipt.",
        "Summarise the figures in the attached spreadsheet.",
        "Is anything unusual about the attached document?",
    ],
}

# Default fixture attachments per kind (basenames from the fixture pack).
# Only used for kind="attachment" when the persona doesn't pass its own.
_DEFAULT_ATTACHMENTS = "sample-invoice.pdf"


def _resolve_pool(kind: str, prompt_pool: str) -> list[str]:
    """Parse the operator-supplied pool, falling back to the kind default.

    ``prompt_pool`` is a ``||``-separated list of message bodies (``||``
    rather than newline/comma so a body can itself contain those). Empty
    → the built-in pool for ``kind`` (or the text pool if ``kind`` is
    unknown, so a typo still produces traffic rather than nothing).
    """
    bodies = [s.strip() for s in prompt_pool.split("||") if s.strip()]
    if bodies:
        return bodies
    return _DEFAULT_POOLS.get(kind, _DEFAULT_POOLS["text"])


async def run_blast(
    *,
    smtp_host: str,
    smtp_port: int,
    persona_from_address: str,
    args: dict,
) -> dict:
    """Core of the ``blast`` tool, extracted so it's unit-testable without
    poking the SDK-decorated handler.

    Validates the args, builds the body/attachment plan, fans the sends out
    under :data:`_BLAST_CONCURRENCY`, and returns the MCP content envelope
    summarising sent/failed. Never raises — a per-send failure is captured
    and surfaced in the summary, an arg/fixture error returns an ``ERROR:``
    string the persona can react to.
    """
    to_addr = str(args.get("to", "")).strip()
    count = int(args.get("count") or 0)
    kind = str(args.get("kind", "text")).strip().lower() or "text"
    prompt_pool = str(args.get("prompt_pool", "") or "")

    if not to_addr:
        return _text("ERROR: 'to' (UID address) is required.")
    if count <= 0:
        return _text("ERROR: 'count' must be a positive integer.")
    if count > _MAX_BLAST:
        return _text(
            f"ERROR: count={count} exceeds the per-call cap of "
            f"{_MAX_BLAST}. Call blast again to send more."
        )

    bodies = _resolve_pool(kind, prompt_pool)

    # Resolve attachments once, up front, so a bad fixture name fails the
    # whole call cleanly instead of mid-flood.
    raw_attachments = str(args.get("attachments", "") or "")
    if not raw_attachments and kind == "attachment":
        raw_attachments = _DEFAULT_ATTACHMENTS
    attachment_names = [s.strip() for s in raw_attachments.split(",") if s.strip()]
    try:
        attachment_paths = [_resolve_attachment_path(n) for n in attachment_names]
    except (ValueError, FileNotFoundError) as exc:
        return _text(f"ERROR resolving attachment: {exc}")

    sem = asyncio.Semaphore(_BLAST_CONCURRENCY)

    async def _one(i: int) -> tuple[bool, str | None, str | None]:
        body = bodies[i % len(bodies)]
        subject = f"[loadtest {kind} #{i + 1}] {body[:48]}"
        async with sem:
            try:
                mid = await asyncio.to_thread(
                    send_smtp,
                    host=smtp_host,
                    port=smtp_port,
                    from_addr=persona_from_address,
                    to_addr=to_addr,
                    subject=subject,
                    body=body,
                    attachments=attachment_paths or None,
                )
                return (True, mid, None)
            except Exception as exc:  # noqa: BLE001 — report, don't crash the run
                return (False, None, repr(exc))

    results = await asyncio.gather(*[_one(i) for i in range(count)])

    sent = [r for r in results if r[0]]
    failed = [r for r in results if not r[0]]
    sample_ids = [r[1] for r in sent[:3] if r[1]]
    # Distinct error strings (first occurrence) so a single recurring SMTP
    # fault doesn't bury the summary in identical lines.
    seen_errors: list[str] = []
    for r in failed:
        if r[2] and r[2] not in seen_errors:
            seen_errors.append(r[2])
        if len(seen_errors) >= 3:
            break

    lines = [
        f"blast complete: {len(sent)}/{count} sent, {len(failed)} failed.",
        f"  from={persona_from_address}  to={to_addr}  kind={kind}",
        f"  bodies cycled: {len(bodies)}"
        + (
            f"  attachments: {', '.join(p.name for p in attachment_paths)}"
            if attachment_paths
            else ""
        ),
    ]
    if sample_ids:
        lines.append(f"  sample message-ids: {', '.join(sample_ids)}")
    if seen_errors:
        lines.append("  error samples:")
        lines.extend(f"    - {e}" for e in seen_errors)
    lines.append(
        "Now read mcp__cost__cost_report + mcp__cost__usage_summary to see "
        "what this volume cost, and watch for fair-use cooldown / backstop / "
        "cost-ceiling responses arriving by email."
    )
    return _text("\n".join(lines))


def build_loadgen_server(
    *,
    smtp_host: str,
    smtp_port: int,
    persona_from_address: str,
):
    """Build the in-process MCP server exposing the ``blast`` tool.

    Mirrors :func:`qa_agents.tools.email.build_email_server`'s SMTP
    parameters — ``persona_from_address`` is the registered (authenticated)
    address the volume is sent from. Returns ``(server, tool_names)`` to
    match the other ``build_*_server`` helpers; the runner qualifies the
    names as ``mcp__loadgen__<name>``.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "blast",
        "Fire a BATCH of emails at one of your SlyReply UID addresses "
        "through the real inbound pipeline, to drive volume. Each message "
        "is a genuine send that hits process_inbound, costs real provider "
        "money, and counts against fair-use. Args: 'to' (the UID address, "
        "e.g. support@slyreply.ai); 'count' (how many, capped at 200 per "
        "call); 'kind' (one of text|image|attachment — picks a built-in "
        "prompt pool and, for attachment, attaches a fixture); optional "
        "'prompt_pool' (a '||'-separated list of message bodies to cycle "
        "through — overrides the built-in pool); optional 'attachments' "
        "(comma-separated fixture basenames, for kind=attachment). Returns "
        "a summary of how many sent vs failed, with sample message-ids and "
        "error samples. Send a smaller batch first, watch the cost/usage "
        "tools, then scale up.",
        {
            "to": str,
            "count": int,
            "kind": str,
            "prompt_pool": str,
            "attachments": str,
        },
    )
    async def blast(args: dict) -> dict:
        return await run_blast(
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            persona_from_address=persona_from_address,
            args=args,
        )

    server = create_sdk_mcp_server(name="loadgen", tools=[blast])
    return server, ["blast"]


def _text(text: str) -> dict:
    """Wrap a string in the MCP tool content envelope."""
    return {"content": [{"type": "text", "text": text}]}
