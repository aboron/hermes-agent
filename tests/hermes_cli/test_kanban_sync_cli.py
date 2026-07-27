"""Tests for `hermes kanban sync init|once|status`."""

from __future__ import annotations

import argparse
import copy
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import build_parser, kanban_command
from hermes_cli.kanban_sync import state

from tests.hermes_cli.kanban_sync_fakes import FakeKanbanProvider


def _live_registry():
    """Resolve the registry through sys.modules at call time.

    Some suite-mates (test_kanban_default_assignee.py) purge hermes_cli*
    from sys.modules, so a module-level import captured at collection can
    be a stale copy — while kanban_command's lazy import of
    hermes_cli.kanban_sync.cli resolves the fresh one. Registering the
    fake on the stale registry makes the CLI see only the builtin
    provider ("unknown provider 'fake'"). Always register on the live
    module instead.
    """
    return importlib.import_module("hermes_cli.kanban_sync.registry")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def fake_provider(kanban_home, monkeypatch):
    sync_registry = _live_registry()
    provider = FakeKanbanProvider()
    sync_registry._reset_for_tests()
    sync_registry.register_provider("fake", lambda cfg: provider)

    cfg = {
        "kanban": {
            "default_assignee": "",
            "sync": {
                "enabled": True,
                "provider": "fake",
                "pairings": [{"board": "", "remote_board": "b1"}],
                "intake": {"mode": "all", "columns": []},
                "export": {"enabled": True, "backfill": False},
                "default_assignee": "worker-bee",
            },
        },
    }
    config_mod = importlib.import_module("hermes_cli.config")
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    yield provider
    _live_registry()._reset_for_tests()


def _args(**kw):
    base = dict(kanban_action="sync", board=None, sync_action=None,
                remote_board=None, full=False)
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Parser wiring
# ---------------------------------------------------------------------------

def test_parser_accepts_sync_subcommands():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    build_parser(sub)
    args = parser.parse_args(
        ["kanban", "sync", "init", "--remote-board", "b1"],
    )
    assert args.kanban_action == "sync"
    assert args.sync_action == "init"
    assert args.remote_board == "b1"
    args = parser.parse_args(["kanban", "sync", "once", "--full"])
    assert args.sync_action == "once" and args.full is True
    args = parser.parse_args(["kanban", "sync", "status"])
    assert args.sync_action == "status"


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def test_sync_init_creates_columns_and_pairing(fake_provider, capsys):
    rc = kanban_command(_args(sync_action="init", remote_board="b1"))
    assert rc == 0
    assert {"Todo", "Ready", "In Progress", "Review", "Blocked"} <= set(
        fake_provider.columns,
    )
    with kb.connect() as conn:
        state.ensure_schema(conn)
        pairings = state.list_pairings(conn)
    assert len(pairings) == 1
    assert pairings[0]["remote_board_ref"] == "b1"
    out = capsys.readouterr().out
    assert "Todo" in out


def test_sync_init_requires_remote_board(fake_provider, capsys):
    rc = kanban_command(_args(sync_action="init", remote_board=None))
    assert rc != 0


def test_sync_init_unknown_provider_fails_loudly(fake_provider, monkeypatch, capsys):
    import hermes_cli.config as config_mod
    cfg = config_mod.load_config()
    cfg["kanban"]["sync"]["provider"] = "no-such"
    rc = kanban_command(_args(sync_action="init", remote_board="b1"))
    assert rc != 0
    err = capsys.readouterr().err
    assert "no-such" in err


# ---------------------------------------------------------------------------
# init — mirror mode bootstrap
# ---------------------------------------------------------------------------

MIRROR_COLUMNS = {"Triage", "Todo", "Scheduled", "Ready", "Running",
                  "Blocked", "Review", "Archived"}


@pytest.fixture
def mirror_setup(kanban_home, monkeypatch):
    """Like fake_provider, but mode=mirror, no pairings yet, and
    save_config/is_managed captured so tests can assert the write-back."""
    sync_registry = _live_registry()
    provider = FakeKanbanProvider()
    sync_registry._reset_for_tests()
    sync_registry.register_provider("fake", lambda cfg: provider)

    cfg = {
        "kanban": {
            "default_assignee": "",
            "sync": {
                "enabled": False,
                "provider": "fake",
                "mode": "mirror",
                "pairings": [],
                "intake": {"mode": "all", "columns": []},
                "export": {"enabled": True, "backfill": False},
                "default_assignee": "worker-bee",
            },
        },
    }
    config_mod = importlib.import_module("hermes_cli.config")
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    saved: "list[dict]" = []
    monkeypatch.setattr(
        config_mod, "save_config",
        lambda c, **kw: saved.append(copy.deepcopy(c)),
    )
    monkeypatch.setattr(config_mod, "is_managed", lambda: False)
    yield SimpleNamespace(provider=provider, cfg=cfg, saved=saved)
    _live_registry()._reset_for_tests()


def test_mirror_init_creates_board_columns_and_writes_config(mirror_setup, capsys):
    rc = kanban_command(_args(sync_action="init"))
    assert rc == 0
    p = mirror_setup.provider
    assert p.writes.count(("create_board", "Hermes_Default")) == 1
    board_ref = next(iter(p.boards))
    assert p.boards[board_ref] == "Hermes_Default"
    assert set(p.columns) == MIRROR_COLUMNS
    with kb.connect() as conn:
        state.ensure_schema(conn)
        pairings = state.list_pairings(conn)
    assert [pr["remote_board_ref"] for pr in pairings] == [board_ref]
    assert len(mirror_setup.saved) == 1
    saved_sync = mirror_setup.saved[0]["kanban"]["sync"]
    assert saved_sync["enabled"] is True
    assert saved_sync["pairings"] == [{"board": "", "remote_board": board_ref}]
    out = capsys.readouterr().out
    assert "Hermes_Default" in out


def test_mirror_init_rerun_reuses_board_and_pairing(mirror_setup, capsys):
    assert kanban_command(_args(sync_action="init")) == 0
    assert kanban_command(_args(sync_action="init")) == 0
    p = mirror_setup.provider
    assert p.writes.count(("create_board", "Hermes_Default")) == 1
    assert len(p.boards) == 1
    assert len(mirror_setup.cfg["kanban"]["sync"]["pairings"]) == 1
    with kb.connect() as conn:
        state.ensure_schema(conn)
        assert len(state.list_pairings(conn)) == 1


def test_mirror_init_respects_board_prefix(mirror_setup, capsys):
    mirror_setup.cfg["kanban"]["sync"]["board_prefix"] = "Work-"
    assert kanban_command(_args(sync_action="init")) == 0
    assert ("create_board", "Work-Default") in mirror_setup.provider.writes


def test_mirror_init_empty_prefix_means_no_prefix(mirror_setup, capsys):
    mirror_setup.cfg["kanban"]["sync"]["board_prefix"] = ""
    assert kanban_command(_args(sync_action="init")) == 0
    assert ("create_board", "Default") in mirror_setup.provider.writes


def test_mirror_init_with_remote_board_keeps_legacy_flow(mirror_setup, capsys):
    rc = kanban_command(_args(sync_action="init", remote_board="b7"))
    assert rc == 0
    p = mirror_setup.provider
    assert not any(w[0] == "create_board" for w in p.writes)
    # No auto write-back on the explicit-pairing path: snippet UX as today.
    assert mirror_setup.saved == []
    assert "Add this pairing" in capsys.readouterr().out
    # The engine still provisions the mirror column set on that board.
    assert set(p.columns) == MIRROR_COLUMNS


def test_mapped_mode_init_without_remote_board_still_exits_2(mirror_setup, capsys):
    mirror_setup.cfg["kanban"]["sync"]["mode"] = "mapped"
    rc = kanban_command(_args(sync_action="init"))
    assert rc == 2
    assert "--remote-board" in capsys.readouterr().err


def test_mirror_init_refuses_board_paired_to_other_local_board(mirror_setup, capsys):
    p = mirror_setup.provider
    p.boards["b9"] = "Hermes_Default"
    mirror_setup.cfg["kanban"]["sync"]["pairings"] = [
        {"board": "other-board", "remote_board": "b9"},
    ]
    rc = kanban_command(_args(sync_action="init"))
    assert rc != 0
    assert not any(w[0] == "create_board" for w in p.writes)
    assert mirror_setup.saved == []
    assert "other-board" in capsys.readouterr().err


def test_mirror_init_managed_config_falls_back_to_snippet(mirror_setup, monkeypatch, capsys):
    config_mod = importlib.import_module("hermes_cli.config")
    monkeypatch.setattr(config_mod, "is_managed", lambda: True)
    rc = kanban_command(_args(sync_action="init"))
    assert rc == 0
    assert mirror_setup.saved == []
    board_ref = next(iter(mirror_setup.provider.boards))
    out = capsys.readouterr().out
    assert "Add this pairing" in out
    assert board_ref in out


# ---------------------------------------------------------------------------
# once
# ---------------------------------------------------------------------------

def test_sync_once_imports_cards(fake_provider, capsys):
    assert kanban_command(_args(sync_action="init", remote_board="b1")) == 0
    fake_provider.human_add_card(title="from the board")
    rc = kanban_command(_args(sync_action="once"))
    assert rc == 0
    with kb.connect() as conn:
        tasks = kb.list_tasks(conn, include_archived=True)
    assert [t.title for t in tasks] == ["from the board"]
    out = capsys.readouterr().out
    assert "created_local=1" in out


def test_sync_once_without_pairings_errors(fake_provider, monkeypatch, capsys):
    import hermes_cli.config as config_mod
    cfg = config_mod.load_config()
    cfg["kanban"]["sync"]["pairings"] = []
    rc = kanban_command(_args(sync_action="once"))
    assert rc != 0
    assert "pairing" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_sync_status_lists_pairings(fake_provider, capsys):
    assert kanban_command(_args(sync_action="init", remote_board="b1")) == 0
    fake_provider.human_add_card(title="tracked")
    assert kanban_command(_args(sync_action="once")) == 0
    capsys.readouterr()
    rc = kanban_command(_args(sync_action="status"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "fake" in out and "b1" in out
    assert "links=1" in out


def test_sync_status_no_pairings(fake_provider, monkeypatch, capsys):
    rc = kanban_command(_args(sync_action="status"))
    assert rc == 0
    assert "no sync pairings" in capsys.readouterr().out.lower()
