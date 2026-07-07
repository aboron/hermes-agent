"""Kanban-sync provider interface.

The sync engine (:mod:`hermes_cli.kanban_sync.engine`) mirrors a local
kanban board to an external service (Fizzy, ...) through this minimal
surface. Providers normalize the remote service's data model into the
``Remote*`` DTOs below; everything provider-specific (auth, pagination,
HTML bodies, state-transition sequencing) stays inside the provider.

Design notes:

- DTOs are frozen: the engine computes change fingerprints from them, so
  accidental in-place mutation would silently corrupt echo suppression.
- ``RemoteCard.body_text`` is normalized plain text. Providers with rich
  text bodies (Fizzy stores HTML) convert on the way in AND out so
  fingerprints survive lossy round-trips.
- ``column_ref=None`` means the provider's untriaged inbox (Fizzy's
  "Maybe?" queue). ``closed`` / ``archived`` model the provider's
  terminal parking spots (Fizzy: "Done" / "Not Now") which are states,
  not columns.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


class SyncProviderError(RuntimeError):
    """Base error for provider failures the engine should surface."""


class SyncAuthError(SyncProviderError):
    """Credentials missing/rejected. Aborts the sync tick; the watcher
    backs off and warns (rate-limited) instead of hammering the API."""


class SyncNotFoundError(SyncProviderError):
    """Remote object gone (404). The engine treats a vanished card as a
    remote delete, not a transient failure."""


class SyncRateLimitError(SyncProviderError):
    """Provider asked us to slow down (429)."""

    def __init__(self, message: str, *, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class RemoteColumn:
    ref: str
    name: str


@dataclass(frozen=True)
class RemoteCard:
    ref: str
    title: str
    body_text: str
    column_ref: Optional[str]
    closed: bool
    archived: bool
    golden: bool
    tags: "tuple[str, ...]"
    creator: str
    url: str
    last_active_at: Optional[str]


@dataclass(frozen=True)
class RemoteComment:
    ref: str
    author: str
    body_text: str
    created_at: Optional[str]


class KanbanSyncProvider(ABC):
    """One external kanban service, scoped to a single account/instance.

    Constructed by the registry factory with the resolved ``kanban.sync``
    config dict. All methods are blocking (the engine runs inside
    ``asyncio.to_thread``) and raise :class:`SyncProviderError` subclasses
    on failure.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Registry name, e.g. ``"fizzy"``."""

    @abstractmethod
    def is_available(self) -> bool:
        """True when config/credentials are present. Must not hit the
        network — this gates watcher startup, not request success."""

    # -- board topology ----------------------------------------------------

    @abstractmethod
    def list_columns(self, board_ref: str) -> "list[RemoteColumn]":
        ...

    @abstractmethod
    def create_column(self, board_ref: str, name: str) -> RemoteColumn:
        ...

    # -- cards ---------------------------------------------------------------

    @abstractmethod
    def list_changed_cards(
        self, board_ref: str, *, cursor: Optional[str],
    ) -> "tuple[list[RemoteCard], Optional[str]]":
        """Return ``(cards changed since cursor, new cursor)``.

        ``cursor`` is provider-opaque; ``None`` requests a full scan.
        Providers may return a small overlap window around the cursor —
        the engine's fingerprints dedup replays.
        """

    @abstractmethod
    def get_card(self, card_ref: str) -> RemoteCard:
        """Fetch one card. Raises :class:`SyncNotFoundError` when gone."""

    @abstractmethod
    def create_card(
        self, board_ref: str, *, title: str, body_text: str,
    ) -> RemoteCard:
        ...

    @abstractmethod
    def update_card(
        self,
        card_ref: str,
        *,
        title: Optional[str] = None,
        body_text: Optional[str] = None,
    ) -> None:
        ...

    @abstractmethod
    def move_card(
        self,
        card_ref: str,
        *,
        column_ref: Optional[str],
        closed: bool = False,
        archived: bool = False,
    ) -> None:
        """Put the card in exactly one location: ``closed=True`` → the
        provider's Done state, ``archived=True`` → its Not-Now/archive
        state, else ``column_ref`` (``None`` = untriaged inbox). The
        provider owns whatever transition sequencing its API needs
        (e.g. Fizzy requires reopening a closed card before re-triage).
        """

    # -- comments ------------------------------------------------------------

    @abstractmethod
    def list_comments(
        self, card_ref: str, *, since_ref: Optional[str],
    ) -> "list[RemoteComment]":
        """Comments newer than ``since_ref`` (provider-ordered), oldest
        first. ``since_ref=None`` returns the full thread."""

    @abstractmethod
    def add_comment(self, card_ref: str, body_text: str) -> str:
        """Post a comment; returns the remote comment ref."""
