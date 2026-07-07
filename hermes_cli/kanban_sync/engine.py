"""Bidirectional sync engine — local kanban board ↔ remote provider board.

The local SQLite board stays the source of truth for the dispatcher
(claims, runs, events untouched); this engine mirrors task state to a
remote board and imports remote edits, so humans can file and steer work
from the remote UI.

Echo suppression
----------------
Every link row stores a fingerprint of each side's last-synced state.
After the engine writes to a side it immediately re-reads that side and
stores the fresh fingerprint, so the next poll sees a "changed" object
whose fingerprint matches and no-ops. Loops terminate deterministically
without timestamp heuristics.

Conflict policy
---------------
When both sides changed since the last sync, the remote wins (humans on
the remote board are the primary workflow) — EXCEPT the local status when
the unseen local events include a terminal worker outcome (completed /
blocked / gave_up): worker results are never silently reverted. Either
way a ``sync_conflict`` event lands on the task for audit.

Body footer
-----------
Synced tasks carry a trailing ``[<provider>] <card url>`` line so humans
hopping between UIs can find the counterpart. The footer lives only on
the local side; it is stripped before pushing body text to the remote
and re-appended when importing.
"""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_sync import state
from hermes_cli.kanban_sync.provider import (
    KanbanSyncProvider,
    RemoteCard,
    SyncAuthError,
    SyncNotFoundError,
    SyncProviderError,
    SyncRateLimitError,
)

logger = logging.getLogger(__name__)

# Statuses that live in remote workflow columns (everything except the
# inbox/terminal states). Order doubles as the reverse-mapping priority
# when two statuses share a column (blocked + scheduled -> "Blocked"):
# a card dragged into that column means the first status listed here.
_COLUMN_STATUS_ORDER = ("todo", "ready", "running", "review", "blocked", "scheduled")

_TERMINAL_EVENT_KINDS = ("completed", "blocked", "gave_up")

DEFAULT_COLUMN_MAP = {
    "todo": "Todo", "ready": "Ready", "running": "In Progress",
    "review": "Review", "blocked": "Blocked", "scheduled": "Blocked",
}


@dataclass
class SyncStats:
    pulled: int = 0
    created_local: int = 0
    updated_local: int = 0
    created_remote: int = 0
    updated_remote: int = 0
    comments_in: int = 0
    comments_out: int = 0
    conflicts: int = 0
    errors: "list[str]" = field(default_factory=list)

    def idle(self) -> bool:
        return not any((
            self.created_local, self.updated_local, self.created_remote,
            self.updated_remote, self.comments_in, self.comments_out,
            self.conflicts, self.errors,
        ))


@dataclass
class _ApplyResult:
    """What one reconcile/push helper actually did — feeds accurate stats
    and the remote-fingerprint bookkeeping."""

    local_changed: bool = False
    remote_wrote: bool = False
    applied_location: "Optional[dict]" = None


class KanbanSyncEngine:
    """One board pairing (local board ↔ remote board), one provider.

    ``sync_once`` is blocking; the gateway watcher runs it inside
    ``asyncio.to_thread``. ``board=None`` targets the default board.
    ``fallback_assignee`` seeds imported tasks when neither an
    ``assignee:<profile>`` tag nor ``sync.default_assignee`` applies
    (the watcher passes ``kanban.default_assignee``).
    """

    def __init__(
        self,
        *,
        provider: KanbanSyncProvider,
        board: Optional[str],
        remote_board_ref: str,
        sync_cfg: dict,
        fallback_assignee: Optional[str] = None,
    ) -> None:
        self.provider = provider
        self.board = board
        self.remote_board_ref = str(remote_board_ref)
        self.cfg = sync_cfg or {}
        self.fallback_assignee = fallback_assignee
        self._ticks = 0

    # -- config ------------------------------------------------------------

    @property
    def _column_map(self) -> "dict[str, str]":
        raw = self.cfg.get("column_map")
        return dict(raw) if isinstance(raw, dict) and raw else dict(DEFAULT_COLUMN_MAP)

    @property
    def _intake(self) -> dict:
        raw = self.cfg.get("intake")
        return raw if isinstance(raw, dict) else {}

    @property
    def _export_cfg(self) -> dict:
        raw = self.cfg.get("export")
        return raw if isinstance(raw, dict) else {}

    # -- entry point ---------------------------------------------------------

    def sync_once(self, *, full: bool = False) -> SyncStats:
        stats = SyncStats()
        lock = state.acquire_pairing_lock(
            board=self.board,
            provider=self.provider.name,
            remote_board_ref=self.remote_board_ref,
        )
        if lock is None:
            # A concurrent run (gateway watcher vs. manual `sync once`)
            # holds this pairing; interleaving two full pipelines would
            # double-import cards and double-push comments.
            stats.errors.append(
                f"pairing {self.provider.name}:{self.remote_board_ref} is "
                f"busy (another sync in progress); skipped"
            )
            return stats
        conn = kb.connect(board=self.board)
        pairing: Optional[dict] = None
        try:
            state.ensure_schema(conn)
            pairing = state.get_or_create_pairing(
                conn,
                provider=self.provider.name,
                remote_board_ref=self.remote_board_ref,
            )
            if not pairing.get("enabled", 1):
                return stats
            self._ticks += 1
            cadence = int(self.cfg.get("full_resync_every") or 0)
            full = full or (cadence > 0 and self._ticks % cadence == 0)

            topology = self._ensure_topology(conn, pairing, force=full)

            cursor = None if full else pairing.get("remote_cursor")
            cards, new_cursor = self.provider.list_changed_cards(
                self.remote_board_ref, cursor=cursor,
            )
            stats.pulled = len(cards)

            handled: "set[str]" = set()
            failed_cursors: "list[Optional[str]]" = []
            for card in cards:
                try:
                    self._reconcile_remote_card(
                        conn, pairing, topology, card, stats, handled,
                    )
                except (SyncAuthError, SyncRateLimitError):
                    raise
                except Exception as exc:
                    logger.warning(
                        "kanban-sync: reconcile of card %s failed: %s",
                        card.ref, exc, exc_info=True,
                    )
                    stats.errors.append(f"card {card.ref}: {exc}")
                    failed_cursors.append(card.last_active_at)

            if full:
                seen_refs = {c.ref for c in cards}
                self._detect_remote_deletes(conn, pairing, seen_refs, stats)

            self._export_unlinked_tasks(conn, pairing, topology, stats)
            self._push_local_changes(conn, pairing, topology, stats, handled)

            # Hold the cursor back to the oldest FAILED card so the next
            # pull re-lists it; advancing past a failed reconcile would
            # drop that card's remote change until the next full rescan.
            effective_cursor = new_cursor
            if failed_cursors:
                if any(c is None for c in failed_cursors):
                    effective_cursor = pairing.get("remote_cursor")
                else:
                    effective_cursor = min(failed_cursors)
            state.update_pairing(
                conn, pairing["id"],
                remote_cursor=effective_cursor,
                last_synced_at=int(time.time()),
                last_error="; ".join(stats.errors[:5]) or None,
            )
        except (SyncAuthError, SyncRateLimitError) as exc:
            if pairing is not None:
                try:
                    state.update_pairing(
                        conn, pairing["id"], last_error=str(exc),
                    )
                except Exception:
                    pass
            raise
        finally:
            conn.close()
            state.release_pairing_lock(lock)
        return stats

    # -- topology ------------------------------------------------------------

    def ensure_remote_topology(self, conn: sqlite3.Connection) -> "dict[str, str]":
        """Create/verify the remote columns; used by ``sync init``."""
        state.ensure_schema(conn)
        pairing = state.get_or_create_pairing(
            conn, provider=self.provider.name,
            remote_board_ref=self.remote_board_ref,
        )
        return self._ensure_topology(conn, pairing, force=True)

    def _ensure_topology(
        self, conn: sqlite3.Connection, pairing: dict, *, force: bool,
    ) -> "dict[str, str]":
        needed = set(self._column_map.values())
        cached = pairing.get("column_ids") or {}
        if not force and needed <= set(cached):
            return dict(cached)
        existing = {
            c.name: c.ref
            for c in self.provider.list_columns(self.remote_board_ref)
        }
        for name in sorted(needed - set(existing)):
            col = self.provider.create_column(self.remote_board_ref, name)
            existing[col.name] = col.ref
        state.update_pairing(conn, pairing["id"], column_ids=existing)
        pairing["column_ids"] = existing
        return dict(existing)

    # -- fingerprints / mapping ----------------------------------------------

    @staticmethod
    def _remote_fp(card: RemoteCard) -> str:
        return state.fingerprint(
            card.title, card.body_text, card.column_ref,
            card.closed, card.archived,
        )

    @staticmethod
    def _local_fp(task) -> str:
        return state.fingerprint(task.title, task.body, task.status, task.priority)

    def _footer(self, url: str) -> str:
        return f"[{self.provider.name}] {url}"

    def _compose_body(self, body_text: str, url: str) -> str:
        parts = [p for p in (body_text.strip("\n"), self._footer(url) if url else "") if p]
        return "\n\n".join(parts)

    def _strip_footer(self, body: str, url: str) -> str:
        """Remove the trailing counterpart footer — but only the EXACT
        footer for this card's URL. A user-written last line that merely
        looks footer-shaped (starts with ``[fizzy] ``) is content and must
        survive the round trip."""
        lines = (body or "").rstrip("\n").splitlines()
        footer = self._footer(url) if url else None
        if footer and lines and lines[-1] == footer:
            lines = lines[:-1]
            while lines and not lines[-1].strip():
                lines.pop()
        return "\n".join(lines)

    def _status_for_location(
        self, card: RemoteCard, topology: "dict[str, str]",
    ) -> Optional[str]:
        """Map a card's location to a hermes status. ``None`` = no opinion
        (card sits in a column outside the configured map)."""
        if card.closed:
            return "done"
        if card.archived:
            return "archived"
        if card.column_ref is None:
            return "triage"
        column_map = self._column_map
        for status in _COLUMN_STATUS_ORDER:
            name = column_map.get(status)
            if name and topology.get(name) == card.column_ref:
                return status
        return None

    def _location_for_status(
        self,
        conn: sqlite3.Connection,
        pairing: dict,
        topology: "dict[str, str]",
        status: str,
    ) -> "dict[str, object]":
        if status == "done":
            return {"column_ref": None, "closed": True, "archived": False}
        if status == "archived":
            return {"column_ref": None, "closed": False, "archived": True}
        if status == "triage":
            return {"column_ref": None, "closed": False, "archived": False}
        name = self._column_map.get(status)
        if not name:
            return {"column_ref": None, "closed": False, "archived": False}
        ref = topology.get(name)
        if ref is None:
            # Self-heal: the column vanished remotely (or config grew).
            col = self.provider.create_column(self.remote_board_ref, name)
            topology[name] = col.ref
            state.update_pairing(conn, pairing["id"], column_ids=topology)
            ref = col.ref
        return {"column_ref": ref, "closed": False, "archived": False}

    # -- remote -> local -------------------------------------------------------

    def _reconcile_remote_card(
        self,
        conn: sqlite3.Connection,
        pairing: dict,
        topology: "dict[str, str]",
        card: RemoteCard,
        stats: SyncStats,
        handled: "set[str]",
    ) -> None:
        pid = pairing["id"]
        link = state.get_link_by_card(conn, pid, card.ref)
        if link is None:
            if self._intake_allows(card, topology):
                task_id = self._import_card(conn, pairing, topology, card, stats)
                if task_id:
                    handled.add(task_id)
            return
        handled.add(link["task_id"])
        if link["deleted"]:
            return
        task = kb.get_task(conn, link["task_id"])
        if task is None:
            self._handle_local_delete(conn, pairing, link)
            return
        # Snapshot BEFORE any engine action: paths that make no local
        # writes must fingerprint this state, not a post-network re-read
        # that would absorb concurrent worker/dashboard writes as
        # "already synced" (lost updates).
        task_snapshot = task
        event_snapshot = self._max_event_id(conn, task.id)

        remote_changed = self._remote_fp(card) != link["remote_fingerprint"]
        local_changed = self._local_fp(task) != link["local_fingerprint"]
        result = _ApplyResult()

        if remote_changed and local_changed:
            stats.conflicts += 1
            terminal = self._has_unseen_terminal_event(
                conn, task.id, link["last_local_event_id"],
            )
            kb.append_task_event(conn, task.id, "sync_conflict", {
                "winner": "local-status" if terminal else "remote",
                "card": card.ref,
                "provider": self.provider.name,
            })
            event_snapshot = self._max_event_id(conn, task.id)
            if terminal:
                # Worker outcome wins on status; remote wins on words.
                result.local_changed |= self._apply_remote_fields(conn, task, card)
                task_snapshot = kb.get_task(conn, task.id)
                if task_snapshot is not None:
                    loc = self._move_card_to_status(
                        conn, pairing, topology, card, task_snapshot.status,
                    )
                    if loc is not None:
                        result.remote_wrote = True
                        result.applied_location = loc
            else:
                result = self._apply_remote_to_local(
                    conn, pairing, topology, card, task,
                )
                task_snapshot = kb.get_task(conn, task.id)
                event_snapshot = self._max_event_id(conn, task.id)
        elif remote_changed:
            result = self._apply_remote_to_local(
                conn, pairing, topology, card, task,
            )
            task_snapshot = kb.get_task(conn, task.id)
            event_snapshot = self._max_event_id(conn, task.id)
        elif local_changed:
            result = self._push_local_to_remote(
                conn, pairing, topology, card, task,
            )

        if result.local_changed:
            stats.updated_local += 1
        if result.remote_wrote:
            stats.updated_remote += 1

        comments_out = self._sync_comments(conn, pairing, card.ref, task.id, stats)
        self._finalize_link(
            conn, pairing, task.id, card.ref,
            remote_dirty=result.remote_wrote or comments_out > 0,
            fallback_card=card,
            task_snapshot=task_snapshot,
            event_snapshot=event_snapshot,
            applied_location=result.applied_location,
        )

    def _intake_allows(self, card: RemoteCard, topology: "dict[str, str]") -> bool:
        if card.draft or card.closed or card.archived:
            return False
        mode = str(self._intake.get("mode") or "all")
        if mode == "columns":
            allowed = set(self._intake.get("columns") or ())
            if card.column_ref is None:
                return False
            ref_to_name = {ref: name for name, ref in topology.items()}
            return ref_to_name.get(card.column_ref) in allowed
        return True

    def _import_card(
        self,
        conn: sqlite3.Connection,
        pairing: dict,
        topology: "dict[str, str]",
        card: RemoteCard,
        stats: SyncStats,
    ) -> Optional[str]:
        assignee = None
        for tag in card.tags:
            if tag.startswith("assignee:"):
                assignee = tag.split(":", 1)[1].strip() or None
                break
        if assignee is None:
            assignee = (
                str(self.cfg.get("default_assignee") or "").strip()
                or self.fallback_assignee
                or None
            )
        try:
            golden_priority = int(self.cfg.get("golden_priority", 2))
        except (TypeError, ValueError):
            golden_priority = 2
        # Idempotency key ties the card ref to at most one live task: a
        # crash between create_task and the link write (two separate
        # transactions) re-imports on the next tick and gets the SAME
        # task back instead of a duplicate.
        task_id = kb.create_task(
            conn,
            title=card.title.strip() or "(untitled card)",
            body=self._compose_body(card.body_text, card.url),
            assignee=assignee,
            priority=golden_priority if card.golden else 0,
            triage=True,
            created_by="fizzy-sync",
            idempotency_key=(
                f"kanban-sync:{self.provider.name}:"
                f"{self.remote_board_ref}:{card.ref}"
            ),
        )
        state.upsert_link(
            conn, pairing["id"],
            task_id=task_id, remote_card_ref=card.ref, origin="remote",
        )
        target = self._status_for_location(card, topology)
        if target and target != "triage":
            task = kb.get_task(conn, task_id)
            if task is not None:
                self._apply_status(conn, task, target)
        task_snapshot = kb.get_task(conn, task_id)
        event_snapshot = self._max_event_id(conn, task_id)
        self._sync_comments(conn, pairing, card.ref, task_id, stats)
        self._finalize_link(
            conn, pairing, task_id, card.ref,
            remote_dirty=False, fallback_card=card,
            task_snapshot=task_snapshot, event_snapshot=event_snapshot,
        )
        stats.created_local += 1
        return task_id

    def _apply_remote_to_local(
        self,
        conn: sqlite3.Connection,
        pairing: dict,
        topology: "dict[str, str]",
        card: RemoteCard,
        task,
    ) -> _ApplyResult:
        """Apply remote title/body/status to the local task."""
        result = _ApplyResult()
        result.local_changed |= self._apply_remote_fields(conn, task, card)
        task = kb.get_task(conn, task.id)
        if task is None:
            # Deleted concurrently; the delete path picks it up next tick.
            return result
        target = self._status_for_location(card, topology)
        # Only re-derive status when the card's location actually
        # DISAGREES with where the local status maps: statuses that share
        # a column (blocked/scheduled -> Blocked) must not collapse into
        # each other on a purely textual remote edit.
        card_location = {
            "column_ref": card.column_ref,
            "closed": card.closed,
            "archived": card.archived,
        }
        local_location = self._location_for_status(
            conn, pairing, topology, task.status,
        )
        if target and task.status != target and card_location != local_location:
            actual = self._apply_status(conn, task, target)
            result.local_changed |= actual != task.status
            if actual != target:
                # Transition refused (e.g. dependency-gated ready) —
                # reflect local truth back on the board immediately.
                loc = self._move_card_to_status(
                    conn, pairing, topology, card, actual,
                )
                if loc is not None:
                    result.remote_wrote = True
                    result.applied_location = loc
        return result

    def _apply_remote_fields(self, conn: sqlite3.Connection, task, card: RemoteCard) -> bool:
        desired_title = card.title.strip() or task.title
        desired_body = self._compose_body(card.body_text, card.url)
        title = desired_title if desired_title != task.title else None
        body = desired_body if desired_body != (task.body or "") else None
        if title is None and body is None:
            return False
        self._edit_task_fields(conn, task.id, title=title, body=body)
        return True

    def _apply_status(self, conn: sqlite3.Connection, task, target: str) -> str:
        """Move a task to ``target`` using the structured verbs where they
        apply, falling back to a direct write. Returns the status the task
        actually landed in (a refused transition returns the old one)."""
        if task.status == target:
            return target
        if target == "done":
            if not kb.complete_task(
                conn, task.id,
                summary=f"Closed on the {self.provider.name} board (kanban-sync)",
            ):
                kb.set_status_direct(conn, task.id, "done", source="kanban-sync")
        elif target == "archived":
            if not kb.archive_task(conn, task.id):
                kb.set_status_direct(conn, task.id, "archived", source="kanban-sync")
        elif target == "blocked":
            if not kb.block_task(
                conn, task.id,
                reason=f"Card moved to the blocked column on the "
                       f"{self.provider.name} board",
                kind="needs_input",
            ):
                kb.set_status_direct(conn, task.id, "blocked", source="kanban-sync")
        elif target == "ready" and task.status in ("blocked", "scheduled"):
            # unblock_task re-gates on parents and lands ready or todo.
            kb.unblock_task(conn, task.id)
        else:
            kb.set_status_direct(conn, task.id, target, source="kanban-sync")
        refreshed = kb.get_task(conn, task.id)
        return refreshed.status if refreshed else target

    # -- local -> remote --------------------------------------------------------

    def _export_unlinked_tasks(
        self,
        conn: sqlite3.Connection,
        pairing: dict,
        topology: "dict[str, str]",
        stats: SyncStats,
    ) -> None:
        export_cfg = self._export_cfg
        if not export_cfg.get("enabled", True):
            return
        backfill = bool(export_cfg.get("backfill", False))
        sql = (
            "SELECT t.id FROM tasks t "
            "LEFT JOIN kanban_sync_links l "
            "  ON l.task_id = t.id AND l.pairing_id = ? "
            "WHERE l.task_id IS NULL "
            "  AND t.status NOT IN ('done', 'archived') "
            "  AND COALESCE(t.created_by, '') != 'fizzy-sync'"
        )
        params: "list[object]" = [pairing["id"]]
        if not backfill:
            sql += " AND t.created_at >= ?"
            params.append(int(pairing.get("created_at") or 0))
        task_ids = [r["id"] for r in conn.execute(sql, params).fetchall()]
        for task_id in task_ids:
            try:
                self._export_task(conn, pairing, topology, task_id, stats)
            except (SyncAuthError, SyncRateLimitError):
                raise
            except Exception as exc:
                logger.warning(
                    "kanban-sync: export of task %s failed: %s",
                    task_id, exc, exc_info=True,
                )
                stats.errors.append(f"task {task_id}: {exc}")

    def _export_task(
        self,
        conn: sqlite3.Connection,
        pairing: dict,
        topology: "dict[str, str]",
        task_id: str,
        stats: SyncStats,
    ) -> None:
        task = kb.get_task(conn, task_id)
        if task is None:
            return
        card = self.provider.create_card(
            self.remote_board_ref,
            title=task.title,
            # Never-synced body carries no footer; send it verbatim.
            body_text=task.body or "",
        )
        # Persist the link IMMEDIATELY: if any later step fails, the task
        # must not be re-exported (one new card per tick) nor the created
        # card re-imported as a duplicate task. The remote fingerprint
        # records the card as created; local_fingerprint stays NULL so the
        # push path treats the task as changed and retries the move/edit
        # next tick. (Residual: a crash between create_card and this write
        # can still orphan one card — a window of one HTTP call.)
        state.upsert_link(
            conn, pairing["id"],
            task_id=task_id, remote_card_ref=card.ref, origin="local",
            remote_fingerprint=self._remote_fp(card),
            remote_etag=card.last_active_at,
        )
        # Stamp the counterpart footer on the local body so both sides
        # carry the link and body fingerprints stay symmetric.
        if card.url:
            body = task.body or ""
            footer = self._footer(card.url)
            if not body.rstrip("\n").endswith(footer):
                self._edit_task_fields(
                    conn, task_id,
                    body=self._compose_body(body, card.url),
                )
        stats.created_remote += 1
        try:
            task_snapshot = kb.get_task(conn, task_id)
            event_snapshot = self._max_event_id(conn, task_id)
            applied = self._move_card_to_status(
                conn, pairing, topology, card, task.status,
            )
            self._sync_comments(conn, pairing, card.ref, task_id, stats)
            self._finalize_link(
                conn, pairing, task_id, card.ref,
                remote_dirty=True, fallback_card=card,
                task_snapshot=task_snapshot, event_snapshot=event_snapshot,
                applied_location=applied,
            )
        except (SyncAuthError, SyncRateLimitError):
            raise
        except Exception as exc:
            # Link already persisted: the push path retries the move and
            # comments next tick (local_fingerprint is still NULL).
            logger.warning(
                "kanban-sync: post-create export steps for %s failed: %s",
                task_id, exc, exc_info=True,
            )
            stats.errors.append(f"task {task_id}: {exc}")

    def _push_local_changes(
        self,
        conn: sqlite3.Connection,
        pairing: dict,
        topology: "dict[str, str]",
        stats: SyncStats,
        handled: "set[str]",
    ) -> None:
        for link in state.list_links(conn, pairing["id"]):
            if link["task_id"] in handled:
                continue
            try:
                self._push_one_link(conn, pairing, topology, link, stats)
            except (SyncAuthError, SyncRateLimitError):
                raise
            except Exception as exc:
                logger.warning(
                    "kanban-sync: push for task %s failed: %s",
                    link["task_id"], exc, exc_info=True,
                )
                stats.errors.append(f"task {link['task_id']}: {exc}")

    def _push_one_link(
        self,
        conn: sqlite3.Connection,
        pairing: dict,
        topology: "dict[str, str]",
        link: dict,
        stats: SyncStats,
    ) -> None:
        task = kb.get_task(conn, link["task_id"])
        if task is None:
            self._handle_local_delete(conn, pairing, link)
            return
        local_changed = self._local_fp(task) != link["local_fingerprint"]
        new_comments = self._max_comment_id(conn, task.id) > link["last_local_comment_id"]
        if not local_changed and not new_comments:
            return
        # Snapshot before network I/O: this is the state being pushed and
        # therefore the state that may be marked "synced".
        task_snapshot = task
        event_snapshot = self._max_event_id(conn, task.id)
        try:
            card = self.provider.get_card(link["remote_card_ref"])
        except SyncNotFoundError:
            self._handle_remote_delete(conn, pairing, link, task)
            return
        result = _ApplyResult()
        if local_changed:
            result = self._push_local_to_remote(
                conn, pairing, topology, card, task,
            )
            if result.remote_wrote:
                stats.updated_remote += 1
        comments_out = self._sync_comments(conn, pairing, card.ref, task.id, stats)
        self._finalize_link(
            conn, pairing, task.id, card.ref,
            remote_dirty=result.remote_wrote or comments_out > 0,
            fallback_card=card,
            task_snapshot=task_snapshot,
            event_snapshot=event_snapshot,
            applied_location=result.applied_location,
        )

    def _push_local_to_remote(
        self,
        conn: sqlite3.Connection,
        pairing: dict,
        topology: "dict[str, str]",
        card: RemoteCard,
        task,
    ) -> _ApplyResult:
        result = _ApplyResult()
        desired_title = task.title
        desired_body = self._strip_footer(task.body or "", card.url)
        title = desired_title if desired_title != card.title else None
        body = desired_body if desired_body != card.body_text else None
        if title is not None or body is not None:
            self.provider.update_card(card.ref, title=title, body_text=body)
            result.remote_wrote = True
        loc = self._move_card_to_status(conn, pairing, topology, card, task.status)
        if loc is not None:
            result.remote_wrote = True
            result.applied_location = loc
        return result

    def _move_card_to_status(
        self,
        conn: sqlite3.Connection,
        pairing: dict,
        topology: "dict[str, str]",
        card: RemoteCard,
        status: str,
    ) -> "Optional[dict]":
        """Move the card to the location mapping ``status``. Returns the
        applied location dict when a move was issued, ``None`` on no-op.

        A first failure retries ONCE with force-refreshed topology: the
        cached column id may be stale (a human deleted/recreated the
        column remotely), and looping on the stale ref would create a
        duplicate card per tick via the export path."""
        location = self._location_for_status(conn, pairing, topology, status)
        current = {
            "column_ref": card.column_ref,
            "closed": card.closed,
            "archived": card.archived,
        }
        if current == location:
            return None
        try:
            self.provider.move_card(card.ref, **location)
        except (SyncAuthError, SyncRateLimitError):
            raise
        except SyncProviderError:
            refreshed = self._ensure_topology(conn, pairing, force=True)
            topology.clear()
            topology.update(refreshed)
            location = self._location_for_status(conn, pairing, topology, status)
            self.provider.move_card(card.ref, **location)
        return location

    # -- comments ------------------------------------------------------------

    def _sync_comments(
        self,
        conn: sqlite3.Connection,
        pairing: dict,
        card_ref: str,
        task_id: str,
        stats: SyncStats,
    ) -> int:
        """Two-way comment sync for one card/task. Returns the number of
        comments pushed to the remote (callers use it as a dirty flag)."""
        pid = pairing["id"]
        link = state.get_link_by_task(conn, pid, task_id)
        if link is None:
            return 0

        last_remote_ref = link["last_remote_comment_ref"]
        last_local_id = link["last_local_comment_id"]
        pushed = 0
        try:
            # Remote -> local. The seen-comment ledger — not authorship or
            # cursor position — identifies comments the engine already
            # handled, because providers attribute everything to the
            # token's user and cursors can be lost.
            for comment in self.provider.list_comments(
                card_ref, since_ref=last_remote_ref,
            ):
                if not state.is_pushed_comment(conn, pid, comment.ref):
                    # Image/attachment-only comments normalize to empty
                    # text; kb.add_comment rejects empty bodies, and a
                    # skipped-but-unledgered comment would wedge the
                    # cursor. Import a placeholder instead.
                    body = comment.body_text
                    if not body.strip():
                        body = "[non-text comment]"
                    local_id = kb.add_comment(
                        conn, task_id,
                        author=f"fizzy:{comment.author or 'unknown'}",
                        body=body,
                    )
                    state.record_pushed_comment(
                        conn, pid, remote_comment_ref=comment.ref,
                        task_id=task_id, local_comment_id=local_id,
                    )
                    stats.comments_in += 1
                last_remote_ref = comment.ref

            # Local -> remote. The ledger also records each import's local
            # rowid, so imported comments are skipped here by ledger
            # lookup — never by author-prefix heuristics (a genuine local
            # author named fizzy:* must still sync out).
            for comment in kb.list_comments(conn, task_id):
                if comment.id <= last_local_id:
                    continue
                if state.is_local_comment_pushed(conn, pid, task_id, comment.id):
                    last_local_id = max(last_local_id, comment.id)
                    continue
                try:
                    ref = self.provider.add_comment(
                        card_ref,
                        f"[hermes:{comment.author or 'unknown'}] {comment.body}",
                    )
                except (SyncAuthError, SyncRateLimitError):
                    raise
                except SyncProviderError as exc:
                    # Poison/transient rejection: stop here (preserves
                    # order), keep the cursor pointing before this comment
                    # and retry next tick. The ledger protects everything
                    # already pushed from being duplicated.
                    stats.errors.append(
                        f"comment {comment.id} on {task_id}: {exc}"
                    )
                    break
                state.record_pushed_comment(
                    conn, pid, remote_comment_ref=ref,
                    task_id=task_id, local_comment_id=comment.id,
                )
                stats.comments_out += 1
                pushed += 1
                last_local_id = max(last_local_id, comment.id)
        finally:
            # Persist whatever progress was made — a mid-loop failure must
            # not rewind the cursors past already-handled comments.
            state.update_link(
                conn, pid, task_id,
                last_remote_comment_ref=last_remote_ref,
                last_local_comment_id=last_local_id,
            )
        return pushed

    # -- deletes ---------------------------------------------------------------

    def _detect_remote_deletes(
        self,
        conn: sqlite3.Connection,
        pairing: dict,
        seen_refs: "set[str]",
        stats: SyncStats,
    ) -> None:
        for link in state.list_links(conn, pairing["id"]):
            if link["remote_card_ref"] in seen_refs:
                continue
            try:
                # Absence from a filtered listing isn't proof; confirm.
                self.provider.get_card(link["remote_card_ref"])
            except SyncNotFoundError:
                task = kb.get_task(conn, link["task_id"])
                self._handle_remote_delete(conn, pairing, link, task)
            except (SyncAuthError, SyncRateLimitError):
                raise
            except Exception as exc:
                stats.errors.append(
                    f"delete-check {link['remote_card_ref']}: {exc}"
                )

    def _handle_remote_delete(
        self, conn: sqlite3.Connection, pairing: dict, link: dict, task,
    ) -> None:
        if task is not None and task.status not in ("done", "archived"):
            if not kb.block_task(
                conn, task.id,
                reason=f"Linked card was deleted on the "
                       f"{self.provider.name} board",
                kind="needs_input",
            ):
                kb.set_status_direct(conn, task.id, "blocked", source="kanban-sync")
        state.update_link(conn, pairing["id"], link["task_id"], deleted=1)
        logger.info(
            "kanban-sync: remote card %s deleted; unlinked task %s",
            link["remote_card_ref"], link["task_id"],
        )

    def _handle_local_delete(
        self, conn: sqlite3.Connection, pairing: dict, link: dict,
    ) -> None:
        try:
            self.provider.add_comment(
                link["remote_card_ref"],
                "[hermes] The linked task was deleted locally; "
                "this card is no longer synced.",
            )
        except SyncNotFoundError:
            pass
        state.update_link(conn, pairing["id"], link["task_id"], deleted=1)
        logger.info(
            "kanban-sync: local task %s deleted; unlinked card %s",
            link["task_id"], link["remote_card_ref"],
        )

    # -- link bookkeeping --------------------------------------------------------

    def _finalize_link(
        self,
        conn: sqlite3.Connection,
        pairing: dict,
        task_id: str,
        card_ref: str,
        *,
        remote_dirty: bool,
        fallback_card: RemoteCard,
        task_snapshot,
        event_snapshot: int,
        applied_location: "Optional[dict]" = None,
    ) -> None:
        """Store post-write fingerprints so the next poll no-ops on our
        own writes — the heart of echo suppression.

        ``task_snapshot``/``event_snapshot`` are the local state as of the
        engine's last LOCAL write (or the pre-reconcile read when it made
        none). Fingerprinting a fresh re-read here would silently absorb
        anything a worker/dashboard committed during the network I/O —
        including terminal outcomes — as "already synced".

        ``applied_location`` overrides the location fields of the fetched
        card: providers whose single-card payload can't express archived
        state (Fizzy without ``postponed``) would otherwise store a
        fingerprint that disagrees with the next listing sweep, creating
        phantom remote changes."""
        card = fallback_card
        if remote_dirty:
            try:
                card = self.provider.get_card(card_ref)
            except SyncNotFoundError:
                pass
        if applied_location is not None:
            card = dataclasses.replace(
                card,
                column_ref=applied_location["column_ref"],
                closed=applied_location["closed"],
                archived=applied_location["archived"],
            )
        fields: dict = {
            "remote_etag": card.last_active_at,
            "remote_fingerprint": self._remote_fp(card),
            "last_local_event_id": event_snapshot,
        }
        if task_snapshot is not None:
            fields["local_fingerprint"] = self._local_fp(task_snapshot)
        state.update_link(conn, pairing["id"], task_id, **fields)

    # -- small local helpers -------------------------------------------------

    def _edit_task_fields(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        *,
        title: Optional[str] = None,
        body: Optional[str] = None,
    ) -> None:
        sets, params, fields = [], [], []
        if title is not None:
            sets.append("title = ?")
            params.append(title)
            fields.append("title")
        if body is not None:
            sets.append("body = ?")
            params.append(body)
            fields.append("body")
        if not sets:
            return
        with kb.write_txn(conn):
            conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
                (*params, task_id),
            )
            kb._append_event(
                conn, task_id, "edited",
                {"fields": fields, "source": "kanban-sync"},
            )

    @staticmethod
    def _max_event_id(conn: sqlite3.Connection, task_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row["m"])

    @staticmethod
    def _max_comment_id(conn: sqlite3.Connection, task_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM task_comments WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row["m"])

    @staticmethod
    def _has_unseen_terminal_event(
        conn: sqlite3.Connection, task_id: str, since_event_id: int,
    ) -> bool:
        placeholders = ", ".join("?" for _ in _TERMINAL_EVENT_KINDS)
        row = conn.execute(
            f"SELECT 1 FROM task_events WHERE task_id = ? AND id > ? "
            f"AND kind IN ({placeholders}) LIMIT 1",
            (task_id, since_event_id, *_TERMINAL_EVENT_KINDS),
        ).fetchone()
        return row is not None
