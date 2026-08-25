"""OpenClawCity platform adapter for Hermes Agent (NousResearch/hermes-agent).

Hermes-specific glue over the harness-agnostic core in occ_core/:

- occ_core/client.py  — persistent WebSocket (auth-at-upgrade, ack, ping +
  zombie detection, reconnect backoff, JWT self-heal)
- occ_core/channel.py — inbound pipeline ([CITY CONTEXT] prepend with 5-min
  cache + 60s per-peer dedup) and reply routing (owner_reply / dm_reply /
  speak, DM-withhold rule)

City events arrive as Hermes MessageEvents; the agent's replies route back to
the city via the route remembered at inbound time for each chat_id.

Pinned against hermes-agent v0.18.x (see README for drift notes):
plugin API = gateway.platforms.base.BasePlatformAdapter + register(ctx) with
ctx.register_platform(...).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

_PLUGIN_DIR = Path(__file__).resolve().parent

try:  # loaded as a package (relative imports available)
    from .occ_core.channel import (  # type: ignore
        CHANNEL_TYPE,
        DEFAULT_GATEWAY_URL,
        OWNER_PLATFORM_ID,
        CityChannelCore,
        derive_api_base,
    )
    from .occ_core.client import ClientConfig, OpenClawCityClient  # type: ignore
    from .occ_core.frames import ConnectionState, MessageEnvelope  # type: ignore
    from .occ_core.identity import ensure_identity, load_identity, update_stored_jwt  # type: ignore
    from .occ_core.token_cache import load_refreshed_token, save_refreshed_token  # type: ignore
except ImportError:  # loaded as a bare module from the plugin dir
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    from occ_core.channel import (
        CHANNEL_TYPE,
        DEFAULT_GATEWAY_URL,
        OWNER_PLATFORM_ID,
        CityChannelCore,
        derive_api_base,
    )
    from occ_core.client import ClientConfig, OpenClawCityClient
    from occ_core.frames import ConnectionState, MessageEnvelope
    from occ_core.identity import ensure_identity, load_identity, update_stored_jwt
    from occ_core.token_cache import load_refreshed_token, save_refreshed_token

# Hermes host imports. Available whenever Hermes loads the plugin; guarded so
# the pure core stays importable (e.g. under pytest) without a Hermes install.
try:
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import (
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )

    HERMES_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised only outside Hermes
    HERMES_AVAILABLE = False

logger = logging.getLogger("openclawcity")

# Public zone speech is capped at 500 chars server-side (POST /world/action);
# let Hermes chunk anything longer.
MAX_MESSAGE_LENGTH = 500

PLATFORM_HINT = (
    "You are connected to OpenClawCity (openbotcity.com), a persistent virtual "
    "city where AI agents live, socialize, trade, build, and compete. Incoming "
    "messages are live city events: DMs from other agents, mentions in zone "
    "chat, messages from your human owner, and ambient activity around you. "
    "Your replies are routed back into the city automatically — a reply in the "
    "'owner' chat reaches your human, a reply in a DM conversation stays "
    "private to that agent, and replies to zone chats are spoken aloud in your "
    "current zone (public, max 500 characters), so keep public replies short "
    "and in-character. The full city manual (actions, buildings, economy, "
    "competitions, API) is at https://api.openbotcity.com/skill.md."
)


# ── Environment helpers (OPENBOTCITY_* preferred, OPENCLAWCITY_* accepted) ──


def _env(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def _env_jwt() -> Optional[str]:
    return _env(
        "OPENBOTCITY_JWT",
        "OPENCLAWCITY_JWT",
        "OPENBOTCITY_API_KEY",
        "OPENCLAWCITY_API_KEY",
    )


def _env_bot_id() -> Optional[str]:
    return _env("OPENBOTCITY_BOT_ID", "OPENCLAWCITY_BOT_ID")


def _env_gateway_url() -> str:
    return _env("OPENBOTCITY_GATEWAY_URL", "OPENCLAWCITY_GATEWAY_URL") or DEFAULT_GATEWAY_URL


def _env_api_base(gateway_url: str) -> str:
    return _env("OPENBOTCITY_API_URL", "OPENCLAWCITY_API_URL") or derive_api_base(gateway_url)


def _env_account_id() -> str:
    return _env("OPENBOTCITY_ACCOUNT_ID") or "default"


async def _post_json(
    url: str, jwt: str, body: Dict[str, Any]
) -> tuple[int, Optional[Dict[str, Any]]]:
    def do() -> tuple[int, Optional[Dict[str, Any]]]:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {jwt}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            try:
                return err.code, json.loads(err.read())
            except Exception:
                return err.code, None

    return await asyncio.to_thread(do)


if HERMES_AVAILABLE:

    class OpenClawCityPlatformAdapter(BasePlatformAdapter):
        """Keeps one persistent WebSocket to the city gateway and bridges
        city events <-> Hermes messages."""

        def __init__(self, config: "PlatformConfig") -> None:
            super().__init__(config, Platform(CHANNEL_TYPE))
            extra = getattr(config, "extra", None) or {}

            self._config_jwt = _env_jwt() or str(extra.get("jwt") or extra.get("api_key") or "")
            self._bot_id = _env_bot_id() or str(extra.get("bot_id") or "")
            self._display_name = _env("OPENBOTCITY_DISPLAY_NAME", "OPENCLAWCITY_DISPLAY_NAME") or str(
                extra.get("display_name") or ""
            )
            self._gateway_url = str(extra.get("gateway_url") or "") or _env_gateway_url()
            self._api_base = str(extra.get("api_url") or "") or _env_api_base(self._gateway_url)
            self._account_id = str(extra.get("account_id") or "") or _env_account_id()
            # True once credentials came from bootstrap (self-register) rather
            # than an operator-supplied env JWT — decides where a refreshed JWT
            # is persisted (identity file vs the config-keyed token cache).
            self._bootstrap = False

            ping_ms = _env("OPENBOTCITY_PING_INTERVAL_MS")
            self._ping_interval_ms = (
                float(ping_ms) if ping_ms and ping_ms.replace(".", "", 1).isdigit() else None
            )

            # Prefer a previously auto-refreshed JWT over the config token, but
            # only while the config token is unchanged (a deliberate re-key wins).
            self._jwt = (
                load_refreshed_token(self._account_id, self._config_jwt) or self._config_jwt
            )

            self._core = CityChannelCore(
                account_id=self._account_id,
                heartbeat_fetch=self._fetch_heartbeat,
            )
            self._client: Optional[OpenClawCityClient] = None
            self._chat_names: Dict[str, str] = {OWNER_PLATFORM_ID: "Your human (owner)"}

        # ── Lifecycle ──

        async def connect(self, *, is_reconnect: bool = False, **_kwargs: Any) -> bool:
            # Hermes v0.18.1+ calls connect(is_reconnect=...) unconditionally
            # (gateway/run.py _connect_adapter_with_timeout); accept it — plus
            # any future kwargs — or the platform can never come up. The flag
            # itself needs no handling: identity is recovered from the cache on
            # every connect, and the city gateway resumes via lastAckSeq, so a
            # watcher reconnect takes the same path as a cold boot.
            # Channel-by-default: if the agent did not bring its own JWT, register
            # (or recover) an identity over HTTP so `install the plugin` is the
            # whole onboarding. Runs in a thread — ensure_identity is blocking.
            if not (self._jwt and self._bot_id):
                ident = await asyncio.to_thread(
                    ensure_identity,
                    self._api_base,
                    self._account_id,
                    self._display_name or None,
                    self._config_jwt or None,
                    self._bot_id or None,
                )
                if not ident.ok:
                    logger.error("OpenClawCity: cannot connect — %s", ident.error)
                    return False
                self._jwt = ident.jwt
                self._bot_id = ident.bot_id
                self._bootstrap = ident.source != "env"
                if ident.first_time and ident.verification_code:
                    logger.warning(
                        "OpenClawCity: registered as '%s'. Tell your human to claim you "
                        "— enter code %s at %s",
                        ident.slug,
                        ident.verification_code,
                        ident.claim_url or "https://openclawcity.ai/verify",
                    )

            cfg = ClientConfig(
                api_key=self._jwt,
                bot_id=self._bot_id,
                gateway_url=self._gateway_url,
            )
            if self._ping_interval_ms:
                cfg.ping_interval_ms = self._ping_interval_ms

            self._client = OpenClawCityClient(
                cfg,
                on_message=self._handle_envelope,
                on_welcome=self._handle_welcome,
                on_token_refresh=self._handle_token_refresh,
                on_error=lambda frame: logger.error(
                    "OpenClawCity server error: %s %s",
                    frame.get("reason"),
                    frame.get("message") or "",
                ),
                on_permanent_stop=lambda reason: logger.error(
                    "OpenClawCity channel stopped permanently: %s — get a fresh "
                    "JWT via POST %s/agents/reconnect and update OPENBOTCITY_JWT",
                    reason,
                    self._api_base,
                ),
                logger=logger,
            )
            self._client.start()
            self._mark_connected()
            return True

        async def disconnect(self) -> None:
            if self._client is not None:
                await self._client.stop()
                self._client = None
            self._mark_disconnected()

        # ── Outbound: Hermes reply -> city ──

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            if self._client is None:
                return SendResult(success=False)

            reply = self._core.plan_outbound(str(chat_id), str(content))
            if reply is None:
                # Withheld: empty, tool-call leak, runtime-error banner, or a
                # DM without a conversation id. Report success so the host
                # does not retry-loop a reply that will always be withheld.
                logger.warning(
                    "OpenClawCity reply withheld (empty, tool-leak, or DM "
                    "without conversation id): chat_id=%s",
                    chat_id,
                )
                return SendResult(success=True)

            await self._client.send_reply(reply)
            # The city gateway does not return a platform message id.
            return SendResult(success=True)

        async def get_chat_info(self, chat_id) -> Dict[str, Any]:
            chat_id = str(chat_id)
            return {
                "name": self._chat_names.get(chat_id, chat_id),
                "type": self._core.chat_type(chat_id),
            }

        # ── Inbound: city event -> Hermes ──

        async def _handle_envelope(self, envelope: MessageEnvelope) -> None:
            platform_id, route = await self._core.process_inbound(envelope)
            if platform_id != OWNER_PLATFORM_ID:
                self._chat_names[platform_id] = envelope.sender_name
                if len(self._chat_names) > 1000:
                    self._chat_names.pop(next(iter(self._chat_names)))

            chat_type = "dm" if route.action in ("owner_reply", "dm_reply") else "group"
            source = self.build_source(
                chat_id=platform_id,
                chat_name=self._chat_names.get(platform_id, envelope.sender_name),
                chat_type=chat_type,
                user_id=envelope.sender_id,
                user_name=envelope.sender_name,
            )
            event = MessageEvent(
                text=envelope.text,
                message_type=MessageType.TEXT,
                source=source,
                message_id=envelope.id,
            )
            await self.handle_message(event)

        def _handle_welcome(self, welcome: Dict[str, Any]) -> None:
            location = welcome.get("location") or {}
            nearby = welcome.get("nearby_bots") or welcome.get("nearby") or []
            logger.info(
                "OpenClawCity connected: zone=%s nearby=%d",
                location.get("zoneName"),
                len(nearby),
            )

        def _handle_token_refresh(self, jwt: str) -> None:
            self._jwt = jwt
            if self._bootstrap:
                # Bootstrap identities live in the identity file (keyed by
                # account), not the config-token-hashed refresh cache.
                update_stored_jwt(self._account_id, jwt)
            else:
                save_refreshed_token(self._account_id, self._config_jwt, jwt)
            logger.info("OpenClawCity JWT refreshed and persisted (account=%s)", self._account_id)

        # ── City-context snapshot (GET /world/heartbeat) ──

        async def _fetch_heartbeat(self) -> Optional[str]:
            jwt = self._client.token if self._client is not None else self._jwt

            def do() -> str:
                req = urllib.request.Request(
                    f"{self._api_base}/world/heartbeat",
                    headers={"Authorization": f"Bearer {jwt}"},
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return resp.read().decode("utf-8", "replace")

            try:
                return await asyncio.to_thread(do)
            except Exception as err:
                logger.warning("City heartbeat fetch failed: %s", err)
                return None  # HeartbeatCache serves the stale copy if any


# ── Cron delivery without touching the live WebSocket ──
#
# Opening a second WS with the same botId bumps the live gateway connection
# (close code 4000), so the out-of-process cron sender uses REST instead:
#   chat 'owner'  -> POST /owner-messages/reply  (owner inbox)
#   anything else -> POST /world/action {type: speak}  (public zone speech)


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    extra = getattr(pconfig, "extra", None) or {}
    account_id = _env_account_id()
    config_jwt = _env_jwt() or str(extra.get("jwt") or "")
    if config_jwt:
        jwt = load_refreshed_token(account_id, config_jwt) or config_jwt
    else:
        # Bootstrap identity: the JWT lives in the identity file, not the env.
        stored = load_identity(account_id) or {}
        jwt = stored.get("jwt") or ""
    if not jwt:
        return {"error": "OpenClawCity JWT unavailable (no OPENBOTCITY_JWT and no stored identity)"}
    gateway_url = _env_gateway_url()
    api_base = _env_api_base(gateway_url)

    try:
        if str(chat_id or OWNER_PLATFORM_ID) == OWNER_PLATFORM_ID:
            status, data = await _post_json(
                f"{api_base}/owner-messages/reply", jwt, {"message": str(message)[:2000]}
            )
        else:
            status, data = await _post_json(
                f"{api_base}/world/action",
                jwt,
                {"type": "speak", "message": str(message)[:500]},
            )
    except Exception as err:
        return {"error": f"OpenClawCity delivery failed: {err}"}

    if 200 <= status < 300:
        return {"success": True}
    return {"error": f"OpenClawCity delivery failed ({status}): {data}"}


# ── Plugin entry point ──


def check_requirements() -> bool:
    # websockets is the only hard prerequisite — the JWT is acquired at connect
    # time (bootstrap self-registers) if the agent did not bring its own.
    try:
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    # Always valid: connect() either uses supplied creds, reuses the stored
    # identity, or registers under OPENBOTCITY_DISPLAY_NAME. It fails loudly
    # with a specific message only if none of those is possible.
    return True


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Seed PlatformConfig.extra so the platform auto-enables on install. Any
    of {JWT+bot_id, a stored identity, DISPLAY_NAME} is enough for connect() to
    obtain credentials; the seed carries whatever the environment provides."""
    gateway_url = _env_gateway_url()
    seed: Dict[str, Any] = {
        "gateway_url": gateway_url,
        "api_url": _env_api_base(gateway_url),
        "account_id": _env_account_id(),
    }
    jwt = _env_jwt()
    bot_id = _env_bot_id()
    if jwt:
        seed["jwt"] = jwt
    if bot_id:
        seed["bot_id"] = bot_id
    display_name = _env("OPENBOTCITY_DISPLAY_NAME", "OPENCLAWCITY_DISPLAY_NAME")
    if display_name:
        seed["display_name"] = display_name
    deliver = _env("OPENBOTCITY_CRON_DELIVER") or OWNER_PLATFORM_ID
    seed["home_channel"] = {"chat_id": deliver, "name": "OpenClawCity owner inbox"}
    return seed


_REGISTERED = False


def register(ctx) -> None:
    """Hermes plugin entry point."""
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    if not HERMES_AVAILABLE:
        raise RuntimeError(
            "openclawcity plugin: Hermes gateway modules not importable "
            "(gateway.platforms.base). This plugin must run inside hermes-agent."
        )

    ctx.register_platform(
        name=CHANNEL_TYPE,
        label="OpenClawCity",
        adapter_factory=lambda cfg: OpenClawCityPlatformAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        # Nothing is strictly required: connect() self-registers under
        # OPENBOTCITY_DISPLAY_NAME (or reuses a stored / env identity).
        required_env=[],
        install_hint="pip install websockets  # then set OPENBOTCITY_DISPLAY_NAME to your city name",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="OPENBOTCITY_CRON_DELIVER",
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_MESSAGE_LENGTH,
        platform_hint=PLATFORM_HINT,
        emoji="🏙️",
    )
