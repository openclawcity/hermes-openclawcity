"""Port of packages/nanoclaw-channel/tests/normalizer.test.ts."""
import time

from occ_core.normalizer import format_event_text, format_welcome_text, normalize


def make_event(**overrides):
    event = {
        "type": "city_event",
        "seq": 1,
        "eventType": "dm_message",
        "from": {"id": "user-1", "name": "Alice", "avatar": "https://example.com/alice.png"},
        "text": "Hello there",
        "metadata": {"conversationId": "conv-1"},
    }
    event.update(overrides)
    return event


# ── format_event_text ──


def test_formats_dm_message():
    assert format_event_text(make_event(eventType="dm_message", text="Hey!")) == "[DM from Alice] Hey!"


def test_formats_dm_request():
    assert (
        format_event_text(make_event(eventType="dm_request", text="Can we chat?"))
        == "[DM request from Alice] Can we chat?"
    )


def test_formats_proposal_received_with_expiry():
    event = make_event(
        eventType="proposal_received",
        text="Let's explore together",
        metadata={"proposalId": "p1", "expiresIn": 10},
    )
    assert format_event_text(event) == "[Proposal from Alice] Let's explore together (expires in 10 min)"


def test_formats_proposal_received_without_expiry():
    event = make_event(eventType="proposal_received", text="Let's go", metadata={})
    assert format_event_text(event) == "[Proposal from Alice] Let's go"


def test_formats_proposal_accepted():
    assert (
        format_event_text(make_event(eventType="proposal_accepted", text="Sounds great!"))
        == "[Proposal accepted by Alice] Sounds great!"
    )


def test_formats_chat_mention_with_building_id():
    event = make_event(
        eventType="chat_mention",
        text="@Bot check this out",
        metadata={"buildingId": "cafe-42", "zoneId": 1},
    )
    assert format_event_text(event) == "[Chat in building cafe-42] Alice: @Bot check this out"


def test_formats_chat_mention_without_building_id():
    event = make_event(eventType="chat_mention", text="@Bot hey", metadata={"zoneId": 3})
    assert format_event_text(event) == "[Chat in Zone 3] Alice: @Bot hey"


def test_chat_mention_null_building_falls_back_to_zone():
    event = make_event(eventType="chat_mention", text="@Bot hey", metadata={"buildingId": None, "zoneId": 5})
    assert format_event_text(event) == "[Chat in Zone 5] Alice: @Bot hey"


def test_formats_owner_message():
    assert (
        format_event_text(make_event(eventType="owner_message", text="How are you doing?"))
        == "[Message from your human] How are you doing?"
    )


def test_formats_building_activity():
    event = make_event(
        eventType="building_activity",
        text="Started a jam session",
        metadata={"buildingId": "music-hall"},
    )
    assert format_event_text(event) == "[Activity in music-hall] Alice: Started a jam session"


def test_formats_artifact_reaction():
    event = make_event(
        eventType="artifact_reaction",
        text="",
        metadata={"artifactId": "art-1", "reaction": "🔥"},
    )
    result = format_event_text(event)
    assert "Alice" in result
    assert "reacted" in result
    assert "🔥" in result
    assert "art-1" in result


def test_formats_welcome():
    assert format_event_text(make_event(eventType="welcome", text="Welcome to the city!")) == "[City] Welcome to the city!"


def test_handles_unknown_event_types():
    assert format_event_text(make_event(eventType="unknown_type", text="something")) == "[unknown_type] Alice: something"


def test_handles_missing_text():
    result = format_event_text(make_event(text=None))
    assert result is not None
    assert "None" not in result


def test_handles_empty_string_text():
    assert format_event_text(make_event(text="")) == "[DM from Alice]"


def test_handles_missing_from_name():
    result = format_event_text(make_event(**{"from": {"id": "u1", "name": ""}}))
    assert result is not None
    # Empty name passes through (nullish check is on None, not falsy)
    assert "[DM from ]" in result


def test_handles_null_from_defensively():
    result = format_event_text(make_event(**{"from": None}))
    assert "Unknown" in result
    assert "None" not in result


# ── format_welcome_text ──


def test_formats_full_welcome_with_building():
    welcome = {
        "type": "welcome",
        "version": 1,
        "location": {"zoneId": 1, "zoneName": "Downtown", "buildingName": "The Byte Cafe"},
        "nearby": [{"id": "b1", "name": "Alice"}, {"id": "b2", "name": "Bob"}],
        "pending": [],
    }
    text = format_welcome_text(welcome)
    assert "Downtown" in text
    assert "The Byte Cafe" in text
    assert "2 bots nearby" in text
    assert "Alice" in text
    assert "Bob" in text


def test_formats_welcome_without_building():
    welcome = {
        "type": "welcome",
        "version": 1,
        "location": {"zoneId": 1, "zoneName": "Downtown"},
        "nearby": [{"id": "b1", "name": "Alice"}],
        "pending": [],
    }
    text = format_welcome_text(welcome)
    assert "Downtown" in text
    assert "in null" not in text
    assert "in None" not in text


def test_handles_no_nearby_bots():
    welcome = {
        "type": "welcome",
        "version": 1,
        "location": {"zoneId": 2, "zoneName": "Suburbs"},
        "nearby": [],
        "pending": [],
    }
    assert "No bots nearby" in format_welcome_text(welcome)


def test_shows_pending_events_count():
    welcome = {
        "type": "welcome",
        "version": 1,
        "location": {"zoneId": 1, "zoneName": "Downtown"},
        "nearby": [],
        "pending": [
            {"type": "city_event", "seq": 1, "eventType": "dm_message", "from": {"id": "u1", "name": "A"}, "metadata": {}},
            {"type": "city_event", "seq": 2, "eventType": "dm_message", "from": {"id": "u2", "name": "B"}, "metadata": {}},
        ],
    }
    assert "2 pending event(s)" in format_welcome_text(welcome)


def test_omits_pending_text_when_none():
    welcome = {
        "type": "welcome",
        "version": 1,
        "location": {"zoneId": 1, "zoneName": "Downtown"},
        "nearby": [],
        "pending": [],
    }
    assert "pending" not in format_welcome_text(welcome)


# ── normalize ──


def test_produces_valid_envelope_with_all_fields():
    envelope = normalize(make_event(seq=42, timestamp=1700000000))
    assert envelope.id == "occ-42"
    assert envelope.timestamp == 1700000000
    assert envelope.channel_id == "openclawcity"
    assert envelope.sender_id == "user-1"
    assert envelope.sender_name == "Alice"
    assert envelope.sender_avatar == "https://example.com/alice.png"
    assert envelope.text == "[DM from Alice] Hello there"
    assert envelope.metadata["eventType"] == "dm_message"
    assert envelope.metadata["seq"] == 42
    assert envelope.metadata["conversationId"] == "conv-1"


def test_uses_now_when_timestamp_missing():
    before = int(time.time() * 1000)
    envelope = normalize(make_event(timestamp=None))
    after = int(time.time() * 1000)
    assert before <= envelope.timestamp <= after


def test_handles_missing_from_gracefully():
    envelope = normalize(make_event(**{"from": None}))
    assert envelope.sender_id == "unknown"
    assert envelope.sender_name == "Unknown"
    assert envelope.sender_avatar is None


def test_handles_missing_metadata_gracefully():
    envelope = normalize(make_event(metadata=None))
    assert envelope.metadata["eventType"] == "dm_message"
    assert envelope.metadata["seq"] == 1


def test_preserves_extra_metadata_fields():
    envelope = normalize(make_event(metadata={"conversationId": "c1", "zoneId": 3, "custom": "value"}))
    assert envelope.metadata["conversationId"] == "c1"
    assert envelope.metadata["zoneId"] == 3
    assert envelope.metadata["custom"] == "value"


def test_id_uses_event_seq_not_a_counter():
    assert normalize(make_event(seq=100)).id == "occ-100"
    assert normalize(make_event(seq=200)).id == "occ-200"


# ── Fuzz: junk frames must degrade like TS optional chaining, never raise ──


JUNK_EVENTS = [
    {},
    {"type": "city_event"},
    {"from": "not-a-dict", "metadata": [1, 2, 3]},
    {"from": 42, "text": 123, "seq": "abc"},
    {"eventType": "chat_mention", "metadata": "zone-as-string"},
    {"eventType": "proposal_received", "from": [], "metadata": ()},
    {"eventType": "dm_message", "from": {"name": 7}, "text": "☂🜲" * 50_000},
    {"eventType": None, "from": None, "text": None, "metadata": None, "seq": None},
    {"eventType": "artifact_reaction", "metadata": {"reaction": None, "artifactId": None}},
]


def test_normalize_never_raises_on_junk_frames():
    for event in JUNK_EVENTS:
        envelope = normalize(dict(event))
        assert isinstance(envelope.text, str)
        assert isinstance(envelope.metadata, dict)
        assert envelope.channel_id == "openclawcity"


def test_format_event_text_never_raises_on_junk_frames():
    for event in JUNK_EVENTS:
        assert isinstance(format_event_text(dict(event)), str)


def test_junk_from_falls_back_to_unknown():
    envelope = normalize({"eventType": "dm_message", "from": "junk", "text": "hi", "seq": 1})
    assert envelope.sender_id == "unknown"
    assert envelope.sender_name == "Unknown"
    assert envelope.text == "[DM from Unknown] hi"


def test_non_dict_metadata_is_ignored_not_merged():
    envelope = normalize({"eventType": "dm_message", "metadata": [1, 2], "seq": 5, "text": "x"})
    assert envelope.metadata["eventType"] == "dm_message"
    assert envelope.metadata["seq"] == 5


def test_format_welcome_text_tolerates_junk():
    junk_welcomes = [
        {"location": "junk", "nearby_bots": "junk", "pending": 5},
        {"nearby": [{"name": None}, "junk", {"name": 5}, {}]},
        {"location": {"zoneName": None, "zoneId": None}},
        {},
    ]
    for welcome in junk_welcomes:
        assert isinstance(format_welcome_text(dict(welcome)), str)


def test_unicode_text_passes_through():
    envelope = normalize(
        {"eventType": "dm_message", "from": {"id": "u1", "name": "Ålice 🌆"},
         "text": "héllo — こんにちは ‮", "seq": 9}
    )
    assert "héllo — こんにちは" in envelope.text
    assert "Ålice 🌆" in envelope.text
