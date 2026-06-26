"""Email tools — real SMTP send + Mailpit-API read for the persona agent.

The spike (qa-agents/spike/roundtrip.py) proved two things this module carries
forward:

1. Sending in is plain SMTP straight at the inbound aiosmtpd service; the
   ``MAIL FROM`` envelope address must be one of the persona's *registered*
   addresses, because inbound auth is sender-is-auth.
2. The mail sink hands back **raw transfer-encoded** content — quoted-printable
   or base64 bodies, RFC-2047 encoded-word headers. Every body and header MUST
   be MIME-decoded before the agent sees it, or it reads gibberish.

Mailpit's HTTP API differs slightly from MailHog's (used in the spike):
``GET /api/v1/messages`` lists, ``GET /api/v1/message/{id}`` fetches one fully
decoded. We still decode defensively in case a future sink does not.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import quopri
import smtplib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from pathlib import Path

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

# #1115 — root of the shared fixture pack the attachment-aggressor persona
# (Cătălina) references by RELATIVE name. We anchor at the package
# directory so the path resolves the same in dev (running from the
# checkout) and in the Docker image (where the fixtures are baked into
# the image alongside the Python package).
_FIXTURE_ROOT = (Path(__file__).resolve().parent.parent / "fixtures" / "attachments")
# Per-message attachment cap. Real SlyReply tiers cap at 3-4
# attachments; the harness allows a few more to exercise the count gate
# (the 5th attachment is the "rejected, lists which got skipped" probe).
_MAX_ATTACHMENTS_PER_MESSAGE = 8
# Single-attachment size cap — sane upper bound so a typo (e.g. asking
# for a 4 GB file) doesn't OOM the harness pod. The biggest deliberate
# fixture is sample-invoice-large.pdf (~3.8 MB). 30 MB covers padding
# up to the Power-tier boundary (~27 MB) with headroom.
_MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024
# #1109 slice 2 — inline download threshold. Files under this size are
# returned base64-encoded inside the download_attachment response so
# the agent can verify the bytes directly. Files over are referenced
# only by URL — including 1 MB of base64 in a tool response would
# burn the agent's context for minimal gain (the sha256 + size are
# almost always enough to verify a re-send). 64 KB lets us inline
# typical text files, CSVs, small JPEGs, and the harness's smallest
# fixture PDFs.
_DOWNLOAD_INLINE_THRESHOLD = 64 * 1024
# #1109 slice 4 — staging directory for prepare_upload_path. Files
# materialised here are paths the persona can pass to the playwright
# MCP's ``browser_file_upload`` tool. Lives under tempfile.gettempdir()
# so it's wiped on pod restart; the dir is created lazily on first
# write so unit tests don't litter the system temp tree.
_UPLOAD_STAGING_SUBDIR = "qa-agents-uploads"


def _upload_staging_dir() -> Path:
    """Return the path used for prepare_upload_path output, creating it
    on first call. Centralised so tests can patch a fixture-friendly
    location via monkeypatch on this function.
    """
    from tempfile import gettempdir  # noqa: PLC0415
    root = Path(gettempdir()) / _UPLOAD_STAGING_SUBDIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_basename(name: str) -> str:
    """Sanitise a filename for the staging dir.

    Drops path separators and ``..`` segments so a hostile filename
    coming back from Mailpit can't escape the staging root. Returns
    ``"file.bin"`` for empty / fully-stripped names — we still want
    SOME extension so playwright's file-upload tool routes the
    multipart correctly.
    """
    cleaned = (name or "").strip().replace("\\", "/").split("/")[-1]
    cleaned = cleaned.replace("..", "_")
    return cleaned or "file.bin"


def _resolve_attachment_path(name: str) -> Path:
    """Resolve a fixture-pack-relative attachment name to an absolute path.

    The persona passes file basenames like ``sample-invoice.pdf``; we
    refuse anything that escapes the fixture root (path traversal) so a
    confused persona can't read arbitrary files off the harness pod.
    """
    if not name:
        raise ValueError("attachment name is empty")
    cleaned = name.strip()
    if cleaned.startswith("/") or ".." in Path(cleaned).parts:
        raise ValueError(
            f"attachment {cleaned!r} must be a basename inside the "
            f"fixture pack, not a path"
        )
    resolved = (_FIXTURE_ROOT / cleaned).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(
            f"attachment {cleaned!r} not found in fixture pack "
            f"({_FIXTURE_ROOT}); available names are listed in "
            f"qa-agents/harness/qa_agents/fixtures/attachments/README.md"
        )
    # Defence in depth — resolve() collapses symlinks, so explicit
    # is_relative_to check guards a hostile symlink in the fixture dir.
    if not str(resolved).startswith(str(_FIXTURE_ROOT.resolve())):
        raise ValueError(
            f"attachment {cleaned!r} resolves outside the fixture pack"
        )
    return resolved


def _sniff_content_type(path: Path) -> tuple[str, str]:
    """Return ``(maintype, subtype)`` for an attachment path.

    Uses Python's mimetypes for the easy cases; falls back to
    application/octet-stream for the unknowns (the .heic case if we
    ever ship one). The DOCX-bytes-with-pdf-extension fixture
    (``sample-mislabeled.pdf``) is INTENTIONALLY routed through the
    extension-based sniff so the SlyReply pipeline gets the
    mismatch it's meant to catch — this is the "trust the filename"
    probe, not a bug in the helper.
    """
    guess, _ = mimetypes.guess_type(str(path))
    if guess and "/" in guess:
        maintype, subtype = guess.split("/", 1)
        return maintype, subtype
    return "application", "octet-stream"


# --------------------------------------------------------------------------
# Pure helpers — unit-tested directly, no network.
# --------------------------------------------------------------------------
def decode_mime_header(raw: str | None) -> str:
    """Decode an RFC-2047 encoded-word header into a plain string."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # pragma: no cover - defensive
        return raw


def decode_transfer_encoded_body(body: str | None, transfer_encoding: str | None) -> str:
    """Decode a quoted-printable / base64 body string to readable text.

    Mirrors ``qa-agents/spike/roundtrip.py``. If the encoding is unknown or
    decoding fails, the body is returned unchanged — a readable approximation
    beats an exception.
    """
    if not body:
        return ""
    cte = (transfer_encoding or "").strip().lower()
    try:
        if cte == "quoted-printable":
            return quopri.decodestring(body.encode()).decode("utf-8", "replace")
        if cte == "base64":
            return base64.b64decode(body).decode("utf-8", "replace")
    except Exception:  # pragma: no cover - defensive
        return body
    return body


@dataclass
class EmailMessageView:
    """A decoded, agent-friendly view of one captured email.

    ``message_id_header`` is the SMTP ``Message-ID`` header (distinct
    from Mailpit's internal ``id``) — needed for RFC 5322 threading so
    that ``reply_in_thread`` can set ``In-Reply-To`` correctly. #1109.

    ``references`` is the parsed ``References`` header chain (a list of
    angle-bracketed message-id tokens, oldest first). A reply must
    re-emit this chain plus the current Message-ID so threaded clients
    (Mailpit, Apple Mail, Gmail) keep the conversation grouped.
    """

    id: str
    from_addr: str
    to_addrs: list[str]
    subject: str
    text_body: str
    created_at: str = ""
    message_id_header: str = ""
    references: list[str] = field(default_factory=list)
    # #1109 slice 2 — attachment metadata pulled from Mailpit's
    # ``Attachments`` array on the single-message endpoint. Each entry
    # is ``{attachment_id, filename, content_type, size_bytes}``. Empty
    # list for messages with no attachments — the common case.
    attachments: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        recipients = ", ".join(self.to_addrs) or "(none)"
        return (
            f"id: {self.id}\n"
            f"from: {self.from_addr}\n"
            f"to: {recipients}\n"
            f"subject: {self.subject}\n"
            f"date: {self.created_at}\n"
            f"---\n{self.text_body.strip()}"
        )


def _addr_list(entries: object) -> list[str]:
    """Normalise Mailpit address objects (``{Name, Address}``) to strings."""
    out: list[str] = []
    if not isinstance(entries, list):
        return out
    for e in entries:
        if isinstance(e, dict):
            addr = e.get("Address") or e.get("address") or ""
            if addr:
                out.append(addr.lower())
        elif isinstance(e, str):
            out.append(e.lower())
    return out


def parse_mailpit_message(raw: dict) -> EmailMessageView:
    """Turn one Mailpit message JSON object into an ``EmailMessageView``.

    Handles both the list-endpoint shape and the single-message shape, and
    decodes the body if the sink handed back transfer-encoded content.
    """
    msg_id = str(raw.get("ID") or raw.get("Id") or raw.get("id") or "")

    from_obj = raw.get("From")
    if isinstance(from_obj, dict):
        from_addr = (from_obj.get("Address") or "").lower()
    elif isinstance(from_obj, str):
        from_addr = from_obj.lower()
    else:
        from_addr = ""

    to_addrs = _addr_list(raw.get("To"))

    subject = decode_mime_header(raw.get("Subject"))

    # Single-message endpoint exposes a decoded ``Text`` field. The list
    # endpoint exposes a ``Snippet``. Fall back to a transfer-encoded body.
    text_body = raw.get("Text")
    if not text_body:
        cte = raw.get("Content-Transfer-Encoding") or raw.get("ContentTransferEncoding")
        text_body = decode_transfer_encoded_body(raw.get("Body"), cte)
    if not text_body:
        text_body = raw.get("Snippet") or ""

    created_at = str(raw.get("Created") or raw.get("Date") or "")

    # #1109 — threading-header capture. Mailpit's single-message
    # endpoint exposes the raw SMTP ``Message-ID`` as ``MessageID``;
    # the ``References`` chain lives under ``Headers`` (a dict whose
    # values can be either a single string or a list of strings).
    # Both fields are optional — list-endpoint responses don't carry
    # them — so missing values degrade gracefully to "".
    message_id_header = str(
        raw.get("MessageID") or raw.get("messageId") or ""
    ).strip()
    references = _parse_references_header(raw.get("Headers") or {})
    attachments = _parse_attachments(raw.get("Attachments"))

    return EmailMessageView(
        id=msg_id,
        from_addr=from_addr,
        to_addrs=to_addrs,
        subject=subject,
        text_body=text_body,
        created_at=created_at,
        message_id_header=message_id_header,
        references=references,
        attachments=attachments,
    )


def _parse_references_header(headers: dict) -> list[str]:
    """Pull the ``References`` chain from a Mailpit ``Headers`` dict.

    Mailpit's headers dict maps header name → value, where the value is
    either a string or a list of strings (when the same header appears
    multiple times). RFC 5322's References header is a whitespace-
    separated list of angle-bracketed message-ids. We split on
    whitespace and keep only tokens that look like ``<...>``, so
    malformed entries don't poison downstream threading.
    """
    if not isinstance(headers, dict):
        return []
    raw = headers.get("References") or headers.get("references") or ""
    if isinstance(raw, list):
        raw = " ".join(str(x) for x in raw)
    tokens = [t.strip() for t in str(raw).split()]
    return [t for t in tokens if t.startswith("<") and t.endswith(">")]


def _parse_attachments(raw_attachments: object) -> list[dict]:
    """Normalise Mailpit's ``Attachments`` array into the agent shape.

    Mailpit emits each attachment as ``{PartID, FileName, ContentType,
    ContentID, Size}`` on the single-message endpoint. We reshape to
    snake_case + drop fields the agent surface doesn't need (ContentID
    is for inline-image references, separate from file attachments).

    Skips entries that lack a ``PartID`` — without it the download
    endpoint can't be addressed and the metadata is useless anyway.
    Returns ``[]`` for None / non-list / empty input.
    """
    if not isinstance(raw_attachments, list):
        return []
    out: list[dict] = []
    for entry in raw_attachments:
        if not isinstance(entry, dict):
            continue
        part_id = str(entry.get("PartID") or entry.get("partId") or "").strip()
        if not part_id:
            continue
        out.append({
            "attachment_id": part_id,
            "filename": str(entry.get("FileName") or entry.get("Filename") or ""),
            "content_type": str(entry.get("ContentType") or "application/octet-stream"),
            "size_bytes": int(entry.get("Size") or 0),
        })
    return out


# --------------------------------------------------------------------------
# Mailpit HTTP client — thin wrapper over the v1 API.
# --------------------------------------------------------------------------
class MailpitClient:
    """Minimal Mailpit API client (list / fetch)."""

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def list_messages(self, limit: int = 50) -> list[dict]:
        url = f"{self.base_url}/api/v1/messages"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url, params={"limit": limit})
            resp.raise_for_status()
            data = resp.json() or {}
        return list(data.get("messages", []))

    def get_message(self, message_id: str) -> dict:
        url = f"{self.base_url}/api/v1/message/{message_id}"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json() or {}

    def get_part(self, message_id: str, part_id: str) -> tuple[bytes, str]:
        """Fetch an attachment's raw bytes by Mailpit's PartID.

        Returns ``(bytes, content_type)`` so callers can decide how to
        present the data. Mailpit responds with the original
        ``Content-Type`` header from the email; we fall back to
        ``application/octet-stream`` when missing.

        #1109 slice 2 — used by the ``download_attachment`` MCP tool.
        """
        url = f"{self.base_url}/api/v1/message/{message_id}/part/{part_id}"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content, resp.headers.get(
                "Content-Type", "application/octet-stream",
            )

    def part_url(self, message_id: str, part_id: str) -> str:
        """Return the Mailpit URL that serves an attachment's bytes.

        The persona's playwright MCP tool can navigate to this URL to
        download the file via the browser, or the harness can fetch
        it directly via :meth:`get_part`. Useful for "verify the
        agent's reply contains the same attachment I sent" flows.
        """
        return f"{self.base_url}/api/v1/message/{message_id}/part/{part_id}"


def _parse_created(raw: dict) -> datetime | None:
    """Parse a Mailpit message's ``Created`` timestamp to an aware datetime.

    Returns ``None`` if there is no timestamp or it does not parse — callers
    treat that as "age unknown" rather than dropping the message.
    """
    value = raw.get("Created") or raw.get("Date") or ""
    if not value:
        return None
    try:
        # Mailpit emits RFC 3339 with a trailing 'Z'; fromisoformat handles
        # that on Python 3.11+.
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def find_newest_for(
    messages: list[dict],
    to_address: str,
    *,
    not_before: datetime | None = None,
) -> dict | None:
    """Return the newest message JSON addressed to ``to_address``.

    Mailpit returns the list newest-first, so the first match wins. When
    ``not_before`` is given, messages created at or before it are skipped —
    this stops a stale message left in the sink by an earlier run from
    satisfying a wait. A message with no parseable ``Created`` timestamp is
    NOT skipped: losing a real reply is worse than tolerating a stale one.
    """
    target = to_address.lower()
    for raw in messages:
        if target not in _addr_list(raw.get("To")):
            continue
        if not_before is not None:
            created = _parse_created(raw)
            if created is not None and created <= not_before:
                continue
        return raw
    return None


# --------------------------------------------------------------------------
# SMTP send.
# --------------------------------------------------------------------------
def send_smtp(
    *,
    host: str,
    port: int,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    attachments: list[Path] | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    timeout: float = 15.0,
) -> str:
    """Send a plain-text email (optionally with attachments) to the
    inbound SMTP server.

    Returns the generated Message-ID. The envelope ``MAIL FROM`` is
    ``from_addr`` — which must be a registered address for the persona.

    ``attachments`` is a list of resolved fixture-pack paths
    (#1115 — for the attachment-aggressor persona). When present, the
    helper reads each file and attaches it with a sniffed content
    type. Callers are expected to have resolved + sanity-checked the
    paths via :func:`_resolve_attachment_path`.

    ``in_reply_to`` + ``references`` (#1109) carry RFC 5322 threading.
    Set both when sending a reply so Mailpit and downstream clients
    keep the message in the same conversation. The current Message-ID
    is appended to ``references`` automatically by the receiving
    threader; we only emit what the caller asked for.
    """
    message_id = f"<qa-{int(time.time() * 1000)}@qa-agents.slyreply.test>"
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    msg.set_content(body)
    for path in (attachments or []):
        data = path.read_bytes()
        if len(data) > _MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"attachment {path.name!r} is {len(data)} bytes — over "
                f"the {_MAX_ATTACHMENT_BYTES} byte harness cap"
            )
        maintype, subtype = _sniff_content_type(path)
        msg.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        smtp.send_message(msg)
    return message_id


# --------------------------------------------------------------------------
# Threading helpers (#1109).
# --------------------------------------------------------------------------
def build_reply_threading(original: EmailMessageView) -> tuple[str, list[str]]:
    """Compute ``(in_reply_to, references)`` for a reply to ``original``.

    Follows RFC 5322 §3.6.4 + 5536 §3.2.3. The reply's ``In-Reply-To``
    is the original's Message-ID; the reply's ``References`` chain is
    the original's References chain plus the original's Message-ID
    (so each link in the chain remembers the conversation root).

    If the original has no Message-ID header (legacy / stripped /
    bare-list endpoint), returns ``("", [])`` — callers should not
    set threading headers in that case; the reply will start a new
    thread, which is the least-surprising fallback.
    """
    mid = (original.message_id_header or "").strip()
    if not mid:
        return "", []
    chain = list(original.references or [])
    if mid not in chain:
        chain.append(mid)
    return mid, chain


def build_reply_subject(original_subject: str) -> str:
    """Return ``original_subject`` prefixed with ``Re:`` if needed.

    Idempotent — replying to a reply does NOT produce ``Re: Re: Re:``.
    Comparison is case-insensitive so ``RE:`` / ``re:`` collapse too.
    """
    s = (original_subject or "").strip()
    if not s:
        return "Re:"
    if s.lower().startswith("re:"):
        return s
    return f"Re: {s}"


def build_forward_subject(original_subject: str) -> str:
    """Return ``original_subject`` prefixed with ``Fwd:`` if needed."""
    s = (original_subject or "").strip()
    if not s:
        return "Fwd:"
    lower = s.lower()
    if lower.startswith("fwd:") or lower.startswith("fw:"):
        return s
    return f"Fwd: {s}"


def build_forward_body(original: EmailMessageView, new_body: str) -> str:
    """Quote ``original`` below ``new_body`` in standard forward format.

    Matches what mainstream clients (Apple Mail, Gmail) emit so the
    AI on the receiving end can parse out the inline reply heuristic
    via the ``---------- Forwarded message ---------`` marker.
    """
    quoted = (original.text_body or "").rstrip()
    sep = "---------- Forwarded message ----------"
    header = (
        f"From: {original.from_addr}\n"
        f"Date: {original.created_at}\n"
        f"Subject: {original.subject}\n"
        f"To: {', '.join(original.to_addrs)}\n"
    )
    return f"{new_body}\n\n{sep}\n{header}\n{quoted}\n"


def build_reply_body(original: EmailMessageView, new_body: str) -> str:
    """Quote ``original`` below ``new_body`` in standard reply format.

    A real mail client, on Reply, prepends the new text above a quoted copy
    of the message being replied to ("On <date>, X wrote:\\n> ..."). SlyReply's
    multi-turn continuity depends entirely on that in-band quoted chain
    accumulating (it stores no bodies), so a faithful test client MUST quote —
    otherwise the agent never sees earlier turns and we measure a context-loss
    that a real client would never produce. Previously this tool sent only the
    new body, so threaded conversations silently lost their history.

    The original's body already carries the prior turns' quotes, so quoting it
    here carries the whole thread forward.
    """
    quoted = "\n".join(
        "> " + line for line in (original.text_body or "").rstrip().splitlines()
    )
    attribution = f"On {original.created_at}, {original.from_addr} wrote:"
    return f"{new_body}\n\n{attribution}\n{quoted}\n"


# --------------------------------------------------------------------------
# SDK tool factory.
# --------------------------------------------------------------------------
def build_email_server(
    *,
    smtp_host: str,
    smtp_port: int,
    mailpit_url: str,
    persona_from_address: str,
    not_before: datetime | None = None,
):
    """Build the in-process MCP server exposing the three email tools.

    ``persona_from_address`` is the registered address the persona sends from;
    the agent never has to know or supply it.

    ``not_before`` fences ``wait_for_email``: only mail created after it can
    satisfy a wait, so a stale message left in the sink by an earlier run is
    ignored. The runner passes the run's start time; left ``None`` (e.g. in a
    unit test) no fencing is applied.
    """
    mailpit = MailpitClient(mailpit_url)

    @tool(
        "send_email",
        "Send an email to a SlyReply UID address (e.g. myagent@slyreply.ai). "
        "Use this to message an AI agent you have created and test the "
        "real email round-trip. The 'from' address is your registered "
        "account email and is set automatically.\n\n"
        "OPTIONAL: ``attachments`` is a comma-separated list of fixture "
        "filenames to attach — basenames only, drawn from "
        "qa-agents/harness/qa_agents/fixtures/attachments/. Examples: "
        "'sample-invoice.pdf', 'sample-receipt.png,sample-expenses.csv'. "
        "The fixture pack ships real PDFs, JPGs, PNGs, DOCX, XLSX, CSV, "
        "ZIP, plus a 0-byte file and a mislabeled-extension probe. See "
        "the fixture pack's README for the full list and what each "
        "file tests.",
        {"to": str, "subject": str, "body": str, "attachments": str},
    )
    async def send_email(args: dict) -> dict:
        to_addr = str(args.get("to", "")).strip()
        subject = str(args.get("subject", "")).strip()
        body = str(args.get("body", ""))
        # #1115 — comma-separated attachment names (basenames only) from
        # the fixture pack. Empty / missing = no attachments (the
        # pre-#1115 behaviour). We split + strip + drop empties so a
        # persona that passes "a.pdf, , b.png" gets {a.pdf, b.png}.
        raw_attachments = str(args.get("attachments", "") or "")
        attachment_names = [
            s.strip() for s in raw_attachments.split(",") if s.strip()
        ]
        if not to_addr:
            return {
                "content": [{"type": "text", "text": "ERROR: 'to' address is required."}]
            }
        if len(attachment_names) > _MAX_ATTACHMENTS_PER_MESSAGE:
            return {
                "content": [{
                    "type": "text",
                    "text": (
                        f"ERROR: {len(attachment_names)} attachments "
                        f"requested but the harness caps at "
                        f"{_MAX_ATTACHMENTS_PER_MESSAGE}. Send a "
                        f"smaller batch and retry."
                    ),
                }]
            }
        # Resolve before sending so a bad name surfaces as a tool error
        # the persona can react to, not an SMTP 4xx from inside the
        # session.
        try:
            attachment_paths = [
                _resolve_attachment_path(n) for n in attachment_names
            ]
        except (ValueError, FileNotFoundError) as exc:
            return {
                "content": [{
                    "type": "text",
                    "text": f"ERROR resolving attachment: {exc}",
                }]
            }
        try:
            message_id = send_smtp(
                host=smtp_host,
                port=smtp_port,
                from_addr=persona_from_address,
                to_addr=to_addr,
                subject=subject,
                body=body,
                attachments=attachment_paths,
            )
        except Exception as exc:  # noqa: BLE001 - report failure to the agent
            return {
                "content": [
                    {"type": "text", "text": f"ERROR sending email: {exc!r}"}
                ]
            }
        attachment_note = (
            f" with {len(attachment_paths)} attachment(s): "
            f"{', '.join(p.name for p in attachment_paths)}"
            if attachment_paths
            else ""
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Sent email from {persona_from_address} to {to_addr} "
                        f"(subject: {subject!r}, message-id {message_id})"
                        f"{attachment_note}. Use wait_for_email to read "
                        f"the reply."
                    ),
                }
            ]
        }

    @tool(
        "wait_for_email",
        "Poll the mailbox for the newest email addressed to a given address, "
        "blocking up to timeout_s seconds. Use this to wait for an AI reply, "
        "a signup verification email, or a password-reset email.",
        {"to_address": str, "timeout_s": int},
    )
    async def wait_for_email(args: dict) -> dict:
        to_address = str(args.get("to_address", "")).strip()
        timeout_s = int(args.get("timeout_s") or 60)
        if not to_address:
            return {
                "content": [
                    {"type": "text", "text": "ERROR: 'to_address' is required."}
                ]
            }
        deadline = time.monotonic() + timeout_s
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                messages = mailpit.list_messages()
                raw = find_newest_for(messages, to_address, not_before=not_before)
                if raw is not None:
                    msg_id = str(raw.get("ID") or raw.get("id") or "")
                    # Re-fetch the full single-message body when we have an id.
                    full = mailpit.get_message(msg_id) if msg_id else raw
                    view = parse_mailpit_message({**raw, **full})
                    return {"content": [{"type": "text", "text": view.summary()}]}
            except Exception as exc:  # noqa: BLE001
                last_error = repr(exc)
            # ``await`` (not the blocking ``time.sleep``): this tool runs on the
            # Agent SDK's asyncio loop, and a sync sleep would stall the SDK's
            # own message streaming for the whole poll interval.
            await asyncio.sleep(2)
        tail = f" (last error: {last_error})" if last_error else ""
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"No email addressed to {to_address} arrived within "
                        f"{timeout_s}s.{tail}"
                    ),
                }
            ]
        }

    @tool(
        "get_email",
        "Fetch one email by its id and return its decoded sender, subject and "
        "body text.",
        {"id": str},
    )
    async def get_email(args: dict) -> dict:
        message_id = str(args.get("id", "")).strip()
        if not message_id:
            return {
                "content": [{"type": "text", "text": "ERROR: 'id' is required."}]
            }
        try:
            raw = mailpit.get_message(message_id)
        except Exception as exc:  # noqa: BLE001
            return {
                "content": [
                    {"type": "text", "text": f"ERROR fetching email: {exc!r}"}
                ]
            }
        view = parse_mailpit_message(raw)
        return {"content": [{"type": "text", "text": view.summary()}]}

    # ----------------------------------------------------------------
    # #1109 — reply_in_thread + forward_email.
    #
    # Threading lets a persona test conversation continuity: send a
    # message to your agent → AI replies → reply IN THREAD and watch
    # the AI keep context. Without proper In-Reply-To + References
    # headers, Mailpit groups the reply as a brand-new conversation
    # and the agent can't observe what threading means to the
    # product. That's the whole point of the test surface.
    # ----------------------------------------------------------------
    @tool(
        "reply_in_thread",
        "Reply to an existing email so the response lands in the same "
        "conversation thread. Pass the Mailpit message id of the email "
        "you're replying to; the tool reads its Message-ID + References "
        "headers and emits a reply with In-Reply-To + References set "
        "correctly. The reply's 'to' is the original's 'from' "
        "automatically; the subject is prefixed with 'Re: ' if not "
        "already. OPTIONAL: ``attachments`` is a comma-separated list "
        "of fixture filenames (same convention as send_email).",
        {"message_id": str, "body": str, "attachments": str},
    )
    async def reply_in_thread(args: dict) -> dict:
        original_id = str(args.get("message_id", "")).strip()
        body = str(args.get("body", ""))
        raw_attachments = str(args.get("attachments", "") or "")
        if not original_id:
            return {"content": [{
                "type": "text",
                "text": "ERROR: 'message_id' is required (Mailpit message id).",
            }]}
        try:
            raw = mailpit.get_message(original_id)
        except Exception as exc:  # noqa: BLE001
            return {"content": [{
                "type": "text",
                "text": f"ERROR fetching original message {original_id}: {exc!r}",
            }]}
        original = parse_mailpit_message(raw)
        if not original.from_addr:
            return {"content": [{
                "type": "text",
                "text": (
                    f"ERROR: original message {original_id} has no "
                    f"resolvable 'From' address — cannot send a reply."
                ),
            }]}

        attachment_names = [
            s.strip() for s in raw_attachments.split(",") if s.strip()
        ]
        if len(attachment_names) > _MAX_ATTACHMENTS_PER_MESSAGE:
            return {"content": [{
                "type": "text",
                "text": (
                    f"ERROR: {len(attachment_names)} attachments "
                    f"requested but the harness caps at "
                    f"{_MAX_ATTACHMENTS_PER_MESSAGE}."
                ),
            }]}
        try:
            attachment_paths = [
                _resolve_attachment_path(n) for n in attachment_names
            ]
        except (ValueError, FileNotFoundError) as exc:
            return {"content": [{
                "type": "text",
                "text": f"ERROR resolving attachment: {exc}",
            }]}

        in_reply_to, references = build_reply_threading(original)
        try:
            new_message_id = send_smtp(
                host=smtp_host,
                port=smtp_port,
                from_addr=persona_from_address,
                to_addr=original.from_addr,
                subject=build_reply_subject(original.subject),
                # Quote the original below the new text, like a real mail
                # client — SlyReply's continuity needs the in-band chain to
                # accumulate, so a reply that sent only the new body made the
                # agent lose earlier turns.
                body=build_reply_body(original, body),
                attachments=attachment_paths,
                in_reply_to=in_reply_to or None,
                references=references or None,
            )
        except Exception as exc:  # noqa: BLE001
            return {"content": [{
                "type": "text",
                "text": f"ERROR sending reply: {exc!r}",
            }]}
        threading_note = (
            f" In-Reply-To={in_reply_to}"
            if in_reply_to
            else " (original had no Message-ID; reply starts a new thread)"
        )
        attachment_note = (
            f" with {len(attachment_paths)} attachment(s)"
            if attachment_paths else ""
        )
        return {"content": [{
            "type": "text",
            "text": (
                f"Replied in thread to {original.from_addr} "
                f"(message-id {new_message_id}){threading_note}"
                f"{attachment_note}. Use wait_for_email to read the "
                f"agent's response."
            ),
        }]}

    @tool(
        "forward_email",
        "Forward an existing email to a new recipient with the original "
        "quoted below. Pass the Mailpit message id of the original; the "
        "tool quotes the original sender/date/subject + body in the "
        "standard '---------- Forwarded message ----------' format. The "
        "new subject is prefixed with 'Fwd: ' if not already. Forwards "
        "deliberately do NOT set In-Reply-To — they start a new "
        "conversation with a new recipient (matching Apple Mail / Gmail "
        "client behaviour). OPTIONAL: ``attachments`` adds new fixture "
        "files alongside the quoted body; existing attachments on the "
        "original are not re-attached in this slice (see slice 2 of "
        "#1109).",
        {"message_id": str, "to": str, "body": str, "attachments": str},
    )
    async def forward_email(args: dict) -> dict:
        original_id = str(args.get("message_id", "")).strip()
        to_addr = str(args.get("to", "")).strip()
        body = str(args.get("body", ""))
        raw_attachments = str(args.get("attachments", "") or "")
        if not original_id:
            return {"content": [{
                "type": "text",
                "text": "ERROR: 'message_id' is required.",
            }]}
        if not to_addr:
            return {"content": [{
                "type": "text",
                "text": "ERROR: 'to' address is required.",
            }]}
        try:
            raw = mailpit.get_message(original_id)
        except Exception as exc:  # noqa: BLE001
            return {"content": [{
                "type": "text",
                "text": f"ERROR fetching original message {original_id}: {exc!r}",
            }]}
        original = parse_mailpit_message(raw)

        attachment_names = [
            s.strip() for s in raw_attachments.split(",") if s.strip()
        ]
        if len(attachment_names) > _MAX_ATTACHMENTS_PER_MESSAGE:
            return {"content": [{
                "type": "text",
                "text": (
                    f"ERROR: {len(attachment_names)} attachments "
                    f"requested but the harness caps at "
                    f"{_MAX_ATTACHMENTS_PER_MESSAGE}."
                ),
            }]}
        try:
            attachment_paths = [
                _resolve_attachment_path(n) for n in attachment_names
            ]
        except (ValueError, FileNotFoundError) as exc:
            return {"content": [{
                "type": "text",
                "text": f"ERROR resolving attachment: {exc}",
            }]}

        forward_body = build_forward_body(original, body)
        try:
            new_message_id = send_smtp(
                host=smtp_host,
                port=smtp_port,
                from_addr=persona_from_address,
                to_addr=to_addr,
                subject=build_forward_subject(original.subject),
                body=forward_body,
                attachments=attachment_paths,
            )
        except Exception as exc:  # noqa: BLE001
            return {"content": [{
                "type": "text",
                "text": f"ERROR forwarding email: {exc!r}",
            }]}
        attachment_note = (
            f" with {len(attachment_paths)} additional attachment(s)"
            if attachment_paths else ""
        )
        return {"content": [{
            "type": "text",
            "text": (
                f"Forwarded original {original_id} to {to_addr} "
                f"(new message-id {new_message_id}){attachment_note}. "
                f"Use wait_for_email to read the reply."
            ),
        }]}

    # ----------------------------------------------------------------
    # #1109 slice 2 — list_attachments + download_attachment.
    #
    # Reading half of the attachment surface. Once a persona has an
    # email in the mailbox (verification, AI reply, forwarded original)
    # they can enumerate its attachments by metadata, then download
    # any one of them to verify integrity (size + sha256) or to
    # re-upload via the browser file-upload tool.
    # ----------------------------------------------------------------
    @tool(
        "list_attachments",
        "List the attachments on an existing email. Pass the Mailpit "
        "message id; returns each attachment's filename, content type, "
        "size, and the ``attachment_id`` to pass to download_attachment. "
        "Returns 'No attachments on message X.' for messages without "
        "any. Useful for verifying that a verification or AI reply "
        "carried back the file you sent.",
        {"message_id": str},
    )
    async def list_attachments(args: dict) -> dict:
        message_id = str(args.get("message_id", "")).strip()
        if not message_id:
            return {"content": [{
                "type": "text",
                "text": "ERROR: 'message_id' is required.",
            }]}
        try:
            raw = mailpit.get_message(message_id)
        except Exception as exc:  # noqa: BLE001
            return {"content": [{
                "type": "text",
                "text": f"ERROR fetching message {message_id}: {exc!r}",
            }]}
        view = parse_mailpit_message(raw)
        if not view.attachments:
            return {"content": [{
                "type": "text",
                "text": f"No attachments on message {message_id}.",
            }]}
        # Render as a numbered list — the agent's eye + the operator
        # reading transcripts both find this much easier than JSON.
        lines = [f"Attachments on message {message_id} ({len(view.attachments)}):"]
        for i, att in enumerate(view.attachments, 1):
            lines.append(
                f"  {i}. {att['filename']!r} "
                f"({att['content_type']}, {att['size_bytes']} bytes, "
                f"id={att['attachment_id']})"
            )
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}

    @tool(
        "download_attachment",
        "Download one attachment's bytes by id. Returns the filename, "
        "content type, size, sha256 (for integrity verification), and "
        "the Mailpit URL the bytes can be re-fetched from. For files "
        f"under {_DOWNLOAD_INLINE_THRESHOLD} bytes the response also "
        "includes a base64-encoded copy of the bytes inline; for "
        "larger files the URL is the only option to keep agent "
        "context manageable. Use list_attachments first to find the "
        "attachment_id.",
        {"message_id": str, "attachment_id": str},
    )
    async def download_attachment(args: dict) -> dict:
        message_id = str(args.get("message_id", "")).strip()
        attachment_id = str(args.get("attachment_id", "")).strip()
        if not message_id:
            return {"content": [{
                "type": "text",
                "text": "ERROR: 'message_id' is required.",
            }]}
        if not attachment_id:
            return {"content": [{
                "type": "text",
                "text": "ERROR: 'attachment_id' is required. Run list_attachments first.",
            }]}
        try:
            data, content_type = mailpit.get_part(message_id, attachment_id)
        except Exception as exc:  # noqa: BLE001
            return {"content": [{
                "type": "text",
                "text": (
                    f"ERROR fetching attachment {attachment_id} of "
                    f"message {message_id}: {exc!r}"
                ),
            }]}
        digest = hashlib.sha256(data).hexdigest()
        size = len(data)
        url = mailpit.part_url(message_id, attachment_id)
        # Try to resolve a filename from the message metadata so the
        # response is self-contained. Falls back to 'unknown' if the
        # part id no longer matches (e.g. the operator copy-pasted
        # ids from a different message by accident).
        filename = ""
        try:
            view = parse_mailpit_message(mailpit.get_message(message_id))
            for att in view.attachments:
                if att["attachment_id"] == attachment_id:
                    filename = att["filename"]
                    break
        except Exception:  # noqa: BLE001
            filename = ""
        header = (
            f"Attachment {attachment_id} from message {message_id}:\n"
            f"  filename: {filename or '(unknown)'}\n"
            f"  content_type: {content_type}\n"
            f"  size_bytes: {size}\n"
            f"  sha256: {digest}\n"
            f"  url: {url}\n"
        )
        if size <= _DOWNLOAD_INLINE_THRESHOLD:
            inline = base64.b64encode(data).decode("ascii")
            header += f"  base64: {inline}\n"
        else:
            header += (
                f"  (file is {size} bytes, over the "
                f"{_DOWNLOAD_INLINE_THRESHOLD} byte inline threshold "
                f"— fetch via the URL above)\n"
            )
        return {"content": [{"type": "text", "text": header}]}

    # ----------------------------------------------------------------
    # #1109 slice 4 — prepare_upload_path.
    #
    # Final piece of the lifecycle tool surface. The playwright MCP
    # exposes ``browser_file_upload`` which takes filesystem paths;
    # this tool materialises one. Two modes:
    #
    #   - ``fixture="sample-invoice.pdf"`` — resolve a fixture pack
    #     file. Path is stable across calls (the pack is read-only),
    #     no copying required.
    #
    #   - ``message_id=X, attachment_id=Y`` — fetch the bytes from
    #     Mailpit and stage them on disk so the persona can re-upload
    #     an attachment the AI just sent them via the web UI.
    #
    # No-mode-supplied / both-modes-supplied are errors; we don't
    # silently pick a default because that's how personas lose half
    # their test (sending the wrong fixture and not noticing).
    # ----------------------------------------------------------------
    @tool(
        "prepare_upload_path",
        "Materialise a file on the harness disk and return its "
        "filesystem path, ready to hand to the playwright MCP's "
        "``browser_file_upload``. TWO modes (exactly one is "
        "required):\n\n"
        "  - ``fixture=<basename>`` — resolve a file from the "
        "harness's fixture pack. Same allowlist as send_email: "
        "sample-invoice.pdf, sample-receipt.png, sample-expenses.csv, "
        "etc. The path is stable across calls; no copying happens.\n\n"
        "  - ``message_id=<id>, attachment_id=<id>`` — fetch the "
        "bytes for an attachment on an email Mailpit holds and "
        "stage them in a writable temp directory. Returns the "
        "freshly-written path so the persona can re-upload an "
        "attachment the AI just sent.\n\n"
        "Use this when you want to test a WEB UI upload flow "
        "(profile picture, document upload in the playground, "
        "agent avatar, etc). Do NOT use it for sending email "
        "attachments — send_email and reply_in_thread already "
        "accept fixture names directly.",
        {"fixture": str, "message_id": str, "attachment_id": str},
    )
    async def prepare_upload_path(args: dict) -> dict:
        fixture = str(args.get("fixture", "") or "").strip()
        message_id = str(args.get("message_id", "") or "").strip()
        attachment_id = str(args.get("attachment_id", "") or "").strip()
        has_fixture = bool(fixture)
        has_mailpit = bool(message_id or attachment_id)

        if not has_fixture and not has_mailpit:
            return {"content": [{
                "type": "text",
                "text": (
                    "ERROR: prepare_upload_path needs either "
                    "``fixture`` or ``message_id`` + ``attachment_id``. "
                    "See the tool description for the modes."
                ),
            }]}
        if has_fixture and has_mailpit:
            return {"content": [{
                "type": "text",
                "text": (
                    "ERROR: prepare_upload_path accepts EITHER "
                    "``fixture`` OR ``message_id`` + ``attachment_id`` "
                    "— not both. Choose one mode so the source is "
                    "unambiguous."
                ),
            }]}
        if has_mailpit and not (message_id and attachment_id):
            return {"content": [{
                "type": "text",
                "text": (
                    "ERROR: when fetching from Mailpit, BOTH "
                    "``message_id`` and ``attachment_id`` are "
                    "required. Run list_attachments first to find "
                    "the attachment_id."
                ),
            }]}

        if has_fixture:
            try:
                path = _resolve_attachment_path(fixture)
            except (ValueError, FileNotFoundError) as exc:
                return {"content": [{
                    "type": "text",
                    "text": f"ERROR resolving fixture: {exc}",
                }]}
            return {"content": [{
                "type": "text",
                "text": (
                    f"Ready to upload from fixture pack.\n"
                    f"  path: {path}\n"
                    f"  size_bytes: {path.stat().st_size}\n"
                    f"  source: fixture\n"
                    f"Pass this path to "
                    f"mcp__playwright__browser_file_upload."
                ),
            }]}

        # Mailpit mode.
        try:
            data, content_type = mailpit.get_part(message_id, attachment_id)
        except Exception as exc:  # noqa: BLE001
            return {"content": [{
                "type": "text",
                "text": (
                    f"ERROR fetching attachment {attachment_id} of "
                    f"message {message_id}: {exc!r}"
                ),
            }]}
        # Look up the filename from the message metadata so the staged
        # file keeps a meaningful name. Falls back to a digest-derived
        # name if the lookup fails.
        filename = ""
        try:
            view = parse_mailpit_message(mailpit.get_message(message_id))
            for att in view.attachments:
                if att["attachment_id"] == attachment_id:
                    filename = att["filename"]
                    break
        except Exception:  # noqa: BLE001
            filename = ""
        safe_name = _safe_basename(filename)
        digest = hashlib.sha256(data).hexdigest()[:12]
        staged = _upload_staging_dir() / f"{digest}-{safe_name}"
        staged.write_bytes(data)
        return {"content": [{
            "type": "text",
            "text": (
                f"Staged {len(data)} bytes from message "
                f"{message_id} attachment {attachment_id}.\n"
                f"  path: {staged}\n"
                f"  size_bytes: {len(data)}\n"
                f"  content_type: {content_type}\n"
                f"  sha256: {hashlib.sha256(data).hexdigest()}\n"
                f"  source: mailpit\n"
                f"Pass this path to "
                f"mcp__playwright__browser_file_upload."
            ),
        }]}

    server = create_sdk_mcp_server(
        name="email",
        tools=[
            send_email,
            wait_for_email,
            get_email,
            reply_in_thread,
            forward_email,
            list_attachments,
            download_attachment,
            prepare_upload_path,
        ],
    )
    return server, [
        "send_email", "wait_for_email", "get_email",
        "reply_in_thread", "forward_email",
        "list_attachments", "download_attachment",
        "prepare_upload_path",
    ]
