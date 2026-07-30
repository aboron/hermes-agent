"""Tests for the Kanboard client + provider (httpx.MockTransport, no network)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from hermes_cli.kanban_sync import kanboard
from hermes_cli.kanban_sync.provider import (
    SyncAuthError,
    SyncNotFoundError,
    SyncProviderError,
    SyncRateLimitError,
)

BASE = "https://kb.example"


def _cfg(**kanboard_overrides):
    k = {
        "base_url": BASE,
        "username": "jsonrpc",
        "token": "tok-123",
        "timeout_seconds": 5,
    }
    k.update(kanboard_overrides)
    return {"provider": "kanboard", "kanboard": k}


class _RpcHandler:
    """Answers JSON-RPC calls from a method map; records (method, params).

    Map values are either a canned result or a callable(params) -> result.
    """

    def __init__(self, methods):
        self.methods = methods
        self.calls = []

    def __call__(self, request):
        body = json.loads(request.content)
        assert body["jsonrpc"] == "2.0"
        assert body["id"] is not None
        method, params = body["method"], body.get("params") or {}
        self.calls.append((method, params))
        handler = self.methods[method]
        result = handler(params) if callable(handler) else handler
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result},
        )

    def methods_called(self):
        return [m for m, _ in self.calls]


def _client(handler):
    transport = httpx.MockTransport(handler)
    return kanboard.KanboardClient(
        base_url=BASE, username="jsonrpc", token="tok-123", transport=transport,
    )


def _provider(handler, **cfg_overrides):
    transport = httpx.MockTransport(handler)
    client = kanboard.KanboardClient(
        base_url=BASE, username="jsonrpc", token="tok-123", transport=transport,
    )
    return kanboard.KanboardProvider(_cfg(**cfg_overrides), client=client)


def _task_json(task_id=7, **overrides):
    base = {
        "id": str(task_id),
        "title": f"Task {task_id}",
        "description": "body",
        "is_active": "1",
        "column_id": "2",
        "swimlane_id": "1",
        "project_id": "1",
        "position": "1",
        "creator_id": "1",
        "owner_id": "0",
        "date_creation": "1753000000",
        "date_modification": "1753800000",
        "date_moved": "1753700000",
        "url": f"{BASE}/?controller=TaskViewController&action=show"
               f"&task_id={task_id}&project_id=1",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Client basics
# ---------------------------------------------------------------------------

def test_client_posts_jsonrpc_envelope_with_basic_auth():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": seen["body"]["id"], "result": []},
        )

    assert _client(handler).call("getAllProjects") == []
    assert seen["url"] == f"{BASE}/jsonrpc.php"
    expected = "Basic " + base64.b64encode(b"jsonrpc:tok-123").decode()
    assert seen["auth"] == expected
    body = seen["body"]
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "getAllProjects"
    assert body["id"]


def test_client_tolerates_pasted_endpoint_url():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": []},
        )

    c = kanboard.KanboardClient(
        base_url=f"{BASE}/jsonrpc.php", username="jsonrpc", token="t",
        transport=httpx.MockTransport(handler),
    )
    c.call("getAllProjects")
    assert seen["url"] == f"{BASE}/jsonrpc.php"


def test_client_http_error_mapping():
    codes = iter([401, 404, 500])

    def handler(request):
        return httpx.Response(next(codes))

    c = _client(handler)
    with pytest.raises(SyncAuthError):
        c.call("getAllProjects")
    with pytest.raises(SyncNotFoundError):
        c.call("getAllProjects")
    with pytest.raises(SyncProviderError):
        c.call("getAllProjects")


def test_client_rate_limit_carries_retry_after():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "17"})

    with pytest.raises(SyncRateLimitError) as exc_info:
        _client(handler).call("getAllProjects")
    assert exc_info.value.retry_after == 17.0


def test_client_jsonrpc_error_object_maps_to_errors():
    errors = iter([
        {"code": -32601, "message": "Method not found"},
        {"code": 401, "message": "Unauthorized"},
    ])

    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "error": next(errors)},
        )

    c = _client(handler)
    with pytest.raises(SyncProviderError) as exc_info:
        c.call("noSuchMethod")
    assert "Method not found" in str(exc_info.value)
    with pytest.raises(SyncAuthError):
        c.call("getAllProjects")


def test_client_html_response_raises_provider_error():
    """A 200 with an HTML body means the request never reached jsonrpc.php
    (misconfigured server) — surface it, don't crash on parsing."""
    def handler(request):
        return httpx.Response(200, text="<html>login</html>")

    with pytest.raises(SyncProviderError):
        _client(handler).call("getAllProjects")


# ---------------------------------------------------------------------------
# Provider basics
# ---------------------------------------------------------------------------

def test_provider_name_and_fixed_states():
    p = kanboard.KanboardProvider(_cfg())
    assert p.name == "kanboard"
    assert p.fixed_states() == {"Done": "closed"}


def test_provider_is_available_requires_base_url_and_token(monkeypatch):
    assert kanboard.KanboardProvider(_cfg()).is_available() is True
    assert kanboard.KanboardProvider(_cfg(token="")).is_available() is False
    assert kanboard.KanboardProvider(_cfg(base_url="")).is_available() is False
    monkeypatch.setenv("MY_KB_TOKEN", "envtok")
    p = kanboard.KanboardProvider(_cfg(token="", token_env="MY_KB_TOKEN"))
    assert p.is_available() is True


def test_non_numeric_ref_raises_provider_error():
    with pytest.raises(SyncProviderError):
        _provider(_RpcHandler({})).get_card("not-a-number")


# ---------------------------------------------------------------------------
# Boards (mirror mode)
# ---------------------------------------------------------------------------

def test_list_boards_maps_projects():
    handler = _RpcHandler({"getAllProjects": [
        {"id": "1", "name": "Hermes_Default", "url": {"board": f"{BASE}/b/1"}},
        {"id": "2", "name": "Beta", "url": None},
    ]})
    boards = _provider(handler).list_boards()
    assert [(b.ref, b.name) for b in boards] == [
        ("1", "Hermes_Default"), ("2", "Beta"),
    ]
    assert boards[0].url == f"{BASE}/b/1"
    assert boards[1].url == ""


def test_create_board_removes_seeded_default_columns():
    """createProject seeds default workflow columns; a seeded "Done" column
    would shadow the "Done" closed state in mirror mode, so create_board
    must return an empty project for the engine to build the topology."""
    handler = _RpcHandler({
        "createProject": 9,
        "getColumns": [
            {"id": "31", "title": "Backlog", "position": "1"},
            {"id": "32", "title": "Done", "position": "2"},
        ],
        "removeColumn": True,
        "getProjectById": {
            "id": "9", "name": "Hermes_Default",
            "url": {"board": f"{BASE}/b/9"},
        },
    })
    board = _provider(handler).create_board("Hermes_Default")
    assert (board.ref, board.name) == ("9", "Hermes_Default")
    assert board.url == f"{BASE}/b/9"
    assert handler.methods_called() == [
        "createProject", "getColumns", "removeColumn", "removeColumn",
        "getProjectById",
    ]
    removed = [p["column_id"] for m, p in handler.calls if m == "removeColumn"]
    assert removed == [31, 32]


def test_create_board_false_result_raises():
    with pytest.raises(SyncProviderError):
        _provider(_RpcHandler({"createProject": False})).create_board("X")


def test_list_columns_maps():
    handler = _RpcHandler({"getColumns": [
        {"id": "31", "title": "Todo", "position": "1"},
        {"id": "32", "title": "Review", "position": "2"},
    ]})
    cols = _provider(handler).list_columns("1")
    assert [(c.ref, c.name) for c in cols] == [("31", "Todo"), ("32", "Review")]
    assert handler.calls == [("getColumns", {"project_id": 1})]


def test_create_column_returns_new_ref():
    handler = _RpcHandler({"addColumn": 33})
    col = _provider(handler).create_column("1", "Blocked")
    assert (col.ref, col.name) == ("33", "Blocked")
    assert handler.calls == [("addColumn", {"project_id": 1, "title": "Blocked"})]


def test_create_column_false_result_raises():
    with pytest.raises(SyncProviderError):
        _provider(_RpcHandler({"addColumn": False})).create_column("1", "X")


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

def test_get_card_maps_task_payload():
    handler = _RpcHandler({
        "getTask": _task_json(7, description="a < b\n* item"),
    })
    card = _provider(handler).get_card("7")
    assert card.ref == "7"
    assert card.title == "Task 7"
    # Markdown passes through verbatim: identity round-trip.
    assert card.body_text == "a < b\n* item"
    assert card.column_ref == "2"
    assert card.closed is False and card.archived is False
    assert card.golden is False and card.draft is False
    assert card.tags == ()
    assert "task_id=7" in card.url
    assert card.last_active_at == "1753800000"
    assert handler.calls == [("getTask", {"task_id": 7})]


def test_get_card_closed_task_normalizes_column_to_none():
    """Kanboard closed tasks keep their column_id; the engine's canonical
    closed location has no column, so the DTO must drop it to avoid
    phantom location diffs."""
    handler = _RpcHandler({"getTask": _task_json(7, is_active="0")})
    card = _provider(handler).get_card("7")
    assert card.closed is True
    assert card.column_ref is None


def test_get_card_handles_boolean_is_active():
    """Kanboard 1.2.x returns is_active as a JSON boolean from getTask
    (the docs say "0"/"1" strings); both shapes must map correctly."""
    handler = _RpcHandler({"getTask": _task_json(7, is_active=False)})
    card = _provider(handler).get_card("7")
    assert card.closed is True
    assert card.column_ref is None

    handler = _RpcHandler({"getTask": _task_json(8, is_active=True)})
    card = _provider(handler).get_card("8")
    assert card.closed is False
    assert card.column_ref == "2"


def test_get_card_null_result_raises_not_found():
    handler = _RpcHandler({"getTask": None})
    with pytest.raises(SyncNotFoundError):
        _provider(handler).get_card("999")


def test_stamp_uses_newer_of_modification_and_moved():
    """A column drag bumps only date_moved; change detection must see it."""
    handler = _RpcHandler({
        "getTask": _task_json(7, date_modification="100", date_moved="200"),
    })
    assert _provider(handler).get_card("7").last_active_at == "200"


def test_list_changed_cards_returns_open_always_and_filters_closed():
    """Comments never bump task stamps, so open tasks are always returned
    (the engine's comment pass needs to see them); only the unbounded
    closed archive is cursor-filtered, with >= boundary overlap."""
    open_tasks = [
        _task_json(1, date_modification="100", date_moved="0"),
        _task_json(2, date_modification="50", date_moved="0"),
    ]
    closed_tasks = [
        _task_json(3, is_active="0", date_modification="80", date_moved="0"),
        _task_json(4, is_active="0", date_modification="60", date_moved="0"),
        _task_json(5, is_active="0", date_modification="10", date_moved="0"),
    ]

    def get_all(params):
        return open_tasks if params["status_id"] == 1 else closed_tasks

    handler = _RpcHandler({"getAllTasks": get_all})
    cards, cursor = _provider(handler).list_changed_cards("1", cursor="60")
    refs = {c.ref for c in cards}
    assert refs == {"1", "2", "3", "4"}  # open 50 kept; closed 60 kept (>=); 10 dropped
    assert cursor == "100"
    assert [(m, p["status_id"]) for m, p in handler.calls] == [
        ("getAllTasks", 1), ("getAllTasks", 0),
    ]


def test_list_changed_cards_full_scan_without_cursor():
    def get_all(params):
        if params["status_id"] == 1:
            return [_task_json(1)]
        return [_task_json(2, is_active="0", date_modification="5", date_moved="0")]

    handler = _RpcHandler({"getAllTasks": get_all})
    cards, cursor = _provider(handler).list_changed_cards("1", cursor=None)
    assert {c.ref for c in cards} == {"1", "2"}
    assert cursor == "1753800000"


def test_create_card_creates_then_fetches():
    handler = _RpcHandler({
        "createTask": 55,
        "getTask": _task_json(55, title="new card", description="hello *md*"),
    })
    card = _provider(handler).create_card("1", title="new card", body_text="hello *md*")
    assert card.ref == "55"
    assert card.title == "new card"
    assert handler.calls[0] == (
        "createTask",
        {"title": "new card", "project_id": 1, "description": "hello *md*"},
    )


def test_create_card_empty_title_gets_placeholder():
    handler = _RpcHandler({"createTask": 56, "getTask": _task_json(56)})
    _provider(handler).create_card("1", title="", body_text="b")
    assert handler.calls[0][1]["title"] == "(untitled)"


def test_create_card_false_result_raises():
    with pytest.raises(SyncProviderError):
        _provider(_RpcHandler({"createTask": False})).create_card(
            "1", title="x", body_text="",
        )


def test_update_card_sends_only_provided_fields():
    handler = _RpcHandler({"updateTask": True})
    p = _provider(handler)
    p.update_card("7", title="new")
    p.update_card("7", body_text="body only")
    assert handler.calls == [
        ("updateTask", {"id": 7, "title": "new"}),
        ("updateTask", {"id": 7, "description": "body only"}),
    ]


def test_update_card_noop_without_fields():
    handler = _RpcHandler({})
    _provider(handler).update_card("7")
    assert handler.calls == []


def test_update_card_false_result_raises():
    with pytest.raises(SyncProviderError):
        _provider(_RpcHandler({"updateTask": False})).update_card("7", title="x")


# ---------------------------------------------------------------------------
# move_card sequencing
# ---------------------------------------------------------------------------

def test_move_open_card_to_closed_calls_close():
    handler = _RpcHandler({"getTask": _task_json(7), "closeTask": True})
    _provider(handler).move_card("7", column_ref=None, closed=True)
    assert handler.methods_called() == ["getTask", "closeTask"]
    assert handler.calls[1] == ("closeTask", {"task_id": 7})


def test_move_already_closed_card_to_closed_is_noop():
    handler = _RpcHandler({"getTask": _task_json(7, is_active="0")})
    _provider(handler).move_card("7", column_ref=None, closed=True)
    assert handler.methods_called() == ["getTask"]


def test_move_archived_closes_like_done():
    """Kanboard has no archive/not-now state: archived means closed."""
    handler = _RpcHandler({"getTask": _task_json(7), "closeTask": True})
    _provider(handler).move_card("7", column_ref=None, archived=True)
    assert handler.methods_called() == ["getTask", "closeTask"]


def test_move_closed_card_to_column_reopens_first():
    handler = _RpcHandler({
        "getTask": _task_json(7, is_active="0", column_id="2", swimlane_id="4"),
        "openTask": True,
        "moveTaskPosition": True,
    })
    _provider(handler).move_card("7", column_ref="3")
    assert handler.methods_called() == ["getTask", "openTask", "moveTaskPosition"]
    assert handler.calls[-1] == ("moveTaskPosition", {
        "project_id": 1, "task_id": 7, "column_id": 3,
        "position": 1, "swimlane_id": 4,
    })


def test_move_card_already_in_column_is_noop():
    handler = _RpcHandler({"getTask": _task_json(7, column_id="3")})
    _provider(handler).move_card("7", column_ref="3")
    assert handler.methods_called() == ["getTask"]


def test_move_card_to_inbox_falls_back_to_first_column():
    """No untriaged inbox in Kanboard (mapped-mode triage): the project's
    first column, by position, stands in."""
    handler = _RpcHandler({
        "getTask": _task_json(7, column_id="3"),
        "getColumns": [
            {"id": "32", "title": "Later", "position": "2"},
            {"id": "31", "title": "First", "position": "1"},
        ],
        "moveTaskPosition": True,
    })
    _provider(handler).move_card("7", column_ref=None)
    assert handler.calls[-1][1]["column_id"] == 31


def test_move_card_false_result_raises():
    handler = _RpcHandler({"getTask": _task_json(7), "moveTaskPosition": False})
    with pytest.raises(SyncProviderError):
        _provider(handler).move_card("7", column_ref="9")


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

def test_list_comments_maps_and_slices_since_ref():
    handler = _RpcHandler({"getAllComments": [
        {"id": "1", "date_creation": "100", "comment": "one",
         "username": "admin", "name": "Admin A"},
        {"id": "2", "date_creation": "200", "comment": "two",
         "username": "doc", "name": None},
        {"id": "3", "date_creation": "300", "comment": "three",
         "username": "", "user_id": "5", "name": None},
    ]})
    p = _provider(handler)
    all_comments = p.list_comments("7", since_ref=None)
    assert [c.ref for c in all_comments] == ["1", "2", "3"]
    assert all_comments[0].author == "Admin A"  # display name preferred
    assert all_comments[1].author == "doc"      # falls back to username
    assert all_comments[2].author == "5"        # last resort: user id
    assert all_comments[1].body_text == "two"
    assert all_comments[1].created_at == "200"
    newer = p.list_comments("7", since_ref="2")
    assert [c.ref for c in newer] == ["3"]
    # Vanished cursor ref -> full list; the engine's ledger dedups.
    assert [c.ref for c in p.list_comments("7", since_ref="99")] == ["1", "2", "3"]


def test_list_comments_null_result_is_empty():
    handler = _RpcHandler({"getAllComments": None})
    assert _provider(handler).list_comments("7", since_ref=None) == []


def test_add_comment_under_app_token_posts_as_admin():
    handler = _RpcHandler({"createComment": 88})
    assert _provider(handler).add_comment("7", "hello") == "88"
    assert handler.calls == [
        ("createComment", {"task_id": 7, "user_id": 1, "content": "hello"}),
    ]


def test_add_comment_with_real_username_resolves_user_once():
    handler = _RpcHandler({
        "getUserByName": {"id": "5", "username": "alice"},
        "createComment": 89,
    })
    p = _provider(handler, username="alice")
    p.add_comment("7", "one")
    p.add_comment("7", "two")
    assert handler.methods_called().count("getUserByName") == 1  # cached
    posts = [params for m, params in handler.calls if m == "createComment"]
    assert [params["user_id"] for params in posts] == [5, 5]


def test_add_comment_username_lookup_miss_falls_back_to_admin():
    handler = _RpcHandler({"getUserByName": None, "createComment": 90})
    _provider(handler, username="ghost").add_comment("7", "x")
    assert handler.calls[-1][1]["user_id"] == 1


def test_add_comment_false_result_raises():
    with pytest.raises(SyncProviderError):
        _provider(_RpcHandler({"createComment": False})).add_comment("7", "x")
