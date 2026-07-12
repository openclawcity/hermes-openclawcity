"""Port of packages/nanoclaw-channel/tests/sanitize-reply.test.ts."""
from occ_core.sanitize import sanitize_reply_text


# ── Clean text passes through unchanged ──


def test_returns_clean_text_as_is():
    assert sanitize_reply_text("Hello, how are you?") == "Hello, how are you?"


def test_preserves_markdown_formatting():
    md = "**Bold** and _italic_ and `code`"
    assert sanitize_reply_text(md) == md


def test_preserves_multi_line_text():
    text = "Line 1\nLine 2\nLine 3"
    assert sanitize_reply_text(text) == text


def test_preserves_legitimate_angle_brackets():
    assert sanitize_reply_text("Use <div> tags for layout") == "Use <div> tags for layout"


def test_preserves_plh_lookalikes():
    assert sanitize_reply_text("See section <PLH> for details") == "See section <PLH> for details"


# ── PLHD tool-call markup stripping ──


def test_strips_simple_plhd_markup():
    leaked = '<PLHD>[{"name":"read","parameters":{"path":"/home/user/file.md"}}]<PLHD>'
    assert sanitize_reply_text(leaked) is None


def test_strips_numbered_plhd_markup():
    leaked = '<PLHD20>[{"name":"read","parameters":{"path":"/home/vincent/.openclaw/workspace/HEARTBEAT.md"}}]<PLHD21>'
    assert sanitize_reply_text(leaked) is None


def test_strips_plhd_preserving_surrounding_text():
    text = 'Let me check that for you. <PLHD>[{"name":"read","parameters":{"path":"/tmp/file"}}]<PLHD> I will look into it.'
    assert sanitize_reply_text(text) == "Let me check that for you.  I will look into it."


def test_strips_multiple_plhd_blocks():
    text = '<PLHD1>[{"name":"read","parameters":{}}]<PLHD2> some text <PLHD3>[{"name":"write","parameters":{}}]<PLHD4>'
    assert sanitize_reply_text(text) == "some text"


def test_strips_plhd_with_multiline_json():
    leaked = '<PLHD>[\n  {\n    "name": "read",\n    "parameters": {\n      "path": "/tmp/file"\n    }\n  }\n]<PLHD>'
    assert sanitize_reply_text(leaked) is None


# ── Empty / whitespace handling ──


def test_returns_none_for_empty_string():
    assert sanitize_reply_text("") is None


def test_returns_none_for_whitespace_only():
    assert sanitize_reply_text("   \n  \t  ") is None


def test_returns_none_when_stripping_leaves_whitespace():
    assert sanitize_reply_text('  <PLHD>[{"name":"x"}]<PLHD>  ') is None


# ── Edge cases ──


def test_lone_opening_plhd_tag_preserved():
    assert sanitize_reply_text("text <PLHD> more text") == "text <PLHD> more text"


def test_very_long_clean_text():
    long_text = "A" * 10000
    assert sanitize_reply_text(long_text) == long_text


def test_trims_whitespace_from_result():
    assert sanitize_reply_text("  hello world  ") == "hello world"


# ── Runtime error banners are never a reply ──


def test_drops_context_overflow_banners():
    assert (
        sanitize_reply_text(
            "⚠️ Context is too large and auto-compaction could not recover this turn. Try again."
        )
        is None
    )
    assert (
        sanitize_reply_text("Context is too large and auto-compaction could not recover this turn.")
        is None
    )


def test_keeps_normal_replies_intact():
    text = "The context of our poem: wings carry words."
    assert sanitize_reply_text(text) == text
