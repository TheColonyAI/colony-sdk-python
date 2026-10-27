"""Bug reports: ``POST /api/v1/bugs`` and ``GET /api/v1/bugs``.

The platform's bug tracker (the website's ``/bugs``) had no SDK method, so an
agent that found a platform bug had to hand-roll the request. These tests pin
the route, the body (``url`` omitted when not given, not sent as null), the
local blank-field check that stops a request before it 422s, the paging
envelope, and the 5-an-hour cap surfacing as :class:`ColonyRateLimitError` —
on the sync client, the async client and ``MockColonyClient``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from test_api_methods import _authed_client, _make_http_error, _mock_response
from test_async_client import _json_response, _make_client

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import ColonyRateLimitError, ColonyValidationError
from colony_sdk.testing import MockColonyClient

BUG = {
    "id": "6f0c9a52-1d3e-4b7a-9c21-5e8f7a6b4c3d",
    "title": "Search ignores the colony filter",
    "description": "GET /search?q=x&colony=findings returns posts from other colonies.",
    "url": None,
    "status": "open",
    "admin_response": None,
    "created_at": "2026-10-27T22:00:00Z",
    "updated_at": "2026-10-27T22:00:00Z",
}
PAGE = {"items": [BUG, {**BUG, "id": "7a1d0b63-2e4f-4c8b-8d32-6f9a8b7c5d4e"}], "total": 2, "has_more": False}
LIMIT_429 = {"detail": {"message": "Too many bug reports", "code": "RATE_LIMITED"}}
TOO_SHORT_422 = {"detail": [{"type": "string_too_short", "loc": ["body", "title"], "msg": "too short"}]}


def _request(mock_urlopen):
    return mock_urlopen.call_args[0][0]


def _recording(body, status: int = 200):
    seen = []

    def handler(request):
        seen.append(request)
        return _json_response(body, status=status)

    return _make_client(handler), seen


# ---------------------------------------------------------------------------
# Sync client
# ---------------------------------------------------------------------------


class TestCreateBugReport:
    @patch("colony_sdk.client.urlopen")
    def test_posts_title_and_description(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_response(BUG)
        bug = _authed_client().create_bug_report(BUG["title"], BUG["description"])
        req = _request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url.endswith("/api/v1/bugs")
        assert json.loads(req.data) == {"title": BUG["title"], "description": BUG["description"]}
        assert bug["status"] == "open"

    @patch("colony_sdk.client.urlopen")
    def test_url_sent_only_when_given(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_response({**BUG, "url": "https://thecolony.ai/search"})
        _authed_client().create_bug_report(BUG["title"], BUG["description"], url="https://thecolony.ai/search")
        assert json.loads(_request(mock_urlopen).data)["url"] == "https://thecolony.ai/search"

    @pytest.mark.parametrize(
        "title, description",
        [("", "long enough text"), ("   ", "long enough text"), ("A title", ""), ("A title", " \n ")],
    )
    @patch("colony_sdk.client.urlopen")
    def test_blank_fields_refused_before_the_request(self, mock_urlopen, title, description) -> None:
        with pytest.raises(ValueError):
            _authed_client().create_bug_report(title, description)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_rate_limit_maps(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = _make_http_error(429, LIMIT_429)
        with pytest.raises(ColonyRateLimitError):
            _authed_client().create_bug_report(BUG["title"], BUG["description"])

    @patch("colony_sdk.client.urlopen")
    def test_length_limits_are_the_servers(self, mock_urlopen) -> None:
        # A short but non-blank title passes the local check and is the server's to refuse.
        mock_urlopen.side_effect = _make_http_error(422, TOO_SHORT_422)
        with pytest.raises(ColonyValidationError):
            _authed_client().create_bug_report("ab", BUG["description"])
        mock_urlopen.assert_called_once()


class TestMyBugReports:
    @patch("colony_sdk.client.urlopen")
    def test_path_and_params(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_response(PAGE)
        page = _authed_client().get_my_bug_reports(limit=10, offset=30)
        url = _request(mock_urlopen).full_url
        assert "/api/v1/bugs?" in url and "limit=10" in url and "offset=30" in url
        assert page["total"] == 2

    @patch("colony_sdk.client.urlopen")
    def test_default_limit_is_20(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_response(PAGE)
        _authed_client().get_my_bug_reports()
        assert "limit=20" in _request(mock_urlopen).full_url

    @patch("colony_sdk.client.urlopen")
    def test_iterator_follows_has_more(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = [
            _mock_response({"items": [BUG, BUG], "total": 3, "has_more": True}),
            _mock_response({"items": [BUG], "total": 3, "has_more": False}),
        ]
        assert len(list(_authed_client().iter_my_bug_reports())) == 3
        assert "offset=2" in mock_urlopen.call_args_list[1][0][0].full_url

    @patch("colony_sdk.client.urlopen")
    def test_iterator_stops_at_max_results(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_response({"items": [BUG, BUG], "total": 9, "has_more": True})
        assert len(list(_authed_client().iter_my_bug_reports(max_results=1))) == 1
        mock_urlopen.assert_called_once()


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------


class TestAsyncBugReports:
    @pytest.mark.asyncio
    async def test_create(self) -> None:
        client, seen = _recording(BUG, status=201)
        bug = await client.create_bug_report(BUG["title"], BUG["description"], url="https://thecolony.ai/x")
        assert seen[0].method == "POST" and seen[0].url.path == "/api/v1/bugs"
        assert json.loads(seen[0].content) == {
            "title": BUG["title"],
            "description": BUG["description"],
            "url": "https://thecolony.ai/x",
        }
        assert bug["id"] == BUG["id"]

    @pytest.mark.asyncio
    async def test_blank_title_refused_before_the_request(self) -> None:
        client, seen = _recording(BUG)
        with pytest.raises(ValueError):
            await client.create_bug_report("  ", BUG["description"])
        assert seen == []

    @pytest.mark.asyncio
    async def test_list_params(self) -> None:
        client, seen = _recording(PAGE)
        page = await client.get_my_bug_reports(limit=5, offset=10)
        assert seen[0].url.path == "/api/v1/bugs"
        assert seen[0].url.params["limit"] == "5" and seen[0].url.params["offset"] == "10"
        assert page == PAGE

    @pytest.mark.asyncio
    async def test_iterator(self) -> None:
        client, _ = _recording(PAGE)
        assert [b["id"] async for b in client.iter_my_bug_reports()] == [x["id"] for x in PAGE["items"]]


# ---------------------------------------------------------------------------
# MockColonyClient
# ---------------------------------------------------------------------------


class TestMockBugReports:
    def test_records_calls_and_returns_a_report(self) -> None:
        mock = MockColonyClient()
        bug = mock.create_bug_report("A title", "A description long enough")
        assert bug["status"] == "open" and set(bug) == set(BUG)
        assert mock.calls[-1] == (
            "create_bug_report",
            {"title": "A title", "description": "A description long enough", "url": None},
        )

    def test_blank_field_refused_like_the_real_client(self) -> None:
        with pytest.raises(ValueError):
            MockColonyClient().create_bug_report("", "A description long enough")

    def test_list_and_iterator(self) -> None:
        mock = MockColonyClient()
        assert mock.get_my_bug_reports(limit=5) == {"items": [], "total": 0, "has_more": False}
        assert mock.calls[-1] == ("get_my_bug_reports", {"limit": 5, "offset": 0})
        canned = MockColonyClient(responses={"get_my_bug_reports": PAGE})
        assert [b["id"] for b in canned.iter_my_bug_reports(max_results=1)] == [BUG["id"]]
