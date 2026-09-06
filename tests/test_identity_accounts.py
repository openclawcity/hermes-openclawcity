"""Multi-account identity isolation — the contract hermes.md §1b documents.

A second city agent lives under its own OPENBOTCITY_ACCOUNT_ID key in the
shared identity file. These tests pin the two §1b promises: a new account's
save never touches the first agent's entry, and per-account loads return only
their own entry. token_cache has the analogous test; identity.py did not.
"""

import json

import occ_core.identity as identity


def _use_tmp_identity_file(tmp_path, monkeypatch):
    path = tmp_path / "openclawcity-identity.json"
    monkeypatch.setenv("OPENBOTCITY_IDENTITY_FILE", str(path))
    return path


def test_second_account_save_leaves_first_untouched(tmp_path, monkeypatch):
    path = _use_tmp_identity_file(tmp_path, monkeypatch)
    identity.save_identity(
        "default",
        {"bot_id": "bot-1", "jwt": "jwt-1", "verification_code": "OBC-AAAA-AAAA"},
    )
    identity.save_identity(
        "my-second-agent",
        {"bot_id": "bot-2", "jwt": "jwt-2", "verification_code": "OBC-BBBB-BBBB"},
    )

    store = json.loads(path.read_text())
    assert store["default"]["bot_id"] == "bot-1"
    assert store["default"]["verification_code"] == "OBC-AAAA-AAAA"
    assert store["my-second-agent"]["bot_id"] == "bot-2"


def test_load_identity_is_keyed_per_account(tmp_path, monkeypatch):
    _use_tmp_identity_file(tmp_path, monkeypatch)
    identity.save_identity("default", {"bot_id": "bot-1", "jwt": "jwt-1"})
    identity.save_identity("my-second-agent", {"bot_id": "bot-2", "jwt": "jwt-2"})

    assert identity.load_identity("default")["bot_id"] == "bot-1"
    assert identity.load_identity("my-second-agent")["bot_id"] == "bot-2"
    assert identity.load_identity("nonexistent") is None


def test_save_merges_within_account_only(tmp_path, monkeypatch):
    path = _use_tmp_identity_file(tmp_path, monkeypatch)
    identity.save_identity("default", {"bot_id": "bot-1", "jwt": "jwt-1"})
    identity.save_identity("default", {"jwt": "jwt-1-refreshed"})

    store = json.loads(path.read_text())
    assert store["default"]["bot_id"] == "bot-1"
    assert store["default"]["jwt"] == "jwt-1-refreshed"
    assert list(store.keys()) == ["default"]
