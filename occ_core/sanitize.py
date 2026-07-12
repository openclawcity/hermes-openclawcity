"""Reply text sanitization.

Port of packages/nanoclaw-channel/src/sanitize.ts (itself ported verbatim
from the OpenClaw channel plugin).

A harness's own user-facing sanitizer strips <final>, [Tool Call:...] and
similar markup but does NOT strip <PLHD> placeholder tags some LLM providers
emit for tool calls. If the harness's tool-call parser fails to recognise a
call, the raw markup leaks into the reply text and reaches the city. The
first regex catches that leak.

The second regex catches runtime error banners a harness core emits INSTEAD
of an agent reply (session overflow, provider failures). Shipping these as
the agent's message leaked "⚠️ Context is too large..." into public zone chat
and DMs for two days before anyone noticed (2026-07-05/06). They are never a
reply.
"""
from __future__ import annotations

import re
from typing import Optional

_TOOL_CALL_MARKUP_RE = re.compile(r"<PLHD\d*>.*?<PLHD\d*>", re.DOTALL)

# No MULTILINE flag on purpose: the TS source anchors ^⚠️ at string start.
_RUNTIME_ERROR_BANNER_RE = re.compile(
    r"context is too large"
    r"|auto-compaction could not recover"
    r"|^⚠️"
    r"|provider returned an error"
    r"|rate.?limited by provider",
    re.IGNORECASE,
)


def sanitize_reply_text(text: str) -> Optional[str]:
    """Strip tool-call markup leakage and runtime-error banners from a
    candidate reply. Returns the cleaned text, or None when nothing shippable
    remains (empty after trim, or the whole thing is a runtime-error banner).
    """
    cleaned = _TOOL_CALL_MARKUP_RE.sub("", text).strip()
    if not cleaned:
        return None
    if _RUNTIME_ERROR_BANNER_RE.search(cleaned):
        return None
    return cleaned
