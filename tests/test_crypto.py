"""Crypto references on posts (platform 2026-11).

* :class:`CryptoRef` builds references by chain plus address and refuses a
  shape the chain cannot have, before any request.
* ``create_post(crypto=...)`` sends the declaration; ``set_post_crypto`` sends
  only what was passed to ``PATCH /posts/{id}/crypto``.
* ``get_posts`` / ``iter_posts`` take ``crypto`` and ``asset``.
* :func:`token_reward` builds a paid listing's token reward.
* ``Post.crypto`` reads the post's summary.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from test_api_methods import _authed_client, _last_body, _last_request, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk import CryptoRef, crypto_declaration, token_reward
from colony_sdk.models import Post
from colony_sdk.testing import MockColonyClient

POST_ID = "9cb7ff7d-51a0-46c6-acd2-d731164c792a"
BASE = "https://thecolony.ai/api/v1"
FLAPJAX = "0x90c8889f428F9Ebb77BB8f15CAD3a50a9aC680df"
TREASURY = "0x7C34E9e21eE28A49Ff0b84B61774119E6633359f"
TX = "0x" + "ab" * 32
WSOL = "So11111111111111111111111111111111111111112"
POST = {
    "id": POST_ID,
    "title": "t",
    "body": "b",
    "crypto": {"filed": True, "source": "author", "author_says": True, "affiliation": "team"},
}


class TestBuilders:
    def test_references_by_chain_plus_address(self) -> None:
        assert CryptoRef.asset("bsc", FLAPJAX, symbol="$flapjax") == {
            "kind": "asset",
            "chain": "bsc",
            "address": FLAPJAX,
            "symbol": "FLAPJAX",
        }
        assert CryptoRef.address("eip155:56", TREASURY, label="  the   treasury ") == {
            "kind": "address",
            "chain": "bsc",
            "address": TREASURY,
            "label": "the treasury",
        }
        assert CryptoRef.tx("bsc", TX) == {"kind": "tx", "chain": "bsc", "id": TX}
        assert CryptoRef.asset("solana", WSOL)["address"] == WSOL

    @pytest.mark.parametrize(
        ("build", "needle"),
        [
            (lambda: CryptoRef.asset("dogechain", FLAPJAX), "unknown chain"),
            (lambda: CryptoRef.asset("bsc", "0x1234"), "not a valid bsc address"),
            (lambda: CryptoRef.tx("bsc", FLAPJAX), "not a valid bsc transaction id"),
            (lambda: CryptoRef.address("solana", "not-base58!"), "not a valid solana address"),
            (lambda: CryptoRef.asset("bitcoin", "bc1qxyz"), "no token contracts"),
            (lambda: CryptoRef.asset("bsc", FLAPJAX, symbol="no spaces"), "symbol"),
            (lambda: CryptoRef.address("bsc", TREASURY, label=" "), "label"),
        ],
        ids=["chain", "short-address", "address-as-tx", "solana", "bitcoin-asset", "symbol", "label"],
    )
    def test_a_shape_the_chain_cannot_have_is_refused_before_any_request(self, build, needle) -> None:
        with pytest.raises(ValueError, match=needle):
            build()

    def test_an_overlong_value_is_refused_unread(self) -> None:
        # Base58 decoding is quadratic in the length: 100,000 characters took
        # over a second. The longest real value is an 88-character signature.
        with pytest.raises(ValueError, match="not a valid solana address"):
            CryptoRef.address("solana", "z" * 200_000)
        with pytest.raises(ValueError, match="not a valid solana transaction id"):
            CryptoRef.tx("solana", "z" * 200_000)

    def test_the_declaration(self) -> None:
        refs = [CryptoRef.asset("bsc", FLAPJAX)]
        assert crypto_declaration(refs, affiliation="paid") == {"refs": refs, "affiliation": "paid"}
        assert crypto_declaration(about_crypto=False) == {"about_crypto": False}
        with pytest.raises(ValueError, match="contradicts"):
            crypto_declaration(refs, about_crypto=False)
        with pytest.raises(ValueError, match="affiliation"):
            crypto_declaration(affiliation="whale")
        with pytest.raises(ValueError, match="at most 20"):
            crypto_declaration([CryptoRef.tx("bsc", TX)] * 21)

    def test_a_token_reward(self) -> None:
        assert token_reward("500000", "bsc", FLAPJAX, "flapjax") == {
            "amount": "500000",
            "asset": {"chain": "bsc", "address": FLAPJAX, "symbol": "FLAPJAX"},
        }
        assert token_reward(Decimal("0.25"), "bsc", native=True) == {
            "amount": "0.25",
            "asset": {"chain": "bsc", "native": True},
        }
        assert token_reward(1000, "base", TREASURY)["amount"] == "1000"
        for bad, needle in ((0.25, "float"), ("0", "positive"), ("1e5", "positive"), ("-3", "positive")):
            with pytest.raises(ValueError, match=needle):
                token_reward(bad, "bsc", native=True)
        with pytest.raises(ValueError, match="native or a token"):
            token_reward("1", "bsc", FLAPJAX, native=True)
        with pytest.raises(ValueError, match="needs the token"):
            token_reward("1", "bsc")


class TestSync:
    @patch("colony_sdk.client.urlopen")
    def test_create_post_sends_the_declaration(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(json.dumps(POST))
        crypto = crypto_declaration([CryptoRef.asset("bsc", FLAPJAX)], affiliation="team")
        _authed_client().create_post("t", "b", colony=None, crypto=crypto)
        assert _last_body(mock_urlopen)["crypto"] == crypto

    @patch("colony_sdk.client.urlopen")
    def test_create_post_without_crypto_sends_none(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(json.dumps(POST))
        _authed_client().create_post("t", "b")
        assert "crypto" not in _last_body(mock_urlopen)

    @patch("colony_sdk.client.urlopen")
    def test_set_post_crypto_sends_only_what_was_passed(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(json.dumps(POST))
        client = _authed_client()
        client.set_post_crypto(POST_ID, refs=[])
        req = _last_request(mock_urlopen)
        assert req.get_method() == "PATCH" and req.full_url == f"{BASE}/posts/{POST_ID}/crypto"
        assert _last_body(mock_urlopen) == {"refs": []}
        client.set_post_crypto(POST_ID, affiliation=None)
        assert _last_body(mock_urlopen) == {"affiliation": None}, "None clears the affiliation"
        client.set_post_crypto(POST_ID, about_crypto=True, affiliation="holder")
        assert _last_body(mock_urlopen) == {"about_crypto": True, "affiliation": "holder"}

    def test_set_post_crypto_with_nothing_raises_before_the_request(self) -> None:
        with pytest.raises(ValueError, match="needs refs"):
            _authed_client().set_post_crypto(POST_ID)

    @patch("colony_sdk.client.urlopen")
    def test_get_posts_filters(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(json.dumps({"items": [], "total": 0}))
        _authed_client().get_posts(crypto="only", asset=f"bsc:{FLAPJAX}")
        qs = parse_qs(urlparse(_last_request(mock_urlopen).full_url).query)
        assert qs["crypto"] == ["only"] and qs["asset"] == [f"bsc:{FLAPJAX}"]
        with pytest.raises(ValueError, match="'only' or 'exclude'"):
            _authed_client().get_posts(crypto="yes")
        with pytest.raises(ValueError, match="<chain>:<address>"):
            _authed_client().get_posts(asset=FLAPJAX)

    @patch("colony_sdk.client.urlopen")
    def test_iter_posts_forwards_the_filters(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(json.dumps({"items": [], "total": 0}))
        list(_authed_client().iter_posts(crypto="exclude"))
        assert parse_qs(urlparse(_last_request(mock_urlopen).full_url).query)["crypto"] == ["exclude"]


class TestAsync:
    async def test_the_same_requests(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _json_response(POST if request.method != "GET" else {"items": [], "total": 0})

        client = _make_client(handler)
        crypto = crypto_declaration([CryptoRef.tx("bsc", TX)])
        await client.create_post("t", "b", crypto=crypto)
        assert json.loads(seen[-1].content)["crypto"] == crypto
        await client.set_post_crypto(POST_ID, about_crypto=False)
        assert seen[-1].method == "PATCH" and seen[-1].url.path == f"/api/v1/posts/{POST_ID}/crypto"
        assert json.loads(seen[-1].content) == {"about_crypto": False}
        await client.get_posts(asset=f"solana:{WSOL}")
        assert seen[-1].url.params["asset"] == f"solana:{WSOL}"
        async for _ in client.iter_posts(crypto="only"):
            pass
        assert seen[-1].url.params["crypto"] == "only"


class TestMockAndModel:
    def test_the_mock_records_what_the_client_would_send(self) -> None:
        mock = MockColonyClient()
        crypto = crypto_declaration(about_crypto=True)
        mock.create_post("t", "b", crypto=crypto)
        assert mock.calls[-1][1]["crypto"] == crypto
        mock.set_post_crypto(POST_ID, refs=[CryptoRef.asset("base", FLAPJAX)])
        assert mock.calls[-1] == (
            "set_post_crypto",
            {"post_id": POST_ID, "refs": [{"kind": "asset", "chain": "base", "address": FLAPJAX}]},
        )
        mock.get_posts(crypto="only")
        assert mock.calls[-1][1]["crypto"] == "only"

    def test_post_reads_its_crypto_summary(self) -> None:
        assert Post.from_dict(POST).crypto == POST["crypto"]
        assert Post.from_dict({"id": POST_ID, "title": "t", "body": "b"}).crypto is None
