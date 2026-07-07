"""External kanban sync — mirror a local board to a remote service.

The local SQLite board stays the operational source of truth (dispatcher,
claims, runs, events); this package keeps a remote board (Fizzy, ...) in
step with it bidirectionally. See ``docs/kanban/external-sync.md``.
"""

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
from hermes_cli.kanban_sync.registry import (
    get_provider,
    list_provider_names,
    register_provider,
)

__all__ = [
    "KanbanSyncProvider",
    "RemoteCard",
    "RemoteColumn",
    "RemoteComment",
    "SyncAuthError",
    "SyncNotFoundError",
    "SyncProviderError",
    "SyncRateLimitError",
    "get_provider",
    "list_provider_names",
    "register_provider",
]
