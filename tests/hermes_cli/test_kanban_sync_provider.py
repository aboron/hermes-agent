"""Tests for the kanban-sync provider interface, DTOs, errors, registry."""

from __future__ import annotations

import dataclasses

import pytest

from hermes_cli.kanban_sync import provider as prov
from hermes_cli.kanban_sync import registry as reg


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------

def _card(**overrides):
    base = dict(
        ref="42",
        title="Fix the flux capacitor",
        body_text="It sparks.",
        column_ref=None,
        closed=False,
        archived=False,
        golden=False,
        tags=("urgent",),
        creator="doc",
        url="https://fizzy.example/1/cards/42",
        last_active_at="2026-07-06T12:00:00Z",
    )
    base.update(overrides)
    return prov.RemoteCard(**base)


def test_remote_card_is_frozen():
    card = _card()
    with pytest.raises(dataclasses.FrozenInstanceError):
        card.title = "nope"


def test_remote_column_and_comment_are_frozen():
    col = prov.RemoteColumn(ref="c1", name="Todo")
    with pytest.raises(dataclasses.FrozenInstanceError):
        col.name = "nope"
    com = prov.RemoteComment(
        ref="m1", author="doc", body_text="hi", created_at=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        com.author = "nope"


def test_remote_card_untriaged_has_no_column():
    assert _card().column_ref is None
    assert _card(column_ref="c9").column_ref == "c9"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def test_error_hierarchy():
    assert issubclass(prov.SyncAuthError, prov.SyncProviderError)
    assert issubclass(prov.SyncNotFoundError, prov.SyncProviderError)
    assert issubclass(prov.SyncRateLimitError, prov.SyncProviderError)
    assert issubclass(prov.SyncProviderError, RuntimeError)


def test_rate_limit_error_carries_retry_after():
    err = prov.SyncRateLimitError("slow down", retry_after=12.5)
    assert err.retry_after == 12.5
    assert prov.SyncRateLimitError("slow down").retry_after is None


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------

def test_provider_abc_is_not_instantiable():
    with pytest.raises(TypeError):
        prov.KanbanSyncProvider()  # abstract


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class _FakeProvider(prov.KanbanSyncProvider):
    def __init__(self, sync_cfg):
        self.sync_cfg = sync_cfg

    @property
    def name(self):
        return "fake"

    def is_available(self):
        return True

    def list_columns(self, board_ref):
        return []

    def create_column(self, board_ref, name):
        return prov.RemoteColumn(ref="c1", name=name)

    def list_changed_cards(self, board_ref, *, cursor):
        return [], cursor

    def get_card(self, card_ref):
        raise prov.SyncNotFoundError(card_ref)

    def create_card(self, board_ref, *, title, body_text):
        return _card(title=title, body_text=body_text)

    def update_card(self, card_ref, *, title=None, body_text=None):
        pass

    def move_card(self, card_ref, *, column_ref, closed=False, archived=False):
        pass

    def list_comments(self, card_ref, *, since_ref):
        return []

    def add_comment(self, card_ref, body_text):
        return "m1"


@pytest.fixture(autouse=True)
def _clean_registry():
    reg._reset_for_tests()
    yield
    reg._reset_for_tests()


def test_register_and_get_provider_passes_cfg_to_factory():
    reg.register_provider("fake", _FakeProvider)
    cfg = {"provider": "fake", "fizzy": {}}
    p = reg.get_provider("fake", cfg)
    assert isinstance(p, _FakeProvider)
    assert p.sync_cfg is cfg


def test_get_provider_unknown_name_returns_none():
    assert reg.get_provider("does-not-exist", {}) is None


def test_list_provider_names_sorted():
    reg.register_provider("zeta", _FakeProvider)
    reg.register_provider("alpha", _FakeProvider)
    names = reg.list_provider_names()
    assert names.index("alpha") < names.index("zeta")


def test_reregistration_overwrites():
    reg.register_provider("fake", _FakeProvider)

    class Other(_FakeProvider):
        pass

    reg.register_provider("fake", Other)
    assert isinstance(reg.get_provider("fake", {}), Other)


def test_register_rejects_blank_name():
    with pytest.raises(ValueError):
        reg.register_provider("  ", _FakeProvider)
