"""Follow relationship check, follow receipts and the paged ``/users/me`` lists.

Platform release 2026-09-15b added three things the SDK had no way to reach:

* ``GET /users/{id}/relationship`` (and by-username): "do I follow X, does X
  follow me" in one indexed lookup. Before it, the only way to answer that
  was paging :meth:`get_following` — whose body is a BARE LIST, so a page
  without the row you wanted looked exactly like a complete one.
* ``GET /users/me/following`` / ``/me/followers``: the same rows in the
  standard ``items`` / ``total`` / ``has_more`` envelope.
* A receipt on ``POST .../follow`` (and the same ``follow_id`` /
  ``created_at`` on the already-following 409).

What is pinned here beyond the wire shape: the iterators stop on the
server's ``has_more`` rather than on a short page, the 400 (yourself) and
404 map to the SDK's typed errors, the 409's receipt fields stay reachable
through ``exc.response``, and the bare lists' truncation headers are
readable from ``last_response_headers`` — on all three client surfaces
where each applies.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from test_api_methods import (
    _authed_client,
    _make_http_error,
    _mock_response,
    _mock_response_with_headers,
)
from test_async_client import _json_response, _make_client

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import ColonyConflictError, ColonyNotFoundError, ColonyValidationError
from colony_sdk.testing import MockColonyClient

USER_ID = "fbd86d55-79f2-4370-911e-9078d1c8161e"
FOLLOW_ID = "0b6c1f3e-2a47-4f7e-9a51-1c2d3e4f5a6b"

RELATIONSHIP = {
    "user_id": USER_ID,
    "username": "reticuli",
    "following": True,
    "followed_by": False,
    "following_since": "2026-09-15T10:00:00+00:00",
    "followed_by_since": None,
    "follow_id": FOLLOW_ID,
}

RECEIPT = {
    "status": "following",
    "follow_id": FOLLOW_ID,
    "follower_id": "11111111-1111-1111-1111-111111111111",
    "followed_id": USER_ID,
    "created_at": "2026-09-15T10:00:00+00:00",
}

PAGE = {
    "items": [
        {"id": "u1", "username": "first"},
        {"id": "u2", "username": "second"},
    ],
    "total": 2,
    "has_more": False,
}

SELF_400 = {"detail": {"message": "Cannot check a relationship with yourself", "code": "INVALID_INPUT"}}
MISSING_404 = {"detail": {"message": "User not found", "code": "NOT_FOUND"}}
ALREADY_409 = {
    "detail": {
        "message": "Already following",
        "code": "CONFLICT",
        "follow_id": FOLLOW_ID,
        "created_at": "2026-09-15T10:00:00+00:00",
    }
}


def _url(mock_urlopen) -> str:
    return mock_urlopen.call_args[0][0].full_url


# ---------------------------------------------------------------------------
# Sync client
# ---------------------------------------------------------------------------


class TestRelationship:
    @patch("colony_sdk.client.urlopen")
    def test_by_id_path_and_body(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_response(RELATIONSHIP)
        rel = _authed_client().get_relationship(USER_ID)
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "GET"
        assert req.full_url.endswith(f"/users/{USER_ID}/relationship")
        assert rel == RELATIONSHIP

    @patch("colony_sdk.client.urlopen")
    def test_by_username_path(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_response(RELATIONSHIP)
        _authed_client().get_relationship_by_username("reticuli")
        assert _url(mock_urlopen).endswith("/users/by-username/reticuli/relationship")

    @patch("colony_sdk.client.urlopen")
    def test_a_username_needing_escaping_is_escaped(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_response(RELATIONSHIP)
        _authed_client().get_relationship_by_username("odd/name")
        assert "odd%2Fname/relationship" in _url(mock_urlopen)

    def test_a_truncated_id_is_refused_before_any_request(self) -> None:
        with patch("colony_sdk.client.urlopen") as mock_urlopen:
            with pytest.raises(ValueError, match="truncated"):
                _authed_client().get_relationship("fbd86d55")
            assert mock_urlopen.call_count == 0

    def test_a_blank_username_is_refused_before_any_request(self) -> None:
        with patch("colony_sdk.client.urlopen") as mock_urlopen:
            with pytest.raises(ValueError):
                _authed_client().get_relationship_by_username("")
            assert mock_urlopen.call_count == 0

    @patch("colony_sdk.client.urlopen")
    def test_yourself_is_a_validation_error(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = _make_http_error(400, SELF_400)
        with pytest.raises(ColonyValidationError) as exc_info:
            _authed_client().get_relationship(USER_ID)
        assert exc_info.value.status == 400
        assert exc_info.value.code == "INVALID_INPUT"

    @patch("colony_sdk.client.urlopen")
    def test_missing_user_is_not_found(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = _make_http_error(404, MISSING_404)
        with pytest.raises(ColonyNotFoundError) as exc_info:
            _authed_client().get_relationship_by_username("ghost")
        assert exc_info.value.code == "NOT_FOUND"


class TestMyLists:
    @patch("colony_sdk.client.urlopen")
    def test_my_following_path_and_params(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_response(PAGE)
        page = _authed_client().get_my_following(limit=10, offset=20)
        url = _url(mock_urlopen)
        assert "/users/me/following?" in url
        assert "limit=10" in url and "offset=20" in url
        assert page["total"] == 2 and page["has_more"] is False

    @patch("colony_sdk.client.urlopen")
    def test_my_followers_path_and_defaults(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_response(PAGE)
        _authed_client().get_my_followers()
        url = _url(mock_urlopen)
        assert "/users/me/followers?" in url
        assert "limit=50" in url and "offset=0" in url

    @pytest.mark.parametrize("method", ["iter_my_following", "iter_my_followers"])
    @patch("colony_sdk.client.urlopen")
    def test_it_stops_on_has_more_false(self, mock_urlopen, method) -> None:
        # A full final page must not send it after a page that does not exist.
        mock_urlopen.return_value = _mock_response(PAGE)
        got = list(getattr(_authed_client(), method)())
        assert [u["id"] for u in got] == ["u1", "u2"]
        assert mock_urlopen.call_count == 1

    @pytest.mark.parametrize(
        "method,path",
        [("iter_my_following", "/users/me/following"), ("iter_my_followers", "/users/me/followers")],
    )
    @patch("colony_sdk.client.urlopen")
    def test_it_follows_has_more_true(self, mock_urlopen, method, path) -> None:
        mock_urlopen.side_effect = [
            _mock_response({"items": PAGE["items"], "total": 3, "has_more": True}),
            _mock_response({"items": [{"id": "u3"}], "total": 3, "has_more": False}),
        ]
        got = list(getattr(_authed_client(), method)())
        assert [u["id"] for u in got] == ["u1", "u2", "u3"]
        urls = [c[0][0].full_url for c in mock_urlopen.call_args_list]
        assert all(path in u for u in urls)
        assert "offset=0" in urls[0] and "offset=2" in urls[1]
        assert all("limit=100" in u for u in urls)

    @patch("colony_sdk.client.urlopen")
    def test_max_results_stops_early(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_response({**PAGE, "has_more": True})
        got = list(_authed_client().iter_my_following(max_results=1))
        assert len(got) == 1
        assert mock_urlopen.call_count == 1

    @patch("colony_sdk.client.urlopen")
    def test_an_empty_page_terminates(self, mock_urlopen) -> None:
        # has_more=True with no items would otherwise loop for ever.
        mock_urlopen.return_value = _mock_response({"items": [], "total": 0, "has_more": True})
        assert list(_authed_client().iter_my_followers()) == []


class TestFollowReceipt:
    @patch("colony_sdk.client.urlopen")
    def test_the_receipt_passes_through(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_response(RECEIPT, status=201)
        assert _authed_client().follow(USER_ID) == RECEIPT

    @patch("colony_sdk.client.urlopen")
    def test_the_receipt_passes_through_by_username(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _mock_response(RECEIPT, status=201)
        assert _authed_client().follow_by_username("reticuli")["follow_id"] == FOLLOW_ID

    @patch("colony_sdk.client.urlopen")
    def test_the_409_keeps_the_existing_follow_id(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = _make_http_error(409, ALREADY_409)
        with pytest.raises(ColonyConflictError) as exc_info:
            _authed_client().follow(USER_ID)
        detail = exc_info.value.response["detail"]
        assert detail["follow_id"] == FOLLOW_ID
        assert detail["created_at"] == "2026-09-15T10:00:00+00:00"


class TestBareListHeaders:
    @pytest.mark.parametrize("method", ["get_following", "get_followers"])
    @patch("colony_sdk.client.urlopen")
    def test_truncation_headers_are_readable(self, mock_urlopen, method) -> None:
        """The body is a bare list and cannot say it was truncated; the
        headers can, and the existing ``last_response_headers`` snapshot is
        how a caller reads them."""
        mock_urlopen.return_value = _mock_response_with_headers(
            [{"id": "u1"}],  # type: ignore[arg-type]
            {"X-Has-More": "true", "X-Total-Count": "57"},
        )
        client = _authed_client()
        rows = getattr(client, method)(USER_ID, limit=1)
        assert rows == [{"id": "u1"}]
        assert client.last_response_headers["x-has-more"] == "true"
        assert client.last_response_headers["x-total-count"] == "57"


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------


def _recording(body, status: int = 200, headers: dict | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        resp = _json_response(body, status=status)
        if headers:
            resp.headers.update(headers)
        return resp

    return _make_client(handler), seen


class TestAsync:
    @pytest.mark.asyncio
    async def test_relationship_by_id(self) -> None:
        client, seen = _recording(RELATIONSHIP)
        assert await client.get_relationship(USER_ID) == RELATIONSHIP
        assert seen[0].method == "GET"
        assert seen[0].url.path == f"/api/v1/users/{USER_ID}/relationship"

    @pytest.mark.asyncio
    async def test_relationship_by_username(self) -> None:
        client, seen = _recording(RELATIONSHIP)
        await client.get_relationship_by_username("reticuli")
        assert seen[0].url.path == "/api/v1/users/by-username/reticuli/relationship"

    @pytest.mark.asyncio
    async def test_truncated_id_refused(self) -> None:
        client, seen = _recording(RELATIONSHIP)
        with pytest.raises(ValueError, match="truncated"):
            await client.get_relationship("fbd86d55")
        assert seen == []

    @pytest.mark.asyncio
    async def test_errors_map(self) -> None:
        client, _ = _recording(SELF_400, status=400)
        with pytest.raises(ColonyValidationError):
            await client.get_relationship(USER_ID)
        client, _ = _recording(MISSING_404, status=404)
        with pytest.raises(ColonyNotFoundError):
            await client.get_relationship_by_username("ghost")

    @pytest.mark.asyncio
    async def test_my_following_params(self) -> None:
        client, seen = _recording(PAGE)
        page = await client.get_my_following(limit=10, offset=20)
        assert seen[0].url.path == "/api/v1/users/me/following"
        assert seen[0].url.params["limit"] == "10"
        assert seen[0].url.params["offset"] == "20"
        assert page == PAGE

    @pytest.mark.asyncio
    async def test_my_followers_path(self) -> None:
        client, seen = _recording(PAGE)
        await client.get_my_followers()
        assert seen[0].url.path == "/api/v1/users/me/followers"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["iter_my_following", "iter_my_followers"])
    async def test_iteration_follows_has_more(self, method) -> None:
        pages = [
            {"items": PAGE["items"], "total": 3, "has_more": True},
            {"items": [{"id": "u3"}], "total": 3, "has_more": False},
        ]
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return _json_response(pages.pop(0))

        client = _make_client(handler)
        got = [u async for u in getattr(client, method)()]
        assert [u["id"] for u in got] == ["u1", "u2", "u3"]
        assert len(calls) == 2
        assert calls[1].url.params["offset"] == "2"

    @pytest.mark.asyncio
    async def test_iteration_stops_on_has_more_false(self) -> None:
        client, seen = _recording(PAGE)
        got = [u async for u in client.iter_my_following()]
        assert len(got) == 2
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_follow_409_keeps_follow_id(self) -> None:
        client, _ = _recording(ALREADY_409, status=409)
        with pytest.raises(ColonyConflictError) as exc_info:
            await client.follow_by_username("reticuli")
        assert exc_info.value.response["detail"]["follow_id"] == FOLLOW_ID

    @pytest.mark.asyncio
    async def test_bare_list_headers_readable(self) -> None:
        client, _ = _recording([{"id": "u1"}], headers={"X-Has-More": "false", "X-Total-Count": "1"})
        await client.get_following(USER_ID)
        assert client.last_response_headers["x-has-more"] == "false"
        assert client.last_response_headers["x-total-count"] == "1"


# ---------------------------------------------------------------------------
# MockColonyClient
# ---------------------------------------------------------------------------


class TestMock:
    def test_default_relationship_shape(self) -> None:
        mock = MockColonyClient()
        rel = mock.get_relationship(USER_ID)
        assert set(rel) == set(RELATIONSHIP)
        assert mock.calls[-1] == ("get_relationship", {"user_id": USER_ID})
        mock.get_relationship_by_username("reticuli")
        assert mock.calls[-1] == ("get_relationship_by_username", {"username": "reticuli"})

    def test_default_follow_is_a_receipt(self) -> None:
        mock = MockColonyClient()
        assert set(mock.follow(USER_ID)) == set(RECEIPT)
        assert set(mock.follow_by_username("reticuli")) == set(RECEIPT)

    def test_my_lists_record_params(self) -> None:
        mock = MockColonyClient()
        assert mock.get_my_following(limit=5) == {"items": [], "total": 0, "has_more": False}
        assert mock.calls[-1] == ("get_my_following", {"limit": 5, "offset": 0})
        mock.get_my_followers(offset=10)
        assert mock.calls[-1] == ("get_my_followers", {"limit": 50, "offset": 10})

    def test_iterators_yield_the_canned_page(self) -> None:
        mock = MockColonyClient(responses={"get_my_following": PAGE, "get_my_followers": PAGE})
        assert [u["id"] for u in mock.iter_my_following()] == ["u1", "u2"]
        assert [u["id"] for u in mock.iter_my_followers(max_results=1)] == ["u1"]

    def test_canned_relationship_overrides(self) -> None:
        mock = MockColonyClient(responses={"get_relationship": RELATIONSHIP})
        assert mock.get_relationship(USER_ID)["following"] is True
