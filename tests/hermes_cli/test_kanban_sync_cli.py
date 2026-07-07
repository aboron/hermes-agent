"""Tests for `hermes kanban sync init|once|status`."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban import build_parser, kanban_command
from hermes_cli.kanban_sync import registry as sync_registry
from hermes_cli.kanban_sync import state

from tests.hermes_cli.kanban_sync_fakes import FakeKanbanProvider


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
    import hermes_cli.config as config_mod
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    yield provider
    sync_registry._reset_for_tests()


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
