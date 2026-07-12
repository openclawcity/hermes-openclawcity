"""Token cache behavior: refresh chain keyed by the CONFIG token."""
import json

import pytest

from occ_core.token_cache import load_refreshed_token, save_refreshed_token


@pytest.fixture(autouse=True)
def cache_path(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    monkeypatch.setenv("OPENBOTCITY_TOKEN_CACHE", str(path))
    return path


def test_round_trip(cache_path):
    save_refreshed_token("default", "config-jwt", "fresh-jwt")
    assert load_refreshed_token("default", "config-jwt") == "fresh-jwt"
    assert cache_path.exists()


def test_rekeyed_config_ignores_cache():
    save_refreshed_token("default", "config-jwt", "fresh-jwt")
    # Owner deliberately rotated the config token — the cache must lose.
    assert load_refreshed_token("default", "NEW-config-jwt") is None


def test_missing_entry_returns_none():
    assert load_refreshed_token("default", "config-jwt") is None


def test_accounts_are_independent():
    save_refreshed_token("a", "jwt-a", "fresh-a")
    save_refreshed_token("b", "jwt-b", "fresh-b")
    assert load_refreshed_token("a", "jwt-a") == "fresh-a"
    assert load_refreshed_token("b", "jwt-b") == "fresh-b"
    assert load_refreshed_token("a", "jwt-b") is None


def test_corrupt_cache_is_tolerated(cache_path):
    cache_path.write_text("{not json")
    assert load_refreshed_token("default", "config-jwt") is None
    save_refreshed_token("default", "config-jwt", "fresh-jwt")  # overwrites
    assert load_refreshed_token("default", "config-jwt") == "fresh-jwt"


def test_file_is_owner_only(cache_path):
    save_refreshed_token("default", "config-jwt", "fresh-jwt")
    assert (cache_path.stat().st_mode & 0o777) == 0o600


def test_source_key_is_hashed_not_stored(cache_path):
    save_refreshed_token("default", "config-jwt-secret", "fresh-jwt")
    raw = json.loads(cache_path.read_text())
    assert "config-jwt-secret" not in json.dumps(raw)
