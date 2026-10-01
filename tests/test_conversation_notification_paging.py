"""DM conversations and notifications can be read past the first page.

``GET /messages/conversations`` and ``GET /notifications`` return a bare JSON
array with no total, no cursor and no ``has_more``, and each serves 50 rows by
default. ``list_conversations()`` sent no parameters at all, so an account
with 90 conversations got the newest 50 and nothing said there were more
(measured on the live API, 2026-09-28). ``get_notifications()`` took a
``limit`` but no ``offset``, so it could not get past its first page either.

These tests pin the fix on all three surfaces: the parameters reach the wire
only when given (a bare call sends exactly the old request), and the new
iterators walk ``limit``/``offset`` to the end, drop a row repeated when the
list shifts mid-walk, and refuse to loop when the server ignores ``offset``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import ColonyAPIError, ColonyClient
from colony_sdk.async_client import AsyncColonyClient
from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"


def _rows(start: int, stop: int) -> list[dict]:
    return [{"id": f"row-{i}"} for i in range(start, stop)]


# ---------------------------------------------------------------------------
# Sync client
# ---------------------------------------------------------------------------


def _mock_response(data: object) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode()
    resp.status = 200
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _client() -> ColonyClient:
    client = ColonyClient("col_test")
    client._token = "fake-jwt"
    client._token_expiry = time.time() + 9999
    return client


def _urls(mock_urlopen: MagicMock) -> list[str]:
    return [c[0][0].full_url for c in mock_urlopen.call_args_list]


def _qs(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


class TestSyncListConversations:
    @patch("colony_sdk.client.urlopen")
    def test_bare_call_sends_the_old_request(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([])
        _client().list_conversations()
        assert _urls(mock_urlopen) == [f"{BASE}/messages/conversations"]

    @patch("colony_sdk.client.urlopen")
    def test_paging_and_archive_params_reach_the_wire(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([])
        _client().list_conversations(limit=100, offset=50, include_archived=True)
        qs = _qs(_urls(mock_urlopen)[0])
        assert qs == {"limit": ["100"], "offset": ["50"], "include_archived": ["true"]}

    @patch("colony_sdk.client.urlopen")
    def test_offset_zero_is_sent_not_dropped(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([])
        _client().list_conversations(offset=0)
        assert _qs(_urls(mock_urlopen)[0]) == {"offset": ["0"]}


class TestSyncGetNotificationsOffset:
    @patch("colony_sdk.client.urlopen")
    def test_offset_absent_unless_given(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([])
        client = _client()
        client.get_notifications()
        client.get_notifications(unread_only=True, limit=10, offset=10)
        first, second = (_qs(u) for u in _urls(mock_urlopen))
        assert "offset" not in first
        assert second == {"limit": ["10"], "unread_only": ["true"], "offset": ["10"]}


class TestSyncIterConversations:
    @patch("colony_sdk.client.urlopen")
    def test_walks_every_page_until_a_short_one(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = [
            _mock_response(_rows(0, 50)),
            _mock_response(_rows(50, 100)),
            _mock_response(_rows(100, 107)),
        ]
        got = list(_client().iter_conversations())
        assert [r["id"] for r in got] == [f"row-{i}" for i in range(107)]
        assert [_qs(u)["offset"] for u in _urls(mock_urlopen)] == [["0"], ["50"], ["100"]]
        assert all(_qs(u)["limit"] == ["50"] for u in _urls(mock_urlopen))

    @patch("colony_sdk.client.urlopen")
    def test_a_full_last_page_is_confirmed_by_an_empty_one(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = [_mock_response(_rows(0, 50)), _mock_response([])]
        assert len(list(_client().iter_conversations())) == 50
        assert mock_urlopen.call_count == 2

    @patch("colony_sdk.client.urlopen")
    def test_a_row_repeated_by_a_shift_is_yielded_once(self, mock_urlopen: MagicMock) -> None:
        # A DM arrived mid-walk: everything slid down one, so page two starts
        # with the last row of page one.
        mock_urlopen.side_effect = [_mock_response(_rows(0, 50)), _mock_response(_rows(49, 60))]
        ids = [r["id"] for r in _client().iter_conversations()]
        assert ids == [f"row-{i}" for i in range(60)]

    @patch("colony_sdk.client.urlopen")
    def test_raises_when_the_server_ignores_offset(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = lambda *a, **k: _mock_response(_rows(0, 50))
        with pytest.raises(ColonyAPIError, match="not honouring offset"):
            list(_client().iter_conversations())
        assert mock_urlopen.call_count == 2

    @patch("colony_sdk.client.urlopen")
    def test_max_results_stops_without_fetching_another_page(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = [_mock_response(_rows(0, 50)), _mock_response(_rows(50, 100))]
        assert len(list(_client().iter_conversations(max_results=50))) == 50
        assert mock_urlopen.call_count == 1

    @patch("colony_sdk.client.urlopen")
    def test_max_results_zero_sends_nothing(self, mock_urlopen: MagicMock) -> None:
        assert list(_client().iter_conversations(max_results=0)) == []
        assert mock_urlopen.call_count == 0

    @patch("colony_sdk.client.urlopen")
    def test_an_envelope_raises_instead_of_reading_as_empty(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": [], "total": 0})
        with pytest.raises(ColonyAPIError, match="expected a JSON array"):
            list(_client().iter_conversations())

    @patch("colony_sdk.client.urlopen")
    def test_page_size_and_archive_flag_are_sent(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([])
        list(_client().iter_conversations(page_size=100, include_archived=True))
        assert _qs(_urls(mock_urlopen)[0]) == {"limit": ["100"], "offset": ["0"], "include_archived": ["true"]}


class TestSyncIterNotifications:
    @patch("colony_sdk.client.urlopen")
    def test_walks_pages_with_unread_filter(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = [_mock_response(_rows(0, 10)), _mock_response(_rows(10, 13))]
        got = list(_client().iter_notifications(unread_only=True, page_size=10))
        assert len(got) == 13
        first, second = (_qs(u) for u in _urls(mock_urlopen))
        assert first == {"limit": ["10"], "unread_only": ["true"], "offset": ["0"]}
        assert second["offset"] == ["10"]

    @patch("colony_sdk.client.urlopen")
    def test_raises_when_the_server_ignores_offset(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = lambda *a, **k: _mock_response(_rows(0, 10))
        with pytest.raises(ColonyAPIError, match=r"iter_notifications.*not honouring offset"):
            list(_client().iter_notifications(page_size=10))


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------


def _async_client(pages: list[object]) -> tuple[AsyncColonyClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = pages[min(len(seen), len(pages)) - 1]
        return httpx.Response(200, content=json.dumps(body).encode())

    client = AsyncColonyClient("col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client._token = "fake-jwt"
    client._token_expiry = 9_999_999_999
    return client, seen


def _aqs(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.url.query.decode())


@pytest.mark.asyncio
class TestAsync:
    async def test_bare_list_conversations_sends_the_old_request(self) -> None:
        client, seen = _async_client([[]])
        await client.list_conversations()
        assert str(seen[0].url) == f"{BASE}/messages/conversations"

    async def test_list_conversations_params(self) -> None:
        client, seen = _async_client([[]])
        await client.list_conversations(limit=100, offset=50, include_archived=True)
        assert _aqs(seen[0]) == {"limit": ["100"], "offset": ["50"], "include_archived": ["true"]}

    async def test_get_notifications_offset(self) -> None:
        client, seen = _async_client([[]])
        await client.get_notifications()
        await client.get_notifications(limit=10, offset=10)
        assert "offset" not in _aqs(seen[0])
        assert _aqs(seen[1]) == {"limit": ["10"], "offset": ["10"]}

    async def test_iter_conversations_walks_and_dedupes(self) -> None:
        client, seen = _async_client([_rows(0, 50), _rows(49, 60)])
        ids = [r["id"] async for r in client.iter_conversations()]
        assert ids == [f"row-{i}" for i in range(60)]
        assert [_aqs(r)["offset"] for r in seen] == [["0"], ["50"]]

    async def test_iter_conversations_raises_when_offset_ignored(self) -> None:
        client, seen = _async_client([_rows(0, 50)])
        with pytest.raises(ColonyAPIError, match="not honouring offset"):
            async for _ in client.iter_conversations():
                pass
        assert len(seen) == 2

    async def test_iter_notifications_max_results(self) -> None:
        client, seen = _async_client([_rows(0, 10), _rows(10, 20)])
        got = [r async for r in client.iter_notifications(unread_only=True, page_size=10, max_results=10)]
        assert len(got) == 10
        assert len(seen) == 1
        assert _aqs(seen[0]) == {"limit": ["10"], "unread_only": ["true"], "offset": ["0"]}

    async def test_iter_max_results_zero_sends_nothing(self) -> None:
        client, seen = _async_client([_rows(0, 10)])
        assert [r async for r in client.iter_conversations(max_results=0)] == []
        assert [r async for r in client.iter_notifications(max_results=0)] == []
        assert seen == []

    async def test_iter_notifications_envelope_raises(self) -> None:
        client, _ = _async_client([{"items": []}])
        with pytest.raises(ColonyAPIError, match="expected a JSON array"):
            async for _ in client.iter_notifications():
                pass


# ---------------------------------------------------------------------------
# MockColonyClient
# ---------------------------------------------------------------------------


class TestMock:
    def test_bare_calls_record_what_they_always_recorded(self) -> None:
        mock = MockColonyClient()
        mock.list_conversations()
        mock.get_notifications()
        assert mock.calls == [
            ("list_conversations", {}),
            ("get_notifications", {"unread_only": False, "limit": 50}),
        ]

    def test_new_params_are_recorded_when_given(self) -> None:
        mock = MockColonyClient()
        mock.list_conversations(limit=5, offset=10, include_archived=True)
        mock.get_notifications(offset=3)
        assert mock.calls == [
            ("list_conversations", {"limit": 5, "offset": 10, "include_archived": True}),
            ("get_notifications", {"unread_only": False, "limit": 50, "offset": 3}),
        ]

    def test_iterators_yield_a_canned_bare_list(self) -> None:
        mock = MockColonyClient(responses={"list_conversations": _rows(0, 3), "get_notifications": _rows(0, 4)})
        assert len(list(mock.iter_conversations())) == 3
        assert len(list(mock.iter_notifications(unread_only=True, max_results=2))) == 2
        assert mock.calls == [
            ("iter_conversations", {"page_size": 50, "max_results": None, "include_archived": False}),
            ("iter_notifications", {"unread_only": True, "page_size": 50, "max_results": 2}),
        ]

    def test_iterators_accept_the_default_envelopes(self) -> None:
        mock = MockColonyClient()
        assert list(mock.iter_conversations()) == []
        assert list(mock.iter_notifications()) == []
        canned = MockColonyClient(responses={"get_notifications": {"items": _rows(0, 2), "total": 2}})
        assert len(list(canned.iter_notifications())) == 2
