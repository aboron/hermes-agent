"""Kanban-sync provider registry.

Mirrors :mod:`agent.web_search_registry`, with one twist: entries are
**factories** (``Callable[[dict], KanbanSyncProvider]``) rather than
instances, because a provider needs the resolved ``kanban.sync`` config
dict (base URL, account slug, token) at construction time and that dict
is only known to the caller (gateway watcher / CLI / tests).

Selection is by the explicit ``kanban.sync.provider`` config key only.
Sync is opt-in, so there is deliberately no fallback-precedence walk —
a misconfigured name should fail loudly in the watcher log and in
``hermes kanban sync status``, not silently pick a different backend.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, List, Optional

from hermes_cli.kanban_sync.provider import KanbanSyncProvider

logger = logging.getLogger(__name__)

ProviderFactory = Callable[[dict], KanbanSyncProvider]

_factories: Dict[str, ProviderFactory] = {}
_lock = threading.Lock()
_builtins_registered = False


def register_provider(name: str, factory: ProviderFactory) -> None:
    """Register a provider factory under ``name``.

    Re-registration overwrites (predictable for tests/dev hot reload).
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("provider name must be a non-empty string")
    key = name.strip()
    with _lock:
        existing = _factories.get(key)
        _factories[key] = factory
    if existing is not None:
        logger.debug("kanban-sync provider '%s' re-registered", key)
    else:
        logger.debug("registered kanban-sync provider '%s'", key)


def get_provider(name: str, sync_cfg: dict) -> Optional[KanbanSyncProvider]:
    """Build the provider registered under ``name`` with ``sync_cfg``.

    Returns ``None`` for unknown names; callers surface the error to the
    user (watcher log / CLI exit) with :func:`list_provider_names` for
    the "did you mean" hint.
    """
    if not isinstance(name, str):
        return None
    _ensure_builtins()
    with _lock:
        factory = _factories.get(name.strip())
    if factory is None:
        return None
    return factory(sync_cfg)


def list_provider_names() -> List[str]:
    _ensure_builtins()
    with _lock:
        return sorted(_factories)


def _ensure_builtins() -> None:
    """Lazily register the in-tree providers.

    Import inside the function so the registry module stays importable
    in stripped-down environments; a broken builtin is logged, not
    fatal (matches the plugin registries' tolerance).
    """
    global _builtins_registered
    with _lock:
        if _builtins_registered:
            return
        _builtins_registered = True
    try:
        from hermes_cli.kanban_sync.fizzy import FizzyProvider
        register_provider("fizzy", FizzyProvider)
    except ImportError:
        logger.debug("builtin fizzy provider unavailable", exc_info=True)


def _reset_for_tests() -> None:
    """Clear registrations (and the builtin latch) between tests."""
    global _builtins_registered
    with _lock:
        _factories.clear()
        _builtins_registered = True  # tests opt back in via register_provider
