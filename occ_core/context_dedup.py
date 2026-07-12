"""City-context injection de-duplication.

Port of packages/nanoclaw-channel/src/context-dedup.ts.

The channel prepends a [CITY CONTEXT] heartbeat snapshot to each inbound
event so the model knows where it is. That snapshot is cached (~5 min), so
within a burst of events every prepend is byte-for-byte identical —
re-injecting it adds no information, only token bloat (the Aaga model-login
loop, 2026-06-30, prepended it ~7x in a minute).

This gates the prepend: inject only when it's the first event for a key, the
window has elapsed, or the snapshot actually changed (cache refresh). Keyed
per (account, peer) so two concurrent conversations each still receive
context — only redundant repeats inside one conversation are suppressed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass
class ContextInjectionRecord:
    at: int  # epoch ms of the last injection
    ctx: str  # the snapshot injected then


def should_inject_city_context(
    state: Dict[str, ContextInjectionRecord],
    key: str,
    ctx: str,
    now: int,
    window_ms: int,
) -> bool:
    """Decide whether to prepend the city-context snapshot for `key`, and
    record the decision in `state`. Returns True to inject (and updates the
    record), False to skip a recent identical repeat. Pure given
    (state, key, ctx, now, window_ms).
    """
    prev = state.get(key)
    inject = prev is None or now - prev.at >= window_ms or prev.ctx != ctx
    if inject:
        state[key] = ContextInjectionRecord(at=now, ctx=ctx)
    return inject
