"""External kanban sync watcher for GatewayRunner.

Polls the configured external board provider (Fizzy, ...) and keeps each
paired local board in step with its remote counterpart via
:class:`hermes_cli.kanban_sync.engine.KanbanSyncEngine`. Follows the same
shape as the other kanban watchers in ``gateway/kanban_watchers.py``:
config-gated at boot, all blocking work in ``asyncio.to_thread``,
1s-sliced sleeps that honor ``self._running``.

Off by default (``kanban.sync.enabled``); ``HERMES_KANBAN_SYNC=0`` is the
env escape hatch that wins over config, mirroring
``HERMES_KANBAN_DISPATCH_IN_GATEWAY``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

from agent.retry_utils import jittered_backoff

logger = logging.getLogger("gateway.run")

INITIAL_DELAY_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 600.0
AUTH_WARN_INTERVAL_SECONDS = 300.0


class GatewayKanbanSyncWatcherMixin:
    """Kanban external-sync loop for GatewayRunner."""

    def _build_kanban_sync_engines(self, cfg: dict, sync_cfg: dict) -> "list[Any]":
        """Resolve the provider + one engine per configured pairing.

        Returns [] (with a logged reason) on any misconfiguration —
        sync is opt-in, so a broken setup must be loud but non-fatal.
        """
        from hermes_cli.kanban_sync import get_provider, list_provider_names
        from hermes_cli.kanban_sync.engine import KanbanSyncEngine

        provider_name = str(sync_cfg.get("provider") or "").strip()
        provider = get_provider(provider_name, sync_cfg)
        if provider is None:
            logger.warning(
                "kanban sync: unknown provider %r (registered: %s); disabled",
                provider_name, ", ".join(list_provider_names()) or "<none>",
            )
            return []
        if not provider.is_available():
            logger.warning(
                "kanban sync: provider %r is not configured (missing base_url/"
                "account/token); disabled", provider_name,
            )
            return []
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        fallback_assignee = (
            str(kanban_cfg.get("default_assignee") or "").strip() or None
        )
        engines: "list[Any]" = []
        for pairing in sync_cfg.get("pairings") or []:
            if not isinstance(pairing, dict):
                continue
            remote_board = str(pairing.get("remote_board") or "").strip()
            if not remote_board:
                logger.warning(
                    "kanban sync: pairing %r has no remote_board; skipped",
                    pairing,
                )
                continue
            board = str(pairing.get("board") or "").strip() or None
            engines.append(KanbanSyncEngine(
                provider=provider,
                board=board,
                remote_board_ref=remote_board,
                sync_cfg=sync_cfg,
                fallback_assignee=fallback_assignee,
            ))
        if not engines:
            logger.info("kanban sync: enabled but no valid pairings; disabled")
        return engines

    async def _kanban_sync_watcher(self) -> None:
        from hermes_cli.kanban_sync.provider import (
            SyncAuthError,
            SyncRateLimitError,
        )

        env_override = os.environ.get("HERMES_KANBAN_SYNC", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban sync: disabled via HERMES_KANBAN_SYNC env")
            return
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban sync: config loader unavailable; disabled")
            return
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban sync: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        sync_cfg = kanban_cfg.get("sync", {}) if isinstance(kanban_cfg, dict) else {}
        if not sync_cfg.get("enabled"):
            return

        engines = self._build_kanban_sync_engines(cfg, sync_cfg)
        if not engines:
            return

        # Singleton backstop, same rationale as the dispatcher lock: two
        # gateways syncing the same pairing would fight over cursors and
        # ping-pong writes. Lock lives at the machine-global kanban root.
        from gateway.kanban_watchers import (
            _acquire_singleton_lock,
            _release_singleton_lock,
        )
        from hermes_cli import kanban_db as _kb

        lock_handle = None
        lock_path = _kb.kanban_home() / "kanban" / ".sync.lock"
        lock_handle, lock_state = _acquire_singleton_lock(lock_path)
        if lock_state == "contended":
            logger.info(
                "kanban sync: another gateway already holds the sync lock "
                "(%s); this gateway will NOT sync.", lock_path,
            )
            return
        if lock_state != "held":
            logger.warning(
                "kanban sync: advisory lock unavailable at %s; proceeding "
                "on config control alone.", lock_path,
            )
            lock_handle = None

        try:
            interval = float(sync_cfg.get("interval_seconds", 30) or 30)
        except (TypeError, ValueError):
            interval = 30.0
        interval = max(interval, 1.0)

        logger.info(
            "kanban sync: watcher started (provider=%s, pairings=%d, "
            "interval=%.0fs)",
            sync_cfg.get("provider"), len(engines), interval,
        )

        if INITIAL_DELAY_SECONDS:
            await asyncio.sleep(INITIAL_DELAY_SECONDS)

        fail_counts: "dict[int, int]" = {}
        last_auth_warn = 0.0
        try:
            while self._running:
                retry_after: Optional[float] = None
                for i, engine in enumerate(engines):
                    if not self._running:
                        break
                    try:
                        stats = await asyncio.to_thread(engine.sync_once)
                        fail_counts[i] = 0
                        if not stats.idle():
                            logger.info(
                                "kanban sync [%s->%s]: pulled=%d +local=%d "
                                "~local=%d +remote=%d ~remote=%d c_in=%d "
                                "c_out=%d conflicts=%d errors=%d",
                                engine.board or "default",
                                engine.remote_board_ref,
                                stats.pulled, stats.created_local,
                                stats.updated_local, stats.created_remote,
                                stats.updated_remote, stats.comments_in,
                                stats.comments_out, stats.conflicts,
                                len(stats.errors),
                            )
                    except SyncAuthError as exc:
                        fail_counts[i] = fail_counts.get(i, 0) + 1
                        now = time.monotonic()
                        if now - last_auth_warn >= AUTH_WARN_INTERVAL_SECONDS:
                            last_auth_warn = now
                            logger.warning(
                                "kanban sync [%s]: auth failure (%s); check "
                                "the provider token. Retrying with backoff.",
                                engine.remote_board_ref, exc,
                            )
                    except SyncRateLimitError as exc:
                        fail_counts[i] = fail_counts.get(i, 0) + 1
                        retry_after = max(retry_after or 0.0, exc.retry_after or 0.0)
                        logger.info(
                            "kanban sync [%s]: rate limited (%s)",
                            engine.remote_board_ref, exc,
                        )
                    except Exception:
                        fail_counts[i] = fail_counts.get(i, 0) + 1
                        logger.exception(
                            "kanban sync [%s]: tick failed",
                            engine.remote_board_ref,
                        )

                worst = max(fail_counts.values(), default=0)
                if worst > 0:
                    delay = jittered_backoff(
                        worst, base_delay=interval, max_delay=MAX_BACKOFF_SECONDS,
                    )
                    if retry_after:
                        delay = max(delay, retry_after)
                else:
                    delay = interval
                slept = 0.0
                while slept < delay and self._running:
                    await asyncio.sleep(min(1.0, delay - slept))
                    slept += 1.0
        except asyncio.CancelledError:
            logger.debug("kanban sync: cancelled")
            raise
        finally:
            _release_singleton_lock(lock_handle)
