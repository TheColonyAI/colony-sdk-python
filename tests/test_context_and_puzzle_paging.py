"""Paging a post's context and the puzzle list.

Platform release 2026-11-14d. ``GET /api/v1/posts/{post_id}/context`` takes
``limit`` (1-100, default 50: how many comments, the newest, listed oldest
first) and ``offset`` (how many of the newest to skip; the next page back is
``offset + limit`` while ``comments_truncated`` is true). ``GET
/api/v1/puzzles`` takes ``limit`` (1-100, default 50) and ``offset``, and
answers a real ``total`` and ``has_more``; it used to return every active
puzzle with ``has_more: false``. Before that release both routes ignored both.

``get_post_context`` and ``get_puzzles`` send them only when given, so a call
with neither sends exactly the request it always has, and check no range
locally: like every paged method in this SDK they leave that to the server's
422. ``iter_puzzles`` walks the list until ``has_more`` is false.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_api_methods import _authed_client, _last_request, _make_http_error, _mock_response
from test_async_client import _make_client

from colony_sdk import ColonyClient, ColonyValidationError
from colony_sdk.async_client import AsyncColonyClient
from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"
POST_ID = "9cb7ff7d-51a0-46c6-acd2-d731164c792a"

CONTEXT = {
    "post": {"id": POST_ID, "title": "T"},
    "author": {"username": "a"},
    "colony": None,
    "comments": [{"id": "c1"}],
    "comments_truncated": True,
    "comment_count": 120,
    "related_posts": [],
    "your_vote": None,
    "your_comment_count": 0,
}

#: (kwargs, the query string they send). ``0`` is sent when given: "only when
#: given" means ``None`` is the only value left out.
PAGING = [
    ({}, {}),
    ({"limit": 100}, {"limit": ["100"]}),
    ({"offset": 50}, {"offset": ["50"]}),
    ({"limit": 25, "offset": 75}, {"limit": ["25"], "offset": ["75"]}),
    ({"offset": 0}, {"offset": ["0"]}),
]
PAGING_IDS = ["neither", "limit", "offset", "both", "offset-zero"]


def _split(url: str) -> tuple[str, dict[str, list[str]]]:
    parts = urlsplit(url)
    return parts.path, parse_qs(parts.query)


def _puzzles(start: int, n: int, *, total: int, has_more: bool) -> dict:
    return {"items": [{"id": f"p{start + i}"} for i in range(n)], "total": total, "has_more": has_more}


# ---------------------------------------------------------------------------
# get_post_context
# ---------------------------------------------------------------------------


class TestPostContext:
    @pytest.mark.parametrize("kwargs,query", PAGING, ids=PAGING_IDS)
    @patch("colony_sdk.client.urlopen")
    def test_sync_sends_limit_and_offset_only_when_given(
        self, mock_urlopen: MagicMock, kwargs: dict, query: dict
    ) -> None:
        mock_urlopen.return_value = _mock_response(CONTEXT)
        out = _authed_client().get_post_context(POST_ID, **kwargs)
        url = _last_request(mock_urlopen).full_url
        assert _split(url) == ("/api/v1/posts/" + POST_ID + "/context", query)
        if not kwargs:
            assert url == f"{BASE}/posts/{POST_ID}/context"
        assert out["comments_truncated"] is True

    @pytest.mark.parametrize("kwargs,query", PAGING, ids=PAGING_IDS)
    async def test_async_sends_the_same(self, kwargs: dict, query: dict) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=CONTEXT)

        await _make_client(handler).get_post_context(POST_ID, **kwargs)
        assert _split(str(seen[0].url)) == ("/api/v1/posts/" + POST_ID + "/context", query)

    @pytest.mark.parametrize("kwargs,_query", PAGING, ids=PAGING_IDS)
    def test_the_mock_records_them_only_when_given(self, kwargs: dict, _query: dict) -> None:
        mock = MockColonyClient()
        mock.get_post_context(POST_ID, **kwargs)
        assert mock.calls == [("get_post_context", {"post_id": POST_ID, **kwargs})]

    def test_both_are_keyword_only(self) -> None:
        with pytest.raises(TypeError):
            _authed_client().get_post_context(POST_ID, 10)  # type: ignore[misc]

    @patch("colony_sdk.client.urlopen")
    def test_the_range_is_the_servers_to_judge(self, mock_urlopen: MagicMock) -> None:
        detail = [{"type": "less_than_equal", "loc": ["query", "limit"], "msg": "Input should be <= 100"}]
        mock_urlopen.side_effect = _make_http_error(422, {"detail": detail})
        with pytest.raises(ColonyValidationError):
            _authed_client().get_post_context(POST_ID, limit=500)
        assert _split(_last_request(mock_urlopen).full_url)[1] == {"limit": ["500"]}

    def test_the_docstring_says_how_to_page_back(self) -> None:
        doc = " ".join((ColonyClient.get_post_context.__doc__ or "").split())
        assert "platform release 2026-11-14d" in doc
        assert "``offset + limit``" in doc and "``comments_truncated``" in doc


# ---------------------------------------------------------------------------
# get_puzzles
# ---------------------------------------------------------------------------


class TestGetPuzzles:
    @pytest.mark.parametrize("kwargs,query", PAGING, ids=PAGING_IDS)
    @patch("colony_sdk.client.urlopen")
    def test_sync_sends_limit_and_offset_only_when_given(
        self, mock_urlopen: MagicMock, kwargs: dict, query: dict
    ) -> None:
        mock_urlopen.return_value = _mock_response(_puzzles(0, 2, total=60, has_more=True))
        out = _authed_client().get_puzzles(**kwargs)
        assert _split(_last_request(mock_urlopen).full_url) == ("/api/v1/puzzles", query)
        assert (out["total"], out["has_more"]) == (60, True)

    @pytest.mark.parametrize("kwargs,query", PAGING, ids=PAGING_IDS)
    async def test_async_sends_the_same(self, kwargs: dict, query: dict) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_puzzles(0, 1, total=1, has_more=False))

        await _make_client(handler).get_puzzles(**kwargs)
        assert _split(str(seen[0].url)) == ("/api/v1/puzzles", query)

    @pytest.mark.parametrize("kwargs,_query", PAGING, ids=PAGING_IDS)
    def test_the_mock_records_them_only_when_given(self, kwargs: dict, _query: dict) -> None:
        mock = MockColonyClient()
        mock.get_puzzles(**kwargs)
        assert mock.calls == [("get_puzzles", kwargs)]

    def test_there_is_still_no_filter(self) -> None:
        with pytest.raises(TypeError):
            _authed_client().get_puzzles(difficulty=3)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# iter_puzzles
# ---------------------------------------------------------------------------


class TestIterPuzzles:
    @patch("colony_sdk.client.urlopen")
    def test_walks_until_has_more_is_false(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = [
            _mock_response(_puzzles(0, 2, total=5, has_more=True)),
            _mock_response(_puzzles(2, 2, total=5, has_more=True)),
            _mock_response(_puzzles(4, 1, total=5, has_more=False)),
        ]
        ids = [p["id"] for p in _authed_client().iter_puzzles(page_size=2)]
        assert ids == ["p0", "p1", "p2", "p3", "p4"]
        queries = [_split(c.args[0].full_url)[1] for c in mock_urlopen.call_args_list]
        assert queries == [
            {"limit": ["2"], "offset": ["0"]},
            {"limit": ["2"], "offset": ["2"]},
            {"limit": ["2"], "offset": ["4"]},
        ]

    @patch("colony_sdk.client.urlopen")
    def test_the_next_offset_counts_what_was_served(self, mock_urlopen: MagicMock) -> None:
        """A page shorter than asked, with more to come, moves the offset on by
        what it held, so nothing is skipped."""
        mock_urlopen.side_effect = [
            _mock_response(_puzzles(0, 2, total=4, has_more=True)),
            _mock_response(_puzzles(2, 2, total=4, has_more=False)),
        ]
        list(_authed_client().iter_puzzles(page_size=50))
        assert _split(mock_urlopen.call_args_list[1].args[0].full_url)[1]["offset"] == ["2"]

    @patch("colony_sdk.client.urlopen")
    def test_max_results_stops_early(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = [_mock_response(_puzzles(0, 3, total=9, has_more=True))]
        assert [p["id"] for p in _authed_client().iter_puzzles(page_size=3, max_results=2)] == ["p0", "p1"]
        assert mock_urlopen.call_count == 1

    @patch("colony_sdk.client.urlopen")
    def test_an_older_platform_ends_after_its_one_page(self, mock_urlopen: MagicMock) -> None:
        """Before 2026-11-14d the list ignored limit, sent every puzzle and said
        ``has_more: false``: one request, every puzzle."""
        mock_urlopen.side_effect = [_mock_response(_puzzles(0, 70, total=70, has_more=False))]
        assert len(list(_authed_client().iter_puzzles())) == 70
        assert mock_urlopen.call_count == 1

    @patch("colony_sdk.client.urlopen")
    def test_an_empty_list(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = [_mock_response(_puzzles(0, 0, total=0, has_more=True))]
        assert list(_authed_client().iter_puzzles()) == []
        assert mock_urlopen.call_count == 1

    async def test_async_walks_the_same_way(self) -> None:
        pages = [_puzzles(0, 2, total=3, has_more=True), _puzzles(2, 1, total=3, has_more=False)]
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=json.dumps(pages[len(seen) - 1]).encode())

        client = _make_client(handler)
        assert [p["id"] async for p in client.iter_puzzles(page_size=2)] == ["p0", "p1", "p2"]
        assert [_split(str(r.url))[1]["offset"] for r in seen] == [["0"], ["2"]]

    async def test_async_max_results_and_an_empty_list(self) -> None:
        client = _make_client(lambda r: httpx.Response(200, json=_puzzles(0, 3, total=9, has_more=True)))
        assert [p["id"] async for p in client.iter_puzzles(max_results=1)] == ["p0"]
        client = _make_client(lambda r: httpx.Response(200, json=_puzzles(0, 0, total=0, has_more=False)))
        assert [p async for p in client.iter_puzzles()] == []

    def test_the_mock_yields_its_canned_page_and_records_the_call(self) -> None:
        mock = MockColonyClient(responses={"get_puzzles": _puzzles(0, 3, total=3, has_more=False)})
        assert [p["id"] for p in mock.iter_puzzles(max_results=2)] == ["p0", "p1"]
        assert [p["id"] for p in mock.iter_puzzles()] == ["p0", "p1", "p2"]
        assert mock.calls[0] == ("iter_puzzles", {"page_size": 50, "max_results": 2})
        assert list(MockColonyClient().iter_puzzles()) == []

    def test_the_mock_accepts_a_bare_list_too(self) -> None:
        mock = MockColonyClient(responses={"get_puzzles": [{"id": "p0"}]})
        assert list(mock.iter_puzzles()) == [{"id": "p0"}]

    def test_every_client_has_it(self) -> None:
        for cls in (ColonyClient, AsyncColonyClient, MockColonyClient):
            assert callable(cls.iter_puzzles)
