"""Tests for the gateway kanban-sync watcher mixin."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from gateway import kanban_sync_watcher as watcher_mod
from gateway.kanban_sync_watcher import GatewayKanbanSyncWatcherMixin
from hermes_cli.kanban_sync import registry as sync_registry
from hermes_cli.kanban_sync.provider import SyncAuthError

from tests.hermes_cli.kanban_sync_fakes import FakeKanbanProvider


class _Runner(GatewayKanbanSyncWatcherMixin):
    def __init__(self):
        self._running = True


@pytest.fixture(autouse=True)
def _fast_watcher(monkeypatch, tmp_path):
    monkeypatch.setattr(watcher_mod, "INITIAL_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(watcher_mod, "jittered_backoff", lambda *a, **k: 0.0)
    # Keep the singleton lock inside the test sandbox.
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    yield


def _patch_config(monkeypatch, cfg):
    import hermes_cli.config as config_mod
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)


def _enabled_cfg(**sync_overrides):
    sync = {
        "enabled": True,
        "provider": "fake",
        "interval_seconds": 1,
        "pairings": [{"board": "", "remote_board": "b1"}],
    }
    sync.update(sync_overrides)
    return {"kanban": {"sync": sync, "default_assignee": "fallback-prof"}}


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def test_mixin_defines_sync_watcher_coroutine():
    assert inspect.iscoroutinefunction(
        GatewayKanbanSyncWatcherMixin._kanban_sync_watcher
    )


def test_gateway_runner_inherits_mixin():
    from gateway.run import GatewayRunner

    assert issubclass(GatewayRunner, GatewayKanbanSyncWatcherMixin)
    owner = next(
        c for c in GatewayRunner.__mro__
        if "_kanban_sync_watcher" in c.__dict__
    )
    assert owner is GatewayKanbanSyncWatcherMixin


# ---------------------------------------------------------------------------
# Boot gates
# ---------------------------------------------------------------------------

def test_disabled_by_default_is_noop(monkeypatch):
    _patch_config(monkeypatch, {"kanban": {"sync": {"enabled": False}}})
    runner = _Runner()
    asyncio.run(asyncio.wait_for(runner._kanban_sync_watcher(), timeout=5))


def test_env_escape_hatch_disables(monkeypatch):
    _patch_config(monkeypatch, _enabled_cfg())
    monkeypatch.setenv("HERMES_KANBAN_SYNC", "0")
    runner = _Runner()
    asyncio.run(asyncio.wait_for(runner._kanban_sync_watcher(), timeout=5))


def test_unknown_provider_is_noop(monkeypatch):
    sync_registry._reset_for_tests()
    _patch_config(monkeypatch, _enabled_cfg(provider="no-such-provider"))
    runner = _Runner()
    asyncio.run(asyncio.wait_for(runner._kanban_sync_watcher(), timeout=5))


def test_no_pairings_is_noop(monkeypatch):
    sync_registry._reset_for_tests()
    sync_registry.register_provider("fake", FakeKanbanProvider)
    _patch_config(monkeypatch, _enabled_cfg(pairings=[]))
    runner = _Runner()
    asyncio.run(asyncio.wait_for(runner._kanban_sync_watcher(), timeout=5))


# ---------------------------------------------------------------------------
# Engine construction
# ---------------------------------------------------------------------------

def test_build_engines_from_config(monkeypatch):
    sync_registry._reset_for_tests()
    sync_registry.register_provider("fake", FakeKanbanProvider)
    cfg = _enabled_cfg(
        pairings=[
            {"board": "", "remote_board": "b1"},
            {"board": "side", "remote_board": "b2"},
            {"remote_board": ""},  # invalid: skipped
        ],
    )
    runner = _Runner()
    engines = runner._build_kanban_sync_engines(cfg, cfg["kanban"]["sync"])
    assert len(engines) == 2
    assert engines[0].remote_board_ref == "b1"
    assert engines[0].board is None
    assert engines[1].board == "side"
    assert engines[0].fallback_assignee == "fallback-prof"


# ---------------------------------------------------------------------------
# Loop behaviour
# ---------------------------------------------------------------------------

class _ScriptedEngine:
    """sync_once runs a scripted step each call; the last step stops the loop."""

    def __init__(self, runner, steps):
        self.runner = runner
        self.steps = list(steps)
        self.calls = 0
        self.board = None
        self.remote_board_ref = "b1"

    def sync_once(self):
        self.calls += 1
        step = self.steps.pop(0)
        if not self.steps:
            self.runner._running = False
        if isinstance(step, Exception):
            raise step
        return step


def _run_with_engine(monkeypatch, runner, engine):
    _patch_config(monkeypatch, _enabled_cfg())
    monkeypatch.setattr(
        runner, "_build_kanban_sync_engines", lambda cfg, sync_cfg: [engine],
    )
    asyncio.run(asyncio.wait_for(runner._kanban_sync_watcher(), timeout=10))


def test_watcher_runs_engine_until_stopped(monkeypatch):
    from hermes_cli.kanban_sync.engine import SyncStats

    runner = _Runner()
    engine = _ScriptedEngine(runner, [SyncStats(), SyncStats()])
    _run_with_engine(monkeypatch, runner, engine)
    assert engine.calls == 2


def test_watcher_survives_engine_errors(monkeypatch):
    from hermes_cli.kanban_sync.engine import SyncStats

    runner = _Runner()
    engine = _ScriptedEngine(
        runner,
        [RuntimeError("boom"), SyncAuthError("bad token"), SyncStats()],
    )
    _run_with_engine(monkeypatch, runner, engine)
    assert engine.calls == 3  # errors did not kill the loop


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

def test_kanban_sync_config_defaults(monkeypatch, tmp_path):
    home = tmp_path / "fresh-hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_cli.config as config_mod
    cfg = config_mod.load_config()
    sync = cfg["kanban"]["sync"]
    assert sync["enabled"] is False
    assert sync["provider"] == "fizzy"
    assert sync["pairings"] == []
    assert sync["column_map"]["running"] == "In Progress"
    assert sync["intake"]["mode"] == "all"
    assert sync["export"]["enabled"] is True
    assert sync["fizzy"]["token_env"] == "HERMES_FIZZY_TOKEN"
