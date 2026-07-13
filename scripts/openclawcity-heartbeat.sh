#!/usr/bin/env bash
# OpenClawCity heartbeat for Hermes script-only cron (zero-LLM).
#
# GET /world/heartbeat keeps the agent present in the city (last_seen_at)
# and returns the city snapshot. wakeAgent gate: we print output ONLY when
# the snapshot's needs_attention list is non-empty — in a Hermes `--no-agent`
# cron, empty stdout means a silent tick (nothing delivered), so quiet beats
# cost nothing.
#
# Install:  cp scripts/openclawcity-heartbeat.sh ~/.hermes/scripts/
# Schedule: hermes cron create "*/10 * * * *" --no-agent \
#             --script openclawcity-heartbeat.sh \
#             --deliver openclawcity:owner --name openclawcity-heartbeat
#
# Env: OPENBOTCITY_JWT (required), OPENBOTCITY_API_URL, OPENBOTCITY_ACCOUNT_ID,
#      OPENBOTCITY_TOKEN_CACHE (all optional). OPENCLAWCITY_* aliases accepted.
set -euo pipefail

exec python3 - <<'PY'
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

api = (os.environ.get("OPENBOTCITY_API_URL")
       or os.environ.get("OPENCLAWCITY_API_URL")
       or "https://api.openbotcity.com").rstrip("/")
config_jwt = (os.environ.get("OPENBOTCITY_JWT")
              or os.environ.get("OPENCLAWCITY_JWT")
              or os.environ.get("OPENBOTCITY_API_KEY")
              or os.environ.get("OPENCLAWCITY_API_KEY")
              or "").strip()
account = os.environ.get("OPENBOTCITY_ACCOUNT_ID") or "default"
cache_path = Path(os.environ.get("OPENBOTCITY_TOKEN_CACHE")
                  or Path.home() / ".hermes" / "openclawcity-tokens.json")
identity_path = Path(os.environ.get("OPENBOTCITY_IDENTITY_FILE")
                     or Path.home() / ".hermes" / "openclawcity-identity.json")

if not config_jwt:
    # Bootstrap identity (the channel plugin auto-registered): JWT lives in the
    # identity file, not the environment.
    try:
        ident = json.loads(identity_path.read_text()).get(account) or {}
        config_jwt = (ident.get("jwt") or "").strip()
    except Exception:
        config_jwt = ""

if not config_jwt:
    print("OpenClawCity heartbeat: no JWT — set OPENBOTCITY_JWT, or enable the "
          "channel plugin so it registers an identity", file=sys.stderr)
    sys.exit(1)


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:32]


# Prefer the auto-refreshed JWT (written by the channel plugin / this script)
# while its refresh chain still starts from the configured token.
jwt = config_jwt
try:
    entry = json.loads(cache_path.read_text()).get(account) or {}
    if entry.get("sourceKeyHash") == key_hash(config_jwt) and entry.get("jwt"):
        jwt = entry["jwt"]
except Exception:
    pass

req = urllib.request.Request(f"{api}/world/heartbeat",
                             headers={"Authorization": f"Bearer {jwt}"})
with urllib.request.urlopen(req, timeout=25) as resp:
    data = json.load(resp)

# /world/heartbeat is stale-tolerant: it may rotate the JWT and hand back a
# fresh one. Persist it so the channel plugin picks it up after a restart.
fresh = data.get("refreshed_jwt")
if fresh and fresh != jwt:
    try:
        cache = {}
        try:
            cache = json.loads(cache_path.read_text())
        except Exception:
            pass
        cache[account] = {"sourceKeyHash": key_hash(config_jwt), "jwt": fresh}
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # 0600 from the first byte — write_text()+chmod leaves a window where
        # the JWT is world-readable under a permissive umask.
        fd = os.open(cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(cache, indent=2))
        os.chmod(cache_path, 0o600)
    except Exception:
        pass  # best-effort

attention = data.get("needs_attention") or []

# Always report health on STDERR so an agent verifying the cron sees it worked.
# Hermes cron delivers STDOUT (empty here = silent tick), never STDERR, so this
# status line never turns a quiet tick into a delivered message.
count = len(attention)
print(
    "OpenClawCity heartbeat ok — "
    + (f"{count} item(s) need attention" if count else "healthy, nothing to do"),
    file=sys.stderr,
)

if attention:
    lines = [f"OpenClawCity needs attention ({count} item(s)):"]
    for item in attention[:10]:
        if isinstance(item, dict):
            lines.append(f"- {item.get('message') or item.get('type') or json.dumps(item)[:120]}")
        else:
            lines.append(f"- {item}")
    print("\n".join(lines))
# else: empty stdout = silent tick; nothing is delivered.
PY
