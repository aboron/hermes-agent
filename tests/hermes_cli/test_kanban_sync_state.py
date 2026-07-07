"""Tests for kanban-sync state persistence (pairings, links, cursors)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_sync import state


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    conn = kb.connect()
    state.ensure_schema(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_ensure_schema_creates_tables_and_is_idempotent(kanban_home):
    conn = kb.connect()
    try:
        state.ensure_schema(conn)
        state.ensure_schema(conn)  # second call must not raise
        names = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "kanban_sync_pairings",
            "kanban_sync_links",
            "kanban_sync_pushed_comments",
        } <= names
    finally:
        conn.close()


def test_ensure_schema_survives_existing_data(conn):
    p = state.get_or_create_pairing(conn, provider="fizzy", remote_board_ref="b1")
    state.ensure_schema(conn)
    again = state.get_or_create_pairing(conn, provider="fizzy", remote_board_ref="b1")
    assert again["id"] == p["id"]


# ---------------------------------------------------------------------------
# Pairings
# ---------------------------------------------------------------------------

def test_get_or_create_pairing_is_stable_per_board(conn):
    a = state.get_or_create_pairing(conn, provider="fizzy", remote_board_ref="b1")
    b = state.get_or_create_pairing(conn, provider="fizzy", remote_board_ref="b1")
    c = state.get_or_create_pairing(conn, provider="fizzy", remote_board_ref="b2")
    assert a["id"] == b["id"]
    assert a["id"] != c["id"]
    assert a["provider"] == "fizzy"
    assert a["remote_cursor"] is None
    assert a["enabled"] == 1


def test_update_pairing_roundtrips_cursor_and_column_ids(conn):
    p = state.get_or_create_pairing(conn, provider="fizzy", remote_board_ref="b1")
    state.update_pairing(
        conn, p["id"],
        remote_cursor="2026-07-06T12:00:00Z",
        column_ids={"Todo": "c1", "Ready": "c2"},
        last_synced_at=1234,
        last_error=None,
    )
    got = state.get_or_create_pairing(conn, provider="fizzy", remote_board_ref="b1")
    assert got["remote_cursor"] == "2026-07-06T12:00:00Z"
    assert got["column_ids"] == {"Todo": "c1", "Ready": "c2"}
    assert got["last_synced_at"] == 1234


def test_update_pairing_rejects_unknown_field(conn):
    p = state.get_or_create_pairing(conn, provider="fizzy", remote_board_ref="b1")
    with pytest.raises(ValueError):
        state.update_pairing(conn, p["id"], nonsense=1)


def test_list_pairings(conn):
    state.get_or_create_pairing(conn, provider="fizzy", remote_board_ref="b1")
    state.get_or_create_pairing(conn, provider="fizzy", remote_board_ref="b2")
    assert len(state.list_pairings(conn)) == 2


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def _pairing(conn):
    return state.get_or_create_pairing(
        conn, provider="fizzy", remote_board_ref="b1",
    )


def test_upsert_and_get_link(conn):
    p = _pairing(conn)
    state.upsert_link(
        conn, p["id"], task_id="t_1", remote_card_ref="42", origin="remote",
        remote_fingerprint="rf", local_fingerprint="lf",
    )
    by_task = state.get_link_by_task(conn, p["id"], "t_1")
    by_card = state.get_link_by_card(conn, p["id"], "42")
    assert by_task == by_card
    assert by_task["remote_fingerprint"] == "rf"
    assert by_task["origin"] == "remote"
    assert by_task["deleted"] == 0
    assert by_task["last_local_event_id"] == 0
    assert state.get_link_by_task(conn, p["id"], "t_missing") is None


def test_upsert_link_updates_in_place(conn):
    p = _pairing(conn)
    state.upsert_link(
        conn, p["id"], task_id="t_1", remote_card_ref="42", origin="remote",
    )
    state.upsert_link(
        conn, p["id"], task_id="t_1", remote_card_ref="42", origin="remote",
        remote_fingerprint="new-rf", last_local_event_id=7,
    )
    link = state.get_link_by_task(conn, p["id"], "t_1")
    assert link["remote_fingerprint"] == "new-rf"
    assert link["last_local_event_id"] == 7
    assert len(state.list_links(conn, p["id"])) == 1


def test_update_link_changes_fields(conn):
    p = _pairing(conn)
    state.upsert_link(
        conn, p["id"], task_id="t_1", remote_card_ref="42", origin="local",
    )
    state.update_link(
        conn, p["id"], "t_1",
        local_fingerprint="lf2", last_local_comment_id=9, deleted=1,
    )
    link = state.get_link_by_task(conn, p["id"], "t_1")
    assert link["local_fingerprint"] == "lf2"
    assert link["last_local_comment_id"] == 9
    assert link["deleted"] == 1


def test_list_links_excludes_deleted_by_default(conn):
    p = _pairing(conn)
    state.upsert_link(conn, p["id"], task_id="t_1", remote_card_ref="1", origin="remote")
    state.upsert_link(conn, p["id"], task_id="t_2", remote_card_ref="2", origin="remote")
    state.update_link(conn, p["id"], "t_2", deleted=1)
    assert [l["task_id"] for l in state.list_links(conn, p["id"])] == ["t_1"]
    assert len(state.list_links(conn, p["id"], include_deleted=True)) == 2


# ---------------------------------------------------------------------------
# Pushed comments
# ---------------------------------------------------------------------------

def test_pushed_comment_roundtrip(conn):
    p = _pairing(conn)
    assert state.is_pushed_comment(conn, p["id"], "m1") is False
    state.record_pushed_comment(
        conn, p["id"], remote_comment_ref="m1", task_id="t_1", local_comment_id=3,
    )
    assert state.is_pushed_comment(conn, p["id"], "m1") is True
    # Duplicate record must not raise.
    state.record_pushed_comment(
        conn, p["id"], remote_comment_ref="m1", task_id="t_1", local_comment_id=3,
    )


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

def test_fingerprint_stable_and_discriminating():
    a = state.fingerprint("title", "body", None, False)
    assert a == state.fingerprint("title", "body", None, False)
    assert a != state.fingerprint("title", "body", "c1", False)
    assert a != state.fingerprint("title", "body", None, True)
    assert isinstance(a, str) and len(a) == 64


def test_fingerprint_distinguishes_none_from_empty_string():
    assert state.fingerprint(None) != state.fingerprint("")
