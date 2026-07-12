"""Harness-agnostic OpenClawCity channel core.

Python port of packages/nanoclaw-channel/src (TypeScript). No Hermes imports
in this package — everything here is unit-testable standalone. The Hermes
glue lives in ../adapter.py.
"""
from .channel import (  # noqa: F401
    CHANNEL_TYPE,
    CITY_CONTEXT_MAX_CHARS,
    CONTEXT_REINJECT_WINDOW_MS,
    DEFAULT_API_BASE,
    DEFAULT_GATEWAY_URL,
    HEARTBEAT_CACHE_MS,
    OWNER_PLATFORM_ID,
    CityChannelCore,
    CityRoute,
    HeartbeatCache,
    derive_api_base,
    infer_route_from_platform_id,
    plan_city_reply,
    prepend_city_context,
    resolve_city_route,
)
from .client import ClientConfig, OpenClawCityClient  # noqa: F401
from .context_dedup import ContextInjectionRecord, should_inject_city_context  # noqa: F401
from .frames import (  # noqa: F401
    DIRECT_MENTION_EVENTS,
    DM_EVENTS,
    ConnectionState,
    MessageEnvelope,
    is_direct_mention,
    is_dm_event,
)
from .normalizer import format_event_text, format_welcome_text, normalize  # noqa: F401
from .sanitize import sanitize_reply_text  # noqa: F401
from .token_cache import load_refreshed_token, save_refreshed_token  # noqa: F401
