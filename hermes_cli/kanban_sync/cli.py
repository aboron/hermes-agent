"""``hermes kanban sync`` — manual control of the external kanban sync.

Three small subcommands for bootstrap and debugging; steady-state syncing
runs in the gateway watcher (``gateway/kanban_sync_watcher.py``):

- ``init``: verify auth, provision the remote board, record the pairing
  row. In mirror mode (the config default) ``--remote-board`` is optional:
  omitted, init creates (or reuses by name) a dedicated
  ``<board_prefix><Board Name>`` remote board and writes the pairing plus
  ``enabled: true`` back to config.yaml. With ``--remote-board <id>`` (and
  always in mapped mode) it pairs the existing board and prints the config
  snippet instead.
- ``once [--full] [--remote-board <id>]``: one blocking sync pass.
- ``status``: pairings, cursors, link counts, last errors.

Board scoping comes for free: ``kanban_command`` pins ``--board`` via
``scoped_current_board`` before dispatching here, and the engine connects
with ``board=None`` (current-board resolution).
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_sync import get_provider, list_provider_names
from hermes_cli.kanban_sync import state
from hermes_cli.kanban_sync.engine import KanbanSyncEngine
from hermes_cli.kanban_sync.provider import (
    KanbanSyncProvider,
    RemoteBoard,
    SyncProviderError,
)


def _load_cfgs() -> "tuple[dict, dict, dict]":
    from hermes_cli.config import load_config

    cfg = load_config()
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    sync_cfg = kanban_cfg.get("sync", {}) if isinstance(kanban_cfg, dict) else {}
    return cfg, kanban_cfg, sync_cfg


def _resolve_provider(sync_cfg: dict) -> Optional[KanbanSyncProvider]:
    name = str(sync_cfg.get("provider") or "").strip()
    provider = get_provider(name, sync_cfg)
    if provider is None:
        registered = ", ".join(list_provider_names()) or "<none>"
        print(
            f"kanban sync: unknown provider {name!r} "
            f"(registered: {registered})",
            file=sys.stderr,
        )
        return None
    if not provider.is_available():
        print(
            f"kanban sync: provider {name!r} is not configured — set "
            f"kanban.sync.{name}.base_url and a token (token or "
            f"token_env) in config.yaml; see docs/kanban/external-sync.md.",
            file=sys.stderr,
        )
        return None
    return provider


def _make_engine(
    provider: KanbanSyncProvider,
    kanban_cfg: dict,
    sync_cfg: dict,
    remote_board: str,
) -> KanbanSyncEngine:
    fallback = str(kanban_cfg.get("default_assignee") or "").strip() or None
    return KanbanSyncEngine(
        provider=provider,
        board=None,  # current-board resolution (honors --board pin)
        remote_board_ref=remote_board,
        sync_cfg=sync_cfg,
        fallback_assignee=fallback,
    )


def _current_board_pairings(sync_cfg: dict) -> "list[dict]":
    current = kb.get_current_board()
    out = []
    for pairing in sync_cfg.get("pairings") or []:
        if not isinstance(pairing, dict):
            continue
        board = str(pairing.get("board") or "").strip() or kb.DEFAULT_BOARD
        remote = str(pairing.get("remote_board") or "").strip()
        if remote and board == current:
            out.append({"board": board, "remote_board": remote})
    return out


def cmd_sync(args: argparse.Namespace) -> int:
    action = getattr(args, "sync_action", None)
    if action == "init":
        return _cmd_init(args)
    if action == "once":
        return _cmd_once(args)
    if action == "status":
        return _cmd_status(args)
    print(
        "usage: hermes kanban sync <init|once|status> [options]",
        file=sys.stderr,
    )
    return 2


def _mirror_board_name(sync_cfg: dict) -> str:
    # An explicit board_prefix of "" means "no prefix" — only a missing
    # key falls back to the default (so no `or`-chaining here).
    prefix = sync_cfg.get("board_prefix")
    prefix = "Hermes_" if prefix is None else str(prefix)
    current = kb.get_current_board()
    display = str(kb.read_board_metadata(current).get("name") or "").strip()
    return f"{prefix}{display or current}"


def _ensure_mirror_board(
    provider: KanbanSyncProvider, sync_cfg: dict,
) -> "Optional[RemoteBoard]":
    """Create — or reuse by exact name — the dedicated mirror board.

    Returns ``None`` (after printing why) when the name-matched remote
    board is already paired to a different local board: two local boards
    with colliding display names must not share one mirror.
    """
    name = _mirror_board_name(sync_cfg)
    current = kb.get_current_board()
    match = next((b for b in provider.list_boards() if b.name == name), None)
    if match is not None:
        for pairing in sync_cfg.get("pairings") or []:
            if not isinstance(pairing, dict):
                continue
            board = str(pairing.get("board") or "").strip() or kb.DEFAULT_BOARD
            if str(pairing.get("remote_board") or "") == match.ref and board != current:
                print(
                    f"kanban sync init: remote board {name!r} ({match.ref}) "
                    f"is already paired to local board {board!r}; set "
                    f"kanban.sync.board_prefix to give this board a "
                    f"distinct name.",
                    file=sys.stderr,
                )
                return None
        print(f"Reusing remote board {name!r} ({provider.name}:{match.ref})")
        return match
    board = provider.create_board(name)
    print(f"Created remote board {name!r} ({provider.name}:{board.ref})")
    return board


def _record_pairing_in_config(cfg: dict, current: str, remote_board: str) -> None:
    kanban_cfg = cfg.setdefault("kanban", {})
    sync_cfg = kanban_cfg.setdefault("sync", {})
    sync_cfg["enabled"] = True
    if not isinstance(sync_cfg.get("pairings"), list):
        sync_cfg["pairings"] = []
    entry = {
        "board": "" if current == kb.DEFAULT_BOARD else current,
        "remote_board": remote_board,
    }
    if entry not in sync_cfg["pairings"]:
        sync_cfg["pairings"].append(entry)


def _cmd_init(args: argparse.Namespace) -> int:
    remote_board = str(getattr(args, "remote_board", None) or "").strip()
    cfg, kanban_cfg, sync_cfg = _load_cfgs()
    # Absent key = legacy mapped behavior; real config always carries the
    # key (DEFAULT_CONFIG says "mirror"), matching the engine's fallback.
    mirror_bootstrap = (
        not remote_board
        and str(sync_cfg.get("mode") or "").strip().lower() == "mirror"
    )
    if not remote_board and not mirror_bootstrap:
        print(
            "kanban sync init: --remote-board <id> is required",
            file=sys.stderr,
        )
        return 2
    provider = _resolve_provider(sync_cfg)
    if provider is None:
        return 1
    if mirror_bootstrap:
        try:
            board = _ensure_mirror_board(provider, sync_cfg)
        except SyncProviderError as exc:
            print(f"kanban sync init: {exc}", file=sys.stderr)
            return 1
        if board is None:
            return 1
        remote_board = board.ref
    engine = _make_engine(provider, kanban_cfg, sync_cfg, remote_board)
    try:
        with kb.connect_closing() as conn:
            topology = engine.ensure_remote_topology(conn)
    except SyncProviderError as exc:
        print(f"kanban sync init: {exc}", file=sys.stderr)
        return 1
    current = kb.get_current_board()
    print(f"Paired local board {current!r} <-> {provider.name}:{remote_board}")
    print("Remote columns:")
    for name in sorted(topology):
        print(f"  {name} -> {topology[name]}")
    configured = {
        p["remote_board"] for p in _current_board_pairings(sync_cfg)
    }
    if remote_board in configured:
        return 0
    if mirror_bootstrap:
        from hermes_cli.config import is_managed, save_config

        if not is_managed():
            _record_pairing_in_config(cfg, current, remote_board)
            save_config(cfg)
            print(
                "\nconfig.yaml updated: pairing recorded and "
                "kanban.sync.enabled set."
            )
            return 0
        # Managed installs can't be written to — fall through to the
        # snippet so the admin can apply it through their config layer.
    board_field = "" if current == kb.DEFAULT_BOARD else current
    print(
        "\nAdd this pairing to config.yaml so the gateway syncs it:\n"
        "  kanban:\n"
        "    sync:\n"
        "      enabled: true\n"
        "      pairings:\n"
        f"        - board: \"{board_field}\"\n"
        f"          remote_board: \"{remote_board}\""
    )
    return 0


def _cmd_once(args: argparse.Namespace) -> int:
    _, kanban_cfg, sync_cfg = _load_cfgs()
    provider = _resolve_provider(sync_cfg)
    if provider is None:
        return 1
    pairings = _current_board_pairings(sync_cfg)
    only = str(getattr(args, "remote_board", None) or "").strip()
    if only:
        pairings = [p for p in pairings if p["remote_board"] == only] or [
            {"board": kb.get_current_board(), "remote_board": only}
        ]
    if not pairings:
        print(
            f"kanban sync once: no pairing configured for board "
            f"{kb.get_current_board()!r}. Add kanban.sync.pairings in "
            f"config.yaml or pass --remote-board <id>.",
            file=sys.stderr,
        )
        return 1
    full = bool(getattr(args, "full", False))
    rc = 0
    for pairing in pairings:
        engine = _make_engine(
            provider, kanban_cfg, sync_cfg, pairing["remote_board"],
        )
        try:
            stats = engine.sync_once(full=full)
        except SyncProviderError as exc:
            print(
                f"kanban sync once [{pairing['remote_board']}]: {exc}",
                file=sys.stderr,
            )
            rc = 1
            continue
        print(
            f"[{pairing['board']} <-> {provider.name}:{pairing['remote_board']}] "
            f"pulled={stats.pulled} "
            f"created_local={stats.created_local} "
            f"updated_local={stats.updated_local} "
            f"created_remote={stats.created_remote} "
            f"updated_remote={stats.updated_remote} "
            f"comments_in={stats.comments_in} "
            f"comments_out={stats.comments_out} "
            f"conflicts={stats.conflicts} "
            f"errors={len(stats.errors)}"
        )
        for err in stats.errors:
            print(f"  ! {err}")
            rc = 1
    return rc


def _cmd_status(args: argparse.Namespace) -> int:
    with kb.connect_closing() as conn:
        state.ensure_schema(conn)
        pairings = state.list_pairings(conn)
        if not pairings:
            print("No sync pairings on this board. Run "
                  "`hermes kanban sync init --remote-board <id>` to create one.")
            return 0
        for pairing in pairings:
            links = state.list_links(conn, pairing["id"])
            print(
                f"{pairing['provider']}:{pairing['remote_board_ref']} "
                f"enabled={bool(pairing['enabled'])} "
                f"links={len(links)} "
                f"cursor={pairing['remote_cursor'] or '-'} "
                f"last_synced_at={pairing['last_synced_at'] or '-'} "
                f"last_error={pairing['last_error'] or '-'}"
            )
    return 0
