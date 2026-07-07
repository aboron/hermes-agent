"""Tests for the Fizzy client + provider (httpx.MockTransport, no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from hermes_cli.kanban_sync import fizzy
from hermes_cli.kanban_sync.provider import (
    SyncAuthError,
    SyncNotFoundError,
    SyncProviderError,
    SyncRateLimitError,
)

BASE = "https://fizzy.example"
ACCT = "897362094"


def _cfg(**fizzy_overrides):
    f = {
        "base_url": BASE,
        "account_slug": ACCT,
        "token": "tok-123",
        "timeout_seconds": 5,
    }
    f.update(fizzy_overrides)
    return {"provider": "fizzy", "fizzy": f}


def _client(handler):
    transport = httpx.MockTransport(handler)
    return fizzy.FizzyClient(
        base_url=BASE, account_slug=ACCT, token="tok-123", transport=transport,
    )


def _card_json(number=42, **overrides):
    base = {
        "id": f"card-{number}",
        "number": number,
        "url": f"{BASE}/{ACCT}/cards/{number}",
        "title": f"Card {number}",
        "description": "<p>body</p>",
        "status": "published",
        "closed": False,
        "golden": False,
        "tags": [],
        "creator": {"name": "doc"},
        "created_at": "2026-07-01T00:00:00Z",
        "last_active_at": "2026-07-06T10:00:00Z",
        "board": {"id": "b1"},
        "steps": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Client basics
# ---------------------------------------------------------------------------

def test_client_sends_bearer_and_account_scoped_path():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[])

    c = _client(handler)
    c.request("GET", "/boards")
    assert seen["auth"] == "Bearer tok-123"
    assert seen["url"] == f"{BASE}/{ACCT}/boards"


def test_client_error_mapping():
    codes = iter([401, 404, 500])

    def handler(request):
        return httpx.Response(next(codes))

    c = _client(handler)
    with pytest.raises(SyncAuthError):
        c.request("GET", "/boards")
    with pytest.raises(SyncNotFoundError):
        c.request("GET", "/boards")
    with pytest.raises(SyncProviderError):
        c.request("GET", "/boards")


def test_client_rate_limit_carries_retry_after():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "17"})

    with pytest.raises(SyncRateLimitError) as exc_info:
        _client(handler).request("GET", "/boards")
    assert exc_info.value.retry_after == 17.0


def test_paginate_follows_link_next():
    def handler(request):
        if "page=2" in str(request.url):
            return httpx.Response(200, json=[{"n": 2}])
        return httpx.Response(
            200,
            json=[{"n": 1}],
            headers={"Link": f'<{BASE}/{ACCT}/cards?page=2>; rel="next"'},
        )

    items = list(_client(handler).paginate("/cards"))
    assert [i["n"] for i in items] == [1, 2]


# ---------------------------------------------------------------------------
# HTML <-> text
# ---------------------------------------------------------------------------

def test_html_to_text_paragraphs_breaks_lists_links():
    html = (
        "<p>First</p><p>Second<br>line</p>"
        "<ul><li>one</li><li>two</li></ul>"
        '<p><a href="https://x.example/a">label</a></p>'
    )
    text = fizzy._html_to_text(html)
    assert "First" in text
    assert "Second\nline" in text
    assert "- one" in text and "- two" in text
    assert "[label](https://x.example/a)" in text
    # Paragraphs separated by blank line.
    assert "First\n\nSecond" in text


def test_html_text_roundtrip_is_fingerprint_stable():
    original = "Line one\nLine two\n\nPara & <two>"
    html = fizzy._text_to_html(original)
    assert fizzy._html_to_text(html) == original


def test_html_to_text_handles_plain_text_passthrough():
    assert fizzy._html_to_text("just words") == "just words"
    assert fizzy._html_to_text("") == ""


# ---------------------------------------------------------------------------
# Provider mapping
# ---------------------------------------------------------------------------

def _provider(handler):
    transport = httpx.MockTransport(handler)
    client = fizzy.FizzyClient(
        base_url=BASE, account_slug=ACCT, token="tok-123", transport=transport,
    )
    return fizzy.FizzyProvider(_cfg(), client=client)


def test_provider_is_available_requires_token(monkeypatch):
    assert fizzy.FizzyProvider(_cfg()).is_available() is True
    assert fizzy.FizzyProvider(_cfg(token="")).is_available() is False
    monkeypatch.setenv("MY_FIZZY_TOKEN", "envtok")
    p = fizzy.FizzyProvider(_cfg(token="", token_env="MY_FIZZY_TOKEN"))
    assert p.is_available() is True


def test_list_changed_cards_maps_and_flags_archived():
    def handler(request):
        url = str(request.url)
        if "indexed_by=not_now" in url:
            return httpx.Response(200, json=[_card_json(7)])
        if "/cards" in url:
            return httpx.Response(200, json=[
                _card_json(42, column={"id": "c9"},
                           last_active_at="2026-07-06T10:00:00Z"),
                _card_json(7, last_active_at="2026-07-06T09:00:00Z"),
                _card_json(5, closed=True,
                           last_active_at="2026-07-06T08:00:00Z"),
                _card_json(3, last_active_at="2026-07-06T07:00:00Z"),
            ])
        raise AssertionError(url)

    cards, cursor = _provider(handler).list_changed_cards("b1", cursor=None)
    by_ref = {c.ref: c for c in cards}
    assert by_ref["42"].column_ref == "c9"
    assert by_ref["42"].body_text == "body"
    assert by_ref["7"].archived is True
    assert by_ref["5"].closed is True
    assert by_ref["3"].column_ref is None and not by_ref["3"].archived
    assert cursor == "2026-07-06T10:00:00Z"


def test_list_changed_cards_stops_at_cursor_with_overlap():
    pages = {"count": 0}

    def handler(request):
        url = str(request.url)
        if "indexed_by=not_now" in url:
            return httpx.Response(200, json=[])
        pages["count"] += 1
        return httpx.Response(200, json=[
            _card_json(42, last_active_at="2026-07-06T10:00:00Z"),
            _card_json(41, last_active_at="2026-07-06T09:00:00Z"),
            _card_json(40, last_active_at="2026-07-06T08:00:00Z"),
        ], headers={"Link": f'<{BASE}/{ACCT}/cards?page=2>; rel="next"'})

    cards, cursor = _provider(handler).list_changed_cards(
        "b1", cursor="2026-07-06T09:00:00Z",
    )
    refs = {c.ref for c in cards}
    # >= cursor kept (boundary overlap included), older dropped.
    assert refs == {"42", "41"}
    # Page had a card older than the cursor -> no next-page fetch.
    assert pages["count"] == 1
    assert cursor == "2026-07-06T10:00:00Z"


def test_get_card_maps_untriaged_inbox():
    def handler(request):
        return httpx.Response(200, json=_card_json(42))

    card = _provider(handler).get_card("42")
    assert card.ref == "42"
    assert card.column_ref is None
    assert card.closed is False


# ---------------------------------------------------------------------------
# move_card sequencing
# ---------------------------------------------------------------------------

class _SequenceHandler:
    """Records (method, path) and answers get_card with a canned state."""

    def __init__(self, card_state):
        self.card_state = card_state
        self.calls = []

    def __call__(self, request):
        path = request.url.path
        if request.method == "GET" and path == f"/{ACCT}/cards/42":
            return httpx.Response(200, json=self.card_state)
        self.calls.append((request.method, path.removeprefix(f"/{ACCT}")))
        return httpx.Response(204)


def test_move_closed_card_to_column_reopens_first():
    h = _SequenceHandler(_card_json(42, closed=True))
    _provider(h).move_card("42", column_ref="c9")
    assert h.calls == [
        ("DELETE", "/cards/42/closure"),
        ("POST", "/cards/42/triage"),
    ]


def test_move_open_card_to_closed():
    h = _SequenceHandler(_card_json(42, column={"id": "c9"}))
    _provider(h).move_card("42", column_ref=None, closed=True)
    assert h.calls == [("POST", "/cards/42/closure")]


def test_move_card_to_inbox_untriages():
    h = _SequenceHandler(_card_json(42, column={"id": "c9"}))
    _provider(h).move_card("42", column_ref=None)
    assert h.calls == [("DELETE", "/cards/42/triage")]


def test_move_card_to_not_now():
    h = _SequenceHandler(_card_json(42))
    _provider(h).move_card("42", column_ref=None, archived=True)
    assert h.calls == [("POST", "/cards/42/not_now")]


def test_move_card_noop_when_already_there():
    h = _SequenceHandler(_card_json(42, column={"id": "c9"}))
    _provider(h).move_card("42", column_ref="c9")
    assert h.calls == []


# ---------------------------------------------------------------------------
# Create / comments
# ---------------------------------------------------------------------------

def test_create_card_follows_location():
    def handler(request):
        path = request.url.path
        if request.method == "POST" and path.endswith("/boards/b1/cards"):
            return httpx.Response(
                201, headers={"Location": f"{BASE}/{ACCT}/cards/77"},
            )
        if request.method == "GET" and path == f"/{ACCT}/cards/77":
            return httpx.Response(200, json=_card_json(77, title="new card"))
        raise AssertionError(f"{request.method} {path}")

    card = _provider(handler).create_card(
        "b1", title="new card", body_text="hello",
    )
    assert card.ref == "77"
    assert card.title == "new card"


def test_add_comment_returns_ref_from_location():
    def handler(request):
        assert request.method == "POST"
        body = json.loads(request.content)
        assert "hello" in body["body"]
        return httpx.Response(
            201,
            headers={"Location": f"{BASE}/{ACCT}/cards/42/comments/m99"},
        )

    ref = _provider(handler).add_comment("42", "hello")
    assert ref == "m99"


def test_list_comments_since_ref_returns_only_newer():
    def handler(request):
        return httpx.Response(200, json=[
            {"id": "m1", "created_at": "t1",
             "body": {"plain_text": "one"}, "creator": {"name": "doc"}},
            {"id": "m2", "created_at": "t2",
             "body": {"plain_text": "two"}, "creator": {"name": "doc"}},
            {"id": "m3", "created_at": "t3",
             "body": {"html": "<p>three</p>"}, "creator": {"name": "marty"}},
        ])

    p = _provider(handler)
    all_comments = p.list_comments("42", since_ref=None)
    assert [c.ref for c in all_comments] == ["m1", "m2", "m3"]
    newer = p.list_comments("42", since_ref="m2")
    assert [c.ref for c in newer] == ["m3"]
    assert newer[0].body_text == "three"
    assert newer[0].author == "marty"


def test_card_drafted_status_maps_to_draft_flag():
    def handler(request):
        return httpx.Response(200, json=_card_json(42, status="drafted"))

    assert _provider(handler).get_card("42").draft is True


def test_card_published_status_is_not_draft():
    def handler(request):
        return httpx.Response(200, json=_card_json(42))

    assert _provider(handler).get_card("42").draft is False
