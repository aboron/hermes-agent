"""Sync-state persistence for external kanban sync.

Pairings (local board ↔ remote board), links (task ↔ card, with change
fingerprints and comment/event cursors), and the pushed-comment ledger
live in each board's own ``kanban.db`` — sync state must share the
board's transactional domain so a link row can never refer to a task
the board doesn't have.

The tables are intentionally NOT part of ``kanban_db.SCHEMA_SQL``: they
are created lazily by :func:`ensure_schema` on first sync use, so
non-sync installs never grow sync tables and ``kanban_db.py`` stays
untouched by this feature.

All helpers follow the kanban_db idiom: free functions taking the
``sqlite3.Connection`` first; multi-statement writes go through
``kanban_db.write_txn``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any, Optional

from hermes_cli import kanban_db as kb

SYNC_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS kanban_sync_pairings (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        provider         TEXT NOT NULL,
        remote_board_ref TEXT NOT NULL,
        remote_cursor    TEXT,
        column_ids       TEXT,
        enabled          INTEGER NOT NULL DEFAULT 1,
        last_synced_at   INTEGER,
        last_error       TEXT,
        created_at       INTEGER NOT NULL,
        UNIQUE (provider, remote_board_ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kanban_sync_links (
        pairing_id              INTEGER NOT NULL,
        task_id                 TEXT NOT NULL,
        remote_card_ref         TEXT NOT NULL,
        remote_etag             TEXT,
        remote_fingerprint      TEXT,
        local_fingerprint       TEXT,
        last_local_comment_id   INTEGER NOT NULL DEFAULT 0,
        last_remote_comment_ref TEXT,
        last_local_event_id     INTEGER NOT NULL DEFAULT 0,
        origin                  TEXT NOT NULL DEFAULT 'remote',
        deleted                 INTEGER NOT NULL DEFAULT 0,
        created_at              INTEGER NOT NULL,
        updated_at              INTEGER NOT NULL,
        PRIMARY KEY (pairing_id, task_id),
        UNIQUE (pairing_id, remote_card_ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kanban_sync_pushed_comments (
        pairing_id         INTEGER NOT NULL,
        remote_comment_ref TEXT NOT NULL,
        task_id            TEXT NOT NULL,
        local_comment_id   INTEGER,
        created_at         INTEGER NOT NULL,
        PRIMARY KEY (pairing_id, remote_comment_ref)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sync_links_task
        ON kanban_sync_links(task_id)
    """,
)

def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the sync tables if missing. Idempotent; call outside any
    open transaction (DDL here is CREATE IF NOT EXISTS, committed
    immediately).

    Deliberately NOT cached per-process: the DB file at a path can be
    deleted and recreated under a long-lived gateway (board reset), and a
    stale "already ensured" cache would suppress the only code path that
    can recreate these tables. Four CREATE IF NOT EXISTS statements per
    sync tick are noise next to the network round-trips.
    """
    for stmt in SYNC_SCHEMA_STATEMENTS:
        conn.execute(stmt)
    conn.commit()


def fingerprint(*parts: Any) -> str:
    """Stable sha256 hex digest over a tuple of JSON-serializable parts.

    ``None`` and ``""`` hash differently (json keeps the distinction),
    which matters for column_ref where None means "untriaged inbox".
    """
    blob = json.dumps(list(parts), ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Pairings
# ---------------------------------------------------------------------------

_PAIRING_FIELDS = frozenset({
    "remote_cursor", "column_ids", "enabled", "last_synced_at", "last_error",
})


def _pairing_dict(row: sqlite3.Row) -> dict:
    d = _row_to_dict(row)
    raw = d.get("column_ids")
    try:
        d["column_ids"] = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        d["column_ids"] = {}
    return d


def get_or_create_pairing(
    conn: sqlite3.Connection, *, provider: str, remote_board_ref: str,
) -> dict:
    row = conn.execute(
        "SELECT * FROM kanban_sync_pairings "
        "WHERE provider = ? AND remote_board_ref = ?",
        (provider, remote_board_ref),
    ).fetchone()
    if row is not None:
        return _pairing_dict(row)
    with kb.write_txn(conn):
        conn.execute(
            "INSERT OR IGNORE INTO kanban_sync_pairings "
            "(provider, remote_board_ref, created_at) VALUES (?, ?, ?)",
            (provider, remote_board_ref, int(time.time())),
        )
    row = conn.execute(
        "SELECT * FROM kanban_sync_pairings "
        "WHERE provider = ? AND remote_board_ref = ?",
        (provider, remote_board_ref),
    ).fetchone()
    return _pairing_dict(row)


def update_pairing(conn: sqlite3.Connection, pairing_id: int, **fields: Any) -> None:
    unknown = set(fields) - _PAIRING_FIELDS
    if unknown:
        raise ValueError(f"unknown pairing fields: {sorted(unknown)}")
    if not fields:
        return
    if "column_ids" in fields and fields["column_ids"] is not None:
        fields["column_ids"] = json.dumps(fields["column_ids"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    with kb.write_txn(conn):
        conn.execute(
            f"UPDATE kanban_sync_pairings SET {cols} WHERE id = ?",
            (*fields.values(), pairing_id),
        )


def list_pairings(conn: sqlite3.Connection) -> "list[dict]":
    rows = conn.execute(
        "SELECT * FROM kanban_sync_pairings ORDER BY id",
    ).fetchall()
    return [_pairing_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

_LINK_FIELDS = frozenset({
    "remote_etag", "remote_fingerprint", "local_fingerprint",
    "last_local_comment_id", "last_remote_comment_ref",
    "last_local_event_id", "origin", "deleted",
})


def upsert_link(
    conn: sqlite3.Connection,
    pairing_id: int,
    *,
    task_id: str,
    remote_card_ref: str,
    origin: str,
    **fields: Any,
) -> None:
    unknown = set(fields) - (_LINK_FIELDS - {"origin"})
    if unknown:
        raise ValueError(f"unknown link fields: {sorted(unknown)}")
    now = int(time.time())
    base = {
        "remote_etag": None,
        "remote_fingerprint": None,
        "local_fingerprint": None,
        "last_local_comment_id": 0,
        "last_remote_comment_ref": None,
        "last_local_event_id": 0,
        "deleted": 0,
    }
    base.update(fields)
    update_cols = ", ".join(f"{k} = excluded.{k}" for k in base)
    with kb.write_txn(conn):
        conn.execute(
            f"""
            INSERT INTO kanban_sync_links (
                pairing_id, task_id, remote_card_ref, origin,
                {', '.join(base)}, created_at, updated_at
            ) VALUES (?, ?, ?, ?, {', '.join('?' for _ in base)}, ?, ?)
            ON CONFLICT (pairing_id, task_id) DO UPDATE SET
                remote_card_ref = excluded.remote_card_ref,
                {update_cols},
                updated_at = excluded.updated_at
            """,
            (pairing_id, task_id, remote_card_ref, origin,
             *base.values(), now, now),
        )


def update_link(
    conn: sqlite3.Connection, pairing_id: int, task_id: str, **fields: Any,
) -> None:
    unknown = set(fields) - _LINK_FIELDS
    if unknown:
        raise ValueError(f"unknown link fields: {sorted(unknown)}")
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with kb.write_txn(conn):
        conn.execute(
            f"UPDATE kanban_sync_links SET {cols}, updated_at = ? "
            f"WHERE pairing_id = ? AND task_id = ?",
            (*fields.values(), int(time.time()), pairing_id, task_id),
        )


def get_link_by_task(
    conn: sqlite3.Connection, pairing_id: int, task_id: str,
) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM kanban_sync_links WHERE pairing_id = ? AND task_id = ?",
        (pairing_id, task_id),
    ).fetchone()
    return _row_to_dict(row) if row else None


def get_link_by_card(
    conn: sqlite3.Connection, pairing_id: int, remote_card_ref: str,
) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM kanban_sync_links "
        "WHERE pairing_id = ? AND remote_card_ref = ?",
        (pairing_id, remote_card_ref),
    ).fetchone()
    return _row_to_dict(row) if row else None


def list_links(
    conn: sqlite3.Connection, pairing_id: int, *, include_deleted: bool = False,
) -> "list[dict]":
    sql = "SELECT * FROM kanban_sync_links WHERE pairing_id = ?"
    if not include_deleted:
        sql += " AND deleted = 0"
    sql += " ORDER BY task_id"
    return [_row_to_dict(r) for r in conn.execute(sql, (pairing_id,)).fetchall()]


# ---------------------------------------------------------------------------
# Pushed-comment ledger
# ---------------------------------------------------------------------------

def record_pushed_comment(
    conn: sqlite3.Connection,
    pairing_id: int,
    *,
    remote_comment_ref: str,
    task_id: str,
    local_comment_id: Optional[int],
) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "INSERT OR IGNORE INTO kanban_sync_pushed_comments "
            "(pairing_id, remote_comment_ref, task_id, local_comment_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (pairing_id, remote_comment_ref, task_id, local_comment_id,
             int(time.time())),
        )


def is_pushed_comment(
    conn: sqlite3.Connection, pairing_id: int, remote_comment_ref: str,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM kanban_sync_pushed_comments "
        "WHERE pairing_id = ? AND remote_comment_ref = ?",
        (pairing_id, remote_comment_ref),
    ).fetchone()
    return row is not None


def is_local_comment_pushed(
    conn: sqlite3.Connection, pairing_id: int, task_id: str, local_comment_id: int,
) -> bool:
    """True when this local comment already has a remote counterpart —
    either pushed by the engine or created BY an import (imports record
    their local rowid too). The ledger, not author heuristics or cursor
    positions, is what makes the outbound comment leg idempotent."""
    row = conn.execute(
        "SELECT 1 FROM kanban_sync_pushed_comments "
        "WHERE pairing_id = ? AND task_id = ? AND local_comment_id = ?",
        (pairing_id, task_id, local_comment_id),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Per-pairing advisory lock
# ---------------------------------------------------------------------------

def _pairing_lock_path(board, provider: str, remote_board_ref: str):
    digest = hashlib.sha256(
        f"{provider}:{remote_board_ref}".encode("utf-8")
    ).hexdigest()[:12]
    return kb.kanban_db_path(board).parent / f".sync-{digest}.lock"


def acquire_pairing_lock(*, board, provider: str, remote_board_ref: str):
    """Exclusive non-blocking advisory lock for one sync pairing.

    Prevents a manual ``hermes kanban sync once`` from interleaving with
    the gateway watcher's tick on the same pairing (each write is its own
    transaction, so interleaved runs could double-import cards or
    double-push comments).

    Returns a handle to pass to :func:`release_pairing_lock`, ``None``
    when another process holds the lock (caller must skip the sync), or
    the sentinel ``"no-lock"`` when locking is unavailable on this
    platform/filesystem (caller proceeds on config discipline alone).
    """
    try:
        from gateway.status import _try_acquire_file_lock
    except ImportError:
        return "no-lock"
    path = _pairing_lock_path(board, provider, remote_board_ref)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+", encoding="utf-8")
    except OSError:
        return "no-lock"
    if not _try_acquire_file_lock(handle):
        handle.close()
        return None
    return handle


def release_pairing_lock(handle) -> None:
    if handle is None or handle == "no-lock":
        return
    try:
        from gateway.status import _release_file_lock
        _release_file_lock(handle)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass
