# Email MCP server

The harness's email tools live in [`email.py`](./email.py) and are
exposed to personas via the `email` MCP server (the `mcp__email__*`
tool names). All eight tools share one Mailpit instance + one SMTP
relay; per-persona state is the `registered_email` the persona signs
up with (set once in `personas.py`, threaded through the runner).

The tools split into three concerns:

1. **One-shot send + receive** — the original three: `send_email`,
   `wait_for_email`, `get_email`. Enough for signup-verification +
   "send my agent a question, wait for reply" flows.
2. **Threading + forwarding** — `reply_in_thread`, `forward_email`.
   Added in #1109 slice 1 to support multi-turn conversation tests
   and inter-agent handoff.
3. **Attachment lifecycle** — `list_attachments`,
   `download_attachment`, `prepare_upload_path`. Added in slices 2
   and 4. Read-side enumeration + integrity-check (SHA256) +
   re-upload staging for web UI tests.

> **Persona authors:** the cheat sheet at the end of this doc says
> which tool to reach for in which situation. Read that first.

---

## Tool reference

### `send_email(to, subject, body, [attachments])`
The canonical send. `to` is the agent address (e.g.
`myagent@slyreply.ai`); the `from` is the persona's
`registered_email` automatically. `attachments` is an OPTIONAL
comma-separated list of fixture basenames from
[`fixtures/attachments/`](../fixtures/attachments/) — see that
directory's README for the full list. The harness caps a single
message at 8 attachments / 30 MB total; oversize fixtures are
rejected before the SMTP call.

Returns a confirmation with the generated `Message-ID`. Pair with
`wait_for_email` to read the AI's reply.

### `wait_for_email(to_address, [timeout_s])`
Polls Mailpit for the newest message addressed to `to_address`,
blocking up to `timeout_s` seconds (default 60). Uses the run's
start time as a fence so a stale message left in the sink by an
earlier run doesn't satisfy the wait. Returns the message body +
sender + subject, NOT a structured object — agents read the
summary text.

Empty after timeout returns a friendly "no email arrived" message
rather than raising; the persona can decide whether to retry, file
a finding, or move on.

### `get_email(id)`
Fetch one message by Mailpit ID and return its decoded headers +
body. Used after `wait_for_email` if the persona wants to re-read
the same message with full content (e.g. to extract a verification
link from a long HTML body).

---

### `reply_in_thread(message_id, body, [attachments])`
*Added #1109 slice 1.*

Fetches the original via Mailpit, reads its `Message-ID` +
`References` headers, and emits a reply with:

- `In-Reply-To: <original Message-ID>`
- `References: <original References> + <original Message-ID>`
- `Subject: "Re: <original>"` (idempotent — `Re: Re:` collapses)
- `To: <original From>` automatically

Mailpit groups by Message-ID chain on its UI, so the persona can
verify threading actually worked by re-reading the chain. Use this
— not `send_email` — when continuing a conversation. Soft-fails
cleanly when the original has no `Message-ID` (legacy / stripped):
the reply still goes out but starts a new thread, and the tool's
response says so.

### `forward_email(message_id, to, body, [attachments])`
*Added #1109 slice 1.*

Quotes the original in the standard
`---------- Forwarded message ----------` format (Apple Mail /
Gmail convention) with sender + date + subject + body. New
subject is prefixed with `Fwd: ` (idempotent). Deliberately does
NOT set `In-Reply-To` — forwards are new conversations with new
recipients.

`attachments` only carries NEW fixture-pack files. Re-attaching
the original's attachments to the forward is out of scope today
(the typical test pattern is: `download_attachment` to verify,
then `prepare_upload_path` to re-stage if needed for a web UI).

---

### `list_attachments(message_id)`
*Added #1109 slice 2.*

Returns a numbered list of attachment metadata:

```
Attachments on message X (2):
  1. 'invoice.pdf' (application/pdf, 12345 bytes, id=2)
  2. 'receipt.png' (image/png, 6789 bytes, id=3)
```

The `id` is what `download_attachment` and `prepare_upload_path`
take. Returns `No attachments on message X.` for empty messages —
empty is a valid state, not an error.

### `download_attachment(message_id, attachment_id)`
*Added #1109 slice 2.*

Fetches an attachment's raw bytes via Mailpit's
`/api/v1/message/{id}/part/{partID}` endpoint. Returns:

- `filename`, `content_type`, `size_bytes`
- **`sha256`** — for integrity verification against what the
  persona sent
- `url` — the Mailpit URL for browser navigation
- For files **under 64 KB**: bytes inlined as base64 (so the
  agent can inspect directly)
- For larger files: URL only, keeping agent context manageable

### `prepare_upload_path(...)`
*Added #1109 slice 4.*

Materialises a file on disk and returns its filesystem path,
ready to hand to `mcp__playwright__browser_file_upload`. Two
modes, exactly one required:

- **`fixture="sample-invoice.pdf"`** — resolve a fixture pack
  file. Same allowlist as `send_email`. Path is stable across
  calls; no copying.
- **`message_id=X, attachment_id=Y`** — fetch bytes from Mailpit
  and stage them under `${TMPDIR}/qa-agents-uploads/` so a
  persona can re-upload an attachment the AI just sent via the
  web UI.

Use this for web UI upload flows: profile pictures, playground
document upload, agent avatars. Do NOT use it for sending email
attachments — `send_email` and `reply_in_thread` already accept
fixture names directly.

---

## Cheat sheet — what can personas do with email?

| You want to…                                            | Use                                          |
|---------------------------------------------------------|----------------------------------------------|
| Send your agent a question                              | `send_email`                                 |
| Wait for the AI to reply                                | `wait_for_email`                             |
| Re-read a specific message in full                      | `get_email`                                  |
| Continue a conversation in the same thread              | `reply_in_thread`                            |
| Hand a thread off to a different agent                  | `forward_email`                              |
| Enumerate what the AI sent you back                     | `list_attachments`                           |
| Verify what came back matches what you sent             | `download_attachment` (compare sha256)       |
| Re-upload an AI reply's attachment via a web UI         | `prepare_upload_path(message_id, …)` + playwright |
| Upload a fixture file via a web UI                      | `prepare_upload_path(fixture=…)` + playwright |
| Test multi-turn context retention                       | `reply_in_thread` × N                        |
| Test forward header leakage                             | `forward_email` → wait → inspect reply       |
| Test attachment-type matrix (PDF, DOCX, XLSX, …)         | `send_email(attachments=…)` per fixture      |

## What can personas NOT do?

- **Reply with attachments lifted from the original.** Slice 1
  said this was deferred; today the pattern is
  `download_attachment` (to verify) → `prepare_upload_path` (to
  re-stage) → `send_email` / `reply_in_thread`
  `attachments=<basename>` (to re-send). Workable but multi-step.
- **Compose HTML email.** Everything is plain text. The product
  receives plain text from the harness; the AI's replies may
  contain HTML which the parser reads as text (existing `Text`
  preference in `parse_mailpit_message`).
- **Mass send.** The per-message cap (8 attachments) and the
  Mailpit polling cadence make burst tests impractical. Use the
  k6 burst test path (#892) for rate-limit fuzzing.
- **Send from an unregistered address.** The `from` is locked to
  the persona's `registered_email`; spoofing is a separate
  test surface (the SMTP protection drill, #1157).

## Operator notes

- **Mailpit persistence** is per-run + cross-run after #1108: each
  run starts with a fresh inbox but messages from prior runs stay
  available for inspection.
- **Fixture pack** lives at
  [`qa_agents/fixtures/attachments/`](../fixtures/attachments/);
  its README enumerates every available basename + intended test.
- **The staging dir** for `prepare_upload_path` is under
  `${TMPDIR}/qa-agents-uploads/`. It's wiped on pod restart, not
  per-run — operators investigating a failed upload after the fact
  can read it for as long as the pod stays up.
- **No HTML, no S/MIME, no DKIM verification.** The email tools
  here are for product-behaviour testing, not protocol
  conformance. The SMTP protection drill (#1157) covers protocol
  hardening separately.
