"""Tests for the email tools — chiefly the MIME decoding the spike proved
necessary, plus Mailpit message parsing and recipient matching.

No real SMTP or HTTP: the pure helpers are tested directly, and the Mailpit
client is exercised against a mocked transport.
"""

from __future__ import annotations

import base64
import quopri
from datetime import UTC, datetime

import httpx
import pytest

from qa_agents.tools.email import (
    EmailMessageView,
    MailpitClient,
    _safe_basename,
    _upload_staging_dir,
    build_forward_body,
    build_forward_subject,
    build_reply_body,
    build_reply_subject,
    build_reply_threading,
    decode_mime_header,
    decode_transfer_encoded_body,
    find_newest_for,
    parse_mailpit_message,
)


# --------------------------------------------------------------------------
# Body transfer-encoding decode.
# --------------------------------------------------------------------------
def test_decode_quoted_printable_body():
    original = "Hello — it works, naïvely. Price: £9/mo.\n"
    encoded = quopri.encodestring(original.encode("utf-8")).decode("ascii")
    assert decode_transfer_encoded_body(encoded, "quoted-printable") == original


def test_decode_base64_body():
    original = "AI reply token: REPLY-OK-BEEFCAFE\n-- \nSent by SlyReply\n"
    encoded = base64.b64encode(original.encode("utf-8")).decode("ascii")
    assert decode_transfer_encoded_body(encoded, "base64") == original


def test_decode_body_unknown_encoding_passes_through():
    body = "plain 7bit body, nothing to decode"
    assert decode_transfer_encoded_body(body, "7bit") == body
    assert decode_transfer_encoded_body(body, None) == body


def test_decode_body_empty():
    assert decode_transfer_encoded_body("", "base64") == ""
    assert decode_transfer_encoded_body(None, "base64") == ""


def test_decode_body_corrupt_base64_does_not_raise():
    # Not valid base64 — must fall back to the raw string, never raise.
    assert decode_transfer_encoded_body("!!!not base64!!!", "base64") == (
        "!!!not base64!!!"
    )


# --------------------------------------------------------------------------
# RFC-2047 header decode.
# --------------------------------------------------------------------------
def test_decode_encoded_word_header():
    # "Re: Spike test — café" with the em-dash + accent as an encoded-word.
    encoded = "=?utf-8?b?UmU6IFNwaWtlIHRlc3Qg4oCUIGNhZsOp?="
    assert decode_mime_header(encoded) == "Re: Spike test — café"


def test_decode_plain_header_unchanged():
    assert decode_mime_header("Re: do you fix boilers?") == (
        "Re: do you fix boilers?"
    )


def test_decode_header_empty():
    assert decode_mime_header("") == ""
    assert decode_mime_header(None) == ""


# --------------------------------------------------------------------------
# Mailpit message parsing.
# --------------------------------------------------------------------------
def test_parse_mailpit_single_message_with_text_field():
    raw = {
        "ID": "abc123",
        "From": {"Name": "Spike Bot", "Address": "spikebot@slyreply.ai"},
        "To": [{"Name": "", "Address": "First-Impression-Critic@QA-Agents.slyreply.test"}],
        "Subject": "Re: do you fix boilers?",
        "Text": "Yes, we fix boilers. — D. Doyle & Sons",
        "Created": "2026-05-19T10:00:00Z",
    }
    view = parse_mailpit_message(raw)
    assert isinstance(view, EmailMessageView)
    assert view.id == "abc123"
    assert view.from_addr == "spikebot@slyreply.ai"
    # Recipient addresses are lower-cased for stable matching.
    assert view.to_addrs == ["first-impression-critic@qa-agents.slyreply.test"]
    assert view.subject == "Re: do you fix boilers?"
    assert "fix boilers" in view.text_body


def test_parse_mailpit_message_decodes_transfer_encoded_body():
    body_text = "AI reply: confirmed.\n"
    raw = {
        "ID": "enc1",
        "From": {"Address": "bot@slyreply.ai"},
        "To": [{"Address": "user@example.com"}],
        "Subject": "=?utf-8?q?Re=3A_t=C3=A9st?=",
        "Body": base64.b64encode(body_text.encode()).decode(),
        "Content-Transfer-Encoding": "base64",
    }
    view = parse_mailpit_message(raw)
    assert view.subject == "Re: tést"
    assert view.text_body == body_text


def test_parse_mailpit_message_falls_back_to_snippet():
    raw = {
        "ID": "snip1",
        "From": {"Address": "bot@slyreply.ai"},
        "To": [{"Address": "user@example.com"}],
        "Subject": "Hi",
        "Snippet": "a short preview",
    }
    assert parse_mailpit_message(raw).text_body == "a short preview"


def test_email_view_summary_is_human_readable():
    view = EmailMessageView(
        id="x1",
        from_addr="bot@slyreply.ai",
        to_addrs=["user@example.com"],
        subject="Re: hello",
        text_body="  body here  ",
        created_at="2026-05-19T10:00:00Z",
    )
    summary = view.summary()
    assert "from: bot@slyreply.ai" in summary
    assert "subject: Re: hello" in summary
    assert "body here" in summary


# --------------------------------------------------------------------------
# Recipient matching.
# --------------------------------------------------------------------------
def test_find_newest_for_matches_case_insensitively_and_returns_first():
    messages = [
        # Same recipient, different case — the matcher must lower-case both
        # sides before comparing. List is newest-first; the first match
        # (id "2") is returned.
        {"ID": "2", "To": [{"Address": "First-Impression-Critic@example.test"}]},
        {"ID": "1", "To": [{"Address": "first-impression-critic@example.test"}]},
    ]
    assert find_newest_for(messages, "first-impression-critic@example.test")["ID"] == "2"


def test_find_newest_for_no_match_returns_none():
    messages = [{"ID": "1", "To": [{"Address": "someone@else.test"}]}]
    assert find_newest_for(messages, "first-impression-critic@example.test") is None


def test_find_newest_for_skips_messages_at_or_before_not_before():
    # A stale message (left in the sink by an earlier run) listed before a
    # fresh one. With not_before set between them, the stale one is skipped.
    messages = [
        {"ID": "stale", "To": [{"Address": "m@example.com"}],
         "Created": "2026-05-20T12:33:48.000Z"},
        {"ID": "fresh", "To": [{"Address": "m@example.com"}],
         "Created": "2026-05-21T19:42:25.126Z"},
    ]
    cutoff = datetime(2026, 5, 21, 0, 0, tzinfo=UTC)
    assert find_newest_for(messages, "m@example.com", not_before=cutoff)["ID"] == "fresh"
    # Without the fence, the first match wins regardless of age.
    assert find_newest_for(messages, "m@example.com")["ID"] == "stale"


def test_find_newest_for_returns_none_when_only_stale_matches():
    messages = [
        {"ID": "stale", "To": [{"Address": "m@example.com"}],
         "Created": "2026-05-20T12:33:48.000Z"},
    ]
    cutoff = datetime(2026, 5, 21, 0, 0, tzinfo=UTC)
    assert find_newest_for(messages, "m@example.com", not_before=cutoff) is None


def test_find_newest_for_keeps_messages_with_unparseable_created():
    # Age unknown → NOT skipped: losing a real reply is worse than tolerating
    # a stale one (the per-run Mailpit wipe is the primary defence anyway).
    messages = [
        {"ID": "x", "To": [{"Address": "m@example.com"}], "Created": ""},
    ]
    cutoff = datetime(2026, 5, 21, 0, 0, tzinfo=UTC)
    assert find_newest_for(messages, "m@example.com", not_before=cutoff)["ID"] == "x"


# --------------------------------------------------------------------------
# MailpitClient against a mocked transport.
# --------------------------------------------------------------------------
def _mock_client(monkeypatch, handler):
    """Patch httpx.Client so MailpitClient hits an in-memory handler."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


def test_mailpit_client_list_messages(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/messages"
        return httpx.Response(
            200, json={"messages": [{"ID": "m1"}, {"ID": "m2"}]}
        )

    _mock_client(monkeypatch, handler)
    client = MailpitClient("http://mailpit:8025")
    msgs = client.list_messages()
    assert [m["ID"] for m in msgs] == ["m1", "m2"]


def test_mailpit_client_get_message(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/message/m1"
        return httpx.Response(200, json={"ID": "m1", "Text": "full body"})

    _mock_client(monkeypatch, handler)
    client = MailpitClient("http://mailpit:8025")
    assert client.get_message("m1")["Text"] == "full body"


def test_mailpit_client_raises_on_http_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _mock_client(monkeypatch, handler)
    client = MailpitClient("http://mailpit:8025")
    with pytest.raises(httpx.HTTPStatusError):
        client.list_messages()


# --------------------------------------------------------------------------
# #1109 — threading-header capture in parse_mailpit_message.
# --------------------------------------------------------------------------
def test_parse_captures_message_id_header_when_mailpit_exposes_it():
    raw = {
        "ID": "mailpit-internal-1",
        "MessageID": "<orig-msg-id-001@slyreply.test>",
        "From": {"Address": "sender@example.com"},
        "To": [{"Address": "agent@slyreply.ai"}],
        "Subject": "thread root",
        "Text": "first message",
    }
    view = parse_mailpit_message(raw)
    assert view.message_id_header == "<orig-msg-id-001@slyreply.test>"
    # References absent → empty list (not None — callers iterate it).
    assert view.references == []


def test_parse_captures_references_chain_from_headers_dict():
    raw = {
        "ID": "mailpit-internal-2",
        "MessageID": "<reply-002@slyreply.test>",
        "From": {"Address": "agent@slyreply.ai"},
        "To": [{"Address": "sender@example.com"}],
        "Subject": "Re: thread root",
        "Text": "AI reply",
        "Headers": {
            "References": "<orig-001@slyreply.test> <middle-002@slyreply.test>",
        },
    }
    view = parse_mailpit_message(raw)
    assert view.references == [
        "<orig-001@slyreply.test>",
        "<middle-002@slyreply.test>",
    ]


def test_parse_handles_references_as_list_value():
    # Mailpit can serialise duplicate headers as a list of strings;
    # the parser must concatenate before tokenising.
    raw = {
        "ID": "x",
        "From": {"Address": "a@b.test"},
        "To": [{"Address": "c@d.test"}],
        "Subject": "Re:",
        "Headers": {
            "References": ["<a@x.test>", "<b@x.test>"],
        },
    }
    view = parse_mailpit_message(raw)
    assert view.references == ["<a@x.test>", "<b@x.test>"]


def test_parse_drops_references_tokens_that_are_not_angle_bracketed():
    # Malformed input must not poison downstream threading. A bare
    # word like 'random-junk' is silently dropped; the well-formed
    # tokens survive.
    raw = {
        "ID": "x",
        "From": {"Address": "a@b.test"},
        "To": [{"Address": "c@d.test"}],
        "Subject": "Re:",
        "Headers": {"References": "<keep@x.test> random-junk <also@x.test>"},
    }
    view = parse_mailpit_message(raw)
    assert view.references == ["<keep@x.test>", "<also@x.test>"]


def test_parse_legacy_message_without_message_id_header_returns_empty_string():
    # Pre-#1109 fixtures + list-endpoint responses have no MessageID;
    # the field degrades to "" so build_reply_threading can take its
    # "no Message-ID → no threading" branch cleanly.
    raw = {
        "ID": "legacy",
        "From": {"Address": "a@b.test"},
        "To": [{"Address": "c@d.test"}],
        "Subject": "hi",
    }
    view = parse_mailpit_message(raw)
    assert view.message_id_header == ""


# --------------------------------------------------------------------------
# #1109 — build_reply_threading.
# --------------------------------------------------------------------------
def test_build_reply_threading_first_in_thread_seeds_chain_with_origin_id():
    # Original is the thread root: no References on it. The reply's
    # In-Reply-To is the original Message-ID, and References becomes
    # [original Message-ID] so the next link in the chain remembers it.
    original = EmailMessageView(
        id="m1", from_addr="x@y.test", to_addrs=["agent@slyreply.ai"],
        subject="root", text_body="", message_id_header="<r1@y.test>",
        references=[],
    )
    in_reply_to, references = build_reply_threading(original)
    assert in_reply_to == "<r1@y.test>"
    assert references == ["<r1@y.test>"]


def test_build_reply_threading_appends_to_existing_references_chain():
    # Mid-thread reply: existing chain + the message we're replying to,
    # in order, so each client sees a complete ancestry.
    original = EmailMessageView(
        id="m2", from_addr="x@y.test", to_addrs=["agent@slyreply.ai"],
        subject="Re: root", text_body="",
        message_id_header="<r2@y.test>",
        references=["<r1@y.test>"],
    )
    in_reply_to, references = build_reply_threading(original)
    assert in_reply_to == "<r2@y.test>"
    assert references == ["<r1@y.test>", "<r2@y.test>"]


def test_build_reply_threading_idempotent_if_origin_id_already_in_chain():
    # Defensive — a poorly-threaded chain might already include the
    # current id. Don't double-add it.
    original = EmailMessageView(
        id="m3", from_addr="x@y.test", to_addrs=[], subject="",
        text_body="",
        message_id_header="<r1@y.test>",
        references=["<r1@y.test>"],
    )
    _, references = build_reply_threading(original)
    assert references == ["<r1@y.test>"]


def test_build_reply_threading_returns_empty_when_origin_has_no_message_id():
    # Without a Message-ID we can't thread; signal that to the caller
    # by returning empty strings so they fall back to "new thread".
    original = EmailMessageView(
        id="legacy", from_addr="x@y.test", to_addrs=[], subject="",
        text_body="", message_id_header="", references=[],
    )
    assert build_reply_threading(original) == ("", [])


# --------------------------------------------------------------------------
# #1109 — build_reply_subject / build_forward_subject.
# --------------------------------------------------------------------------
def test_build_reply_subject_prefixes_when_missing():
    assert build_reply_subject("Verify your email") == "Re: Verify your email"


def test_build_reply_subject_is_idempotent_for_existing_re_prefix():
    # Comparison is case-insensitive — RE:, re:, Re: all collapse.
    assert build_reply_subject("Re: hi") == "Re: hi"
    assert build_reply_subject("RE: HI") == "RE: HI"
    assert build_reply_subject("re: hi") == "re: hi"


def test_build_reply_subject_handles_blank():
    assert build_reply_subject("") == "Re:"
    assert build_reply_subject("   ") == "Re:"


def test_build_forward_subject_prefixes_when_missing():
    assert build_forward_subject("Quarterly report") == "Fwd: Quarterly report"


def test_build_forward_subject_is_idempotent_for_fwd_or_fw():
    assert build_forward_subject("Fwd: meeting") == "Fwd: meeting"
    assert build_forward_subject("FW: meeting") == "FW: meeting"


# --------------------------------------------------------------------------
# #1109 — build_forward_body quoting.
# --------------------------------------------------------------------------
def test_build_forward_body_quotes_original_with_standard_marker():
    original = EmailMessageView(
        id="m1", from_addr="alice@x.test", to_addrs=["bob@y.test"],
        subject="lunch?", text_body="See you at 1?",
        created_at="2026-05-29T10:00:00Z",
    )
    out = build_forward_body(original, "Bob, take a look at this.")
    # New body comes first, then the marker, then quoted headers + body.
    assert "Bob, take a look at this." in out
    assert "---------- Forwarded message ----------" in out
    assert "From: alice@x.test" in out
    assert "Subject: lunch?" in out
    assert "See you at 1?" in out
    # Order matters — new body before the marker, marker before quoted.
    assert out.index("Bob, take a look") < out.index("Forwarded message")
    assert out.index("Forwarded message") < out.index("See you at 1?")


# --------------------------------------------------------------------------
# #1109 slice 2 — attachment metadata parsing.
# --------------------------------------------------------------------------
def test_parse_attachments_from_mailpit_message():
    raw = {
        "ID": "m1",
        "From": {"Address": "a@b.test"},
        "To": [{"Address": "c@d.test"}],
        "Subject": "with-files",
        "Attachments": [
            {
                "PartID": "2",
                "FileName": "invoice.pdf",
                "ContentType": "application/pdf",
                "ContentID": "",
                "Size": 12345,
            },
            {
                "PartID": "3",
                "FileName": "receipt.png",
                "ContentType": "image/png",
                "ContentID": "",
                "Size": 6789,
            },
        ],
    }
    view = parse_mailpit_message(raw)
    assert view.attachments == [
        {
            "attachment_id": "2",
            "filename": "invoice.pdf",
            "content_type": "application/pdf",
            "size_bytes": 12345,
        },
        {
            "attachment_id": "3",
            "filename": "receipt.png",
            "content_type": "image/png",
            "size_bytes": 6789,
        },
    ]


def test_parse_attachments_empty_for_messages_without_any():
    raw = {
        "ID": "m1",
        "From": {"Address": "a@b.test"},
        "To": [{"Address": "c@d.test"}],
        "Subject": "no files",
    }
    assert parse_mailpit_message(raw).attachments == []


def test_parse_attachments_skips_entries_with_no_part_id():
    # PartID is what the download endpoint addresses; without it the
    # metadata is unusable. Defensive: drop the entry rather than
    # surface an "undownloadable" one to the agent.
    raw = {
        "ID": "m1",
        "From": {"Address": "a@b.test"},
        "To": [{"Address": "c@d.test"}],
        "Attachments": [
            {"FileName": "no-id.pdf", "ContentType": "application/pdf", "Size": 100},
            {"PartID": "5", "FileName": "ok.pdf", "Size": 200},
        ],
    }
    atts = parse_mailpit_message(raw).attachments
    assert len(atts) == 1
    assert atts[0]["filename"] == "ok.pdf"


def test_parse_attachments_defaults_unknown_content_type():
    raw = {
        "ID": "m1",
        "From": {"Address": "a@b.test"},
        "Attachments": [{"PartID": "2", "FileName": "blob.bin", "Size": 10}],
    }
    atts = parse_mailpit_message(raw).attachments
    assert atts[0]["content_type"] == "application/octet-stream"


def test_parse_attachments_tolerates_missing_size():
    # If Mailpit omits Size (unlikely but defensive), default to 0
    # rather than crashing on int(None).
    raw = {
        "ID": "m1",
        "From": {"Address": "a@b.test"},
        "Attachments": [{"PartID": "2", "FileName": "x.txt"}],
    }
    assert parse_mailpit_message(raw).attachments[0]["size_bytes"] == 0


# --------------------------------------------------------------------------
# #1109 slice 2 — MailpitClient.get_part / part_url.
# --------------------------------------------------------------------------
def test_mailpit_client_get_part_returns_bytes_and_content_type(monkeypatch):
    expected_bytes = b"%PDF-1.4 hello world"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/message/m1/part/2"
        return httpx.Response(
            200,
            content=expected_bytes,
            headers={"Content-Type": "application/pdf"},
        )

    _mock_client(monkeypatch, handler)
    client = MailpitClient("http://mailpit:8025")
    data, content_type = client.get_part("m1", "2")
    assert data == expected_bytes
    assert content_type == "application/pdf"


def test_mailpit_client_get_part_falls_back_to_octet_stream(monkeypatch):
    # Some Mailpit versions / proxies may strip Content-Type. The
    # client falls back to application/octet-stream so callers always
    # have a non-empty type string to render.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"raw", headers={})

    _mock_client(monkeypatch, handler)
    client = MailpitClient("http://mailpit:8025")
    _, content_type = client.get_part("m1", "2")
    assert content_type == "application/octet-stream"


def test_mailpit_client_part_url_composes_navigable_address():
    # Used as a return value in download_attachment so the persona
    # can navigate to the URL in browser. No trailing slash; no
    # accidental double-slash from the base url.
    client = MailpitClient("http://mailpit:8025/")
    assert client.part_url("abc", "2") == (
        "http://mailpit:8025/api/v1/message/abc/part/2"
    )


def test_mailpit_client_get_part_raises_on_404(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    _mock_client(monkeypatch, handler)
    client = MailpitClient("http://mailpit:8025")
    with pytest.raises(httpx.HTTPStatusError):
        client.get_part("nope", "2")


# --------------------------------------------------------------------------
# #1109 slice 4 — prepare_upload_path helpers.
# --------------------------------------------------------------------------
def test_safe_basename_strips_path_separators():
    # Mailpit attachment filenames are persona-controlled in practice
    # (the persona supplied the original FileName when they sent the
    # attachment). Defensive: a hostile filename like
    # "/etc/passwd" or "..\\..\\..\\windows\\hosts" must not let the
    # staged file land outside the staging dir.
    assert _safe_basename("/etc/passwd") == "passwd"
    assert _safe_basename("..\\..\\windows\\hosts") == "hosts"
    assert _safe_basename("nested/path/file.pdf") == "file.pdf"


def test_safe_basename_neutralises_dotdot_segments():
    # ``..`` inside the basename itself is sanitised to underscore
    # rather than dropped (so the filename remains distinguishable in
    # the staging dir — a quiet drop could let two distinct sources
    # collide on the same staged name).
    assert _safe_basename("..hidden.pdf") == "_hidden.pdf"
    assert _safe_basename("ok..file.pdf") == "ok_file.pdf"


def test_safe_basename_returns_fallback_for_empty():
    # Mailpit very occasionally returns an attachment with no FileName
    # set (inline parts with only a Content-ID). The helper returns a
    # sentinel with an extension so playwright's file-upload tool
    # still routes the multipart correctly.
    assert _safe_basename("") == "file.bin"
    assert _safe_basename("   ") == "file.bin"
    assert _safe_basename(None) == "file.bin"  # type: ignore[arg-type]


def test_upload_staging_dir_creates_dir_on_first_call(tmp_path, monkeypatch):
    # Redirect the staging root into a pytest tmp_path so this test
    # doesn't litter the system temp tree.
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    target = _upload_staging_dir()
    assert target.exists()
    assert target.is_dir()
    # Idempotent — calling again does not raise even though the dir
    # already exists.
    assert _upload_staging_dir() == target


# --------------------------------------------------------------------------
# build_reply_body quoting — SlyReply's continuity needs reply_in_thread to
# quote the original, like a real client (otherwise the agent loses earlier
# turns and we measure a context-loss a real client would never produce).
# --------------------------------------------------------------------------
def test_build_reply_body_quotes_original_with_attribution():
    original = EmailMessageView(
        id="m1", from_addr="agent@slyreply.ai", to_addrs=["me@x.test"],
        subject="Re: lunch?", text_body="Sure, 1pm works.",
        created_at="2026-05-29T10:00:00Z",
    )
    out = build_reply_body(original, "Great, see you then. PROBE_TOKEN.")
    # New body first, then a standard "On <date>, X wrote:" attribution,
    # then the original quoted with "> ".
    assert "Great, see you then. PROBE_TOKEN." in out
    assert "On 2026-05-29T10:00:00Z, agent@slyreply.ai wrote:" in out
    assert "> Sure, 1pm works." in out


def test_build_reply_body_deepens_an_existing_quote_chain():
    # The original already carries a prior turn as a quote; replying again
    # nests it deeper, carrying the whole thread forward.
    original = EmailMessageView(
        id="m2", from_addr="agent@slyreply.ai", to_addrs=["me@x.test"],
        subject="Re: t", text_body="reply two\n\nOn .., me wrote:\n> turn one",
        created_at="2026-05-29T11:00:00Z",
    )
    out = build_reply_body(original, "turn three")
    assert "> reply two" in out
    assert "> > turn one" in out
