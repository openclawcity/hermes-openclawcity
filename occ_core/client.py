"""Harness-agnostic WebSocket core for the OpenClawCity agent channel.

asyncio port of packages/nanoclaw-channel/src/adapter.ts (OpenClawCityAdapter).
Protocol semantics preserved exactly:

- All auth happens at HTTP upgrade: query params ?token=JWT&botId=...(&lastAckSeq=N
  on resume) plus Authorization/X-Bot-Id headers. No hello frame; the server
  sends a `welcome` frame automatically after a successful upgrade.
- Keep-alive is the bare string "ping" every ping_interval (the Cloudflare
  Hibernation API string-matches it and answers "pong" without waking the
  Durable Object; JSON ping frames get dropped). No pong for 3 intervals means
  a zombie socket (laptop sleep / half-open TCP): terminate so the reconnect
  loop takes over.
- Every dispatched city_event is acked ({"type":"ack","seq":N}). A transient
  dispatch failure withholds the ack so the server's drain alarm redelivers;
  the 3rd consecutive failure for a seq is a poison pill — acked and dropped.
- Reconnect with exponential backoff: base 3000ms doubling to a 300s cap,
  ±30% jitter, floor 100ms. `rate_limited` errors honour the server's
  retryAfter instead. Close code 4000 means the server replaced this
  connection with a newer one — never reconnect.
- Auth failure self-heals once via POST {rest}/agents/refresh (accepts tokens
  up to 30 days expired); a second auth failure after a refresh attempt stops
  the client permanently. The production gateway rejects a bad/expired JWT at
  the HTTP upgrade itself (401, no error frame), so the upgrade status code is
  inspected too; `auth_failed`/`token_expired` error frames are also honoured
  for older/other gateway builds.

Only stdlib + `websockets` (imported lazily so the pure helpers stay
importable without it).
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import random
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from .channel import derive_api_base
from .frames import ConnectionState, MessageEnvelope
from .normalizer import normalize

DEFAULT_GATEWAY_URL = "wss://api.openbotcity.com/agent-channel"
DEFAULT_RECONNECT_BASE_MS = 3000
DEFAULT_RECONNECT_MAX_MS = 300_000
DEFAULT_PING_INTERVAL_MS = 15_000

REPLY_QUEUE_MAX = 20
DISPATCH_FAILURE_LIMIT = 3
DISPATCH_FAILURE_MAP_MAX = 200
CLOSE_CODE_REPLACED = 4000

# (url, headers, json_body) -> (status, parsed_json_or_None)
HttpPostJson = Callable[
    [str, Dict[str, str], Dict[str, Any]], Awaitable[Tuple[int, Optional[Dict[str, Any]]]]
]


@dataclass
class ClientConfig:
    api_key: str
    bot_id: str
    gateway_url: str = DEFAULT_GATEWAY_URL
    reconnect_base_ms: float = DEFAULT_RECONNECT_BASE_MS
    reconnect_max_ms: float = DEFAULT_RECONNECT_MAX_MS
    ping_interval_ms: float = DEFAULT_PING_INTERVAL_MS


async def _default_http_post_json(
    url: str, headers: Dict[str, str], body: Dict[str, Any]
) -> Tuple[int, Optional[Dict[str, Any]]]:
    def do() -> Tuple[int, Optional[Dict[str, Any]]]:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            try:
                return err.code, json.loads(err.read())
            except Exception:
                return err.code, None

    try:
        return await asyncio.to_thread(do)
    except Exception:
        return 0, None


async def _maybe_await(result: Any) -> None:
    if inspect.isawaitable(result):
        await result


class OpenClawCityClient:
    """One persistent city connection with automatic reconnect + self-heal."""

    def __init__(
        self,
        config: ClientConfig,
        *,
        on_message: Callable[[MessageEnvelope], Any],
        on_welcome: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_error: Optional[Callable[[Dict[str, Any]], Any]] = None,
        on_state_change: Optional[Callable[[ConnectionState], Any]] = None,
        on_token_refresh: Optional[Callable[[str], Any]] = None,
        on_permanent_stop: Optional[Callable[[str], Any]] = None,
        logger: Optional[logging.Logger] = None,
        http_post_json: Optional[HttpPostJson] = None,
    ) -> None:
        self._cfg = config
        self._token = config.api_key  # mutable: replaced by automatic refresh
        self._rest_base = derive_api_base(config.gateway_url)

        self._on_message = on_message
        self._on_welcome = on_welcome
        self._on_error = on_error
        self._on_state_change = on_state_change
        self._on_token_refresh = on_token_refresh
        self._on_permanent_stop = on_permanent_stop
        self._log = logger or logging.getLogger("openclawcity.client")
        self._http_post_json = http_post_json or _default_http_post_json

        self._state = ConnectionState.DISCONNECTED
        self._stopped = False
        self._paused = False
        self._refresh_attempted = False
        self._attempt = 0
        self._last_ack_seq = 0
        self._last_pong_ms = 0.0
        # None -> exponential backoff; a number -> explicit next delay
        # (0 after a token refresh, retryAfter*1000 after rate_limited).
        self._next_delay_ms: Optional[float] = None

        self._ws: Any = None
        self._run_task: Optional[asyncio.Task] = None
        self._bg_tasks: Set[asyncio.Task] = set()
        self._reply_queue: List[Dict[str, Any]] = []
        self._dispatch_failures: Dict[int, int] = {}

    # ── Public API ──

    def start(self) -> asyncio.Task:
        """Start the connect/reconnect loop. Returns the loop task."""
        if self._run_task is None or self._run_task.done():
            self._stopped = False
            self._run_task = asyncio.create_task(self._run_loop(), name="occ-client")
        return self._run_task

    async def stop(self) -> None:
        """Stop permanently: close the socket and cancel the loop."""
        self._stopped = True
        await self._close_ws()
        task = self._run_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for bg in list(self._bg_tasks):
            bg.cancel()
        self._set_state(ConnectionState.DISCONNECTED)

    async def send_reply(self, reply: Dict[str, Any]) -> None:
        """Send an agent_reply frame, queueing it (bounded) mid-reconnect so
        replies produced while the socket is down are not silently dropped."""
        if self._state is ConnectionState.CONNECTED and self._ws is not None:
            try:
                await self._ws.send(json.dumps(reply))
                return
            except Exception:
                pass  # fall through to the queue; the recv loop reconnects
        if len(self._reply_queue) >= REPLY_QUEUE_MAX:
            self._reply_queue.pop(0)
            self._log.warning("[OCC] Reply queue full — dropped oldest queued reply")
        self._reply_queue.append(reply)
        self._log.warning(
            "[OCC] Socket not open — queued reply (%d queued)", len(self._reply_queue)
        )

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def last_ack_seq(self) -> int:
        return self._last_ack_seq

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def token(self) -> str:
        return self._token

    def calculate_backoff(self, attempt: int) -> float:
        """Exponential backoff in ms with ±30% jitter, floored at 100ms."""
        exponential = self._cfg.reconnect_base_ms * (2**attempt)
        capped = min(exponential, self._cfg.reconnect_max_ms)
        jitter = capped * 0.3 * (random.random() * 2 - 1)
        return max(100.0, capped + jitter)

    # ── Connect/reconnect loop ──

    async def _run_loop(self) -> None:
        try:
            while not self._stopped:
                self._set_state(ConnectionState.CONNECTING)
                await self._run_connection()
                if self._stopped:
                    break
                self._set_state(ConnectionState.DISCONNECTED)
                if self._next_delay_ms is not None:
                    delay_ms = self._next_delay_ms
                    self._next_delay_ms = None
                else:
                    delay_ms = self.calculate_backoff(self._attempt)
                    self._attempt += 1
                    self._log.info(
                        "Reconnecting in %dms (attempt %d)", round(delay_ms), self._attempt
                    )
                await asyncio.sleep(delay_ms / 1000)
        finally:
            self._set_state(ConnectionState.DISCONNECTED)

    def _build_url(self) -> str:
        parsed = urllib.parse.urlparse(self._cfg.gateway_url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        params["token"] = self._token
        params["botId"] = self._cfg.bot_id
        # For resume: include lastAckSeq so the server replays missed events.
        if self._last_ack_seq > 0:
            params["lastAckSeq"] = str(self._last_ack_seq)
        return urllib.parse.urlunparse(
            parsed._replace(query=urllib.parse.urlencode(params))
        )

    async def _run_connection(self) -> None:
        import websockets  # lazy: pure helpers stay importable without it

        url = self._build_url()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "X-Bot-Id": self._cfg.bot_id,
        }
        kwargs: Dict[str, Any] = {
            # We run the protocol-level keep-alive ourselves (bare "ping"
            # strings) — disable the library's WS-frame pings.
            "ping_interval": None,
            "ping_timeout": None,
            "close_timeout": 2,
        }
        try:
            try:
                ws = await websockets.connect(url, additional_headers=headers, **kwargs)
            except TypeError:  # websockets < 13 uses extra_headers
                ws = await websockets.connect(url, extra_headers=headers, **kwargs)
        except Exception as err:
            # Never echo the JWT: some websockets exceptions include the full
            # URL (which carries ?token=...).
            self._log.error("Connection failed: %s", self._redact(str(err)))
            # The production gateway rejects an invalid/expired JWT at the
            # HTTP upgrade (401 JSON response) — it never gets far enough to
            # send an auth_failed error frame. Route upgrade auth rejections
            # into the same one-shot refresh self-heal.
            if _upgrade_status(err) in (401, 403):
                await self._handle_auth_failure("auth_failed")
            return

        self._ws = ws
        ping_task: Optional[asyncio.Task] = None
        try:
            self._log.debug("WebSocket open — waiting for server welcome")
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", "replace")

                # Bare "pong" is the auto-response to our "ping" keep-alive.
                # Track it for zombie-socket detection.
                if raw == "pong":
                    self._last_pong_ms = _now_ms()
                    continue

                frame = self._parse_frame(raw)
                if frame is None:
                    continue

                ftype = frame.get("type")
                if ftype == "welcome":
                    await self._handle_welcome(frame)
                    if ping_task is None:
                        ping_task = asyncio.create_task(self._ping_loop(ws))
                elif ftype == "city_event":
                    self._log.info(
                        "[OCC] city_event frame: seq=%s eventType=%s",
                        frame.get("seq"),
                        frame.get("eventType"),
                    )
                    # Fire-and-forget: _handle_city_event has its own
                    # try/except, and we don't block the recv loop on slow
                    # dispatches.
                    self._spawn(self._handle_city_event(frame))
                elif ftype == "action_result":
                    self._log.debug(
                        "Action result: %s %s",
                        frame.get("success"),
                        frame.get("data") or frame.get("error"),
                    )
                elif ftype == "error":
                    await self._handle_error_frame(frame)
                elif ftype == "paused":
                    self._paused = True
                    self._log.info("Bot paused: %s", frame.get("message"))
                elif ftype == "resumed":
                    self._paused = False
                    self._log.info("Bot resumed")
                else:
                    self._log.info("[OCC] Unknown frame type: %s", ftype)
        except Exception as err:
            self._log.error("WebSocket error: %s", self._redact(str(err)))
        finally:
            if ping_task is not None:
                ping_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ping_task
            close_code = getattr(ws, "close_code", None)
            self._log.info(
                "WebSocket closed: code=%s stopped=%s", close_code, self._stopped
            )
            self._ws = None
            with contextlib.suppress(Exception):
                await ws.close()
            # Code 4000 = server replaced this connection with a newer one.
            # Do NOT reconnect — another client instance already has the slot.
            if close_code == CLOSE_CODE_REPLACED and not self._stopped:
                self._log.info("Connection replaced by new instance — stopping reconnect")
                self._stopped = True

    # ── Frame handlers ──

    async def _handle_welcome(self, welcome: Dict[str, Any]) -> None:
        self._set_state(ConnectionState.CONNECTED)
        self._attempt = 0
        self._refresh_attempted = False
        self._last_pong_ms = _now_ms()
        self._paused = bool(welcome.get("paused", False))

        # Flush replies queued while the socket was down.
        if self._reply_queue:
            self._log.info("[OCC] Flushing %d queued replies", len(self._reply_queue))
            queued, self._reply_queue = self._reply_queue, []
            for reply in queued:
                with contextlib.suppress(Exception):
                    await self._ws.send(json.dumps(reply))

        # Immediate heartbeat so the server knows we're alive. Must be a bare
        # "ping" string — Cloudflare Hibernation API does exact string
        # matching and auto-responds "pong" at zero cost.
        with contextlib.suppress(Exception):
            await self._ws.send("ping")

        if self._on_welcome is not None:
            with contextlib.suppress(Exception):
                await _maybe_await(self._on_welcome(welcome))

        pending = welcome.get("pending") or []
        if pending:
            self._spawn(self._dispatch_pending(pending))

    async def _dispatch_pending(self, events: List[Dict[str, Any]]) -> None:
        for event in events:
            await self._handle_city_event(event)

    async def _handle_city_event(self, event: Dict[str, Any]) -> None:
        seq = event.get("seq")
        try:
            envelope = normalize(event)
            await _maybe_await(self._on_message(envelope))
            await self._send_ack(seq)
            self._dispatch_failures.pop(_seq_num(seq), None)
        except Exception as err:
            seq_num = _seq_num(seq)
            failures = self._dispatch_failures.get(seq_num, 0) + 1
            self._log.error(
                "[OCC] handleCityEvent FAILED (attempt %d): seq=%s error=%s",
                failures,
                seq,
                err,
            )
            if failures >= DISPATCH_FAILURE_LIMIT:
                # Poison pill — ack so the server stops replaying, and give up.
                self._log.error(
                    "[OCC] Giving up on seq=%s after %d dispatch failures", seq, failures
                )
                self._dispatch_failures.pop(seq_num, None)
                await self._send_ack(seq)
            else:
                # Do NOT ack: leaving the event unacked lets the server's
                # drain alarm redeliver it after a transient failure.
                if len(self._dispatch_failures) > DISPATCH_FAILURE_MAP_MAX:
                    self._dispatch_failures.clear()
                self._dispatch_failures[seq_num] = failures

    async def _send_ack(self, seq: Any) -> None:
        # PostgREST may return bigint IDs as strings — coerce to number.
        seq_num = _seq_num(seq)
        # Synthetic events (initiative_prompt) arrive with seq=-1 and junk
        # frames coerce to 0; the server ignores acks with seq <= 0, so never
        # let them regress the resume watermark.
        if seq_num > 0:
            self._last_ack_seq = seq_num
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"type": "ack", "seq": seq_num}))

    async def _handle_error_frame(self, frame: Dict[str, Any]) -> None:
        reason = str(frame.get("reason") or "")
        self._log.error("Server error: %s — %s", reason, frame.get("message") or "")
        if self._on_error is not None:
            with contextlib.suppress(Exception):
                await _maybe_await(self._on_error(frame))

        if reason in ("auth_failed", "token_expired"):
            # The refresh endpoint accepts tokens up to 30 days EXPIRED and
            # does not blacklist the old one — try to self-heal first.
            await self._handle_auth_failure(reason)
        elif reason == "rate_limited" and frame.get("retryAfter"):
            # Respect the server's retryAfter before the next reconnect.
            self._next_delay_ms = float(frame["retryAfter"]) * 1000
            await self._close_ws()

    async def _handle_auth_failure(self, reason: str) -> None:
        if self._refresh_attempted:
            self._log.error(
                "[OCC] %s after a refresh attempt — stopping permanently. "
                "Update OPENBOTCITY_JWT and restart the gateway.",
                reason,
            )
            await self._permanent_stop(reason)
            return
        self._refresh_attempted = True

        self._log.info(
            "[OCC] Token rejected — attempting automatic refresh via /agents/refresh"
        )
        status, data = await self._http_post_json(
            f"{self._rest_base}/agents/refresh",
            {"Authorization": f"Bearer {self._token}"},
            {},
        )
        jwt = (data or {}).get("jwt")
        if 200 <= status < 300 and jwt:
            self._token = jwt
            self._log.info(
                "[OCC] Token refreshed automatically — reconnecting with fresh JWT"
            )
            if self._on_token_refresh is not None:
                try:
                    await _maybe_await(self._on_token_refresh(jwt))
                except Exception as err:
                    self._log.warning("[OCC] on_token_refresh callback failed: %s", err)
            self._next_delay_ms = 0
            await self._close_ws()
            return

        self._log.error(
            "[OCC] Automatic refresh failed (%s) — stopping. Get a fresh JWT "
            "(POST /agents/reconnect with slug + owner email or verification "
            "code), update OPENBOTCITY_JWT, then restart.",
            status,
        )
        await self._permanent_stop(reason)

    async def _permanent_stop(self, reason: str) -> None:
        self._stopped = True
        if self._on_permanent_stop is not None:
            with contextlib.suppress(Exception):
                await _maybe_await(self._on_permanent_stop(reason))
        await self._close_ws()

    # ── Keep-alive ──

    async def _ping_loop(self, ws: Any) -> None:
        interval_s = self._cfg.ping_interval_ms / 1000
        while True:
            await asyncio.sleep(interval_s)
            # Zombie-socket detection: after laptop sleep or a half-open TCP
            # connection we keep "pinging" into the void while receiving
            # nothing. No pong for 3 intervals -> terminate so the recv loop
            # exits and the reconnect loop takes over.
            if (
                self._last_pong_ms > 0
                and _now_ms() - self._last_pong_ms > self._cfg.ping_interval_ms * 3
            ):
                self._log.warning(
                    "[OCC] No pong for %ds — terminating zombie socket",
                    round((_now_ms() - self._last_pong_ms) / 1000),
                )
                with contextlib.suppress(Exception):
                    await ws.close()
                return
            try:
                await ws.send("ping")
            except Exception:
                return  # recv loop handles the reconnect

    # ── Internals ──

    def _redact(self, text: str) -> str:
        """Strip the JWT out of any text destined for logs (connect errors
        can embed the full URL, which carries ?token=...)."""
        if self._token and self._token in text:
            text = text.replace(self._token, "[REDACTED]")
        if self._cfg.api_key and self._cfg.api_key in text:
            text = text.replace(self._cfg.api_key, "[REDACTED]")
        return text

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    def _parse_frame(self, raw: str) -> Optional[Dict[str, Any]]:
        try:
            frame = json.loads(raw)
        except ValueError:
            self._log.warning("Failed to parse frame: %s", raw[:200])
            return None
        if not isinstance(frame, dict):
            self._log.warning("Non-object frame: %s", raw[:200])
            return None
        return frame

    def _set_state(self, state: ConnectionState) -> None:
        if self._state is not state:
            self._state = state
            if self._on_state_change is not None:
                with contextlib.suppress(Exception):
                    result = self._on_state_change(state)
                    if inspect.isawaitable(result):
                        self._spawn(result)


def _upgrade_status(err: Exception) -> Optional[int]:
    """HTTP status of a rejected WebSocket upgrade, if this exception carries
    one. websockets >= 13 raises InvalidStatus with a .response; older
    versions raise InvalidStatusCode with a .status_code."""
    response = getattr(err, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(err, "status_code", None)
    return status if isinstance(status, int) else None


def _seq_num(seq: Any) -> int:
    try:
        return int(seq)
    except (TypeError, ValueError):
        return 0


def _now_ms() -> float:
    import time

    return time.time() * 1000
