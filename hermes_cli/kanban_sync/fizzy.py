"""Fizzy provider — Basecamp's self-hostable kanban (github.com/basecamp/fizzy).

API notes (docs/api in the Fizzy repo):

- Auth: personal access token, ``Authorization: Bearer <token>``.
- Paths are account-scoped: ``{base_url}/{account_slug}/...``.
- Card locations: untriaged inbox ("Maybe?"), a workflow column, closed
  ("Done"), or "Not Now". Moves: ``POST /cards/{n}/triage`` (to column),
  ``DELETE .../triage`` (to inbox), ``POST|DELETE .../closure``,
  ``POST .../not_now``.
- List endpoints paginate via the ``Link`` header (``rel="next"``).
- ``last_active_at`` on cards is the change cursor: the default ``latest``
  sort returns most-recently-active first, so a pull can stop paging at
  the first card older than the stored cursor.
- A single-card payload does not say whether the card is in "Not Now";
  only the ``indexed_by=not_now`` listing does. ``list_changed_cards``
  runs that listing as a second sweep to set ``archived``;
  :meth:`FizzyProvider.get_card` always reports ``archived=False``.
- Comments are rich text and attributed to the token's user — callers
  that relay someone else's words must prefix the body (the sync engine
  uses ``[hermes:<author>]``).
"""

from __future__ import annotations

import html as html_lib
import logging
import os
from html.parser import HTMLParser
from typing import Any, Iterator, Optional

import httpx

from hermes_cli.kanban_sync.provider import (
    KanbanSyncProvider,
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


# ---------------------------------------------------------------------------
# HTML <-> text
# ---------------------------------------------------------------------------
# Fizzy stores card descriptions and comment bodies as sanitized HTML
# (Trix-style). The sync engine works in plain text and fingerprints it,
# so both directions must be deterministic: text -> html -> text is the
# identity for text we generate ourselves.

_BLOCK_TAGS = {"p", "div", "ul", "ol", "blockquote", "pre",
               "h1", "h2", "h3", "h4", "h5", "h6"}


class _HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._out: "list[str]" = []
        self._href: Optional[str] = None
        self._link_text: "list[str]" = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "br":
            self._emit("\n")
        elif tag == "li":
            self._break(1)
            self._emit("- ")
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            text = "".join(self._link_text).strip()
            href = self._href
            self._href = None
            if href and text and href != text:
                self._emit(f"[{text}]({href})")
            else:
                self._emit(text or href or "")
        elif tag == "li":
            self._break(1)
        elif tag in _BLOCK_TAGS:
            self._break(2)

    def handle_data(self, data: str) -> None:
        self._emit(data)

    def _emit(self, text: str) -> None:
        if self._href is not None:
            self._link_text.append(text)
        else:
            self._out.append(text)

    def _break(self, newlines: int) -> None:
        # Trim trailing spaces on the current line, then ensure exactly
        # `newlines` line breaks (never more than requested here; runs
        # are collapsed again in _html_to_text).
        joined = "".join(self._out).rstrip(" ")
        self._out = [joined]
        while not joined.endswith("\n" * newlines):
            joined += "\n"
        self._out = [joined]

    def text(self) -> str:
        return "".join(self._out)


def _html_to_text(html: str) -> str:
    """Normalize sanitized HTML to plain text (markdown-ish lists/links)."""
    if not html:
        return ""
    parser = _HtmlToText()
    parser.feed(html)
    parser.close()
    text = parser.text()
    # Collapse 3+ newlines to a paragraph break; trim outer whitespace.
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip("\n").strip(" ") if text.strip() else ""


def _text_to_html(text: str) -> str:
    """Escape plain text into the HTML Fizzy accepts.

    Newlines become ``<br>`` so ``_html_to_text`` round-trips exactly —
    that identity is what keeps fingerprints stable across an outbound
    write followed by the next inbound poll.
    """
    return html_lib.escape(text, quote=False).replace("\n", "<br>")


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class FizzyClient:
    """Thin blocking HTTP client for one Fizzy account.

    ``transport`` is injectable for tests (httpx.MockTransport), matching
    the pattern in plugins/spotify/client.py.
    """

    def __init__(
        self,
        *,
        base_url: str,
        account_slug: str,
        token: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._account = str(account_slug).strip("/")
        self._token = token
        self._client = httpx.Client(
            timeout=timeout, transport=transport, follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def _url(self, path: str) -> str:
        return f"{self._base}/{self._account}{path}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
    ) -> httpx.Response:
        url = path if path.startswith(("http://", "https://")) else self._url(path)
        try:
            resp = self._client.request(
                method, url, params=params, json=json,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise SyncProviderError(f"{method} {url}: {exc}") from exc
        if resp.status_code in (401, 403):
            raise SyncAuthError(
                f"{method} {url} -> {resp.status_code} (check fizzy token)"
            )
        if resp.status_code == 404:
            raise SyncNotFoundError(f"{method} {url} -> 404")
        if resp.status_code == 429:
            retry_after: Optional[float] = None
            raw = resp.headers.get("Retry-After")
            try:
                retry_after = float(raw) if raw else None
            except ValueError:
                retry_after = None
            raise SyncRateLimitError(
                f"{method} {url} -> 429", retry_after=retry_after,
            )
        if resp.status_code >= 400:
            raise SyncProviderError(
                f"{method} {url} -> {resp.status_code}: {resp.text[:200]}"
            )
        return resp

    def paginate(
        self, path: str, *, params: Optional[dict] = None,
    ) -> Iterator[dict]:
        """Yield items across Link-header (rel=next) pages, lazily.

        Abandoning the iterator early (e.g. once items get older than the
        pull cursor) stops further page fetches.
        """
        url: Optional[str] = path
        while url:
            resp = self.request("GET", url, params=params)
            params = None  # the next-link already carries its query string
            data = resp.json()
            if isinstance(data, list):
                yield from data
            url = resp.links.get("next", {}).get("url")

    def _absolute(self, location: str) -> str:
        """Resolve a Location header to a full URL.

        Fizzy emits path-relative Locations that already contain the
        account slug (boards.md: ``Location: /897362094/boards/x.json``),
        so they must be joined to the bare base URL — routing them through
        ``request``'s account-scoped path join would double the slug.
        """
        if location.startswith(("http://", "https://")):
            return location
        if location.startswith("/"):
            return f"{self._base}{location}"
        return self._url(f"/{location}")

    def _follow_create(self, resp: httpx.Response) -> dict:
        """Resolve a 201 response to the created object's JSON.

        Fizzy answers creates with a Location header (sometimes with a
        body too); prefer the body when present, else GET the Location.
        """
        try:
            data = resp.json()
            if isinstance(data, dict) and data.get("id") is not None:
                return data
        except ValueError:
            pass
        location = resp.headers.get("Location")
        if not location:
            raise SyncProviderError("create returned neither body nor Location")
        return self.request("GET", self._absolute(location)).json()


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class FizzyProvider(KanbanSyncProvider):
    """KanbanSyncProvider over a Fizzy instance.

    Constructed by the registry with the resolved ``kanban.sync`` config
    dict; reads its own settings from the ``fizzy`` sub-dict.
    """

    def __init__(self, sync_cfg: dict, *, client: Optional[FizzyClient] = None):
        self._cfg = (sync_cfg or {}).get("fizzy") or {}
        self._client_instance = client

    @property
    def name(self) -> str:
        return "fizzy"

    def _resolve_token(self) -> str:
        token = str(self._cfg.get("token") or "").strip()
        if token:
            return token
        env_name = str(self._cfg.get("token_env") or "").strip()
        if env_name:
            return (os.environ.get(env_name) or "").strip()
        return ""

    def is_available(self) -> bool:
        return bool(
            str(self._cfg.get("base_url") or "").strip()
            and str(self._cfg.get("account_slug") or "").strip()
            and self._resolve_token()
        )

    @property
    def _client(self) -> FizzyClient:
        if self._client_instance is None:
            timeout_raw = self._cfg.get("timeout_seconds")
            try:
                timeout = float(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT_SECONDS
            except (TypeError, ValueError):
                timeout = DEFAULT_TIMEOUT_SECONDS
            self._client_instance = FizzyClient(
                base_url=str(self._cfg.get("base_url") or ""),
                account_slug=str(self._cfg.get("account_slug") or ""),
                token=self._resolve_token(),
                timeout=timeout,
            )
        return self._client_instance

    # -- mapping ----------------------------------------------------------

    @staticmethod
    def _tag_titles(raw: Any) -> "tuple[str, ...]":
        titles = []
        for item in raw or []:
            if isinstance(item, dict):
                title = item.get("title") or item.get("name")
            else:
                title = item
            if title:
                titles.append(str(title))
        return tuple(titles)

    def _card_to_dto(self, data: dict, *, archived: bool = False) -> RemoteCard:
        column = data.get("column") or None
        creator = data.get("creator") or {}
        # Card payloads carry the plain-text rendering in ``description``
        # and the rich text in ``description_html`` — only the latter may
        # be fed through the HTML parser (parsing plain text would eat
        # literal '<' sequences and double-unescape entities).
        description_html = data.get("description_html")
        if description_html is not None:
            body_text = _html_to_text(str(description_html))
        else:
            body_text = str(data.get("description") or "")
        return RemoteCard(
            ref=str(data.get("number")),
            title=str(data.get("title") or ""),
            body_text=body_text,
            column_ref=str(column["id"]) if column and column.get("id") is not None else None,
            closed=bool(data.get("closed")),
            # ``postponed`` (present on some payloads) encodes "Not Now"
            # directly; the not_now sweep remains the fallback signal for
            # servers that omit it.
            archived=archived or bool(data.get("postponed")),
            golden=bool(data.get("golden")),
            tags=self._tag_titles(data.get("tags")),
            creator=str(creator.get("name") or ""),
            url=str(data.get("url") or ""),
            last_active_at=data.get("last_active_at"),
            draft=data.get("status") == "drafted",
        )

    @staticmethod
    def _comment_to_dto(data: dict) -> RemoteComment:
        body = data.get("body") or {}
        text = body.get("plain_text")
        if text is None:
            text = _html_to_text(str(body.get("html") or ""))
        creator = data.get("creator") or {}
        return RemoteComment(
            ref=str(data.get("id")),
            author=str(creator.get("name") or ""),
            body_text=str(text),
            created_at=data.get("created_at"),
        )

    # -- topology ----------------------------------------------------------

    def list_columns(self, board_ref: str) -> "list[RemoteColumn]":
        data = self._client.request(
            "GET", f"/boards/{board_ref}/columns",
        ).json()
        return [
            RemoteColumn(ref=str(c["id"]), name=str(c.get("name") or ""))
            for c in data
        ]

    def create_column(self, board_ref: str, name: str) -> RemoteColumn:
        resp = self._client.request(
            "POST", f"/boards/{board_ref}/columns", json={"name": name},
        )
        data = self._client._follow_create(resp)
        return RemoteColumn(ref=str(data["id"]), name=str(data.get("name") or name))

    # -- cards ---------------------------------------------------------------

    def list_changed_cards(
        self, board_ref: str, *, cursor: Optional[str],
    ) -> "tuple[list[RemoteCard], Optional[str]]":
        changed: "dict[str, RemoteCard]" = {}
        max_seen = cursor

        def sweep(indexed_by: str, archived: bool) -> None:
            nonlocal max_seen
            params = {"board_ids[]": board_ref, "indexed_by": indexed_by}
            for item in self._client.paginate("/cards", params=params):
                last_active = item.get("last_active_at") or ""
                # Default sort is most-recently-active first, so the first
                # pre-cursor card ends the sweep. `<` (not `<=`) keeps a
                # one-boundary overlap; engine fingerprints dedup replays.
                if cursor and last_active and last_active < cursor:
                    break
                dto = self._card_to_dto(item, archived=archived)
                changed[dto.ref] = dto
                if last_active and (max_seen is None or last_active > max_seen):
                    max_seen = last_active

        # The docs don't define what ``indexed_by=all`` includes (the
        # separate maybe/closed/not_now indexes suggest it may mirror the
        # board view and exclude Done / Not Now), so sweep the terminal
        # indexes explicitly: ``closed`` so completions are never missed,
        # and ``not_now`` last so its archived flag wins for cards the
        # other sweeps also returned.
        sweep("all", archived=False)
        sweep("closed", archived=False)
        sweep("not_now", archived=True)
        return list(changed.values()), max_seen

    def get_card(self, card_ref: str) -> RemoteCard:
        data = self._client.request("GET", f"/cards/{card_ref}").json()
        return self._card_to_dto(data)

    def create_card(
        self, board_ref: str, *, title: str, body_text: str,
    ) -> RemoteCard:
        resp = self._client.request(
            "POST",
            f"/boards/{board_ref}/cards",
            json={"title": title, "description": _text_to_html(body_text)},
        )
        return self._card_to_dto(self._client._follow_create(resp))

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
            payload["description"] = _text_to_html(body_text)
        if not payload:
            return
        self._client.request("PUT", f"/cards/{card_ref}", json=payload)

    def move_card(
        self,
        card_ref: str,
        *,
        column_ref: Optional[str],
        closed: bool = False,
        archived: bool = False,
    ) -> None:
        current = self.get_card(card_ref)
        if closed:
            if not current.closed:
                self._client.request("POST", f"/cards/{card_ref}/closure")
            return
        if current.closed:
            # Reopen before any re-triage; Fizzy won't move a closed card.
            self._client.request("DELETE", f"/cards/{card_ref}/closure")
        if archived:
            self._client.request("POST", f"/cards/{card_ref}/not_now")
            return
        if column_ref is not None:
            if current.column_ref != column_ref or current.closed or current.archived:
                # Triaging is also the documented way out of "Not Now".
                self._client.request(
                    "POST", f"/cards/{card_ref}/triage",
                    json={"column_id": column_ref},
                )
        else:
            if current.column_ref is not None or current.closed or current.archived:
                self._client.request("DELETE", f"/cards/{card_ref}/triage")

    # -- comments ------------------------------------------------------------

    def list_comments(
        self, card_ref: str, *, since_ref: Optional[str],
    ) -> "list[RemoteComment]":
        comments = [
            self._comment_to_dto(item)
            for item in self._client.paginate(f"/cards/{card_ref}/comments")
        ]
        if since_ref is None:
            return comments
        for i, comment in enumerate(comments):
            if comment.ref == since_ref:
                return comments[i + 1:]
        # Cursor ref vanished (comment deleted remotely). Return everything;
        # the engine's seen-comment ledger dedups re-imports.
        return comments

    def add_comment(self, card_ref: str, body_text: str) -> str:
        resp = self._client.request(
            "POST",
            f"/cards/{card_ref}/comments",
            json={"body": _text_to_html(body_text)},
        )
        try:
            data = resp.json()
            if isinstance(data, dict) and data.get("id") is not None:
                return str(data["id"])
        except ValueError:
            pass
        location = resp.headers.get("Location") or ""
        ref = location.rstrip("/").rsplit("/", 1)[-1]
        if ref.endswith(".json"):
            ref = ref[: -len(".json")]
        if not ref:
            raise SyncProviderError("comment create returned no id/Location")
        return ref
