"""Deprecated names reported by the platform, and renamed response fields.

Three platform facts this file pins the SDK's handling of:

1. A REST request that used a deprecated query parameter gets a response
   header ``X-Colony-Deprecated-Params: <sent>=<preferred>, ...``. The client
   turns it into one :class:`ColonyDeprecationWarning` per pair, once per
   (route, param) per client instance.
2. Renamed response fields are sent under BOTH names, but only by servers
   carrying the rename; older ones send only the old name. Anything the SDK
   reads must prefer the new name and fall back to the old.
3. ``GET /api/v1/deprecations`` lists every deprecated name.

Sync tests mock ``urlopen`` and async tests use ``httpx.MockTransport``, as
the rest of the unit suite does.
"""

from __future__ import annotations

import json
import sys
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import colony_sdk
from colony_sdk import ColonyClient, ColonyDeprecationWarning, ColonyNotFoundError
from colony_sdk.client import _parse_deprecated_params, _parse_deprecated_values
from colony_sdk.models import Echo
from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"
HEADER = "X-Colony-Deprecated-Params"
VALUES_HEADER = "X-Colony-Deprecated-Values"
COLONY_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
COLONY_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ECHO_ID = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(data: dict | list | None = None, headers: dict[str, str] | None = None) -> MagicMock:
    """A mock urllib response (a context manager) carrying ``headers``."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(data if data is not None else {}).encode()
    resp.status = 200
    resp.getheaders.return_value = list((headers or {}).items())
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_http_error(code: int, headers: dict[str, str]) -> Exception:
    import io
    from urllib.error import HTTPError

    err = HTTPError(url="http://test", code=code, msg="error", hdrs=MagicMock(), fp=io.BytesIO(b"{}"))
    err.headers.get = lambda key, default=None, _h=headers: _h.get(key, default)  # type: ignore[method-assign]
    return err


def _authed_client() -> ColonyClient:
    client = ColonyClient("col_test")
    client._token = "fake-jwt"
    client._token_expiry = time.time() + 9999
    return client


@contextmanager
def _recorded() -> Iterator[list[warnings.WarningMessage]]:
    """Record every warning. ``always`` defeats the ``warnings`` module's own
    once-per-location registry, so any dedupe observed is the client's."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield caught


def _colony_msgs(caught: list[warnings.WarningMessage]) -> list[str]:
    return [str(w.message) for w in caught if issubclass(w.category, ColonyDeprecationWarning)]


# ---------------------------------------------------------------------------
# The warning class
# ---------------------------------------------------------------------------


class TestWarningClass:
    def test_exported_from_the_package_root(self) -> None:
        assert "ColonyDeprecationWarning" in colony_sdk.__all__
        assert colony_sdk.ColonyDeprecationWarning is ColonyDeprecationWarning

    def test_is_a_deprecation_warning(self) -> None:
        """So ``-W error::DeprecationWarning`` and existing DeprecationWarning
        filters cover it, while it can still be filtered on its own."""
        assert issubclass(ColonyDeprecationWarning, DeprecationWarning)


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


class TestParseHeader:
    def test_the_documented_shape(self) -> None:
        assert _parse_deprecated_params("colony_name=colony, search=q") == [("colony_name", "colony"), ("search", "q")]

    def test_spaces_are_stripped(self) -> None:
        assert _parse_deprecated_params("  colony_name =  colony ,search= q  ") == [
            ("colony_name", "colony"),
            ("search", "q"),
        ]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", []),
            (" ", []),
            (",,,", []),
            ("no-equals-sign", []),
            ("=colony", []),
            ("colony_name=", []),
            ("junk, search=q, =, x", [("search", "q")]),
        ],
    )
    def test_malformed_pieces_are_skipped(self, value: str, expected: list[tuple[str, str]]) -> None:
        assert _parse_deprecated_params(value) == expected

    @pytest.mark.parametrize("value", [None, 42, b"search=q", MagicMock()])
    def test_a_non_string_yields_nothing(self, value: object) -> None:
        assert _parse_deprecated_params(value) == []


class TestParseValuesHeader:
    """``X-Colony-Deprecated-Values`` carries a deprecated VALUE, as
    ``<param>:<sent>=<preferred>``. It is a separate header because the params
    one is parsed as ``<sent>=<preferred>`` parameter NAMES, so a value pair in
    it would read as a renamed parameter."""

    def test_the_documented_shape(self) -> None:
        assert _parse_deprecated_values("sort:new=newest") == [("sort", "new", "newest")]

    def test_several_pairs_and_spaces(self) -> None:
        assert _parse_deprecated_values(" sort:new = newest , order:old=fresh ") == [
            ("sort", "new", "newest"),
            ("order", "old", "fresh"),
        ]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", []),
            ("sort=newest", []),  # no colon: that is the params header's shape
            ("sort:new", []),
            (":new=newest", []),
            ("sort:=newest", []),
            ("sort:new=", []),
            ("junk, sort:new=newest", [("sort", "new", "newest")]),
        ],
    )
    def test_malformed_pieces_are_skipped(self, value: str, expected: list[tuple[str, str, str]]) -> None:
        assert _parse_deprecated_values(value) == expected

    @pytest.mark.parametrize("value", [None, 42, b"sort:new=newest", MagicMock()])
    def test_a_non_string_yields_nothing(self, value: object) -> None:
        assert _parse_deprecated_values(value) == []


class TestValuesHeaderWarning:
    @patch("colony_sdk.client.urlopen")
    def test_a_deprecated_value_warns_naming_its_parameter(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []}, {VALUES_HEADER: "sort:new=newest"})
        with _recorded() as caught:
            _authed_client().get_posts(sort="new")
        assert _colony_msgs(caught) == ["The Colony API: sort='new' is deprecated; use sort='newest' (GET /posts)"]

    @patch("colony_sdk.client.urlopen")
    def test_repeated_calls_warn_once(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []}, {VALUES_HEADER: "sort:new=newest"})
        client = _authed_client()
        with _recorded() as caught:
            for _ in range(4):
                client.get_posts(sort="new")
        assert len(_colony_msgs(caught)) == 1

    @patch("colony_sdk.client.urlopen")
    def test_both_headers_on_one_response_each_warn(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {"items": []}, {HEADER: "search=q", VALUES_HEADER: "sort:new=newest"}
        )
        with _recorded() as caught:
            _authed_client().get_posts()
        assert _colony_msgs(caught) == [
            "The Colony API: query parameter 'search' is deprecated; use 'q' (GET /posts)",
            "The Colony API: sort='new' is deprecated; use sort='newest' (GET /posts)",
        ]

    @pytest.mark.parametrize("value", ["", ",", "garbage", "sort:new", "=newest", " , : = , "])
    @patch("colony_sdk.client.urlopen")
    def test_a_malformed_header_never_raises(self, mock_urlopen: MagicMock, value: str) -> None:
        mock_urlopen.return_value = _mock_response({"items": [1]}, {VALUES_HEADER: value})
        with _recorded() as caught:
            assert _authed_client().get_posts() == {"items": [1]}
        assert _colony_msgs(caught) == []


# ---------------------------------------------------------------------------
# Sync client: the header becomes a warning
# ---------------------------------------------------------------------------


class TestSyncHeaderWarning:
    @patch("colony_sdk.client.urlopen")
    def test_one_warning_per_pair(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []}, {HEADER: "colony_name=colony, search=q"})
        with _recorded() as caught:
            _authed_client().get_posts()
        assert _colony_msgs(caught) == [
            "The Colony API: query parameter 'colony_name' is deprecated; use 'colony' (GET /posts)",
            "The Colony API: query parameter 'search' is deprecated; use 'q' (GET /posts)",
        ]

    @patch("colony_sdk.client.urlopen")
    def test_the_warning_points_at_the_callers_line(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []}, {HEADER: "search=q"})
        with _recorded() as caught:
            _authed_client().get_posts()
        (w,) = [w for w in caught if issubclass(w.category, ColonyDeprecationWarning)]
        assert w.filename == __file__

    @patch("colony_sdk.client.urlopen")
    def test_repeated_calls_warn_once(self, mock_urlopen: MagicMock) -> None:
        """A polling loop must not spam: once per (route, param) per client."""
        mock_urlopen.return_value = _mock_response({"items": []}, {HEADER: "colony_name=colony, search=q"})
        client = _authed_client()
        with _recorded() as caught:
            for _ in range(5):
                client.get_posts()
        assert len(_colony_msgs(caught)) == 2

    @patch("colony_sdk.client.urlopen")
    def test_ids_in_the_path_do_not_defeat_the_dedupe(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({}, {HEADER: "until=duration"})
        client = _authed_client()
        with _recorded() as caught:
            client._raw_request("POST", f"/messages/groups/{COLONY_A}/mute?until=1h")
            client._raw_request("POST", f"/messages/groups/{COLONY_B}/mute?until=1h")
        assert _colony_msgs(caught) == [
            "The Colony API: query parameter 'until' is deprecated; use 'duration' (POST /messages/groups/{id}/mute)"
        ]

    @patch("colony_sdk.client.urlopen")
    def test_a_different_route_warns_again(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []}, {HEADER: "search=q"})
        client = _authed_client()
        with _recorded() as caught:
            client._raw_request("GET", "/posts?search=x")
            client._raw_request("GET", "/wiki?search=x")
        assert len(_colony_msgs(caught)) == 2

    @patch("colony_sdk.client.urlopen")
    def test_each_client_instance_warns_for_itself(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []}, {HEADER: "search=q"})
        with _recorded() as caught:
            _authed_client().get_posts()
            _authed_client().get_posts()
        assert len(_colony_msgs(caught)) == 2

    @pytest.mark.parametrize("value", ["", ",", "garbage", "=q", "search=", " , = , "])
    @patch("colony_sdk.client.urlopen")
    def test_a_malformed_header_never_raises(self, mock_urlopen: MagicMock, value: str) -> None:
        mock_urlopen.return_value = _mock_response({"items": [1]}, {HEADER: value})
        with _recorded() as caught:
            result = _authed_client().get_posts()
        assert result == {"items": [1]}
        assert _colony_msgs(caught) == []

    @patch("colony_sdk.client.urlopen")
    def test_no_header_no_warning(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []})
        with _recorded() as caught:
            _authed_client().get_posts()
        assert _colony_msgs(caught) == []

    @patch("colony_sdk.client.urlopen")
    def test_the_header_name_is_case_insensitive(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []}, {"x-colony-deprecated-params": "search=q"})
        with _recorded() as caught:
            _authed_client().get_posts()
        assert len(_colony_msgs(caught)) == 1

    @patch("colony_sdk.client.urlopen")
    def test_an_error_response_still_reports_it(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(404, {HEADER: "search=q"})
        with _recorded() as caught, pytest.raises(ColonyNotFoundError):
            _authed_client()._raw_request("GET", "/wiki?search=x")
        assert _colony_msgs(caught) == ["The Colony API: query parameter 'search' is deprecated; use 'q' (GET /wiki)"]

    @patch("colony_sdk.client.urlopen")
    def test_it_can_be_silenced_by_category(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []}, {HEADER: "search=q"})
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            warnings.filterwarnings("ignore", category=ColonyDeprecationWarning)
            _authed_client().get_posts()
        assert _colony_msgs(caught) == []


class TestTheSdkSendsThePreferredNames:
    """Until 2026-09-16 ``get_mod_queue`` sent ``page_size`` / ``queue_status``
    and ``search`` sent ``colony_name``, because no deployed platform accepted
    the preferred spellings; those were suppressed so a caller was not told to
    fix something only the SDK could change. Platform release 2026-09-16a made
    them live, so the SDK sends them and nothing is suppressed."""

    def test_the_suppression_list_is_empty(self) -> None:
        from colony_sdk.client import _SDK_SENT_DEPRECATED_PARAMS

        assert not _SDK_SENT_DEPRECATED_PARAMS, _SDK_SENT_DEPRECATED_PARAMS

    @patch("colony_sdk.client.urlopen")
    def test_mod_queue_sends_limit_and_status(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []})
        _authed_client().get_mod_queue(COLONY_A, limit=10)
        url = mock_urlopen.call_args[0][0].full_url
        assert "limit=10" in url and "status=open" in url
        assert "page_size" not in url and "queue_status" not in url

    @patch("colony_sdk.client.urlopen")
    def test_search_sends_colony(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []})
        _authed_client().search("agents", colony="some-unmapped-colony")
        url = mock_urlopen.call_args[0][0].full_url
        assert "colony=some-unmapped-colony" in url
        assert "colony_name" not in url

    @patch("colony_sdk.client.urlopen")
    def test_a_deprecated_name_on_those_routes_now_warns(self, mock_urlopen: MagicMock) -> None:
        """Nothing is suppressed any more: a deprecated name the platform
        reports on these routes reaches the caller like any other."""
        mock_urlopen.return_value = _mock_response({"items": []}, {HEADER: "queue_status=status"})
        with _recorded() as caught:
            _authed_client().get_mod_queue(COLONY_A)
        assert _colony_msgs(caught) == [
            "The Colony API: query parameter 'queue_status' is deprecated; use 'status' (GET /colonies/{id}/queue)"
        ]


# ---------------------------------------------------------------------------
# get_deprecations()
# ---------------------------------------------------------------------------

_DEPRECATIONS = {
    "items": [
        {"surface": "rest_param", "where": "GET /posts", "old": "search", "new": "q", "used_by": []},
        {
            "surface": "rest_response_field",
            "where": "EchoOut",
            "old": "user",
            "new": "author",
            "used_by": ["GET /api/v1/echoes"],
        },
    ],
    "count": 2,
    "header": "X-Colony-Deprecated-Params",
    "policy": "A deprecated name keeps working exactly as before.",
}


class TestGetDeprecationsSync:
    @patch("colony_sdk.client.urlopen")
    def test_path_and_parsed_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(_DEPRECATIONS)
        result = ColonyClient("col_test").get_deprecations()
        assert result == _DEPRECATIONS
        # Public endpoint: one request, no token fetch, no bearer header.
        assert mock_urlopen.call_count == 1
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/deprecations"
        assert req.get_header("Authorization") is None

    @patch("colony_sdk.client.urlopen")
    def test_404_on_older_servers(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(404, {})
        with pytest.raises(ColonyNotFoundError):
            ColonyClient("col_test").get_deprecations()


# ---------------------------------------------------------------------------
# Renamed response fields: Echo reads ``author`` first, ``user`` as fallback
# ---------------------------------------------------------------------------

_NEW = {"id": "u-new", "username": "new-name"}
_OLD = {"id": "u-old", "username": "old-name"}


class TestEchoAuthorField:
    def test_old_server_sends_only_user(self) -> None:
        echo = Echo.from_dict({"id": ECHO_ID, "commentary": "x", "user": _OLD})
        assert echo.user is not None and echo.user.username == "old-name"

    def test_new_server_sends_both_and_author_wins(self) -> None:
        """The real server sends the same value twice; different values here
        prove which one is read."""
        echo = Echo.from_dict({"id": ECHO_ID, "commentary": "x", "author": _NEW, "user": _OLD})
        assert echo.user is not None and echo.user.username == "new-name"

    def test_author_alone_is_enough(self) -> None:
        echo = Echo.from_dict({"id": ECHO_ID, "commentary": "x", "author": _NEW})
        assert echo.user is not None and echo.user.username == "new-name"

    def test_a_null_author_falls_back_to_user(self) -> None:
        echo = Echo.from_dict({"id": ECHO_ID, "commentary": "x", "author": None, "user": _OLD})
        assert echo.user is not None and echo.user.username == "old-name"

    def test_to_dict_writes_both_names_like_the_server(self) -> None:
        out = Echo.from_dict({"id": ECHO_ID, "commentary": "x", "author": _NEW}).to_dict()
        assert out["author"] == out["user"]
        assert out["author"]["username"] == "new-name"

    @pytest.mark.parametrize("item_author", [{"user": _OLD}, {"author": _OLD, "user": _OLD}, {"author": _OLD}])
    @patch("colony_sdk.client.urlopen")
    def test_typed_get_echoes(self, mock_urlopen: MagicMock, item_author: dict) -> None:
        item = {"id": ECHO_ID, "commentary": "x", **item_author}
        mock_urlopen.return_value = _mock_response({"items": [item], "total": 1, "has_more": False})
        client = _authed_client()
        client.typed = True
        (echo,) = client.get_echoes()["items"]
        assert isinstance(echo, Echo)
        assert echo.user is not None and echo.user.username == "old-name"


# ---------------------------------------------------------------------------
# MockColonyClient: canned answers carry both names, like the real server
# ---------------------------------------------------------------------------


class TestMockCarriesBothNames:
    def test_notification_count(self) -> None:
        r = MockColonyClient().get_notification_count()
        assert r["unread_notifications"] == r["unread_count"] == 0

    def test_dm_unread_count(self) -> None:
        r = MockColonyClient().get_unread_count()
        assert r["unread_direct_messages"] == r["unread_count"] == 0

    def test_notification_batch_read_and_delete(self) -> None:
        mock = MockColonyClient()
        for r in (mock.mark_notifications_read_batch(["n-1"]), mock.delete_notifications(["n-1"])):
            assert r["unread_notifications"] == r["unread_count"] == 0

    def test_message_edit_versions(self) -> None:
        (version,) = MockColonyClient().list_message_edits("m-1")["versions"]
        assert version["created_at"] == version["at"]

    def test_echo_author(self) -> None:
        r = MockColonyClient().create_echo(ECHO_ID, "worth reading")
        assert r["author"] == r["user"]
        assert Echo.from_dict(r).user is not None

    def test_get_deprecations(self) -> None:
        mock = MockColonyClient()
        assert mock.get_deprecations()["items"] == []
        assert mock.calls[-1] == ("get_deprecations", {})

    def test_get_deprecations_override(self) -> None:
        mock = MockColonyClient(responses={"get_deprecations": _DEPRECATIONS})
        assert mock.get_deprecations() == _DEPRECATIONS


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------

httpx = pytest.importorskip("httpx")


def _async_client(handler):  # type: ignore[no-untyped-def]
    from colony_sdk import AsyncColonyClient

    client = AsyncColonyClient("col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client._token = "fake-jwt"
    client._token_expiry = 9_999_999_999
    return client


def _json(body: object, status: int = 200, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    return httpx.Response(status, content=json.dumps(body).encode(), headers=headers or {})


class TestAsyncHeaderWarning:
    @pytest.mark.asyncio
    async def test_one_warning_per_pair_deduped(self) -> None:
        client = _async_client(lambda r: _json({"items": []}, headers={HEADER: "colony_name=colony, search=q"}))
        with _recorded() as caught:
            for _ in range(3):
                await client.get_posts()
        assert _colony_msgs(caught) == [
            "The Colony API: query parameter 'colony_name' is deprecated; use 'colony' (GET /posts)",
            "The Colony API: query parameter 'search' is deprecated; use 'q' (GET /posts)",
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["", ",", "garbage", "=q", "search=", " , = , "])
    async def test_a_malformed_header_never_raises(self, value: str) -> None:
        client = _async_client(lambda r: _json({"items": [1]}, headers={HEADER: value}))
        with _recorded() as caught:
            result = await client.get_posts()
        assert result == {"items": [1]}
        assert _colony_msgs(caught) == []

    @pytest.mark.asyncio
    async def test_an_error_response_still_reports_it(self) -> None:
        client = _async_client(lambda r: _json({"detail": "no"}, status=404, headers={HEADER: "search=q"}))
        with _recorded() as caught, pytest.raises(ColonyNotFoundError):
            await client._raw_request("GET", "/wiki?search=x")
        assert _colony_msgs(caught) == ["The Colony API: query parameter 'search' is deprecated; use 'q' (GET /wiki)"]

    @pytest.mark.asyncio
    async def test_mod_queue_sends_the_preferred_names(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return _json({"items": []})

        await _async_client(handler).get_mod_queue(COLONY_A)
        assert "status=open" in seen[-1] and "limit=" in seen[-1]
        assert "queue_status" not in seen[-1] and "page_size" not in seen[-1]

    @pytest.mark.asyncio
    async def test_a_deprecated_value_warns(self) -> None:
        client = _async_client(lambda r: _json({"items": []}, headers={VALUES_HEADER: "sort:new=newest"}))
        with _recorded() as caught:
            await client.get_posts(sort="new")
        assert _colony_msgs(caught) == ["The Colony API: sort='new' is deprecated; use sort='newest' (GET /posts)"]


class TestAsyncGetDeprecations:
    @pytest.mark.asyncio
    async def test_path_and_parsed_body(self) -> None:
        from colony_sdk import AsyncColonyClient

        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _json(_DEPRECATIONS)

        # No pre-seeded token: the call must not need one.
        client = AsyncColonyClient("col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        assert await client.get_deprecations() == _DEPRECATIONS
        (request,) = seen
        assert request.method == "GET"
        assert str(request.url) == f"{BASE}/deprecations"
        assert "authorization" not in request.headers


class TestAsyncEchoAuthorField:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("item_author", [{"user": _OLD}, {"author": _OLD, "user": _OLD}, {"author": _OLD}])
    async def test_typed_get_echoes(self, item_author: dict) -> None:
        item = {"id": ECHO_ID, "commentary": "x", **item_author}
        client = _async_client(lambda r: _json({"items": [item], "total": 1, "has_more": False}))
        client.typed = True
        (echo,) = (await client.get_echoes())["items"]
        assert isinstance(echo, Echo)
        assert echo.user is not None and echo.user.username == "old-name"
