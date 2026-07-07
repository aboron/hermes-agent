"""In-memory KanbanSyncProvider fake for engine/CLI tests.

Keeps a mutable board (columns, cards, comments) plus a write-call log so
tests can assert exactly which mutations the sync engine performed —
the echo-suppression tests hinge on "second sync makes zero writes".

``human_*`` helpers simulate remote-side edits (a person using the
service's UI) without touching the write log.
"""

from __future__ import annotations

from typing import Optional

from hermes_cli.kanban_sync.provider import (
    KanbanSyncProvider,
    RemoteCard,
    RemoteColumn,
    RemoteComment,
    SyncNotFoundError,
)


class FakeKanbanProvider(KanbanSyncProvider):
    def __init__(self, sync_cfg: Optional[dict] = None):
        self.cfg = sync_cfg or {}
        self.columns: "dict[str, str]" = {}          # name -> ref
        self.cards: "dict[str, dict]" = {}           # ref -> mutable state
        self.comments: "dict[str, list[RemoteComment]]" = {}
        self.writes: "list[tuple]" = []
        self._seq = 0
        self._card_n = 0
        self._comment_n = 0
        self._col_n = 0
        # Failure injection: op name -> queue of exceptions; each matching
        # call pops and raises one until the queue is empty.
        self.fail_ops: "dict[str, list[Exception]]" = {}
        # Content-based poison: add_comment raises while any marker is in
        # the body (persistent until the test clears the set).
        self.poison_comment_bodies: "set[str]" = set()
        # Concurrency hooks: op name -> callable(*args) run at op start.
        self.hooks: "dict[str, object]" = {}
        # Mimic providers whose single-card payload can't express the
        # archived state (Fizzy without the `postponed` field).
        self.get_card_hides_archived = False

    def _maybe_fail(self, op: str, *args) -> None:
        hook = self.hooks.get(op)
        if hook is not None:
            hook(*args)
        queue = self.fail_ops.get(op)
        if queue:
            raise queue.pop(0)

    # -- test helpers (simulate the remote human) ---------------------------

    def human_add_card(
        self,
        *,
        title: str,
        body_text: str = "",
        column_name: Optional[str] = None,
        closed: bool = False,
        archived: bool = False,
        golden: bool = False,
        tags: "tuple[str, ...]" = (),
        creator: str = "human",
        draft: bool = False,
    ) -> str:
        self._card_n += 1
        ref = str(self._card_n)
        column_ref = self.columns.get(column_name) if column_name else None
        if column_name and column_ref is None:
            raise AssertionError(f"no such column: {column_name}")
        self.cards[ref] = {
            "title": title,
            "body_text": body_text,
            "column_ref": column_ref,
            "closed": closed,
            "archived": archived,
            "golden": golden,
            "tags": tuple(tags),
            "creator": creator,
            "draft": draft,
        }
        self.comments.setdefault(ref, [])
        self._bump(ref)
        return ref

    def human_move(
        self,
        ref: str,
        *,
        column_name: Optional[str] = None,
        closed: bool = False,
        archived: bool = False,
    ) -> None:
        card = self.cards[ref]
        card["closed"] = closed
        card["archived"] = archived
        card["column_ref"] = (
            self.columns[column_name] if column_name else None
        )
        self._bump(ref)

    def human_edit(self, ref: str, *, title=None, body_text=None) -> None:
        if title is not None:
            self.cards[ref]["title"] = title
        if body_text is not None:
            self.cards[ref]["body_text"] = body_text
        self._bump(ref)

    def human_comment(self, ref: str, author: str, body_text: str) -> str:
        return self._append_comment(ref, author, body_text)

    def human_delete(self, ref: str) -> None:
        self.cards.pop(ref, None)

    def column_name_of(self, ref: str) -> Optional[str]:
        col_ref = self.cards[ref]["column_ref"]
        for name, r in self.columns.items():
            if r == col_ref:
                return name
        return None

    # -- internals -----------------------------------------------------------

    def _bump(self, ref: str) -> None:
        self._seq += 1
        self.cards[ref]["last_active_at"] = f"t{self._seq:08d}"

    def _append_comment(self, ref: str, author: str, body_text: str) -> str:
        self._comment_n += 1
        cref = f"m{self._comment_n}"
        self.comments.setdefault(ref, []).append(
            RemoteComment(ref=cref, author=author, body_text=body_text,
                          created_at=None)
        )
        if ref in self.cards:
            self._bump(ref)
        return cref

    def _dto(self, ref: str) -> RemoteCard:
        c = self.cards[ref]
        return RemoteCard(
            ref=ref,
            title=c["title"],
            body_text=c["body_text"],
            column_ref=c["column_ref"],
            closed=c["closed"],
            archived=c["archived"],
            golden=c["golden"],
            tags=c["tags"],
            creator=c["creator"],
            url=f"fake://cards/{ref}",
            last_active_at=c["last_active_at"],
            draft=c["draft"],
        )

    # -- KanbanSyncProvider --------------------------------------------------

    @property
    def name(self) -> str:
        return "fake"

    def is_available(self) -> bool:
        return True

    def list_columns(self, board_ref):
        return [RemoteColumn(ref=r, name=n) for n, r in self.columns.items()]

    def create_column(self, board_ref, name):
        self.writes.append(("create_column", name))
        self._col_n += 1
        ref = f"c{self._col_n}"
        self.columns[name] = ref
        return RemoteColumn(ref=ref, name=name)

    def list_changed_cards(self, board_ref, *, cursor):
        cards = [
            self._dto(ref)
            for ref in sorted(self.cards, key=int)
            if cursor is None or self.cards[ref]["last_active_at"] > cursor
        ]
        new_cursor = f"t{self._seq:08d}" if self._seq else cursor
        return cards, new_cursor

    def get_card(self, card_ref):
        self._maybe_fail("get_card", card_ref)
        if card_ref not in self.cards:
            raise SyncNotFoundError(card_ref)
        dto = self._dto(card_ref)
        if self.get_card_hides_archived and dto.archived:
            import dataclasses
            dto = dataclasses.replace(dto, archived=False)
        return dto

    def create_card(self, board_ref, *, title, body_text):
        self.writes.append(("create_card", title))
        self._card_n += 1
        ref = str(self._card_n)
        self.cards[ref] = {
            "title": title, "body_text": body_text, "column_ref": None,
            "closed": False, "archived": False, "golden": False,
            "tags": (), "creator": "sync-bot", "draft": False,
        }
        self.comments.setdefault(ref, [])
        self._bump(ref)
        return self._dto(ref)

    def update_card(self, card_ref, *, title=None, body_text=None):
        if card_ref not in self.cards:
            raise SyncNotFoundError(card_ref)
        self.writes.append(("update_card", card_ref, title, body_text))
        if title is not None:
            self.cards[card_ref]["title"] = title
        if body_text is not None:
            self.cards[card_ref]["body_text"] = body_text
        self._bump(card_ref)

    def move_card(self, card_ref, *, column_ref, closed=False, archived=False):
        self._maybe_fail("move_card", card_ref)
        if card_ref not in self.cards:
            raise SyncNotFoundError(card_ref)
        if column_ref is not None and column_ref not in self.columns.values():
            raise SyncNotFoundError(f"column {column_ref}")
        self.writes.append(("move_card", card_ref, column_ref, closed, archived))
        card = self.cards[card_ref]
        card["closed"] = closed
        card["archived"] = archived
        card["column_ref"] = None if (closed or archived) else column_ref
        self._bump(card_ref)

    def list_comments(self, card_ref, *, since_ref):
        self._maybe_fail("list_comments", card_ref)
        comments = list(self.comments.get(card_ref, ()))
        if since_ref is None:
            return comments
        for i, c in enumerate(comments):
            if c.ref == since_ref:
                return comments[i + 1:]
        return comments

    def add_comment(self, card_ref, body_text):
        from hermes_cli.kanban_sync.provider import SyncProviderError
        self._maybe_fail("add_comment", card_ref, body_text)
        if any(marker in body_text for marker in self.poison_comment_bodies):
            raise SyncProviderError(f"422 rejected: {body_text[:40]}")
        if card_ref not in self.cards:
            raise SyncNotFoundError(card_ref)
        self.writes.append(("add_comment", card_ref, body_text))
        return self._append_comment(card_ref, "sync-bot", body_text)
