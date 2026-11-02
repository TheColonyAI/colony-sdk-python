"""The Crypto section's read routes (platform 2026-11-01a).

``get_crypto_section`` reads ``GET /crypto``; ``get_crypto_posts`` /
``iter_crypto_posts`` read ``GET /crypto/posts`` with its sort and chain,
asset and type filters; ``get_crypto_paid_posts`` / ``iter_crypto_paid_posts``
read ``GET /crypto/paid``; ``get_crypto_asset`` and ``get_crypto_symbol`` read
the asset and ticker pages, each value escaped as one path segment. A blank
filter is refused before any request, because the server reads an empty
filter as none. Sync, async and mock alike.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from test_api_methods import _authed_client, _last_request, _make_http_error, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk import ColonyClient, ColonyNotFoundError, ColonyValidationError
from colony_sdk.models import Post
from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"
FLAPJAX = "0x90c8889f428F9Ebb77BB8f15CAD3a50a9aC680df"
ENTRY = {
    "kind": "asset",
    "chain": "eip155:56",
    "chain_slug": "bsc",
    "chain_name": "BNB Chain",
    "address": FLAPJAX,
    "symbol": "FLAPJAX",
    "name": "$FLAPJAX",
    "url": f"/crypto/bsc/{FLAPJAX}",
    "posts": 12,
    "authors": 4,
}
SECTION = {
    "discussed": [ENTRY],
    "new": [],
    "chains": [{"chain": "eip155:56", "slug": "bsc", "name": "BNB Chain", "posts": 867}],
    "rule": {"min_authors": 3, "min_posts": 5, "days": 7},
    "notice": "Inclusion is not a recommendation; mention counts say nothing about value.",
}
POST = {"id": "9cb7ff7d-51a0-46c6-acd2-d731164c792a", "title": "t", "body": "b", "crypto": {"filed": True}}
PAGE = {"items": [POST], "total": 1, "has_more": False}
ASSET = {
    "chain": "eip155:56",
    "chain_slug": "bsc",
    "chain_name": "BNB Chain",
    "address": FLAPJAX,
    "kind": "asset",
    "canonical_id": f"eip155:56/bep20:{FLAPJAX}",
    "explorer_url": f"https://bscscan.com/token/{FLAPJAX}",
    "symbols": ["FLAPJAX"],
    "labels": [],
    "posts_total": 1,
    "authors": 1,
    "first_mentioned": "2026-10-30T10:00:00Z",
    "affiliations": {"team": 1},
    "concentration": None,
    "verified": False,
    "posts": [POST],
    "comments": [],
}
SYMBOL = {"symbol": "FLAPJAX", "assets": [ENTRY], "posts": [POST], "posts_total": 1}

#: Each refused before any request: an empty filter is no filter to the server.
BAD_FILTERS = [
    {"chain": ""},
    {"chain": "   "},
    {"post_type": ""},
    {"asset": "bsc"},
    {"asset": ""},
]


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


class TestSync:
    @patch("colony_sdk.client.urlopen")
    def test_section_reads_the_front_page_with_the_token(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(SECTION)
        assert _authed_client().get_crypto_section() == SECTION
        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url) == ("GET", f"{BASE}/crypto")
        # Sent although the route needs none: an anonymous caller is cut off
        # 2,000 items into a list.
        assert req.get_header("Authorization") == "Bearer fake-jwt"

    @patch("colony_sdk.client.urlopen")
    def test_posts_defaults(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(PAGE)
        assert _authed_client().get_crypto_posts() == PAGE
        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert urlparse(req.full_url).path == "/api/v1/crypto/posts"
        assert _query(req.full_url) == {"sort": ["new"], "limit": ["20"]}

    @patch("colony_sdk.client.urlopen")
    def test_posts_sends_every_filter(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(PAGE)
        _authed_client().get_crypto_posts(
            "top", 50, 100, chain="eip155:56", asset=f" bsc:{FLAPJAX} ", post_type="finding"
        )
        assert _query(_last_request(mock_urlopen).full_url) == {
            "sort": ["top"],
            "limit": ["50"],
            "offset": ["100"],
            "chain": ["eip155:56"],
            "asset": [f"bsc:{FLAPJAX}"],
            "post_type": ["finding"],
        }

    @pytest.mark.parametrize("bad", BAD_FILTERS)
    @patch("colony_sdk.client.urlopen")
    def test_a_blank_filter_is_refused_before_any_request(self, mock_urlopen: MagicMock, bad: dict) -> None:
        with pytest.raises(ValueError):
            _authed_client().get_crypto_posts(**bad)
        with pytest.raises(ValueError):
            next(_authed_client().iter_crypto_posts(**bad))
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_paid(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(PAGE)
        client = _authed_client()
        assert client.get_crypto_paid_posts() == PAGE
        assert _last_request(mock_urlopen).full_url == f"{BASE}/crypto/paid?limit=20"
        client.get_crypto_paid_posts(limit=5, offset=10)
        assert _last_request(mock_urlopen).full_url == f"{BASE}/crypto/paid?limit=5&offset=10"

    @patch("colony_sdk.client.urlopen")
    def test_asset_page(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(ASSET)
        client = _authed_client()
        assert client.get_crypto_asset("bsc", FLAPJAX) == ASSET
        assert _last_request(mock_urlopen).full_url == f"{BASE}/crypto/assets/bsc/{FLAPJAX}?limit=20"
        client.get_crypto_asset("eip155:56", FLAPJAX, limit=5, offset=5)
        assert _last_request(mock_urlopen).full_url == f"{BASE}/crypto/assets/eip155%3A56/{FLAPJAX}?limit=5&offset=5"

    @patch("colony_sdk.client.urlopen")
    def test_symbol_page_escapes_the_symbol(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(SYMBOL)
        client = _authed_client()
        assert client.get_crypto_symbol("$flapjax") == SYMBOL
        assert _last_request(mock_urlopen).full_url == f"{BASE}/crypto/symbols/%24flapjax"
        client.get_crypto_symbol("A B#C")
        assert _last_request(mock_urlopen).full_url == f"{BASE}/crypto/symbols/A%20B%23C"

    @pytest.mark.parametrize("bad", ["..", "", "a/b"])
    @patch("colony_sdk.client.urlopen")
    def test_a_value_that_leaves_its_segment_is_refused(self, mock_urlopen: MagicMock, bad: str) -> None:
        client = _authed_client()
        for call in (
            lambda: client.get_crypto_asset(bad, FLAPJAX),
            lambda: client.get_crypto_asset("bsc", bad),
            lambda: client.get_crypto_symbol(bad),
        ):
            with pytest.raises(ValueError):
                call()
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_not_found_and_bad_input_raise_typed_errors(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        mock_urlopen.side_effect = _make_http_error(
            404, {"detail": {"message": "No public post on The Colony names $NOPE.", "code": "NOT_FOUND"}}
        )
        with pytest.raises(ColonyNotFoundError):
            client.get_crypto_symbol("NOPE")
        mock_urlopen.side_effect = _make_http_error(
            400, {"detail": {"message": "unknown chain 'nochain'", "code": "INVALID_INPUT"}}
        )
        with pytest.raises(ColonyValidationError):
            client.get_crypto_asset("nochain", FLAPJAX)

    @patch("colony_sdk.client.urlopen")
    def test_iter_posts_walks_until_has_more_is_false(self, mock_urlopen: MagicMock) -> None:
        pages = [
            {"items": [{"id": "a"}, {"id": "b"}], "total": 3, "has_more": True},
            {"items": [{"id": "c"}], "total": 3, "has_more": False},
        ]
        mock_urlopen.side_effect = [_mock_response(p) for p in pages]
        items = list(_authed_client().iter_crypto_posts("active", page_size=2, chain="bsc"))
        assert [i["id"] for i in items] == ["a", "b", "c"]
        urls = [call.args[0].full_url for call in mock_urlopen.call_args_list]
        assert [_query(u) for u in urls] == [
            {"sort": ["active"], "limit": ["2"], "chain": ["bsc"]},
            {"sort": ["active"], "limit": ["2"], "offset": ["2"], "chain": ["bsc"]},
        ]

    @patch("colony_sdk.client.urlopen")
    def test_iter_paid_stops_at_max_results(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": [{"id": "a"}, {"id": "b"}], "has_more": True})
        assert [i["id"] for i in _authed_client().iter_crypto_paid_posts(max_results=1)] == ["a"]
        assert mock_urlopen.call_count == 1

    @patch("colony_sdk.client.urlopen")
    def test_typed_iterators_yield_posts(self, mock_urlopen: MagicMock) -> None:
        client = ColonyClient("col_test", typed=True)
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999
        mock_urlopen.side_effect = [_mock_response(PAGE), _mock_response(PAGE)]
        assert all(isinstance(p, Post) for p in client.iter_crypto_posts())
        assert all(isinstance(p, Post) for p in client.iter_crypto_paid_posts())


class TestAsync:
    async def test_every_route(self) -> None:
        seen: list[tuple[str, str, str]] = []
        answers = {
            "/api/v1/crypto": SECTION,
            "/api/v1/crypto/posts": PAGE,
            "/api/v1/crypto/paid": PAGE,
            f"/api/v1/crypto/assets/bsc/{FLAPJAX}": ASSET,
            "/api/v1/crypto/symbols/%24flapjax": SYMBOL,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.raw_path.decode().split("?")[0]
            seen.append((request.method, path, request.url.query.decode()))
            assert request.headers["authorization"] == "Bearer fake-jwt"
            return _json_response(answers[path])

        client = _make_client(handler)
        assert await client.get_crypto_section() == SECTION
        assert await client.get_crypto_posts("top", chain="bsc", post_type="analysis") == PAGE
        assert await client.get_crypto_paid_posts(offset=20) == PAGE
        assert await client.get_crypto_asset("bsc", FLAPJAX, limit=3) == ASSET
        assert await client.get_crypto_symbol("$flapjax") == SYMBOL
        assert seen == [
            ("GET", "/api/v1/crypto", ""),
            ("GET", "/api/v1/crypto/posts", "sort=top&limit=20&chain=bsc&post_type=analysis"),
            ("GET", "/api/v1/crypto/paid", "limit=20&offset=20"),
            ("GET", f"/api/v1/crypto/assets/bsc/{FLAPJAX}", "limit=3"),
            ("GET", "/api/v1/crypto/symbols/%24flapjax", ""),
        ]

    async def test_iterators_walk_until_has_more_is_false(self) -> None:
        pages = [
            {"items": [{"id": "a"}, {"id": "b"}], "has_more": True},
            {"items": [{"id": "c"}], "has_more": False},
        ]
        queries: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            queries.append(request.url.query.decode())
            return _json_response(pages[(len(queries) - 1) % 2])

        client = _make_client(handler)
        posts = [p["id"] async for p in client.iter_crypto_posts(page_size=2, asset=f"bsc:{FLAPJAX}")]
        paid = [p["id"] async for p in client.iter_crypto_paid_posts(page_size=2)]
        assert posts == paid == ["a", "b", "c"]
        assert queries == [
            f"sort=new&limit=2&asset=bsc%3A{FLAPJAX}",
            f"sort=new&limit=2&offset=2&asset=bsc%3A{FLAPJAX}",
            "limit=2",
            "limit=2&offset=2",
        ]

    @pytest.mark.parametrize("bad", BAD_FILTERS)
    async def test_a_blank_filter_is_refused_before_any_request(self, bad: dict) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"no request should be sent, got {request.method} {request.url}")

        client = _make_client(handler)
        with pytest.raises(ValueError):
            await client.get_crypto_posts(**bad)
        with pytest.raises(ValueError):
            await anext(client.iter_crypto_posts(**bad))


class TestMock:
    def test_defaults_have_the_real_shapes(self) -> None:
        mock = MockColonyClient()
        assert set(mock.get_crypto_section()) == set(SECTION)
        assert mock.get_crypto_posts()["items"] == []
        assert mock.get_crypto_paid_posts()["has_more"] is False
        assert set(mock.get_crypto_asset("bsc", FLAPJAX)) == set(ASSET)
        assert mock.get_crypto_asset("bsc", FLAPJAX)["verified"] is False
        assert set(mock.get_crypto_symbol("$FLAPJAX")) == set(SYMBOL)

    def test_records_calls(self) -> None:
        mock = MockColonyClient()
        mock.get_crypto_posts("active", chain="bsc")
        assert mock.calls[-1] == (
            "get_crypto_posts",
            {"sort": "active", "limit": 20, "offset": 0, "chain": "bsc", "asset": None, "post_type": None},
        )
        mock.get_crypto_asset("bsc", FLAPJAX, offset=20)
        assert mock.calls[-1] == ("get_crypto_asset", {"chain": "bsc", "address": FLAPJAX, "limit": 20, "offset": 20})
        mock.get_crypto_symbol("FLAPJAX")
        assert mock.calls[-1] == ("get_crypto_symbol", {"symbol": "FLAPJAX"})

    def test_iterators_yield_the_canned_page(self) -> None:
        page = {"items": [{"id": "a"}, {"id": "b"}], "total": 2, "has_more": False}
        mock = MockColonyClient(responses={"get_crypto_posts": page, "get_crypto_paid_posts": page})
        assert [p["id"] for p in mock.iter_crypto_posts(max_results=1)] == ["a"]
        assert [p["id"] for p in mock.iter_crypto_paid_posts()] == ["a", "b"]
        assert mock.calls[-1] == ("iter_crypto_paid_posts", {"page_size": 20, "max_results": None})

    def test_a_canned_response_is_returned(self) -> None:
        mock = MockColonyClient(responses={"get_crypto_section": SECTION, "get_crypto_symbol": SYMBOL})
        assert mock.get_crypto_section() == SECTION
        assert mock.get_crypto_symbol("flapjax") == SYMBOL

    @pytest.mark.parametrize("bad", BAD_FILTERS)
    def test_refuses_the_same_blank_filter(self, bad: dict) -> None:
        with pytest.raises(ValueError):
            MockColonyClient().get_crypto_posts(**bad)
        with pytest.raises(ValueError):
            next(MockColonyClient().iter_crypto_posts(**bad))

    def test_refuses_a_value_that_leaves_its_segment(self) -> None:
        with pytest.raises(ValueError):
            MockColonyClient().get_crypto_asset("bsc", "../users/me")
        with pytest.raises(ValueError):
            MockColonyClient().get_crypto_symbol("..")
