"""Tests for occ_core.identity — the channel-by-default credential bootstrap.

No network: a fake http_post records calls and returns canned responses. The
identity file is redirected to a tmp path via OPENBOTCITY_IDENTITY_FILE.
"""
import base64
import json
import os
import re
import stat

import pytest

from occ_core.identity import (
    Identity,
    bot_id_from_jwt,
    ensure_identity,
    generate_agent_key,
    load_identity,
    save_identity,
    update_stored_jwt,
)

API = "https://api.openbotcity.com"
ACCOUNT = "default"


@pytest.fixture(autouse=True)
def _identity_file(tmp_path, monkeypatch):
    path = tmp_path / "identity.json"
    monkeypatch.setenv("OPENBOTCITY_IDENTITY_FILE", str(path))
    return path


def _make_jwt(sub: str) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'HS256'})}.{seg({'sub': sub})}.sig"


class FakePost:
    """Records (url, body) calls and replays canned (status, data) responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, body, jwt):
        self.calls.append((url, body, jwt))
        return self._responses.pop(0)


# ── helpers ──


def test_generate_agent_key_matches_server_pattern():
    pattern = re.compile(r"^[A-Za-z0-9._-]{16,128}$")
    for _ in range(50):
        assert pattern.match(generate_agent_key())


def test_bot_id_from_jwt_decodes_sub():
    assert bot_id_from_jwt(_make_jwt("bot-abc")) == "bot-abc"
    assert bot_id_from_jwt("not-a-jwt") is None
    assert bot_id_from_jwt("") is None


def test_save_and_load_identity_roundtrip(_identity_file):
    save_identity(ACCOUNT, {"jwt": "j1", "bot_id": "b1", "agent_key": "k1"})
    loaded = load_identity(ACCOUNT)
    assert loaded["jwt"] == "j1"
    assert loaded["bot_id"] == "b1"
    assert loaded["agent_key"] == "k1"


def test_identity_file_is_owner_only(_identity_file):
    save_identity(ACCOUNT, {"jwt": "secret"})
    mode = stat.S_IMODE(os.stat(_identity_file).st_mode)
    assert mode == 0o600


def test_save_identity_merges_without_dropping_fields(_identity_file):
    save_identity(ACCOUNT, {"agent_key": "k1", "jwt": "j1", "bot_id": "b1"})
    update_stored_jwt(ACCOUNT, "j2")
    loaded = load_identity(ACCOUNT)
    assert loaded["jwt"] == "j2"
    assert loaded["agent_key"] == "k1"  # not clobbered
    assert loaded["bot_id"] == "b1"


# ── ensure_identity ──


def test_env_credentials_win_without_network():
    post = FakePost([])
    ident = ensure_identity(API, ACCOUNT, None, "env-jwt", "env-bot", http_post=post)
    assert ident.ok
    assert ident.source == "env"
    assert (ident.jwt, ident.bot_id) == ("env-jwt", "env-bot")
    assert post.calls == []  # no registration


def test_env_jwt_without_bot_id_decodes_sub():
    post = FakePost([])
    jwt = _make_jwt("bot-from-sub")
    ident = ensure_identity(API, ACCOUNT, None, jwt, None, http_post=post)
    assert ident.ok
    assert ident.bot_id == "bot-from-sub"
    assert post.calls == []


def test_first_time_registration(_identity_file):
    post = FakePost([(200, {
        "jwt": "new-jwt", "bot_id": "new-bot", "slug": "croissantman",
        "verification_code": "OBC-1234-5678", "claim_url": "https://openclawcity.ai/verify",
    })])
    ident = ensure_identity(API, ACCOUNT, "croissantman", None, None, http_post=post)
    assert ident.ok
    assert ident.source == "register"
    assert ident.first_time is True
    assert ident.verification_code == "OBC-1234-5678"
    # registered against the real endpoint with a valid agent_key + name
    url, body, jwt = post.calls[0]
    assert url == f"{API}/agents/register"
    assert body["display_name"] == "croissantman"
    assert re.match(r"^[A-Za-z0-9._-]{16,128}$", body["agent_key"])
    assert jwt is None
    # persisted for next time
    stored = load_identity(ACCOUNT)
    assert stored["jwt"] == "new-jwt"
    assert stored["agent_key"] == body["agent_key"]


def test_no_display_name_and_nothing_stored_is_an_actionable_error(_identity_file):
    post = FakePost([])
    ident = ensure_identity(API, ACCOUNT, None, None, None, http_post=post)
    assert not ident.ok
    assert "OPENBOTCITY_DISPLAY_NAME" in ident.error
    assert post.calls == []  # never hit the network without a name


def test_stored_full_credentials_reused_without_network(_identity_file):
    save_identity(ACCOUNT, {"jwt": "cached-jwt", "bot_id": "cached-bot", "slug": "me"})
    post = FakePost([])
    ident = ensure_identity(API, ACCOUNT, "ignored", None, None, http_post=post)
    assert ident.ok
    assert ident.source == "cache"
    assert (ident.jwt, ident.bot_id) == ("cached-jwt", "cached-bot")
    assert post.calls == []


def test_stored_agent_key_reregisters_idempotently(_identity_file):
    # A wiped jwt but a remembered agent_key -> re-register (same key => same bot).
    save_identity(ACCOUNT, {"agent_key": "stable-key-0123456789", "display_name": "me", "slug": "me"})
    post = FakePost([(200, {"jwt": "fresh-jwt", "bot_id": "same-bot", "slug": "me"})])
    ident = ensure_identity(API, ACCOUNT, None, None, None, http_post=post)
    assert ident.ok
    assert ident.source == "reregister"
    assert ident.first_time is False
    url, body, _ = post.calls[0]
    assert url == f"{API}/agents/register"
    assert body["agent_key"] == "stable-key-0123456789"  # SAME key, no duplicate
    assert body["display_name"] == "me"


def test_registration_failure_surfaces_error(_identity_file):
    post = FakePost([(409, {"error": "name taken"})])
    ident = ensure_identity(API, ACCOUNT, "taken", None, None, http_post=post)
    assert not ident.ok
    assert "registration failed" in ident.error.lower()


# ── 25 Aug 2026 audit round: brand, display_name persistence, claimed-agent recovery ──


def test_register_declares_the_openclawcity_brand(_identity_file):
    """urllib sends no Origin header, so without an explicit brand the server
    falls back to OpenBotCity branding for the claim URL and emails."""
    post = FakePost([(200, {"jwt": "j", "bot_id": "b", "slug": "s",
                            "verification_code": "OBC-1111-2222"})])
    ensure_identity(API, ACCOUNT, "brandy", None, None, http_post=post)
    _, body, _ = post.calls[0]
    assert body["brand"] == "openclawcity"


def test_fresh_registration_persists_the_sent_display_name(_identity_file):
    """The 201 response has no display_name — keep the name we registered
    under so post-restart recovery paths still know it."""
    post = FakePost([(200, {"jwt": "j", "bot_id": "b", "slug": "fancy-name",
                            "verification_code": "OBC-1111-2222"})])
    ident = ensure_identity(API, ACCOUNT, "Fancy Name", None, None, http_post=post)
    assert ident.display_name == "Fancy Name"
    assert load_identity(ACCOUNT)["display_name"] == "Fancy Name"


def test_claimed_agent_recovers_via_owner_email(_identity_file):
    """Once a human claims the agent the verification-code path 403s forever;
    with OPENBOTCITY_OWNER_EMAIL set the plugin falls back to {slug, email}."""
    save_identity(ACCOUNT, {
        "agent_key": "stable-key-0123456789", "display_name": "me", "slug": "me",
        "verification_code": "OBC-1234-5678",
    })
    post = FakePost([
        (500, None),                                  # re-register: transient failure
        (403, {"error": "Invalid credentials"}),      # code reconnect: bot is claimed
        (200, {"jwt": "fresh", "bot_id": "same-bot", "slug": "me"}),  # email reconnect
    ])
    ident = ensure_identity(
        API, ACCOUNT, None, None, None,
        owner_email="human@example.com", http_post=post,
    )
    assert ident.ok
    assert ident.source == "reconnect"
    url1, body1, _ = post.calls[1]
    url2, body2, _ = post.calls[2]
    assert url1 == url2 == f"{API}/agents/reconnect"
    assert body1 == {"slug": "me", "verification_code": "OBC-1234-5678"}
    assert body2 == {"slug": "me", "email": "human@example.com"}
    assert load_identity(ACCOUNT)["jwt"] == "fresh"


def test_recovery_failure_hints_at_owner_email_when_unset(_identity_file):
    save_identity(ACCOUNT, {
        "agent_key": "stable-key-0123456789", "display_name": "me", "slug": "me",
        "verification_code": "OBC-1234-5678",
    })
    post = FakePost([(500, None), (403, None)])
    ident = ensure_identity(API, ACCOUNT, None, None, None, http_post=post)
    assert not ident.ok
    assert "OPENBOTCITY_OWNER_EMAIL" in ident.error
