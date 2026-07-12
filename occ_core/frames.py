"""Frame shapes and the normalized message envelope.

Python port of packages/nanoclaw-channel/src/types.ts. Server frames stay
plain dicts (they are JSON on the wire); only the normalized envelope gets a
dataclass because the glue mutates its text (city-context prepend).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

PROTOCOL_VERSION = 1


class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"


# City event types addressed directly to the agent (auto-engage). Ambient
# observations (building_activity, artifact_reaction, welcome) are not listed.
DIRECT_MENTION_EVENTS = frozenset(
    {
        "owner_message",
        "dm_request",
        "dm_message",
        "dm",
        "dm_approved",
        "chat_mention",
        "proposal_received",
        "proposal_accepted",
    }
)

# City event types whose reply is a private DM. Only these route to dm_reply;
# everything else (including dm_request) speaks.
DM_EVENTS = frozenset({"dm_message", "dm", "dm_approved"})


@dataclass
class MessageEnvelope:
    """Normalized city event, mirroring types.ts MessageEnvelope."""

    id: str
    timestamp: int  # epoch milliseconds
    channel_id: str
    sender_id: str
    sender_name: str
    text: str
    sender_avatar: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def is_direct_mention(event_type: str) -> bool:
    return event_type in DIRECT_MENTION_EVENTS


def is_dm_event(event_type: str) -> bool:
    return event_type in DM_EVENTS
