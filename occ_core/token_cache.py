"""Persistence for automatically-refreshed JWTs.

Port of packages/nanoclaw-channel/src/token-cache.ts, relocated under
~/.hermes/ (Hermes' own config dir) instead of ~/.openclaw/.

Hermes has no config-write API for plugin env vars, so a token refreshed at
runtime (client self-heal on token_expired) would be lost on restart and the
account would boot with the old config token again. We cache the refreshed
JWT on disk, keyed by a hash of the CONFIG token it was derived from: if the
owner deliberately re-keys the config, the cache no longer matches and is
ignored — a manual rotation always wins.

File I/O only in this module (no network).
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _cache_file() -> Path:
    override = os.environ.get("OPENBOTCITY_TOKEN_CACHE")
    if override:
        return Path(override)
    return Path.home() / ".hermes" / "openclawcity-tokens.json"


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _read_cache() -> Dict[str, Any]:
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_owner_only(path: Path, text: str) -> None:
    """Create/overwrite with mode 0600 from the first byte — a plain
    write_text() + chmod() leaves a umask-default window where the JWT is
    world-readable."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)  # tighten files that pre-existed with wider modes


def load_refreshed_token(account_id: str, config_api_key: str) -> Optional[str]:
    """Return a previously-refreshed JWT for this account, but ONLY if the
    config token is still the one that refresh chain started from."""
    entry = _read_cache().get(account_id)
    if not isinstance(entry, dict):
        return None
    if entry.get("sourceKeyHash") != _hash_key(config_api_key):
        return None  # config was re-keyed — respect it
    return entry.get("jwt") or None


def save_refreshed_token(account_id: str, config_api_key: str, jwt: str) -> None:
    try:
        cache = _read_cache()
        cache[account_id] = {
            "sourceKeyHash": _hash_key(config_api_key),
            "jwt": jwt,
            "savedAt": datetime.now(timezone.utc).isoformat(),
        }
        path = _cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_owner_only(path, json.dumps(cache, indent=2))
    except Exception:
        # Best-effort: in-memory token still works for this process lifetime
        pass
