"""Port of packages/nanoclaw-channel/tests/adapter.test.ts.

The WebSocket core is exercised against a real local `websockets` server (a
fake city gateway) — no mocking of the wire, and never the production
gateway.
"""
import asyncio
import contextlib
import json
import time
import urllib.parse

import websockets

from occ_core.client import ClientConfig, OpenClawCityClient
from occ_core.frames import ConnectionState

WELCOME = {
    "type": "welcome",
    "version": 1,
    "location": {"zoneId": 1, "zoneName": "Downtown"},
    "nearby": [{"id": "b1", "name": "Alice"}],
    "pending": [],
}


def city_event(seq, **overrides):
    event = {
        "type": "city_event",
        "seq": seq,
        "eventType": "dm_message",
        "from": {"id": "u1", "name": "Alice"},
        "text": "Hello",
        "metadata": {},
    }
    event.update(overrides)
    return event


class FakeConn:
    def __init__(self, ws):
        self.ws = ws
        self.path = ws.request.path
        self.headers = ws.request.headers
        self.inbox = asyncio.Queue()  # parsed JSON frames from the client
        self.pings = 0
        self.closed = asyncio.Event()

    @property
    def query(self):
        return {
            k: v[0]
            for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()
        }

    async def send_json(self, obj):
        await self.ws.send(json.dumps(obj))

    async def next_frame(self, timeout=2.0):
        return await asyncio.wait_for(self.inbox.get(), timeout)

    async def close(self, code=1000, reason=""):
        await self.ws.close(code, reason)


class FakeCityServer:
    """Minimal fake of the city gateway for driving the client."""

    def __init__(self, respond_pong=True, reject_tokens=()):
        self.respond_pong = respond_pong
        # Tokens rejected at the HTTP upgrade with 401, exactly like the
        # production DO (validateJWTWithBlacklist fails -> 401 JSON body).
        self.reject_tokens = set(reject_tokens)
        self.rejected_upgrades = 0
        self.connections = asyncio.Queue()
        self._server = None

    async def _process_request(self, connection, request):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.path).query)
        token = (query.get("token") or [""])[0]
        if token in self.reject_tokens:
            self.rejected_upgrades += 1
            import http as _http

            return connection.respond(
                _http.HTTPStatus.UNAUTHORIZED, '{"error":"Invalid or expired token"}'
            )
        return None

    async def start(self):
        self._server = await websockets.serve(
            self._handler,
            "127.0.0.1",
            0,
            ping_interval=None,
            process_request=self._process_request,
        )
        port = self._server.sockets[0].getsockname()[1]
        self.gateway_url = f"ws://127.0.0.1:{port}/agent-channel"
        return self

    async def stop(self):
        self._server.close()
        await self._server.wait_closed()

    async def next_connection(self, timeout=3.0):
        return await asyncio.wait_for(self.connections.get(), timeout)

    async def _handler(self, ws):
        conn = FakeConn(ws)
        await self.connections.put(conn)
        try:
            async for raw in ws:
                if raw == "ping":
                    conn.pings += 1
                    if self.respond_pong:
                        await ws.send("pong")
                    continue
                await conn.inbox.put(json.loads(raw))
        except websockets.ConnectionClosed:
            pass
        finally:
            conn.closed.set()


def make_client(gateway_url, **kwargs):
    on_messages = []

    async def default_on_message(envelope):
        on_messages.append(envelope)

    config = ClientConfig(
        api_key=kwargs.pop("api_key", "test-token-abc"),
        bot_id=kwargs.pop("bot_id", "test-bot-123"),
        gateway_url=gateway_url,
        reconnect_base_ms=kwargs.pop("reconnect_base_ms", 50),
        reconnect_max_ms=kwargs.pop("reconnect_max_ms", 400),
        ping_interval_ms=kwargs.pop("ping_interval_ms", 10_000),
    )
    kwargs.setdefault("on_message", default_on_message)
    client = OpenClawCityClient(config, **kwargs)
    client.received = on_messages
    return client


async def wait_until(predicate, timeout=3.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within timeout")


async def assert_no_frame(conn, wait=0.15):
    await asyncio.sleep(wait)
    assert conn.inbox.empty()


@contextlib.asynccontextmanager
async def running(server, client):
    try:
        client.start()
        yield
    finally:
        await client.stop()
        await server.stop()


def run(coro):
    asyncio.run(asyncio.wait_for(coro, timeout=20))


# ── Connection ──


def test_authenticates_via_url_params_no_hello_frame():
    async def main():
        server = await FakeCityServer().start()
        welcomes = []
        client = make_client(server.gateway_url, on_welcome=welcomes.append)
        async with running(server, client):
            conn = await server.next_connection()
            # Auth is at HTTP upgrade via URL params + headers
            assert conn.query["token"] == "test-token-abc"
            assert conn.query["botId"] == "test-bot-123"
            assert "lastAckSeq" not in conn.query
            assert conn.headers["Authorization"] == "Bearer test-token-abc"
            assert conn.headers["X-Bot-Id"] == "test-bot-123"
            # No hello frame sent before the welcome
            await assert_no_frame(conn)
            await conn.send_json(WELCOME)
            await wait_until(lambda: client.state is ConnectionState.CONNECTED)
            assert welcomes == [WELCOME]
            # Still no JSON frames — only the bare "ping" keep-alive
            await assert_no_frame(conn)
            await wait_until(lambda: conn.pings >= 1)

    run(main())


def test_reconnect_includes_last_ack_seq_in_url():
    async def main():
        server = await FakeCityServer().start()
        client = make_client(server.gateway_url)
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)
            await conn.send_json(city_event(42, metadata={"conversationId": "c1"}))
            assert await conn.next_frame() == {"type": "ack", "seq": 42}
            assert client.last_ack_seq == 42

            # Simulate disconnect: the client must resume with lastAckSeq
            await conn.close()
            conn2 = await server.next_connection()
            assert conn2.query["lastAckSeq"] == "42"
            # No resume frame either — resume is pure URL params
            await assert_no_frame(conn2)

    run(main())


# ── Reconnection ──


def test_exponential_backoff_timing():
    client = make_client("ws://localhost:9/none", reconnect_base_ms=100, reconnect_max_ms=10_000)
    for attempt in range(6):
        expected = min(100 * (2**attempt), 10_000)
        delay = client.calculate_backoff(attempt)
        assert delay >= max(100, expected * 0.7)
        assert delay <= expected * 1.3


# ── Ping ──


def test_sends_ping_at_configured_interval():
    async def main():
        server = await FakeCityServer().start()
        client = make_client(server.gateway_url, ping_interval_ms=50)
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)
            # welcome ping + at least two interval pings
            await wait_until(lambda: conn.pings >= 3, timeout=2)

    run(main())


def test_terminates_zombie_socket_when_pongs_stop():
    async def main():
        server = await FakeCityServer(respond_pong=False).start()
        client = make_client(server.gateway_url, ping_interval_ms=50)
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)
            await wait_until(lambda: client.state is ConnectionState.CONNECTED)
            # No pong for 3 intervals -> client terminates and reconnects
            conn2 = await server.next_connection(timeout=5)
            assert conn2 is not conn

    run(main())


# ── Ack ──


def test_sends_ack_after_event_dispatch():
    async def main():
        server = await FakeCityServer().start()
        client = make_client(server.gateway_url)
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)
            await conn.send_json(city_event(7, **{"from": {"id": "u1", "name": "Bob"}}, text="Hi there"))
            assert await conn.next_frame() == {"type": "ack", "seq": 7}
            assert len(client.received) == 1
            assert client.received[0].text == "[DM from Bob] Hi there"
            assert client.last_ack_seq == 7

    run(main())


def test_string_seq_is_coerced_to_number():
    async def main():
        server = await FakeCityServer().start()
        client = make_client(server.gateway_url)
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)
            # PostgREST may return bigint IDs as strings
            await conn.send_json(city_event("31"))
            assert await conn.next_frame() == {"type": "ack", "seq": 31}
            assert client.last_ack_seq == 31

    run(main())


def test_withholds_ack_on_transient_failure_then_poison_pills():
    async def main():
        async def failing_on_message(envelope):
            raise RuntimeError("dispatch failed")

        server = await FakeCityServer().start()
        client = make_client(server.gateway_url, on_message=failing_on_message)
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)

            evt = city_event(99, **{"from": {"id": "u1", "name": "Eve"}}, text="crash")

            # First failure: NO ack — the event stays replayable server-side
            await conn.send_json(evt)
            await assert_no_frame(conn)
            assert client.last_ack_seq == 0

            # Redelivery 2: still no ack
            await conn.send_json(evt)
            await assert_no_frame(conn)

            # Redelivery 3: poison pill — acked so the server stops replaying
            await conn.send_json(evt)
            assert await conn.next_frame() == {"type": "ack", "seq": 99}
            assert client.last_ack_seq == 99

    run(main())


# ── Pending events ──


def test_dispatches_pending_events_sequentially():
    async def main():
        order = []

        async def on_message(envelope):
            order.append(envelope.metadata["seq"])

        server = await FakeCityServer().start()
        client = make_client(server.gateway_url, on_message=on_message)
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(
                {
                    **WELCOME,
                    "pending": [
                        city_event(1, **{"from": {"id": "u1", "name": "A"}}, text="first"),
                        city_event(2, **{"from": {"id": "u2", "name": "B"}}, text="second"),
                        city_event(3, **{"from": {"id": "u3", "name": "C"}}, text="third"),
                    ],
                }
            )
            acks = [await conn.next_frame() for _ in range(3)]
            assert order == [1, 2, 3]
            assert acks == [{"type": "ack", "seq": n} for n in (1, 2, 3)]

    run(main())


# ── Pause / Resume ──


def test_handles_paused_and_resumed_frames():
    async def main():
        server = await FakeCityServer().start()
        client = make_client(server.gateway_url)
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)
            await wait_until(lambda: client.state is ConnectionState.CONNECTED)
            assert client.paused is False

            await conn.send_json({"type": "paused", "message": "Owner paused the bot"})
            await wait_until(lambda: client.paused is True)

            await conn.send_json({"type": "resumed"})
            await wait_until(lambda: client.paused is False)

    run(main())


def test_welcome_paused_flag():
    async def main():
        server = await FakeCityServer().start()
        client = make_client(server.gateway_url)
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json({**WELCOME, "paused": True})
            await wait_until(lambda: client.paused is True)

    run(main())


# ── Error handling: JWT self-heal ──


def test_self_heals_on_token_expired_and_reconnects_with_fresh_jwt():
    async def main():
        refresh_calls = []

        async def http_post_json(url, headers, body):
            refresh_calls.append((url, headers))
            return 200, {"jwt": "fresh-jwt"}

        refreshed = []
        stopped = []
        server = await FakeCityServer().start()
        client = make_client(
            server.gateway_url,
            http_post_json=http_post_json,
            on_token_refresh=refreshed.append,
            on_permanent_stop=stopped.append,
        )
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)
            await wait_until(lambda: client.state is ConnectionState.CONNECTED)

            await conn.send_json({"type": "error", "reason": "token_expired"})
            await wait_until(lambda: refreshed == ["fresh-jwt"])
            assert refresh_calls[0][0].endswith("/agents/refresh")
            assert refresh_calls[0][1]["Authorization"] == "Bearer test-token-abc"

            # Reconnects immediately (no backoff) with the fresh JWT
            conn2 = await server.next_connection()
            assert conn2.query["token"] == "fresh-jwt"
            assert conn2.headers["Authorization"] == "Bearer fresh-jwt"
            await conn2.send_json(WELCOME)
            await wait_until(lambda: client.state is ConnectionState.CONNECTED)
            assert stopped == []

    run(main())


def test_stops_permanently_when_refresh_fails():
    async def main():
        async def http_post_json(url, headers, body):
            return 401, {"error": "nope"}

        errors = []
        stopped = []
        server = await FakeCityServer().start()
        client = make_client(
            server.gateway_url,
            http_post_json=http_post_json,
            on_error=errors.append,
            on_permanent_stop=stopped.append,
        )
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)
            await conn.send_json({"type": "error", "reason": "auth_failed", "message": "Bad token"})
            await wait_until(lambda: stopped == ["auth_failed"])
            assert errors and errors[0]["reason"] == "auth_failed"
            await wait_until(lambda: client.state is ConnectionState.DISCONNECTED)
            # No reconnect attempt
            await asyncio.sleep(0.3)
            assert server.connections.empty()

    run(main())


def test_error_before_welcome_also_self_heals_or_stops():
    async def main():
        async def http_post_json(url, headers, body):
            return 0, None  # refresh endpoint unreachable

        stopped = []
        server = await FakeCityServer().start()
        client = make_client(server.gateway_url, http_post_json=http_post_json, on_permanent_stop=stopped.append)
        async with running(server, client):
            conn = await server.next_connection()
            # Server sends auth_failed instead of welcome
            await conn.send_json({"type": "error", "reason": "auth_failed", "message": "Invalid JWT"})
            await wait_until(lambda: stopped == ["auth_failed"])
            assert client.stopped is True

    run(main())


def test_respects_rate_limited_retry_after():
    async def main():
        server = await FakeCityServer().start()
        client = make_client(server.gateway_url)
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)
            await wait_until(lambda: client.state is ConnectionState.CONNECTED)

            await conn.send_json({"type": "error", "reason": "rate_limited", "retryAfter": 0.6})

            # Should NOT reconnect before retryAfter has elapsed
            await asyncio.sleep(0.3)
            assert server.connections.empty()

            # After retryAfter, it reconnects
            conn2 = await server.next_connection(timeout=2)
            assert conn2.query["token"] == "test-token-abc"

    run(main())


# ── Replaced connection ──


def test_close_code_4000_stops_reconnecting():
    async def main():
        server = await FakeCityServer().start()
        client = make_client(server.gateway_url)
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)
            await wait_until(lambda: client.state is ConnectionState.CONNECTED)

            # Server replaced this connection with a newer one
            await conn.close(code=4000, reason="replaced")
            await wait_until(lambda: client.stopped is True)
            await asyncio.sleep(0.3)
            assert server.connections.empty()

    run(main())


# ── sendReply queueing ──


def test_send_reply_queues_when_disconnected_and_flushes_on_welcome():
    async def main():
        server = await FakeCityServer().start()
        client = make_client(server.gateway_url)
        # Queue BEFORE starting — socket not open
        await client.send_reply({"type": "agent_reply", "action": "speak", "text": "queued-1"})
        await client.send_reply({"type": "agent_reply", "action": "speak", "text": "queued-2"})
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)
            first = await conn.next_frame()
            second = await conn.next_frame()
            assert first["text"] == "queued-1"
            assert second["text"] == "queued-2"

    run(main())


def test_reply_queue_drops_oldest_beyond_cap():
    async def main():
        server = await FakeCityServer().start()
        client = make_client(server.gateway_url)
        for i in range(25):
            await client.send_reply({"type": "agent_reply", "action": "speak", "text": f"r{i}"})
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)
            flushed = [await conn.next_frame() for _ in range(20)]
            assert flushed[0]["text"] == "r5"  # r0..r4 dropped
            assert flushed[-1]["text"] == "r24"
            await assert_no_frame(conn)

    run(main())


def test_send_reply_goes_straight_through_when_connected():
    async def main():
        server = await FakeCityServer().start()
        client = make_client(server.gateway_url)
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)
            await wait_until(lambda: client.state is ConnectionState.CONNECTED)
            await client.send_reply({"type": "agent_reply", "action": "owner_reply", "message": "hi"})
            assert await conn.next_frame() == {
                "type": "agent_reply",
                "action": "owner_reply",
                "message": "hi",
            }

    run(main())


# ── Stop ──


def test_stops_cleanly_and_does_not_reconnect():
    async def main():
        server = await FakeCityServer().start()
        client = make_client(server.gateway_url)
        client.start()
        conn = await server.next_connection()
        await conn.send_json(WELCOME)
        await wait_until(lambda: client.state is ConnectionState.CONNECTED)

        await client.stop()
        assert client.state is ConnectionState.DISCONNECTED
        await asyncio.sleep(0.3)
        assert server.connections.empty()
        await server.stop()

    run(main())


# ── Upgrade-level auth rejection (what the production DO actually does) ──
#
# The real gateway never sends an auth_failed error frame: an invalid/expired
# JWT is rejected at the HTTP upgrade with a 401 JSON response. The client
# must treat that exactly like an auth_failed frame — one refresh attempt,
# then reconnect with the fresh JWT or stop permanently.


def test_upgrade_401_triggers_refresh_and_reconnect():
    async def main():
        refresh_calls = []

        async def http_post_json(url, headers, body):
            refresh_calls.append((url, headers))
            return 200, {"jwt": "fresh-jwt"}

        refreshed = []
        stopped = []
        server = await FakeCityServer(reject_tokens={"expired-token"}).start()
        client = make_client(
            server.gateway_url,
            api_key="expired-token",
            http_post_json=http_post_json,
            on_token_refresh=refreshed.append,
            on_permanent_stop=stopped.append,
        )
        async with running(server, client):
            await wait_until(lambda: refreshed == ["fresh-jwt"])
            assert refresh_calls[0][0].endswith("/agents/refresh")
            assert refresh_calls[0][1]["Authorization"] == "Bearer expired-token"

            # Reconnects with the fresh JWT and completes the welcome handshake
            conn = await server.next_connection()
            assert conn.query["token"] == "fresh-jwt"
            assert conn.headers["Authorization"] == "Bearer fresh-jwt"
            await conn.send_json(WELCOME)
            await wait_until(lambda: client.state is ConnectionState.CONNECTED)
            assert stopped == []
            assert server.rejected_upgrades == 1

    run(main())


def test_upgrade_401_with_failed_refresh_stops_permanently():
    async def main():
        async def http_post_json(url, headers, body):
            return 401, {"error": "Invalid or too-old token"}

        stopped = []
        server = await FakeCityServer(reject_tokens={"expired-token"}).start()
        client = make_client(
            server.gateway_url,
            api_key="expired-token",
            http_post_json=http_post_json,
            on_permanent_stop=stopped.append,
        )
        async with running(server, client):
            await wait_until(lambda: stopped == ["auth_failed"])
            assert client.stopped is True
            # No reconnect storm: the one rejected upgrade, then silence.
            await asyncio.sleep(0.3)
            assert server.rejected_upgrades == 1
            assert server.connections.empty()

    run(main())


def test_upgrade_401_after_refresh_also_stops_permanently():
    async def main():
        async def http_post_json(url, headers, body):
            return 200, {"jwt": "still-bad-jwt"}  # refresh "succeeds"…

        stopped = []
        # …but the gateway rejects the fresh token too (e.g. clock skew,
        # revoked bot). The second 401 must stop the client, not loop.
        server = await FakeCityServer(
            reject_tokens={"expired-token", "still-bad-jwt"}
        ).start()
        client = make_client(
            server.gateway_url,
            api_key="expired-token",
            http_post_json=http_post_json,
            on_permanent_stop=stopped.append,
        )
        async with running(server, client):
            await wait_until(lambda: stopped == ["auth_failed"])
            assert client.stopped is True
            await asyncio.sleep(0.3)
            assert server.rejected_upgrades == 2  # initial + one retry, no storm

    run(main())


# ── Synthetic events must not regress the ack watermark ──


def test_initiative_prompt_seq_minus_one_does_not_regress_watermark():
    async def main():
        server = await FakeCityServer().start()
        client = make_client(server.gateway_url)
        async with running(server, client):
            conn = await server.next_connection()
            await conn.send_json(WELCOME)
            await conn.send_json(city_event(42))
            assert await conn.next_frame() == {"type": "ack", "seq": 42}

            # The production DO pushes initiative_prompt with seq=-1.
            await conn.send_json(
                city_event(-1, eventType="initiative_prompt",
                           **{"from": {"id": "city", "name": "The City"}},
                           text="What's unfinished?")
            )
            assert await conn.next_frame() == {"type": "ack", "seq": -1}
            assert client.last_ack_seq == 42  # watermark preserved

            # Resume still carries the real watermark
            await conn.close()
            conn2 = await server.next_connection()
            assert conn2.query["lastAckSeq"] == "42"

    run(main())


# ── JWT never reaches the logs ──


def test_redact_strips_token_from_log_text():
    client = make_client("ws://localhost:9/none", api_key="secret-jwt-abc123")
    msg = client._redact(
        "connect failed: ws://localhost:9/none?token=secret-jwt-abc123&botId=b"
    )
    assert "secret-jwt-abc123" not in msg
    assert "[REDACTED]" in msg
