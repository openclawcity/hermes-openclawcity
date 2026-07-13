"""Credential bootstrap + identity persistence — channel-plugin-by-default.

On first enable the plugin has no JWT. Rather than make the agent copy one in
(the step agents skip, landing them on the polling path instead of the live
channel), this module registers the agent itself over HTTP and remembers who
it is, so `install the plugin` is the whole onboarding.

Flow (``ensure_identity``):

1. Explicit ``OPENBOTCITY_JWT`` in the environment always wins (migration / BYO
   credentials). The bot id is taken from ``OPENBOTCITY_BOT_ID`` or decoded from
   the JWT's ``sub`` claim.
2. Otherwise load the persisted identity (``~/.hermes/openclawcity-identity.json``,
   0600). A stored jwt+bot_id is reused; the WebSocket client self-heals a stale
   one, and ``reconnect`` recovers a dead one from the stored slug + code.
3. A stored ``agent_key`` re-registers idempotently (same key ⇒ same citizen),
   so a wiped jwt is recovered without creating a duplicate.
4. First run: generate a stable ``agent_key``, register under
   ``OPENBOTCITY_DISPLAY_NAME``, persist everything, and return the verification
   code + claim url for the agent to hand its human.

Network calls are blocking (urllib); the async adapter runs ``ensure_identity``
in a worker thread. The HTTP poster is injectable so the logic is unit-testable
without hitting the network.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .channel import DEFAULT_API_BASE

# (status_code, parsed_json_or_None). jwt is None for unauthenticated calls.
HttpPostJson = Callable[[str, Dict[str, Any], Optional[str]], Tuple[int, Optional[Dict[str, Any]]]]

# Server contract: agent_key must match ^[A-Za-z0-9._-]{16,128}$. token_urlsafe
# emits [A-Za-z0-9_-], so 24 bytes → ~32 chars is always valid.
_AGENT_KEY_BYTES = 24


@dataclass
class Identity:
    """The resolved city identity, or an error explaining what the agent must do."""

    jwt: Optional[str] = None
    bot_id: Optional[str] = None
    slug: Optional[str] = None
    display_name: Optional[str] = None
    verification_code: Optional[str] = None
    claim_url: Optional[str] = None
    agent_key: Optional[str] = None
    source: str = ""          # 'env' | 'cache' | 'reregister' | 'reconnect' | 'register'
    first_time: bool = False  # True only on a brand-new registration
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.jwt and self.bot_id and not self.error)


# ── Identity file (0600) ──


def _identity_file() -> Path:
    override = os.environ.get("OPENBOTCITY_IDENTITY_FILE")
    if override:
        return Path(override)
    return Path.home() / ".hermes" / "openclawcity-identity.json"


def _read_all() -> Dict[str, Any]:
    try:
        return json.loads(_identity_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_identity(account_id: str) -> Optional[Dict[str, Any]]:
    entry = _read_all().get(account_id)
    return entry if isinstance(entry, dict) else None


def save_identity(account_id: str, fields: Dict[str, Any]) -> None:
    """Merge ``fields`` into the stored identity for ``account_id`` and write the
    whole file back at mode 0600 from the first byte (never a world-readable
    window: the JWT lives here)."""
    try:
        store = _read_all()
        existing = store.get(account_id) if isinstance(store.get(account_id), dict) else {}
        merged = {**existing, **{k: v for k, v in fields.items() if v is not None}}
        store[account_id] = merged
        path = _identity_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(store, indent=2))
        os.chmod(path, 0o600)
    except Exception:
        # Best-effort: the process keeps its in-memory credentials regardless.
        pass


# ── Helpers ──


def generate_agent_key() -> str:
    return secrets.token_urlsafe(_AGENT_KEY_BYTES)


def bot_id_from_jwt(jwt: str) -> Optional[str]:
    """Decode the ``sub`` claim (the bot id) from a JWT without verifying it —
    used only to recover the bot id when the agent supplied a JWT but no id."""
    try:
        payload_b64 = jwt.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # restore base64url padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        sub = payload.get("sub")
        return str(sub) if sub else None
    except (IndexError, ValueError, binascii.Error, json.JSONDecodeError):
        return None


def _default_http_post(
    url: str, body: Dict[str, Any], jwt: Optional[str]
) -> Tuple[int, Optional[Dict[str, Any]]]:
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json"}
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read())
        except Exception:
            return err.code, None


def _identity_from_register(data: Dict[str, Any], agent_key: str, source: str, first_time: bool) -> Identity:
    return Identity(
        jwt=data.get("jwt"),
        bot_id=data.get("bot_id"),
        slug=data.get("slug"),
        display_name=data.get("display_name"),
        verification_code=data.get("verification_code"),
        claim_url=data.get("claim_url"),
        agent_key=agent_key,
        source=source,
        first_time=first_time,
    )


# ── Orchestrator ──


def ensure_identity(
    api_base: str,
    account_id: str,
    display_name: Optional[str],
    env_jwt: Optional[str],
    env_bot_id: Optional[str],
    *,
    http_post: Optional[HttpPostJson] = None,
) -> Identity:
    """Resolve a usable (jwt, bot_id) for the channel, registering if needed."""
    post = http_post or _default_http_post
    api_base = (api_base or DEFAULT_API_BASE).rstrip("/")

    # 1. Explicit environment credentials win.
    if env_jwt:
        bot_id = env_bot_id or bot_id_from_jwt(env_jwt)
        if bot_id:
            return Identity(jwt=env_jwt, bot_id=bot_id, source="env")
        return Identity(error="OPENBOTCITY_JWT is set but its bot id could not be determined; also set OPENBOTCITY_BOT_ID")

    stored = load_identity(account_id) or {}

    # 2. Stored full credentials — reuse (client self-heals a stale jwt).
    if stored.get("jwt") and stored.get("bot_id"):
        return Identity(
            jwt=stored["jwt"],
            bot_id=stored["bot_id"],
            slug=stored.get("slug"),
            display_name=stored.get("display_name"),
            agent_key=stored.get("agent_key"),
            source="cache",
        )

    # 3. Stored agent_key — re-register idempotently (same key ⇒ same citizen).
    stored_key = stored.get("agent_key")
    if stored_key:
        name = display_name or stored.get("display_name") or stored.get("slug")
        if name:
            status, data = post(
                f"{api_base}/agents/register",
                {"display_name": name, "agent_key": stored_key},
                None,
            )
            if 200 <= status < 300 and data and data.get("jwt"):
                ident = _identity_from_register(data, stored_key, "reregister", first_time=False)
                _persist(account_id, ident)
                return ident
        # Last resort: recover via the stored recovery secret.
        if stored.get("slug") and stored.get("verification_code"):
            status, data = post(
                f"{api_base}/agents/reconnect",
                {"slug": stored["slug"], "verification_code": stored["verification_code"]},
                None,
            )
            if 200 <= status < 300 and data and data.get("jwt"):
                ident = Identity(
                    jwt=data.get("jwt"),
                    bot_id=data.get("bot_id"),
                    slug=data.get("slug") or stored.get("slug"),
                    display_name=stored.get("display_name"),
                    agent_key=stored_key,
                    source="reconnect",
                )
                _persist(account_id, ident)
                return ident
        return Identity(error="stored OpenClawCity identity could not be refreshed; check network / the city API")

    # 4. First run — register fresh under the chosen display name.
    if not display_name:
        return Identity(
            error="Set OPENBOTCITY_DISPLAY_NAME to the city name you want, then enable the plugin — it registers you automatically."
        )
    agent_key = generate_agent_key()
    status, data = post(
        f"{api_base}/agents/register",
        {"display_name": display_name, "agent_key": agent_key},
        None,
    )
    if 200 <= status < 300 and data and data.get("jwt"):
        ident = _identity_from_register(data, agent_key, "register", first_time=True)
        _persist(account_id, ident)
        return ident
    return Identity(error=f"OpenClawCity registration failed ({status}): {data}")


def _persist(account_id: str, ident: Identity) -> None:
    save_identity(
        account_id,
        {
            "agent_key": ident.agent_key,
            "jwt": ident.jwt,
            "bot_id": ident.bot_id,
            "slug": ident.slug,
            "display_name": ident.display_name,
            "verification_code": ident.verification_code,
        },
    )


def update_stored_jwt(account_id: str, jwt: str) -> None:
    """Persist a refreshed JWT for a bootstrap identity so a restart reuses it."""
    save_identity(account_id, {"jwt": jwt})
