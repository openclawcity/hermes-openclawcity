"""Pin the adapter against the Hermes host-call contract.

Hermes v0.18.1+ (gateway/run.py `_connect_adapter_with_timeout`) calls
``adapter.connect(is_reconnect=...)`` unconditionally, with no fallback for
adapters that reject the keyword. An override defined as ``connect(self)``
raises TypeError there and the platform can never come up — exactly the bug
this suite guards against. The Hermes modules are faked in sys.modules so the
real adapter class definition (gated on HERMES_AVAILABLE) is exercised
without a hermes-agent install.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


class _FakeBasePlatformAdapter:
    def __init__(self, config, platform):
        self.config = config
        self.platform = platform
        self.connected = False

    def _mark_connected(self):
        self.connected = True

    def _mark_disconnected(self):
        self.connected = False

    def build_source(self, **kwargs):
        return kwargs


class _FakeSendResult:
    def __init__(self, success, message_id=None, error=None):
        self.success = success
        self.message_id = message_id
        self.error = error


class _FakeMessageEvent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeMessageType:
    TEXT = "text"


class _FakePlatform(str):
    """Hermes Platform enum stand-in: accepts the channel-type string."""


class _FakePlatformConfig:
    extra: dict = {}


@pytest.fixture()
def adapter_module(monkeypatch, tmp_path):
    """Import adapter.py with faked Hermes host modules on sys.modules."""
    gateway = types.ModuleType("gateway")
    gateway_config = types.ModuleType("gateway.config")
    gateway_config.Platform = _FakePlatform
    gateway_config.PlatformConfig = _FakePlatformConfig
    gateway_platforms = types.ModuleType("gateway.platforms")
    gateway_base = types.ModuleType("gateway.platforms.base")
    gateway_base.BasePlatformAdapter = _FakeBasePlatformAdapter
    gateway_base.MessageEvent = _FakeMessageEvent
    gateway_base.MessageType = _FakeMessageType
    gateway_base.SendResult = _FakeSendResult

    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.config", gateway_config)
    monkeypatch.setitem(sys.modules, "gateway.platforms", gateway_platforms)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", gateway_base)

    # Isolate the identity/token caches and force env-supplied credentials so
    # connect() never performs a bootstrap HTTP round-trip.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENBOTCITY_JWT", "test-jwt")
    monkeypatch.setenv("OPENBOTCITY_BOT_ID", "bot-123")
    for name in ("OPENBOTCITY_DISPLAY_NAME", "OPENBOTCITY_GATEWAY_URL", "OPENBOTCITY_API_URL"):
        monkeypatch.delenv(name, raising=False)

    spec = importlib.util.spec_from_file_location(
        "occ_adapter_under_test", PLUGIN_DIR / "adapter.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.HERMES_AVAILABLE, "fake Hermes modules were not picked up"
    return module


class _StubClient:
    """Stands in for OpenClawCityClient: never dials the network."""

    def __init__(self, cfg, **callbacks):
        self.cfg = cfg
        self.started = False

    def start(self):
        self.started = True

    async def stop(self):
        self.started = False


def _make_adapter(adapter_module, monkeypatch):
    monkeypatch.setattr(adapter_module, "OpenClawCityClient", _StubClient)
    return adapter_module.OpenClawCityPlatformAdapter(_FakePlatformConfig())


def test_connect_accepts_is_reconnect_keyword(adapter_module, monkeypatch):
    adapter = _make_adapter(adapter_module, monkeypatch)
    assert asyncio.run(adapter.connect(is_reconnect=True)) is True
    assert adapter.connected


def test_connect_still_works_without_keyword(adapter_module, monkeypatch):
    adapter = _make_adapter(adapter_module, monkeypatch)
    assert asyncio.run(adapter.connect()) is True


def test_connect_tolerates_unknown_future_kwargs(adapter_module, monkeypatch):
    adapter = _make_adapter(adapter_module, monkeypatch)
    assert asyncio.run(adapter.connect(is_reconnect=False, some_future_flag=1)) is True
