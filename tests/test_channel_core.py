"""Port of packages/nanoclaw-channel/tests/channel.test.ts (glue behaviors).

CityChannelCore carries the harness-agnostic part of the NanoClaw glue:
inbound context injection + route memory, outbound reply planning with the
DM-withhold rule and sanitization. Hermes-only conversion (MessageEvent) is
a thin shim over this and is not testable without a Hermes install.
"""
import asyncio

from occ_core.channel import (
    OWNER_PLATFORM_ID,
    CityChannelCore,
    CityRoute,
    derive_api_base,
    plan_city_reply,
    prepend_city_context,
    resolve_city_route,
)
from occ_core.frames import DIRECT_MENTION_EVENTS, is_direct_mention
from occ_core.normalizer import normalize


def city_event(**overrides):
    event = {
        "type": "city_event",
        "seq": 1,
        "eventType": "dm_message",
        "from": {"id": "user-1", "name": "Alice"},
        "text": "hi",
        "metadata": {},
    }
    event.update(overrides)
    return event


def run(coro):
    return asyncio.run(coro)


def make_core(**kwargs):
    return CityChannelCore(**kwargs)


def fetch_returning(text):
    calls = {"count": 0}

    async def fetch():
        calls["count"] += 1
        return text

    fetch.calls = calls
    return fetch


# ── derive_api_base ──


def test_derive_api_base_wss():
    assert derive_api_base("wss://api.openbotcity.com/agent-channel") == "https://api.openbotcity.com"


def test_derive_api_base_ws_maps_to_http():
    assert derive_api_base("ws://localhost:8787/agent-channel") == "http://localhost:8787"


def test_derive_api_base_defaults():
    assert derive_api_base(None) == "https://api.openbotcity.com"
    assert derive_api_base("not a url") == "https://api.openbotcity.com"


# ── Inbound mapping: route + direct/ambient flags ──


def test_owner_message_routes_to_owner():
    envelope = normalize(city_event(seq=7, eventType="owner_message", **{"from": {"id": "owner-x", "name": "Vincent"}}))
    platform_id, route = resolve_city_route(envelope)
    assert platform_id == OWNER_PLATFORM_ID
    assert route == CityRoute(action="owner_reply")
    assert envelope.id == "occ-7"
    assert is_direct_mention("owner_message")


def test_dm_with_conversation_id_routes_to_it():
    envelope = normalize(city_event(seq=8, eventType="dm_message", metadata={"conversationId": "conv-9"}))
    platform_id, route = resolve_city_route(envelope)
    assert platform_id == "conv-9"
    assert route == CityRoute(action="dm_reply", conversation_id="conv-9")


def test_dm_without_conversation_id_gets_stable_platform_id():
    envelope = normalize(city_event(seq=8, eventType="dm_message", **{"from": {"id": "u7", "name": "Zed"}}))
    platform_id, route = resolve_city_route(envelope)
    assert platform_id == "dm:u7"
    assert route.action == "dm_reply"
    assert route.conversation_id is None


def test_chat_mention_routes_to_sender_as_direct():
    envelope = normalize(city_event(seq=9, eventType="chat_mention", **{"from": {"id": "u4", "name": "Cara"}}, metadata={"zoneId": 2}))
    platform_id, route = resolve_city_route(envelope)
    assert platform_id == "u4"
    assert route == CityRoute(action="speak")
    assert is_direct_mention("chat_mention")


def test_building_activity_is_ambient():
    assert not is_direct_mention("building_activity")
    assert not is_direct_mention("artifact_reaction")
    assert not is_direct_mention("welcome")
    assert "dm_request" in DIRECT_MENTION_EVENTS


# ── City-context injection + dedup ──


def test_prepends_city_context_to_first_inbound_for_a_peer():
    async def main():
        core = make_core(heartbeat_fetch=fetch_returning("ZONE=Downtown; MOOD=curious"))
        envelope = normalize(city_event(seq=1, eventType="chat_mention", **{"from": {"id": "peer-a", "name": "A"}}, text="yo"))
        await core.process_inbound(envelope)
        assert "[CITY CONTEXT]" in envelope.text
        assert "ZONE=Downtown; MOOD=curious" in envelope.text
        assert "[/CITY CONTEXT]" in envelope.text
        # original body still present after the context block
        assert "A: yo" in envelope.text

    run(main())


def test_dedupes_identical_snapshot_for_same_peer_and_caches_heartbeat():
    async def main():
        fetch = fetch_returning("CTX_V1")
        core = make_core(heartbeat_fetch=fetch)
        first = normalize(city_event(seq=1, eventType="chat_mention", **{"from": {"id": "peer-a", "name": "A"}}, text="one"))
        second = normalize(city_event(seq=2, eventType="chat_mention", **{"from": {"id": "peer-a", "name": "A"}}, text="two"))
        await core.process_inbound(first)
        await core.process_inbound(second)
        assert "[CITY CONTEXT]" in first.text
        # Second event, same cached snapshot within 60s -> not re-injected.
        assert "[CITY CONTEXT]" not in second.text
        # Heartbeat is fetched once and cached (5 min window).
        assert fetch.calls["count"] == 1

    run(main())


def test_still_injects_context_for_second_distinct_peer():
    async def main():
        core = make_core(heartbeat_fetch=fetch_returning("CTX_V1"))
        first = normalize(city_event(seq=1, eventType="chat_mention", **{"from": {"id": "peer-a", "name": "A"}}, text="one"))
        second = normalize(city_event(seq=2, eventType="chat_mention", **{"from": {"id": "peer-b", "name": "B"}}, text="two"))
        await core.process_inbound(first)
        await core.process_inbound(second)
        assert "[CITY CONTEXT]" in second.text

    run(main())


def test_truncates_oversized_heartbeat_to_cap():
    async def main():
        core = make_core(heartbeat_fetch=fetch_returning("X" * 50_000), city_context_max_chars=500)
        envelope = normalize(city_event(seq=1, eventType="chat_mention", **{"from": {"id": "peer-a", "name": "A"}}, text="hey"))
        await core.process_inbound(envelope)
        assert "city context truncated" in envelope.text
        # The injected snapshot must be far smaller than the raw 50k body.
        assert len(envelope.text) < 2_000

    run(main())


def test_heartbeat_failure_serves_stale_then_none():
    async def main():
        state = {"fail": False}

        async def fetch():
            if state["fail"]:
                raise RuntimeError("offline")
            return "CTX_V1"

        now = {"ms": 0}
        core = make_core(heartbeat_fetch=fetch, now_ms=lambda: now["ms"])
        e1 = normalize(city_event(seq=1, eventType="chat_mention", **{"from": {"id": "a", "name": "A"}}, text="x"))
        await core.process_inbound(e1)
        assert "CTX_V1" in e1.text

        # Cache expired AND fetch failing -> stale copy still injected for a new peer.
        state["fail"] = True
        now["ms"] = 10 * 60 * 1000
        e2 = normalize(city_event(seq=2, eventType="chat_mention", **{"from": {"id": "b", "name": "B"}}, text="y"))
        await core.process_inbound(e2)
        assert "CTX_V1" in e2.text

    run(main())


def test_no_heartbeat_fetch_means_no_context():
    async def main():
        core = make_core()  # no heartbeat_fetch wired
        envelope = normalize(city_event(seq=1, eventType="chat_mention", **{"from": {"id": "a", "name": "A"}}, text="plain"))
        await core.process_inbound(envelope)
        assert "[CITY CONTEXT]" not in envelope.text

    run(main())


def test_prepend_city_context_shape():
    assert prepend_city_context("body", "ctx") == "[CITY CONTEXT]\nctx\n[/CITY CONTEXT]\n\nbody"


# ── Outbound reply routing ──


def test_owner_message_reply_routes_to_owner_reply():
    async def main():
        core = make_core()
        await core.process_inbound(normalize(city_event(seq=1, eventType="owner_message", **{"from": {"id": "owner-x", "name": "V"}})))
        assert core.plan_outbound(OWNER_PLATFORM_ID, "hello owner") == {
            "type": "agent_reply",
            "action": "owner_reply",
            "message": "hello owner",
        }

    run(main())


def test_dm_reply_carries_conversation_id():
    async def main():
        core = make_core()
        await core.process_inbound(normalize(city_event(seq=1, eventType="dm_message", **{"from": {"id": "u2", "name": "Bob"}}, metadata={"conversationId": "conv-9"})))
        assert core.plan_outbound("conv-9", "secret reply") == {
            "type": "agent_reply",
            "action": "dm_reply",
            "message": "secret reply",
            "conversation_id": "conv-9",
        }

    run(main())


def test_dm_and_dm_approved_also_route_to_dm_reply():
    async def main():
        core = make_core()
        await core.process_inbound(normalize(city_event(seq=1, eventType="dm", **{"from": {"id": "u2", "name": "B"}}, metadata={"conversationId": "c-dm"})))
        await core.process_inbound(normalize(city_event(seq=2, eventType="dm_approved", **{"from": {"id": "u3", "name": "C"}}, metadata={"conversationId": "c-appr"})))
        assert core.plan_outbound("c-dm", "a") == {
            "type": "agent_reply", "action": "dm_reply", "message": "a", "conversation_id": "c-dm",
        }
        assert core.plan_outbound("c-appr", "b") == {
            "type": "agent_reply", "action": "dm_reply", "message": "b", "conversation_id": "c-appr",
        }

    run(main())


def test_any_other_event_type_speaks():
    async def main():
        core = make_core()
        await core.process_inbound(normalize(city_event(seq=1, eventType="chat_mention", **{"from": {"id": "u4", "name": "Cara"}}, metadata={"zoneId": 1})))
        assert core.plan_outbound("u4", "hello zone") == {
            "type": "agent_reply", "action": "speak", "text": "hello zone",
        }

    run(main())


def test_unknown_platform_id_falls_back_to_speak():
    core = make_core()
    assert core.plan_outbound("never-seen", "broadcast") == {
        "type": "agent_reply", "action": "speak", "text": "broadcast",
    }


def test_owner_platform_id_infers_owner_reply_without_remembered_route():
    core = make_core()
    assert core.plan_outbound(OWNER_PLATFORM_ID, "hi human") == {
        "type": "agent_reply", "action": "owner_reply", "message": "hi human",
    }


# ── DM-withhold rule ──


def test_withholds_dm_reply_without_conversation_id():
    async def main():
        core = make_core()
        # dm_message WITHOUT a conversationId -> platform 'dm:<senderId>', route has no conversation id.
        await core.process_inbound(normalize(city_event(seq=1, eventType="dm_message", **{"from": {"id": "u7", "name": "Zed"}}, metadata={})))
        # Reply is dropped rather than downgraded to a public speak.
        assert core.plan_outbound("dm:u7", "this must NOT leak to public chat") is None

    run(main())


def test_plan_city_reply_pure_withhold():
    assert plan_city_reply(CityRoute(action="dm_reply", conversation_id=None), "secret") is None
    assert plan_city_reply(CityRoute(action="dm_reply", conversation_id="c1"), "secret") == {
        "type": "agent_reply", "action": "dm_reply", "message": "secret", "conversation_id": "c1",
    }


# ── Sanitize applied to replies ──


def test_trims_whitespace_on_shippable_reply():
    async def main():
        core = make_core()
        await core.process_inbound(normalize(city_event(seq=1, eventType="owner_message", **{"from": {"id": "o", "name": "V"}})))
        assert core.plan_outbound(OWNER_PLATFORM_ID, "  hello there  ") == {
            "type": "agent_reply", "action": "owner_reply", "message": "hello there",
        }

    run(main())


def test_withholds_runtime_error_banner():
    async def main():
        core = make_core()
        await core.process_inbound(normalize(city_event(seq=1, eventType="chat_mention", **{"from": {"id": "u4", "name": "C"}}, metadata={})))
        assert core.plan_outbound("u4", "⚠️ Context is too large and auto-compaction could not recover this turn.") is None

    run(main())


def test_withholds_pure_tool_call_markup():
    async def main():
        core = make_core()
        await core.process_inbound(normalize(city_event(seq=1, eventType="owner_message", **{"from": {"id": "o", "name": "V"}})))
        assert core.plan_outbound(OWNER_PLATFORM_ID, '<PLHD>[{"name":"read","parameters":{}}]<PLHD>') is None

    run(main())


# ── Route memory bounds + chat types ──


def test_route_memory_evicts_oldest_beyond_cap():
    async def main():
        core = make_core(max_remembered_routes=3)
        for i in range(5):
            await core.process_inbound(normalize(city_event(seq=i, eventType="dm_message", **{"from": {"id": f"u{i}", "name": "X"}}, metadata={"conversationId": f"c{i}"})))
        # Oldest routes evicted -> falls back to speak (safe public default)
        assert core.plan_outbound("c0", "x") == {"type": "agent_reply", "action": "speak", "text": "x"}
        # Newest still remembered as a DM
        assert core.plan_outbound("c4", "y")["action"] == "dm_reply"

    run(main())


def test_chat_type_mapping():
    async def main():
        core = make_core()
        await core.process_inbound(normalize(city_event(seq=1, eventType="owner_message", **{"from": {"id": "o", "name": "V"}})))
        await core.process_inbound(normalize(city_event(seq=2, eventType="dm_message", **{"from": {"id": "u2", "name": "B"}}, metadata={"conversationId": "c1"})))
        await core.process_inbound(normalize(city_event(seq=3, eventType="chat_mention", **{"from": {"id": "u3", "name": "C"}}, metadata={})))
        assert core.chat_type(OWNER_PLATFORM_ID) == "dm"
        assert core.chat_type("c1") == "dm"
        assert core.chat_type("u3") == "group"
        assert core.chat_type("unknown-peer") == "group"

    run(main())
