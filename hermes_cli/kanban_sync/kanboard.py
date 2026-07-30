"""Kanboard provider — self-hosted kanban (kanboard.org).

API notes (docs.kanboard.org/v1/api/):

- Single endpoint: ``POST {base_url}/jsonrpc.php``; every procedure is a
  JSON-RPC 2.0 call. HTTP Basic auth: the application API token pairs
  with the literal username ``jsonrpc`` (no per-project permission
  checks); a user's personal token pairs with their real username.
- Every scalar comes back as a string ("id": "1", "is_active": "1");
  timestamps are unix-epoch-seconds strings.
- ``updateTask`` cannot change column or open/closed state: placement
  goes through ``moveTaskPosition`` (all five params required) and state
  through ``closeTask``/``openTask``.
- Descriptions and comments are Markdown and pass through verbatim, so
  text -> Kanboard -> text is the identity and fingerprints stay stable.
- Comments never touch the task row (no ``date_modification`` bump), so
  ``list_changed_cards`` always returns every open task — the engine's
  comment pass discovers new comments on them — and cursor-filters only
  the (append-only, unbounded) closed sweep.
- Failed writes usually come back as ``{"result": false}`` with no error
  object; every result must be checked per call.
- Three failed auth attempts lock the account (web-form unlock), so a
  401 must never be retried.
"""

from __future__ import annotations

import itertools
import logging
import os
from typing import Any, Optional

import httpx

from hermes_cli.kanban_sync.provider import (
    KanbanSyncProvider,
    RemoteBoard,
    RemoteCard,
    RemoteColumn,
    RemoteComment,
    SyncAuthError,
    SyncNotFoundError,
    SyncProviderError,
    SyncRateLimitError,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_USERNAME = "jsonrpc"
# createComment requires a user_id; under the application token there is
# no session user, so fall back to Kanboard's built-in admin (id 1).
_FALLBACK_COMMENT_USER_ID = 1


# ---------------------------------------------------------------------------
# JSON-RPC client
# ---------------------------------------------------------------------------

class KanboardClient:
    """Thin blocking JSON-RPC 2.0 client for one Kanboard instance.

    ``transport`` is injectable for tests (httpx.MockTransport), matching
    the pattern in FizzyClient / plugins/spotify/client.py.
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        token: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        # Tolerate a pasted endpoint URL: base_url may or may not already
        # end in /jsonrpc.php.
        base = base_url.rstrip("/").removesuffix("/jsonrpc.php")
        self._endpoint = f"{base}/jsonrpc.php"
        self._ids = itertools.count(1)
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            auth=(username or DEFAULT_USERNAME, token),
        )

    def close(self) -> None:
        self._client.close()

    def call(self, method: str, **params: Any) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": next(self._ids),
            "params": params,
        }
        try:
            resp = self._client.post(self._endpoint, json=payload)
        except httpx.HTTPError as exc:
            raise SyncProviderError(f"{method}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise SyncAuthError(
                f"{method} -> HTTP {resp.status_code} (check kanboard "
                "credentials; three failed attempts lock the account)"
            )
        if resp.status_code == 404:
            raise SyncNotFoundError(f"{method} -> HTTP 404 (check base_url)")
        if resp.status_code == 429:
            retry_after: Optional[float] = None
            raw = resp.headers.get("Retry-After")
            try:
                retry_after = float(raw) if raw else None
            except ValueError:
                retry_after = None
            raise SyncRateLimitError(
                f"{method} -> HTTP 429", retry_after=retry_after,
            )
        if resp.status_code >= 400:
            raise SyncProviderError(
                f"{method} -> HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            # An HTML body means the request never reached jsonrpc.php
            # (reverse proxy / auth misconfiguration).
            raise SyncProviderError(
                f"{method}: non-JSON response: {resp.text[:200]}"
            ) from exc
        if not isinstance(data, dict):
            raise SyncProviderError(f"{method}: unexpected response shape")
        error = data.get("error")
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            message = (
                error.get("message") if isinstance(error, dict) else str(error)
            )
            if code in (401, 403):
                raise SyncAuthError(
                    f"{method}: JSON-RPC error {code}: {message}"
                )
            raise SyncProviderError(
                f"{method}: JSON-RPC error {code}: {message}"
            )
        return data.get("result")


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class KanboardProvider(KanbanSyncProvider):
    """KanbanSyncProvider over a Kanboard instance.

    Constructed by the registry with the resolved ``kanban.sync`` config
    dict; reads its own settings from the ``kanboard`` sub-dict. Refs are
    Kanboard ids as strings: board=project_id, column=column_id,
    card=task_id, comment=comment_id.
    """

    def __init__(self, sync_cfg: dict, *, client: Optional[KanboardClient] = None):
        self._cfg = (sync_cfg or {}).get("kanboard") or {}
        self._client_instance = client
        self._comment_user: Optional[int] = None

    @property
    def name(self) -> str:
        return "kanboard"

    def _resolve_token(self) -> str:
        token = str(self._cfg.get("token") or "").strip()
        if token:
            return token
        env_name = str(self._cfg.get("token_env") or "").strip()
        if env_name:
            return (os.environ.get(env_name) or "").strip()
        return ""

    @property
    def _username(self) -> str:
        return str(self._cfg.get("username") or "").strip() or DEFAULT_USERNAME

    def is_available(self) -> bool:
        return bool(
            str(self._cfg.get("base_url") or "").strip()
            and self._resolve_token()
        )

    @property
    def _client(self) -> KanboardClient:
        if self._client_instance is None:
            timeout_raw = self._cfg.get("timeout_seconds")
            try:
                timeout = float(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT_SECONDS
            except (TypeError, ValueError):
                timeout = DEFAULT_TIMEOUT_SECONDS
            self._client_instance = KanboardClient(
                base_url=str(self._cfg.get("base_url") or ""),
                username=self._username,
                token=self._resolve_token(),
                timeout=timeout,
            )
        return self._client_instance

    # -- mapping ----------------------------------------------------------

    @staticmethod
    def _int(ref: Any) -> int:
        try:
            return int(str(ref))
        except (TypeError, ValueError):
            raise SyncProviderError(f"kanboard: non-numeric ref {ref!r}") from None

    @staticmethod
    def _is_closed(raw: Any) -> bool:
        """``is_active`` is "1"/"0" per the docs but a JSON boolean on
        Kanboard 1.2.x — handle both shapes."""
        if isinstance(raw, bool):
            return not raw
        return str(raw).strip() == "0"

    @staticmethod
    def _stamp(data: dict) -> int:
        """Change stamp: a column drag bumps only ``date_moved``."""
        def num(key: str) -> int:
            try:
                return int(data.get(key) or 0)
            except (TypeError, ValueError):
                return 0
        return max(num("date_modification"), num("date_moved"))

    def _task_to_dto(self, data: dict) -> RemoteCard:
        closed = self._is_closed(data.get("is_active"))
        column_id = data.get("column_id")
        stamp = self._stamp(data)
        return RemoteCard(
            ref=str(data.get("id")),
            title=str(data.get("title") or ""),
            body_text=str(data.get("description") or ""),
            # Closed tasks keep their column_id in Kanboard; the engine's
            # canonical closed location has no column, so drop it to
            # avoid phantom location diffs.
            column_ref=(
                None if closed or column_id is None else str(column_id)
            ),
            closed=closed,
            archived=False,
            golden=False,
            # getAllTasks payloads omit tags; fetching them is one extra
            # RPC per card, so the assignee:<profile> tag mapping is not
            # supported — imports fall back to sync.default_assignee.
            tags=(),
            creator=str(data.get("creator_id") or ""),
            url=str(data.get("url") or ""),
            last_active_at=str(stamp) if stamp else None,
            draft=False,
        )

    @staticmethod
    def _comment_to_dto(data: dict) -> RemoteComment:
        author = (
            data.get("name") or data.get("username") or data.get("user_id") or ""
        )
        return RemoteComment(
            ref=str(data.get("id")),
            author=str(author),
            body_text=str(data.get("comment") or ""),
            created_at=str(data.get("date_creation") or "") or None,
        )

    @staticmethod
    def _project_to_board(data: dict) -> RemoteBoard:
        urls = data.get("url")
        url = str(urls.get("board") or "") if isinstance(urls, dict) else ""
        return RemoteBoard(
            ref=str(data.get("id")),
            name=str(data.get("name") or ""),
            url=url,
        )

    # -- boards ------------------------------------------------------------

    def fixed_states(self) -> "dict[str, str]":
        # Hermes "done" reuses Kanboard's native closed state; Kanboard
        # has no inbox or archive, so every other status gets a column.
        return {"Done": "closed"}

    def list_boards(self) -> "list[RemoteBoard]":
        projects = self._client.call("getAllProjects") or []
        return [self._project_to_board(p) for p in projects]

    def create_board(self, name: str) -> RemoteBoard:
        pid = self._client.call("createProject", name=name)
        if not pid:
            raise SyncProviderError(f"createProject {name!r} failed")
        # createProject seeds default workflow columns; drop them so the
        # engine builds the mirror topology from scratch (a seeded "Done"
        # column would shadow the "Done" closed state and swallow cards
        # humans drag into it).
        for col in self._client.call("getColumns", project_id=self._int(pid)) or []:
            self._client.call("removeColumn", column_id=self._int(col.get("id")))
        data = self._client.call("getProjectById", project_id=self._int(pid))
        if not isinstance(data, dict):
            raise SyncProviderError(f"getProjectById {pid} returned no project")
        return self._project_to_board(data)

    # -- topology ----------------------------------------------------------

    def list_columns(self, board_ref: str) -> "list[RemoteColumn]":
        cols = self._client.call(
            "getColumns", project_id=self._int(board_ref),
        ) or []
        return [
            RemoteColumn(ref=str(c.get("id")), name=str(c.get("title") or ""))
            for c in cols
        ]

    def create_column(self, board_ref: str, name: str) -> RemoteColumn:
        cid = self._client.call(
            "addColumn", project_id=self._int(board_ref), title=name,
        )
        if not cid:
            raise SyncProviderError(f"addColumn {name!r} failed")
        return RemoteColumn(ref=str(cid), name=name)

    # -- cards ---------------------------------------------------------------

    def list_changed_cards(
        self, board_ref: str, *, cursor: Optional[str],
    ) -> "tuple[list[RemoteCard], Optional[str]]":
        pid = self._int(board_ref)
        open_raw = self._client.call("getAllTasks", project_id=pid, status_id=1)
        closed_raw = self._client.call("getAllTasks", project_id=pid, status_id=0)
        if not isinstance(open_raw, list) or not isinstance(closed_raw, list):
            raise SyncProviderError(f"getAllTasks for project {pid} failed")
        try:
            since = int(cursor) if cursor else None
        except (TypeError, ValueError):
            since = None
        cards = [self._task_to_dto(t) for t in open_raw]
        # Only the closed sweep is cursor-filtered (see module docstring);
        # ``>=`` keeps a one-boundary overlap, engine fingerprints dedup.
        cards.extend(
            self._task_to_dto(t) for t in closed_raw
            if since is None or self._stamp(t) >= since
        )
        max_seen = max(
            (self._stamp(t) for t in (*open_raw, *closed_raw)), default=0,
        )
        return cards, (str(max_seen) if max_seen else cursor)

    def _get_task_raw(self, card_ref: str) -> dict:
        data = self._client.call("getTask", task_id=self._int(card_ref))
        if not isinstance(data, dict):
            raise SyncNotFoundError(f"task {card_ref} not found")
        return data

    def get_card(self, card_ref: str) -> RemoteCard:
        return self._task_to_dto(self._get_task_raw(card_ref))

    def create_card(
        self, board_ref: str, *, title: str, body_text: str,
    ) -> RemoteCard:
        tid = self._client.call(
            "createTask",
            title=title or "(untitled)",  # Kanboard rejects empty titles
            project_id=self._int(board_ref),
            description=body_text,
        )
        if not tid:
            raise SyncProviderError(f"createTask {title!r} failed")
        # The engine fingerprints the returned DTO; fetch the full task.
        return self.get_card(str(tid))

    def update_card(
        self,
        card_ref: str,
        *,
        title: Optional[str] = None,
        body_text: Optional[str] = None,
    ) -> None:
        payload: "dict[str, str]" = {}
        if title is not None:
            payload["title"] = title
        if body_text is not None:
            payload["description"] = body_text
        if not payload:
            return
        if not self._client.call("updateTask", id=self._int(card_ref), **payload):
            raise SyncProviderError(f"updateTask {card_ref} failed")

    def move_card(
        self,
        card_ref: str,
        *,
        column_ref: Optional[str],
        closed: bool = False,
        archived: bool = False,
    ) -> None:
        data = self._get_task_raw(card_ref)
        task_id = self._int(data.get("id") or card_ref)
        currently_closed = self._is_closed(data.get("is_active"))
        if closed or archived:
            # Kanboard has no archive/not-now state: archived means
            # closed (mapped-mode archived reads back as done once,
            # then settles — see docs/kanban/external-sync.md).
            if not currently_closed:
                if not self._client.call("closeTask", task_id=task_id):
                    raise SyncProviderError(f"closeTask {card_ref} failed")
            return
        if currently_closed:
            # Reopen before placing; the task resurfaces in its old column.
            if not self._client.call("openTask", task_id=task_id):
                raise SyncProviderError(f"openTask {card_ref} failed")
        project_id = self._int(data.get("project_id"))
        target = column_ref
        if target is None:
            # No untriaged inbox in Kanboard (mapped-mode triage): the
            # project's first column stands in.
            cols = self._client.call("getColumns", project_id=project_id) or []

            def pos(col: dict) -> int:
                try:
                    return int(col.get("position") or 0)
                except (TypeError, ValueError):
                    return 0

            cols = sorted(cols, key=pos)
            if not cols:
                raise SyncProviderError(f"project {project_id} has no columns")
            target = str(cols[0].get("id"))
        if str(data.get("column_id")) != str(target):
            ok = self._client.call(
                "moveTaskPosition",
                project_id=project_id,
                task_id=task_id,
                column_id=self._int(target),
                position=1,
                swimlane_id=self._int(data.get("swimlane_id") or 0),
            )
            if not ok:
                raise SyncProviderError(
                    f"moveTaskPosition {card_ref} -> column {target} failed"
                )

    # -- comments ------------------------------------------------------------

    def list_comments(
        self, card_ref: str, *, since_ref: Optional[str],
    ) -> "list[RemoteComment]":
        raw = self._client.call("getAllComments", task_id=self._int(card_ref))
        if not isinstance(raw, list):
            return []
        comments = [self._comment_to_dto(c) for c in raw]  # API sorts oldest first
        if since_ref is None:
            return comments
        for i, comment in enumerate(comments):
            if comment.ref == since_ref:
                return comments[i + 1:]
        # Cursor ref vanished (comment deleted remotely). Return everything;
        # the engine's seen-comment ledger dedups re-imports.
        return comments

    def _comment_user_id(self) -> int:
        if self._comment_user is None:
            user_id = _FALLBACK_COMMENT_USER_ID
            username = self._username
            if username != DEFAULT_USERNAME:
                try:
                    user = self._client.call("getUserByName", username=username)
                except SyncAuthError:
                    raise
                except SyncProviderError:
                    logger.debug(
                        "kanboard: getUserByName(%s) failed", username,
                        exc_info=True,
                    )
                    user = None
                if isinstance(user, dict) and user.get("id") is not None:
                    user_id = self._int(user["id"])
            self._comment_user = user_id
        return self._comment_user

    def add_comment(self, card_ref: str, body_text: str) -> str:
        cid = self._client.call(
            "createComment",
            task_id=self._int(card_ref),
            user_id=self._comment_user_id(),
            content=body_text,
        )
        if not cid:
            raise SyncProviderError(f"createComment on task {card_ref} failed")
        return str(cid)
