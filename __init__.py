"""OpenClawCity platform plugin for Hermes Agent.

Entry point shim: Hermes discovers `register(ctx)` here (or directly in
adapter.py, depending on loader). Both paths resolve to the same idempotent
register() in adapter.py.
"""
from __future__ import annotations

try:  # loaded as a package
    from .adapter import register  # noqa: F401
except ImportError:  # loaded as a bare file — import adapter.py by path
    import importlib.util
    import sys
    from pathlib import Path

    _path = Path(__file__).resolve().parent / "adapter.py"
    _spec = importlib.util.spec_from_file_location("openclawcity_hermes_adapter", _path)
    assert _spec is not None and _spec.loader is not None
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules.setdefault("openclawcity_hermes_adapter", _mod)
    _spec.loader.exec_module(_mod)
    register = _mod.register  # noqa: F401

__all__ = ["register"]
