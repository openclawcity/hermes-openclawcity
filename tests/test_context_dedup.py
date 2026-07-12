"""Port of packages/nanoclaw-channel/tests/context-dedup.test.ts."""
from occ_core.context_dedup import ContextInjectionRecord, should_inject_city_context

WINDOW = 60_000
CTX = "[CITY CONTEXT blob]"


def test_injects_on_first_event_for_a_key():
    state = {}
    assert should_inject_city_context(state, "acct:peer", CTX, 1_000, WINDOW) is True
    assert state["acct:peer"] == ContextInjectionRecord(at=1_000, ctx=CTX)


def test_skips_identical_repeat_inside_window_the_aaga_burst():
    state = {}
    assert should_inject_city_context(state, "k", CTX, 0, WINDOW) is True
    # 6 more events over the next ~50s, same cached context -> all skipped
    for t in (5_000, 12_000, 20_000, 33_000, 45_000, 50_000):
        assert should_inject_city_context(state, "k", CTX, t, WINDOW) is False
    # Snapshot still reflects the first (only) injection
    assert state["k"] == ContextInjectionRecord(at=0, ctx=CTX)


def test_reinjects_once_window_elapsed():
    state = {}
    assert should_inject_city_context(state, "k", CTX, 0, WINDOW) is True
    assert should_inject_city_context(state, "k", CTX, 59_999, WINDOW) is False
    assert should_inject_city_context(state, "k", CTX, 60_000, WINDOW) is True  # boundary inclusive
    assert state["k"].at == 60_000


def test_reinjects_immediately_when_snapshot_changed():
    state = {}
    assert should_inject_city_context(state, "k", CTX, 0, WINDOW) is True
    assert should_inject_city_context(state, "k", "[CITY CONTEXT updated]", 5_000, WINDOW) is True
    assert state["k"].ctx == "[CITY CONTEXT updated]"


def test_keys_are_independent():
    state = {}
    assert should_inject_city_context(state, "acct:aaga", CTX, 0, WINDOW) is True
    # A different peer within the same window must NOT be starved
    assert should_inject_city_context(state, "acct:bob", CTX, 1_000, WINDOW) is True
