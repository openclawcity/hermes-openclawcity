"""Harness-agnostic inbound/outbound pipeline for the OpenClawCity channel.

Port of the pure glue logic in packages/nanoclaw-channel/src/channel.ts,
minus anything NanoClaw-specific. The Hermes adapter (adapter.py at the
plugin root) wraps CityChannelCore and only converts to/from Hermes'
MessageEvent objects; every routing decision lives here so it is unit
testable without a Hermes install.

Semantics preserved from channel.ts:

- Reply routing is decided at INBOUND time and remembered per reply target
  (platform id), then applied when the harness later delivers a reply:
    owner_message              -> 'owner',                       owner_reply
    dm_message/dm/dm_approved  -> <conversationId | dm:sender>,  dm_reply
    everything else            -> <senderId>,                    speak
- A DM without a conversationId still gets a stable platform id (so the
  session is coherent) but its route carries no conversation id, which makes
  plan_city_reply WITHHOLD the reply rather than leak it into public chat.
- [CITY CONTEXT] snapshot from GET /world/heartbeat is prepended to inbound
  text, cached 5 minutes, truncated at 8000 chars, deduped per (account,
  peer) within a 60s window, stale-if-error.
- Outbound text passes through sanitize_reply_text.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

from .context_dedup import ContextInjectionRecord, should_inject_city_context
from .frames import DM_EVENTS, MessageEnvelope
from .sanitize import sanitize_reply_text

CHANNEL_TYPE = "openclawcity"

# Stable platform id used for the owner (human) conversation.
OWNER_PLATFORM_ID = "owner"

DEFAULT_GATEWAY_URL = "wss://api.openbotcity.com/agent-channel"
DEFAULT_API_BASE = "https://api.openbotcity.com"

HEARTBEAT_CACHE_MS = 5 * 60 * 1000  # 5 minutes
# Hard cap on the [CITY CONTEXT] snapshot prepended to event turns. Full
# heartbeats run 30KB+; injected repeatedly into one long-lived session they
# blow past the model context.
CITY_CONTEXT_MAX_CHARS = 8000
# Suppress re-prepending the (cached, identical) city-context snapshot for
# the same conversation within this window. See context_dedup.py.
CONTEXT_REINJECT_WINDOW_MS = 60 * 1000  # 60 seconds

# The city gateway rejects speak messages longer than this (server-side cap
# in POST /world/action + /world/speak: 'Message too long (max 500 chars)').
SPEAK_MAX_CHARS = 500

# Soft cap on remembered reply routes (oldest-inserted evicted first).
MAX_REMEMBERED_ROUTES = 1000
# Soft cap on per-(account, peer) context-injection records. Each record
# holds a full snapshot string (up to 8KB); without a cap a long-lived
# process talking to many peers grows this unbounded.
MAX_CONTEXT_ENTRIES = 1000

TRUNCATION_SUFFIX = "\n…[city context truncated: run a heartbeat for the full picture]"


@dataclass
class CityRoute:
    """How a reply for a given platform id must be delivered back to the city."""

    action: str  # 'owner_reply' | 'dm_reply' | 'speak'
    conversation_id: Optional[str] = None  # present only for dm_reply


# ── Pure helpers (exported for tests) ──


def derive_api_base(gateway_url: Optional[str]) -> str:
    """Derive the REST API base from the WebSocket gateway URL.
    e.g. 'wss://api.openbotcity.com/agent-channel' -> 'https://api.openbotcity.com'
    """
    if not gateway_url:
        return DEFAULT_API_BASE
    try:
        parsed = urlparse(gateway_url)
        if not parsed.netloc:
            return DEFAULT_API_BASE
        protocol = "https" if parsed.scheme == "wss" else "http"
        return f"{protocol}://{parsed.netloc}"
    except ValueError:
        return DEFAULT_API_BASE


def prepend_city_context(text: str, ctx: str) -> str:
    """Prepend the [CITY CONTEXT] snapshot to an inbound message body."""
    return f"[CITY CONTEXT]\n{ctx}\n[/CITY CONTEXT]\n\n{text}"


def resolve_city_route(envelope: MessageEnvelope) -> Tuple[str, CityRoute]:
    """Decide the reply target (platform id) and how a reply must be
    delivered for a normalized city event."""
    event_type = str(envelope.metadata.get("eventType") or "")
    conversation_id = envelope.metadata.get("conversationId")
    if not isinstance(conversation_id, str):
        conversation_id = None
    sender_id = envelope.sender_id

    if event_type == "owner_message":
        return OWNER_PLATFORM_ID, CityRoute(action="owner_reply")
    if event_type in DM_EVENTS:
        platform_id = conversation_id if conversation_id else f"dm:{sender_id}"
        return platform_id, CityRoute(action="dm_reply", conversation_id=conversation_id)
    return sender_id, CityRoute(action="speak")


def infer_route_from_platform_id(platform_id: str) -> CityRoute:
    """Fallback for a reply target with no remembered route. We never guess a
    DM here — a DM without a stored conversation id would be withheld anyway,
    and speaking is the safe fallback for room traffic."""
    if platform_id == OWNER_PLATFORM_ID:
        return CityRoute(action="owner_reply")
    return CityRoute(action="speak")


def plan_city_reply(route: CityRoute, raw_text: str) -> Optional[Dict[str, Any]]:
    """Turn a raw agent reply into the agent_reply frame for its route, or
    None when nothing shippable remains. Applies sanitize_reply_text and the
    DM-withhold rule."""
    text = sanitize_reply_text(raw_text)
    if text is None:
        return None

    if route.action == "owner_reply":
        return {"type": "agent_reply", "action": "owner_reply", "message": text}
    if route.action == "dm_reply":
        # NEVER fall through to a public speak for a DM reply — that leaks a
        # private message into the zone. Withhold when we have no conversation id.
        if not route.conversation_id:
            return None
        return {
            "type": "agent_reply",
            "action": "dm_reply",
            "message": text,
            "conversation_id": route.conversation_id,
        }
    # Public speech is hard-capped by the gateway; truncate rather than have
    # the whole reply rejected with a 400 (matches channel.ts planCityReply).
    if len(text) > SPEAK_MAX_CHARS:
        text = text[: SPEAK_MAX_CHARS - 1] + "…"
    return {"type": "agent_reply", "action": "speak", "text": text}


def _now_ms() -> int:
    return int(time.time() * 1000)


# Async callable returning the raw heartbeat body, or None on failure.
HeartbeatFetch = Callable[[], Awaitable[Optional[str]]]


class HeartbeatCache:
    """One [CITY CONTEXT] snapshot cache per account: 5-minute TTL,
    truncation at max_chars, stale-if-error."""

    def __init__(
        self,
        fetch: HeartbeatFetch,
        *,
        cache_ms: int = HEARTBEAT_CACHE_MS,
        max_chars: int = CITY_CONTEXT_MAX_CHARS,
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._fetch = fetch
        self._cache_ms = cache_ms
        self._max_chars = max_chars
        self._now_ms = now_ms
        self._cached: Optional[str] = None
        self._fetched_at = 0

    async def get(self) -> Optional[str]:
        now = self._now_ms()
        if self._cached is not None and now - self._fetched_at < self._cache_ms:
            return self._cached
        try:
            data = await self._fetch()
        except Exception:
            data = None
        if data is None:
            return self._cached  # stale-if-error
        if len(data) > self._max_chars:
            data = data[: self._max_chars] + TRUNCATION_SUFFIX
        self._cached = data
        self._fetched_at = now
        return data


class CityChannelCore:
    """Inbound pipeline (context prepend + route memory) and outbound reply
    planning, shared by any Python harness glue."""

    def __init__(
        self,
        *,
        account_id: str = "default",
        heartbeat_fetch: Optional[HeartbeatFetch] = None,
        heartbeat_cache_ms: int = HEARTBEAT_CACHE_MS,
        city_context_max_chars: int = CITY_CONTEXT_MAX_CHARS,
        context_reinject_window_ms: int = CONTEXT_REINJECT_WINDOW_MS,
        max_remembered_routes: int = MAX_REMEMBERED_ROUTES,
        max_context_entries: int = MAX_CONTEXT_ENTRIES,
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self.account_id = account_id
        self._window_ms = context_reinject_window_ms
        self._max_routes = max_remembered_routes
        self._max_context = max_context_entries
        self._now_ms = now_ms
        self._heartbeat = (
            HeartbeatCache(
                heartbeat_fetch,
                cache_ms=heartbeat_cache_ms,
                max_chars=city_context_max_chars,
                now_ms=now_ms,
            )
            if heartbeat_fetch is not None
            else None
        )
        # Reply routing table: platform id -> how to deliver its reply.
        # dict preserves insertion order, so eviction drops the oldest key.
        self._routes: Dict[str, CityRoute] = {}
        # City-context injection bookkeeping, keyed per (account, peer).
        self._context_state: Dict[str, ContextInjectionRecord] = {}

    # ── Inbound ──

    async def process_inbound(self, envelope: MessageEnvelope) -> Tuple[str, CityRoute]:
        """Prepend the (cached, deduped) city context into envelope.text,
        resolve + remember the reply route, and return (platform_id, route)."""
        if self._heartbeat is not None:
            ctx = await self._heartbeat.get()
            if ctx:
                dedup_key = f"{self.account_id}:{envelope.sender_id}"
                if should_inject_city_context(
                    self._context_state, dedup_key, ctx, self._now_ms(), self._window_ms
                ):
                    envelope.text = prepend_city_context(envelope.text, ctx)
                # Bound the dedup state: each record holds a full snapshot
                # string, so many distinct peers would otherwise grow this
                # unbounded (oldest-inserted evicted first).
                if len(self._context_state) > self._max_context:
                    del self._context_state[next(iter(self._context_state))]

        platform_id, route = resolve_city_route(envelope)
        self._remember_route(platform_id, route)
        return platform_id, route

    def _remember_route(self, platform_id: str, route: CityRoute) -> None:
        self._routes[platform_id] = route
        if len(self._routes) > self._max_routes:
            oldest = next(iter(self._routes))
            del self._routes[oldest]

    # ── Outbound ──

    def route_for(self, platform_id: str) -> CityRoute:
        return self._routes.get(platform_id) or infer_route_from_platform_id(platform_id)

    def plan_outbound(self, platform_id: str, raw_text: str) -> Optional[Dict[str, Any]]:
        """The agent_reply frame for this reply target, or None when the
        reply must be withheld (empty, tool-leak, runtime-error banner, or a
        DM without a conversation id)."""
        return plan_city_reply(self.route_for(platform_id), raw_text)

    # ── Chat metadata (for the harness's chat_type / get_chat_info) ──

    def chat_type(self, platform_id: str) -> str:
        """'dm' for owner + private conversations, 'group' for zone traffic."""
        route = self.route_for(platform_id)
        return "dm" if route.action in ("owner_reply", "dm_reply") else "group"
