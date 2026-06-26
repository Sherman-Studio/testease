"""Tests for ``runner._format_tool_args`` — Slice 1 of #1078.

The recorder + live-log layers both call this function to render a
single timeline-row string per ToolUseBlock. Slice 1 of run-detail
v2 rewrites the row contents from terse ``key=val`` pairs into
English-y prose ("Clicked X", "Typed 'Y' into Z", "Waited for 'foo'
to appear") and introduces the ``__SKIP__`` sentinel so known-noise
tool calls (snapshot, console_messages, payload-less unknowns) drop
out of the timeline entirely while still advancing the step ordinal
in ``qa_run_steps`` (findings + screenshots key on step_n).

This file pins the per-tool surface contract for the rewrite. The
existing ``test_runner.test_format_tool_args_dispatch`` retains the
"smoke" coverage; this one is granular + parametrized so a regression
in any single row pinpoints exactly which tool's prose changed.

``runner.py`` imports ``claude_agent_sdk`` at module load, which the
local pyenv install doesn't carry; run this file under the harness's
container/CI image or with the SDK installed in your local venv.
"""

from __future__ import annotations

import pytest

from qa_agents.runner import _SKIP_LOG_ROW, _format_tool_args


# ---------------------------------------------------------------------------
# Slice-2 contract: browser_navigate stays RAW so the URL spine parser works.
# ---------------------------------------------------------------------------
class TestBrowserNavigateStaysRaw:
    def test_basic_url(self):
        assert _format_tool_args(
            "mcp__playwright__browser_navigate", {"url": "http://frontend/login"}
        ) == "url=http://frontend/login"

    def test_https_url(self):
        assert _format_tool_args(
            "mcp__playwright__browser_navigate",
            {"url": "https://example.com/foo?bar=baz"},
        ) == "url=https://example.com/foo?bar=baz"

    def test_navigate_back_is_prose(self):
        assert _format_tool_args(
            "mcp__playwright__browser_navigate_back", {}
        ) == "Went back"


# ---------------------------------------------------------------------------
# Known-noise tools — the recorder uses this sentinel to drop the row.
# ---------------------------------------------------------------------------
class TestSkipSentinel:
    def test_skip_constant_is_double_underscore_skip_double_underscore(self):
        """The literal string is part of the contract with run_recorder."""
        assert _SKIP_LOG_ROW == "__SKIP__"

    def test_snapshot_returns_skip(self):
        assert _format_tool_args(
            "mcp__playwright__browser_snapshot", {}
        ) == _SKIP_LOG_ROW

    def test_console_messages_returns_skip(self):
        assert _format_tool_args(
            "mcp__playwright__browser_console_messages", {}
        ) == _SKIP_LOG_ROW

    def test_unknown_tool_no_args_returns_skip(self):
        # Payload-less unknowns have nothing to show — drop the row.
        assert _format_tool_args(
            "mcp__playwright__browser_brand_new_tool", {}
        ) == _SKIP_LOG_ROW

    def test_unknown_tool_with_static_kwarg_returns_skip(self):
        """``static=False`` and similar internal kwargs are noise."""
        assert _format_tool_args(
            "mcp__playwright__browser_brand_new_tool", {"static": False}
        ) == _SKIP_LOG_ROW

    def test_unknown_tool_with_bare_bool_first_returns_skip(self):
        """Bare booleans as the first kwarg are zero-signal."""
        assert _format_tool_args(
            "mcp__playwright__browser_brand_new_tool", {"enabled": True}
        ) == _SKIP_LOG_ROW

    def test_unknown_tool_with_string_first_falls_through(self):
        """A new tool with a useful first kwarg stays visible — we don't
        want a brand-new tool to be silently invisible on the timeline."""
        assert _format_tool_args(
            "mcp__playwright__browser_brand_new_tool", {"target": "homepage"}
        ) == "target=homepage"


# ---------------------------------------------------------------------------
# Click — ref first, falls back to text/element.
# ---------------------------------------------------------------------------
class TestBrowserClick:
    def test_ref_preferred(self):
        assert _format_tool_args(
            "mcp__playwright__browser_click",
            {"ref": "btn-23", "text": "Sign in"},
        ) == "Clicked btn-23"

    def test_text_fallback_when_no_ref(self):
        assert _format_tool_args(
            "mcp__playwright__browser_click",
            {"text": "Sign in"},
        ) == "Clicked Sign in"

    def test_element_fallback_when_no_ref_or_text(self):
        assert _format_tool_args(
            "mcp__playwright__browser_click",
            {"element": "submit-button"},
        ) == "Clicked submit-button"

    def test_no_target_just_says_clicked(self):
        assert _format_tool_args(
            "mcp__playwright__browser_click", {}
        ) == "Clicked"


# ---------------------------------------------------------------------------
# Type — truncates the typed value to 80 chars.
# ---------------------------------------------------------------------------
class TestBrowserType:
    def test_short_text(self):
        assert _format_tool_args(
            "mcp__playwright__browser_type",
            {"ref": "email-input", "text": "hi@example.com"},
        ) == "Typed 'hi@example.com' into email-input"

    def test_no_ref_just_typed_value(self):
        assert _format_tool_args(
            "mcp__playwright__browser_type",
            {"text": "anonymous text"},
        ) == "Typed 'anonymous text'"

    def test_long_text_truncated_at_80(self):
        long_value = "x" * 200
        out = _format_tool_args(
            "mcp__playwright__browser_type",
            {"ref": "r1", "text": long_value},
        )
        # Format: "Typed '<value-or-truncated>' into r1"
        assert out.startswith("Typed '")
        assert out.endswith(" into r1")
        # The single-quoted payload must be capped (ellipsis added when truncating).
        payload = out[len("Typed '"):].rsplit("'", 1)[0]
        assert len(payload) <= 80


# ---------------------------------------------------------------------------
# Fill-form — counts the field list.
# ---------------------------------------------------------------------------
class TestBrowserFillForm:
    def test_single_field(self):
        assert _format_tool_args(
            "mcp__playwright__browser_fill_form",
            {"fields": [{"ref": "x", "value": "y"}]},
        ) == "Filled 1 field"

    def test_multiple_fields(self):
        assert _format_tool_args(
            "mcp__playwright__browser_fill_form",
            {
                "fields": [
                    {"ref": "a", "value": "1"},
                    {"ref": "b", "value": "2"},
                    {"ref": "c", "value": "3"},
                ]
            },
        ) == "Filled 3 fields"

    def test_zero_fields_says_zero(self):
        assert _format_tool_args(
            "mcp__playwright__browser_fill_form", {"fields": []}
        ) == "Filled 0 fields"


# ---------------------------------------------------------------------------
# Select option — values may be string or list.
# ---------------------------------------------------------------------------
class TestBrowserSelectOption:
    def test_string_value(self):
        assert _format_tool_args(
            "mcp__playwright__browser_select_option",
            {"ref": "country", "values": "UK"},
        ) == "Selected 'UK' in country"

    def test_list_values(self):
        out = _format_tool_args(
            "mcp__playwright__browser_select_option",
            {"ref": "tags", "values": ["news", "sports"]},
        )
        assert out == "Selected 'news, sports' in tags"

    def test_no_ref(self):
        assert _format_tool_args(
            "mcp__playwright__browser_select_option", {"values": "UK"}
        ) == "Selected 'UK'"


# ---------------------------------------------------------------------------
# Press key / hover / wait_for — simple prose.
# ---------------------------------------------------------------------------
class TestSimpleInteractions:
    def test_press_key(self):
        assert _format_tool_args(
            "mcp__playwright__browser_press_key", {"key": "Enter"}
        ) == "Pressed Enter"

    def test_press_key_missing(self):
        assert _format_tool_args(
            "mcp__playwright__browser_press_key", {}
        ) == "Pressed key"

    def test_hover(self):
        assert _format_tool_args(
            "mcp__playwright__browser_hover", {"ref": "nav-account"}
        ) == "Hovered nav-account"

    def test_hover_no_ref(self):
        assert _format_tool_args(
            "mcp__playwright__browser_hover", {}
        ) == "Hovered"


class TestBrowserWaitFor:
    def test_wait_for_text(self):
        assert _format_tool_args(
            "mcp__playwright__browser_wait_for",
            {"text": "Welcome back"},
        ) == "Waited for 'Welcome back' to appear"

    def test_wait_for_time(self):
        assert _format_tool_args(
            "mcp__playwright__browser_wait_for",
            {"time": 5},
        ) == "Waited 5s"

    def test_wait_no_args(self):
        assert _format_tool_args(
            "mcp__playwright__browser_wait_for", {}
        ) == "Waited"


# ---------------------------------------------------------------------------
# Screenshot — keeps the row, prose label. Slice 3 wires the inline image.
# ---------------------------------------------------------------------------
class TestScreenshot:
    def test_take_screenshot_prose(self):
        assert _format_tool_args(
            "mcp__playwright__browser_take_screenshot", {}
        ) == "Captured screenshot"


# ---------------------------------------------------------------------------
# Resize / evaluate / tabs.
# ---------------------------------------------------------------------------
class TestMiscBrowserTools:
    def test_resize(self):
        assert _format_tool_args(
            "mcp__playwright__browser_resize", {"width": 1280, "height": 720}
        ) == "Resized viewport to 1280×720"

    def test_evaluate(self):
        assert _format_tool_args(
            "mcp__playwright__browser_evaluate",
            {"function": "() => document.title"},
        ) == "Ran custom JavaScript"

    def test_run_code_unsafe(self):
        assert _format_tool_args(
            "mcp__playwright__browser_run_code_unsafe",
            {"code": "window.location.reload()"},
        ) == "Ran custom JavaScript"

    def test_tabs(self):
        assert _format_tool_args(
            "mcp__playwright__browser_tabs", {"action": "new"}
        ) == "Tabs: new"


# ---------------------------------------------------------------------------
# Findings — UNCHANGED (the findings panel already surfaces this well).
# ---------------------------------------------------------------------------
class TestFindingsNote:
    def test_note_finding_preserves_cat_sev_title(self):
        out = _format_tool_args(
            "mcp__findings__note_finding",
            {
                "category": "confusion",
                "severity": "major",
                "title": "Couldn't find where to sign up",
                "body": "...",
            },
        )
        assert out.startswith("confusion/major ")
        assert "Couldn't find where to sign up" in out

    def test_note_finding_long_title_truncated(self):
        out = _format_tool_args(
            "mcp__findings__note_finding",
            {
                "category": "ux",
                "severity": "minor",
                "title": "x" * 200,
            },
        )
        # Title is truncated; the prefix is still intact.
        assert out.startswith("ux/minor ")
        # Truncated ellipsis must be present somewhere.
        assert "…" in out


# ---------------------------------------------------------------------------
# Email tools — prose form.
# ---------------------------------------------------------------------------
class TestEmailTools:
    def test_send_email(self):
        assert _format_tool_args(
            "mcp__email__send_email",
            {"to": "agent@slyreply.ai", "subject": "Hi there", "body": "..."},
        ) == "Emailed agent@slyreply.ai: 'Hi there'"

    def test_wait_for_email(self):
        assert _format_tool_args(
            "mcp__email__wait_for_email",
            {"to_address": "agent@slyreply.ai", "timeout_s": 30},
        ) == "Waited for email to agent@slyreply.ai (30s timeout)"

    def test_wait_for_email_accepts_plain_to_kwarg(self):
        # Some callers pass ``to=`` rather than ``to_address=``.
        assert _format_tool_args(
            "mcp__email__wait_for_email",
            {"to": "agent@slyreply.ai", "timeout_s": 30},
        ) == "Waited for email to agent@slyreply.ai (30s timeout)"

    def test_get_email(self):
        assert _format_tool_args(
            "mcp__email__get_email", {"id": "abc-123"}
        ) == "Read email abc-123"


# ---------------------------------------------------------------------------
# Parametrized smoke — one row per known tool stays prose / sentinel / raw.
# A regression in any single entry pinpoints which row's contract changed.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,args,expected",
    [
        (
            "mcp__playwright__browser_navigate",
            {"url": "http://x"},
            "url=http://x",
        ),
        ("mcp__playwright__browser_navigate_back", {}, "Went back"),
        ("mcp__playwright__browser_snapshot", {}, "__SKIP__"),
        ("mcp__playwright__browser_console_messages", {}, "__SKIP__"),
        (
            "mcp__playwright__browser_click",
            {"ref": "btn-1"},
            "Clicked btn-1",
        ),
        (
            "mcp__playwright__browser_type",
            {"ref": "in-1", "text": "hi"},
            "Typed 'hi' into in-1",
        ),
        (
            "mcp__playwright__browser_press_key",
            {"key": "Tab"},
            "Pressed Tab",
        ),
        (
            "mcp__playwright__browser_hover",
            {"ref": "menu"},
            "Hovered menu",
        ),
        (
            "mcp__playwright__browser_take_screenshot",
            {},
            "Captured screenshot",
        ),
        (
            "mcp__playwright__browser_resize",
            {"width": 1024, "height": 768},
            "Resized viewport to 1024×768",
        ),
        (
            "mcp__playwright__browser_evaluate",
            {"function": "() => 1"},
            "Ran custom JavaScript",
        ),
        (
            "mcp__playwright__browser_tabs",
            {"action": "switch"},
            "Tabs: switch",
        ),
        (
            "mcp__email__get_email",
            {"id": "id-1"},
            "Read email id-1",
        ),
    ],
)
def test_prose_smoke(name, args, expected):
    assert _format_tool_args(name, args) == expected
