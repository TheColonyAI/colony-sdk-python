"""Conditional reads: a kept answer is revalidated with ``If-None-Match``.

The API sends an ETag on some reads (a post's comments, for any caller,
since 2026-11-02). The client keeps the body under its ETag, sends the tag
back on the next read of the same URL, and answers a ``304 Not Modified``
with the kept body, exactly as the 200 returned it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
from test_api_methods import _authed_client, _last_request, _make_http_error, _mock_response
from test_async_client import _make_client

from colony_sdk import ColonyClient
from colony_sdk.client import _Revalidator

POST_ID = "11111111-1111-4111-8111-111111111111"
PAGE = {"items": [{"id": "c1", "body": "first"}], "total": 1, "page": 1, "has_more": False}
ETAG = 'W/"0123456789abcdef"'


def _with_headers(data: dict, headers: dict[str, str]) -> MagicMock:
    resp = _mock_response(data)
    resp.getheaders.return_value = list(headers.items())
    return resp


def _sent(mock_urlopen: MagicMock, name: str) -> str | None:
    return _last_request(mock_urlopen).get_header(name.capitalize())


class TestSync:
    @patch("colony_sdk.client.urlopen")
    def test_a_304_returns_the_kept_body(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        mock_urlopen.return_value = _with_headers(PAGE, {"ETag": ETAG})
        first = client.get_comments(POST_ID)
        assert _sent(mock_urlopen, "If-None-Match") is None
        assert client.last_response_not_modified is False

        mock_urlopen.side_effect = _make_http_error(304)
        again = client.get_comments(POST_ID)

        assert _sent(mock_urlopen, "If-None-Match") == ETAG
        assert again == first == PAGE
        assert client.last_response_not_modified is True

    @patch("colony_sdk.client.urlopen")
    def test_changing_a_result_does_not_change_the_next_one(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        mock_urlopen.return_value = _with_headers(PAGE, {"ETag": ETAG})
        client.get_comments(POST_ID)["items"].clear()
        mock_urlopen.side_effect = _make_http_error(304)
        assert client.get_comments(POST_ID) == PAGE

    @patch("colony_sdk.client.urlopen")
    def test_a_changed_answer_replaces_the_kept_one(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        mock_urlopen.return_value = _with_headers(PAGE, {"ETag": ETAG})
        client.get_comments(POST_ID)
        newer = {**PAGE, "total": 2}
        mock_urlopen.return_value = _with_headers(newer, {"ETag": 'W/"fedcba9876543210"'})
        assert client.get_comments(POST_ID) == newer
        assert client.last_response_not_modified is False
        client.get_comments(POST_ID)
        assert _sent(mock_urlopen, "If-None-Match") == 'W/"fedcba9876543210"'

    @patch("colony_sdk.client.urlopen")
    def test_an_answer_without_an_etag_is_not_revalidated(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        mock_urlopen.return_value = _with_headers(PAGE, {"ETag": ETAG})
        client.get_comments(POST_ID)
        mock_urlopen.return_value = _mock_response(PAGE)
        client.get_comments(POST_ID)
        client.get_comments(POST_ID)
        assert _sent(mock_urlopen, "If-None-Match") is None

    @patch("colony_sdk.client.urlopen")
    def test_each_url_keeps_its_own_tag(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        mock_urlopen.return_value = _with_headers(PAGE, {"ETag": ETAG})
        client.get_comments(POST_ID, page=1)
        client.get_comments(POST_ID, page=2)
        assert _sent(mock_urlopen, "If-None-Match") is None

    @patch("colony_sdk.client.urlopen")
    def test_revalidate_false_never_asks(self, mock_urlopen: MagicMock) -> None:
        client = ColonyClient("col_test", revalidate=False)
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999
        mock_urlopen.return_value = _with_headers(PAGE, {"ETag": ETAG})
        client.get_comments(POST_ID)
        client.get_comments(POST_ID)
        assert _sent(mock_urlopen, "If-None-Match") is None

    @patch("colony_sdk.client.urlopen")
    def test_a_write_never_sends_a_kept_tag(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        client._revalidator.remember(f"{client.base_url}/posts/{POST_ID}/comments", ETAG, json.dumps(PAGE))
        mock_urlopen.return_value = _mock_response({"id": "c2"})
        client.create_comment(POST_ID, "hello")
        assert _sent(mock_urlopen, "If-None-Match") is None

    @patch("colony_sdk.client.urlopen")
    def test_response_hooks_see_the_304(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        seen: list[tuple[int, object]] = []
        client.on_response(lambda method, url, status, data: seen.append((status, data)))
        mock_urlopen.return_value = _with_headers(PAGE, {"ETag": ETAG})
        client.get_comments(POST_ID)
        mock_urlopen.side_effect = _make_http_error(304)
        client.get_comments(POST_ID)
        assert seen == [(200, PAGE), (304, PAGE)]


class TestAsync:
    async def test_a_304_returns_the_kept_body(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.headers.get("if-none-match") == ETAG:
                return httpx.Response(304, headers={"ETag": ETAG})
            return httpx.Response(200, content=json.dumps(PAGE).encode(), headers={"ETag": ETAG})

        client = _make_client(handler)
        first = await client.get_comments(POST_ID)
        assert client.last_response_not_modified is False
        first["items"].clear()
        again = await client.get_comments(POST_ID)

        assert "if-none-match" not in seen[0].headers
        assert seen[1].headers["if-none-match"] == ETAG
        assert again == PAGE
        assert client.last_response_not_modified is True

    async def test_revalidate_false_never_asks(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=json.dumps(PAGE).encode(), headers={"ETag": ETAG})

        from colony_sdk.async_client import AsyncColonyClient

        client = AsyncColonyClient(
            "col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), revalidate=False
        )
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999
        await client.get_comments(POST_ID)
        await client.get_comments(POST_ID)
        assert all("if-none-match" not in r.headers for r in seen)


class TestStore:
    def test_it_keeps_the_newest_urls(self) -> None:
        store = _Revalidator(max_entries=2)
        store.remember("a", ETAG, "1")
        store.remember("b", ETAG, "2")
        store.recall("a")  # a is now the most recently used
        store.remember("c", ETAG, "3")
        assert store.etag_for("a") == ETAG
        assert store.etag_for("b") is None
        assert store.etag_for("c") == ETAG

    def test_no_etag_forgets_the_url(self) -> None:
        store = _Revalidator()
        store.remember("a", ETAG, "1")
        store.remember("a", None, "2")
        assert store.etag_for("a") is None and store.recall("a") is None
