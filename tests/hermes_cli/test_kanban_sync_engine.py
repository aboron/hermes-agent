"""Tests for the bidirectional kanban-sync engine.

Every mutation scenario ends with an echo-suppression check: running
``sync_once`` again must produce zero provider writes and zero local
changes — the fingerprint bookkeeping is what makes the bridge safe to
poll forever.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_sync import state
from hermes_cli.kanban_sync.engine import KanbanSyncEngine, SyncStats

from tests.hermes_cli.kanban_sync_fakes import FakeKanbanProvider

BOARD_REF = "b1"

BASE_CFG = {
    "provider": "fake",
    "column_map": {
        "todo": "Todo", "ready": "Ready", "running": "In Progress",
        "review": "Review", "blocked": "Blocked", "scheduled": "Blocked",
    },
    "intake": {"mode": "all", "columns": []},
    "export": {"enabled": True, "backfill": False},
    "default_assignee": "worker-bee",
    "golden_priority": 2,
    "full_resync_every": 0,
}


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def provider(kanban_home):
    return FakeKanbanProvider()


def make_engine(provider, **overrides):
    cfg = copy.deepcopy(BASE_CFG)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return KanbanSyncEngine(
        provider=provider,
        board=None,
        remote_board_ref=BOARD_REF,
        sync_cfg=cfg,
    )


def assert_quiescent(engine, provider):
    """A follow-up sync must be a no-op on both sides."""
    writes_before = list(provider.writes)
    with kb.connect() as conn:
        tasks_before = [
            (t.id, t.title, t.body, t.status, t.priority)
            for t in kb.list_tasks(conn, include_archived=True)
        ]
        comments_before = {
            t[0]: len(kb.list_comments(conn, t[0])) for t in tasks_before
        }
    stats = engine.sync_once()
    assert provider.writes == writes_before, "provider writes on second sync"
    with kb.connect() as conn:
        tasks_after = [
            (t.id, t.title, t.body, t.status, t.priority)
            for t in kb.list_tasks(conn, include_archived=True)
        ]
        comments_after = {
            t[0]: len(kb.list_comments(conn, t[0])) for t in tasks_after
        }
    assert tasks_after == tasks_before, "local task changes on second sync"
    assert comments_after == comments_before
    assert stats.created_local == 0 and stats.updated_local == 0
    assert stats.created_remote == 0 and stats.updated_remote == 0
    assert stats.comments_in == 0 and stats.comments_out == 0
    return stats


def _single_task(conn):
    tasks = kb.list_tasks(conn, include_archived=True)
    assert len(tasks) == 1, [t.id for t in tasks]
    return tasks[0]


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------

def test_first_sync_creates_missing_columns(provider):
    engine = make_engine(provider)
    engine.sync_once()
    assert {"Todo", "Ready", "In Progress", "Review", "Blocked"} <= set(
        provider.columns
    )
    # Column set is cached on the pairing; second sync creates nothing.
    assert_quiescent(engine, provider)


def test_existing_columns_are_reused(provider):
    provider.columns = {"Todo": "c1", "Ready": "c2", "In Progress": "c3",
                        "Review": "c4", "Blocked": "c5"}
    engine = make_engine(provider)
    engine.sync_once()
    assert provider.writes == []


# ---------------------------------------------------------------------------
# Import (remote -> local)
# ---------------------------------------------------------------------------

def test_inbox_card_imports_as_triage_task(provider):
    engine = make_engine(provider)
    engine.sync_once()  # topology
    ref = provider.human_add_card(
        title="From a human", body_text="please do this", creator="doc",
    )
    stats = engine.sync_once()
    assert stats.created_local == 1
    with kb.connect() as conn:
        task = _single_task(conn)
        assert task.title == "From a human"
        assert task.status == "triage"
        assert task.assignee == "worker-bee"
        assert task.created_by == "fizzy-sync"
        assert "please do this" in task.body
        assert f"fake://cards/{ref}" in task.body
    assert_quiescent(engine, provider)


def test_column_card_imports_with_mapped_status(provider):
    engine = make_engine(provider)
    engine.sync_once()
    provider.human_add_card(title="WIP", column_name="In Progress")
    engine.sync_once()
    with kb.connect() as conn:
        assert _single_task(conn).status == "running"
    assert_quiescent(engine, provider)


def test_draft_closed_and_archived_cards_are_not_imported(provider):
    engine = make_engine(provider)
    engine.sync_once()
    provider.human_add_card(title="draft", draft=True)
    provider.human_add_card(title="old done", closed=True)
    provider.human_add_card(title="parked", archived=True)
    engine.sync_once()
    with kb.connect() as conn:
        assert kb.list_tasks(conn, include_archived=True) == []
    assert_quiescent(engine, provider)


def test_intake_columns_mode_limits_import(provider):
    engine = make_engine(provider, intake={"mode": "columns", "columns": ["Todo"]})
    engine.sync_once()
    provider.human_add_card(title="wanted", column_name="Todo")
    provider.human_add_card(title="ignored", column_name="Ready")
    provider.human_add_card(title="inbox ignored")
    engine.sync_once()
    with kb.connect() as conn:
        tasks = kb.list_tasks(conn, include_archived=True)
        assert [t.title for t in tasks] == ["wanted"]
    assert_quiescent(engine, provider)


def test_assignee_tag_overrides_default(provider):
    engine = make_engine(provider)
    engine.sync_once()
    provider.human_add_card(title="tagged", tags=("assignee:specialist",))
    engine.sync_once()
    with kb.connect() as conn:
        assert _single_task(conn).assignee == "specialist"


def test_golden_card_gets_priority(provider):
    engine = make_engine(provider)
    engine.sync_once()
    provider.human_add_card(title="shiny", golden=True)
    engine.sync_once()
    with kb.connect() as conn:
        assert _single_task(conn).priority == 2


def test_existing_remote_comments_import_with_card(provider):
    engine = make_engine(provider)
    engine.sync_once()
    ref = provider.human_add_card(title="commented")
    provider.human_comment(ref, "doc", "context here")
    engine.sync_once()
    with kb.connect() as conn:
        task = _single_task(conn)
        comments = kb.list_comments(conn, task.id)
        assert [(c.author, c.body) for c in comments] == [
            ("fizzy:doc", "context here"),
        ]
    assert_quiescent(engine, provider)


# ---------------------------------------------------------------------------
# Export (local -> remote)
# ---------------------------------------------------------------------------

def test_local_task_exports_to_card_in_mapped_column(provider):
    engine = make_engine(provider)
    engine.sync_once()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="local work", body="details")
    stats = engine.sync_once()
    assert stats.created_remote == 1
    ref = next(iter(provider.cards))
    card = provider.cards[ref]
    assert card["title"] == "local work"
    # Parentless local task is 'ready' -> Ready column.
    assert provider.column_name_of(ref) == "Ready"
    with kb.connect() as conn:
        pairing = state.list_pairings(conn)[0]
        link = state.get_link_by_task(conn, pairing["id"], tid)
        assert link is not None and link["origin"] == "local"
    assert_quiescent(engine, provider)


def test_export_disabled_creates_nothing(provider):
    engine = make_engine(provider, export={"enabled": False, "backfill": False})
    engine.sync_once()
    with kb.connect() as conn:
        kb.create_task(conn, title="local only")
    engine.sync_once()
    assert all(w[0] == "create_column" for w in provider.writes)


def test_no_backfill_of_pre_pairing_tasks(provider):
    engine = make_engine(provider)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ancient")
        conn.execute(
            "UPDATE tasks SET created_at = created_at - 86400 WHERE id = ?",
            (tid,),
        )
        conn.commit()
    engine.sync_once()
    assert not any(w[0] == "create_card" for w in provider.writes)

    backfill_engine = make_engine(provider, export={"backfill": True})
    backfill_engine.sync_once()
    assert any(w[0] == "create_card" for w in provider.writes)


# ---------------------------------------------------------------------------
# Remote changes -> local status
# ---------------------------------------------------------------------------

def _import_one(engine, provider, **card_kwargs):
    engine.sync_once()
    ref = provider.human_add_card(**card_kwargs)
    engine.sync_once()
    with kb.connect() as conn:
        task = _single_task(conn)
    return ref, task.id


def test_remote_move_to_ready_promotes_local(provider):
    engine = make_engine(provider)
    ref, tid = _import_one(engine, provider, title="promote me")
    provider.human_move(ref, column_name="Ready")
    engine.sync_once()
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"
    assert_quiescent(engine, provider)


def test_remote_close_completes_running_task(provider):
    engine = make_engine(provider)
    ref, tid = _import_one(engine, provider, title="ship it",
                           column_name="Ready")
    with kb.connect() as conn:
        assert kb.claim_task(conn, tid) is not None
    # Claiming moved the task to running; resync so the link fingerprint
    # reflects that before the human closes the card.
    engine.sync_once()
    provider.human_move(ref, closed=True)
    engine.sync_once()
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        run = kb.latest_run(conn, tid)
        assert run is not None and run.outcome == "completed"
    assert_quiescent(engine, provider)


def test_remote_move_to_blocked_blocks_task(provider):
    engine = make_engine(provider)
    ref, tid = _import_one(engine, provider, title="stuck",
                           column_name="Ready")
    provider.human_move(ref, column_name="Blocked")
    engine.sync_once()
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == "needs_input"
    assert_quiescent(engine, provider)


def test_remote_title_edit_updates_local(provider):
    engine = make_engine(provider)
    ref, tid = _import_one(engine, provider, title="old title")
    provider.human_edit(ref, title="new title", body_text="new body")
    engine.sync_once()
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.title == "new title"
        assert "new body" in task.body
        assert f"fake://cards/{ref}" in task.body  # footer preserved
    assert_quiescent(engine, provider)


def test_ready_promotion_refused_pushes_local_truth_back(provider):
    engine = make_engine(provider)
    engine.sync_once()
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=(parent,))
    engine.sync_once()  # exports both
    with kb.connect() as conn:
        pairing = state.list_pairings(conn)[0]
        child_ref = state.get_link_by_task(conn, pairing["id"], child)[
            "remote_card_ref"
        ]
    # Human drags the dependency-gated child to Ready.
    provider.human_move(child_ref, column_name="Ready")
    engine.sync_once()
    with kb.connect() as conn:
        assert kb.get_task(conn, child).status == "todo"
    # Engine pushed the card back to the column matching local truth.
    assert provider.column_name_of(child_ref) == "Todo"
    assert_quiescent(engine, provider)


# ---------------------------------------------------------------------------
# Local changes -> remote
# ---------------------------------------------------------------------------

def test_local_status_change_moves_card(provider):
    engine = make_engine(provider)
    ref, tid = _import_one(engine, provider, title="triaged")
    with kb.connect() as conn:
        kb.set_status_direct(conn, tid, "todo", source="test")
    engine.sync_once()
    assert provider.column_name_of(ref) == "Todo"
    assert_quiescent(engine, provider)


def test_local_completion_closes_card(provider):
    engine = make_engine(provider)
    ref, tid = _import_one(engine, provider, title="work",
                           column_name="Ready")
    with kb.connect() as conn:
        kb.claim_task(conn, tid)
    engine.sync_once()
    with kb.connect() as conn:
        assert kb.complete_task(conn, tid, summary="did the thing")
    engine.sync_once()
    assert provider.cards[ref]["closed"] is True
    assert_quiescent(engine, provider)


def test_local_archive_parks_card(provider):
    engine = make_engine(provider)
    ref, tid = _import_one(engine, provider, title="park me")
    with kb.connect() as conn:
        kb.archive_task(conn, tid)
    engine.sync_once()
    assert provider.cards[ref]["archived"] is True
    assert_quiescent(engine, provider)


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

def test_comments_flow_both_ways_without_ping_pong(provider):
    engine = make_engine(provider)
    ref, tid = _import_one(engine, provider, title="chatty")
    with kb.connect() as conn:
        kb.add_comment(conn, tid, author="techlead", body="local note")
    provider.human_comment(ref, "doc", "remote note")
    engine.sync_once()
    # Local comment pushed with author prefix.
    pushed = [w for w in provider.writes if w[0] == "add_comment"]
    assert len(pushed) == 1
    assert pushed[0][2] == "[hermes:techlead] local note"
    # Remote comment imported with provenance author.
    with kb.connect() as conn:
        authors = [(c.author, c.body) for c in kb.list_comments(conn, tid)]
    assert ("fizzy:doc", "remote note") in authors
    # No echoes in either direction.
    assert_quiescent(engine, provider)
    remote_bodies = [c.body_text for c in provider.comments[ref]]
    assert remote_bodies.count("[hermes:techlead] local note") == 1
    with kb.connect() as conn:
        local_bodies = [c.body for c in kb.list_comments(conn, tid)]
    assert local_bodies.count("remote note") == 1


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------

def test_conflict_remote_wins_by_default(provider):
    engine = make_engine(provider)
    ref, tid = _import_one(engine, provider, title="contested")
    with kb.connect() as conn:
        kb.set_status_direct(conn, tid, "todo", source="test")
    provider.human_move(ref, column_name="In Progress")
    stats = engine.sync_once()
    assert stats.conflicts == 1
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"
        events = [e.kind for e in kb.list_events(conn, tid)]
        assert "sync_conflict" in events
    assert_quiescent(engine, provider)


def test_conflict_local_terminal_outcome_wins(provider):
    engine = make_engine(provider)
    ref, tid = _import_one(engine, provider, title="worker finished",
                           column_name="Ready")
    with kb.connect() as conn:
        kb.claim_task(conn, tid)
    engine.sync_once()
    with kb.connect() as conn:
        assert kb.complete_task(conn, tid, summary="all done")
    provider.human_move(ref, column_name="Review")
    stats = engine.sync_once()
    assert stats.conflicts == 1
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "done"
    assert provider.cards[ref]["closed"] is True
    assert_quiescent(engine, provider)


# ---------------------------------------------------------------------------
# Deletes
# ---------------------------------------------------------------------------

def test_remote_delete_blocks_local_task_on_full_scan(provider):
    engine = make_engine(provider)
    ref, tid = _import_one(engine, provider, title="doomed")
    provider.human_delete(ref)
    engine.sync_once(full=True)
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
        pairing = state.list_pairings(conn)[0]
        link = state.get_link_by_task(conn, pairing["id"], tid)
        assert link["deleted"] == 1
    assert_quiescent(engine, provider)


def test_local_delete_leaves_farewell_comment(provider):
    engine = make_engine(provider)
    ref, tid = _import_one(engine, provider, title="removed locally")
    with kb.connect() as conn:
        kb.delete_task(conn, tid)
    engine.sync_once()
    farewells = [w for w in provider.writes if w[0] == "add_comment"]
    assert len(farewells) == 1
    with kb.connect() as conn:
        pairing = state.list_pairings(conn)[0]
        assert state.get_link_by_task(conn, pairing["id"], tid)["deleted"] == 1
    assert_quiescent(engine, provider)


# ---------------------------------------------------------------------------
# Cursor behaviour
# ---------------------------------------------------------------------------

def test_cursor_advances_and_unchanged_cards_skip_reconcile(provider):
    engine = make_engine(provider)
    ref, tid = _import_one(engine, provider, title="stable")
    with kb.connect() as conn:
        pairing = state.list_pairings(conn)[0]
    cursor_after_import = pairing["remote_cursor"]
    assert cursor_after_import is not None
    engine.sync_once()
    with kb.connect() as conn:
        assert state.list_pairings(conn)[0]["remote_cursor"] >= cursor_after_import


def test_append_task_event_public_wrapper(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="events")
        kb.append_task_event(conn, tid, "sync_conflict", {"winner": "remote"})
        events = kb.list_events(conn, tid)
        assert events[-1].kind == "sync_conflict"
        assert events[-1].payload == {"winner": "remote"}
