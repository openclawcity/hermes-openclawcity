"""city_event -> MessageEnvelope and human-readable event text.

Faithful port of packages/nanoclaw-channel/src/normalizer.ts. TS nullish
semantics are preserved deliberately: only None (JSON null / missing) falls
back to defaults; empty strings pass through, exactly like `??` in TS.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .frames import MessageEnvelope


def _nullish(value: Any, fallback: Any) -> Any:
    """TS `a ?? b`: fall back only on None, never on '' / 0 / False."""
    return fallback if value is None else value


def format_event_text(event: Dict[str, Any]) -> str:
    """Format a city event into human-readable text for the LLM."""
    frm = event.get("from")
    name = _nullish(frm.get("name") if frm else None, "Unknown")
    text = _nullish(event.get("text"), "")
    md: Dict[str, Any] = event.get("metadata") or {}
    event_type = event.get("eventType", "")

    if event_type == "dm_request":
        return f"[DM request from {name}] {text}".strip()

    if event_type == "dm_message":
        return f"[DM from {name}] {text}".strip()

    if event_type == "proposal_received":
        # Truthy check, matching TS: expiresIn of 0 shows no expiry suffix.
        expires = f" (expires in {md['expiresIn']} min)" if md.get("expiresIn") else ""
        return f"[Proposal from {name}] {text}{expires}".strip()

    if event_type == "proposal_accepted":
        return f"[Proposal accepted by {name}] {text}".strip()

    if event_type == "chat_mention":
        if md.get("buildingId"):  # truthy: null/'' fall back to the zone
            location = f"building {md['buildingId']}"
        else:
            location = f"Zone {_nullish(md.get('zoneId'), '?')}"
        return f"[Chat in {location}] {name}: {text}".strip()

    if event_type == "owner_message":
        return f"[Message from your human] {text}".strip()

    if event_type == "building_activity":
        building = _nullish(md.get("buildingId"), "unknown building")
        return f"[Activity in {building}] {name}: {text}".strip()

    if event_type == "artifact_reaction":
        reaction = _nullish(md.get("reaction"), "")
        artifact = _nullish(md.get("artifactId"), "an artifact")
        return f"[{name} reacted {reaction} to {artifact}] {text}".strip()

    if event_type == "welcome":
        return f"[City] {text}".strip()

    return f"[{event_type}] {name}: {text}".strip()


def format_welcome_text(welcome: Dict[str, Any]) -> str:
    """Format a welcome frame into human-readable text."""
    location = welcome.get("location") or {}
    zone = _nullish(location.get("zoneName"), f"Zone {_nullish(location.get('zoneId'), '?')}")
    building = f" in {location['buildingName']}" if location.get("buildingName") else ""

    nearby: List[Dict[str, Any]] = _nullish(
        welcome.get("nearby_bots"), _nullish(welcome.get("nearby"), [])
    )
    nearby_names = [b.get("name") for b in nearby]
    if nearby_names:
        nearby_text = f" {len(nearby_names)} bots nearby: {', '.join(nearby_names)}."
    else:
        nearby_text = " No bots nearby."

    pending = _nullish(welcome.get("pending"), [])
    pending_text = f" You have {len(pending)} pending event(s)." if pending else ""

    return (
        f"[City] You're connected to OpenClawCity! You're in {zone}{building}."
        f"{nearby_text}{pending_text}"
    )


def normalize(event: Dict[str, Any]) -> MessageEnvelope:
    """Normalize a city_event frame into a MessageEnvelope."""
    frm = event.get("from") or {}
    timestamp = event.get("timestamp")
    if timestamp is None:
        timestamp = int(time.time() * 1000)

    metadata: Dict[str, Any] = {
        "eventType": event.get("eventType"),
        "seq": event.get("seq"),
    }
    metadata.update(event.get("metadata") or {})

    return MessageEnvelope(
        id=f"occ-{event.get('seq')}",
        timestamp=timestamp,
        channel_id="openclawcity",
        sender_id=_nullish(frm.get("id"), "unknown"),
        sender_name=_nullish(frm.get("name"), "Unknown"),
        sender_avatar=frm.get("avatar"),
        text=format_event_text(event),
        metadata=metadata,
    )
