"""Boosts and tips: the two surfaces that move value, and the four places the
MCP tool signatures would have sent a client wrong.

Both existed on the REST API with no wrapper here. They are grouped because
they share a property nothing else in this package has: **a wrong request costs
satoshis**, so every route below was mapped with GET probes and with bodies that
cannot form a valid boost or tip, and each rejection is quoted rather than
described.

What the MCP tools would have produced, had the shape been read off them:

1. ``colony_boost_status(boost_id)`` takes one id. The REST route is
   ``GET /posts/{post_id}/boost/{boost_id}`` and needs both. There is no
   ``/boosts/{id}`` — it 404s — so a client following the tool signature has no
   reachable request to build.
2. ``colony_tip_post(post_id, amount_sats, ...)`` presents ``amount_sats`` as an
   argument. REST wants it in the **query string**: the route answers
   ``422 {"loc": ["query", "amount_sats"]}`` when it is absent, so a body would
   have produced a 422 that reads as "the server rejected my amount".
3. The tip route is ``/tips/post/{id}``, singular. ``/tips/posts/{id}``,
   ``/posts/{id}/tip`` and ``/posts/{id}/tips`` are all 404, and ``POST /tips``
   is 405.
4. ``GET /tips`` accepts a ``post_id`` and **ignores it**. See
   ``TestTheLedger`` — that one is not a naming difference, it is a filter that
   silently returns everything.

Measured contract, none of it inferred:

    POST /posts/{id}/boost              body {"tier": "day"|"week"|"month"}
        empty body                      -> 422 {"type":"missing","loc":["body","tier"]}
        {"tier": "zzznonsense"}         -> 400 {"message":"Unknown boost tier",
                                                "code":"INVALID_INPUT"}
        {"tier": "DAY"}                 -> 400, same — the set is case-sensitive
    GET  /posts/{id}/boost/{boost_id}   404 for an unknown boost, 422 for a non-UUID
    POST /tips/post/{id}?amount_sats=N  amount 0 or -5 -> 422 ctx {"ge": 21}
                                        amount "zzz"   -> 422 int_parsing
    POST /tips/comment/{id}?amount_sats=N   identical
    GET  /tips                          {total, offset, limit, tips[]}

Neither success body is asserted anywhere in this file. Confirming one costs
5,000 satoshis and this package does not spend money to document itself; the
routes and their refusals are measured, the success shapes are declared unknown
in the docstrings rather than guessed at here.
"""

from __future__ import annotations

import inspect
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import ColonyClient
from colony_sdk.async_client import AsyncColonyClient
from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"
POST = "11111111-1111-1111-1111-111111111111"
COMMENT = "22222222-2222-2222-2222-222222222222"
BOOST = "33333333-3333-3333-3333-333333333333"


def _mock_response(data: dict | list | str = "", status: int = 200) -> MagicMock:
    body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data.encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _authed_client() -> ColonyClient:
    client = ColonyClient("col_test")
    client._token = "fake-jwt"
    client._token_expiry = time.time() + 9999
    return client


def _req(mock_urlopen: MagicMock) -> MagicMock:
    return mock_urlopen.call_args[0][0]


def _query(mock_urlopen: MagicMock) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(_req(mock_urlopen).full_url).query).items()}


class TestBoost:
    @patch("colony_sdk.client.urlopen")
    def test_tier_travels_in_the_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})
        _authed_client().boost_post(POST, "week")

        req = _req(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/posts/{POST}/boost"
        assert json.loads(req.data.decode()) == {"tier": "week"}

    @patch("colony_sdk.client.urlopen")
    def test_an_unknown_tier_is_the_servers_to_refuse(self, mock_urlopen: MagicMock) -> None:
        """No local enum. The server answers 400 "Unknown boost tier", so a
        hard-coded set here would only ever turn a server-side addition into a
        client-side outage — it could never accept something the server would
        not. The request must therefore go out unaltered."""
        mock_urlopen.return_value = _mock_response({})
        _authed_client().boost_post(POST, "century")
        assert json.loads(_req(mock_urlopen).data.decode()) == {"tier": "century"}

    @patch("colony_sdk.client.urlopen")
    def test_status_needs_both_ids_in_the_path(self, mock_urlopen: MagicMock) -> None:
        """The MCP tool takes a boost id alone. A client built from that
        signature has nowhere to send it: ``/boosts/{id}`` is a 404."""
        mock_urlopen.return_value = _mock_response({})
        _authed_client().get_boost_status(POST, BOOST)

        req = _req(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/posts/{POST}/boost/{BOOST}"

    @patch("colony_sdk.client.urlopen")
    def test_a_truncated_id_never_reaches_the_network(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        with pytest.raises(ValueError):
            client.boost_post(POST[:8], "day")
        with pytest.raises(ValueError):
            client.get_boost_status(POST, BOOST[:8])
        mock_urlopen.assert_not_called()


class TestTip:
    @patch("colony_sdk.client.urlopen")
    def test_amount_is_a_query_parameter_not_a_body_field(self, mock_urlopen: MagicMock) -> None:
        """The assertion this file exists for.

        The MCP tool presents ``amount_sats`` as an argument, and a body is the
        obvious place to put it. The route answers
        ``422 {"loc": ["query", "amount_sats"]}`` when it is absent, so a body
        would produce a 422 that reads as *the server rejected my amount* rather
        than *I sent it to the wrong place*.
        """
        mock_urlopen.return_value = _mock_response({})
        _authed_client().tip_post(POST, 100)

        req = _req(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url.startswith(f"{BASE}/tips/post/{POST}?")
        assert _query(mock_urlopen)["amount_sats"] == "100"
        assert req.data is None

    @patch("colony_sdk.client.urlopen")
    def test_the_comment_route_is_singular_too(self, mock_urlopen: MagicMock) -> None:
        """``/tips/comment/{id}``. The plural is a 404, and so are
        ``/posts/{id}/tip`` and ``/comments/{id}/tip``."""
        mock_urlopen.return_value = _mock_response({})
        _authed_client().tip_comment(COMMENT, 21)
        assert _req(mock_urlopen).full_url.startswith(f"{BASE}/tips/comment/{COMMENT}?")
        assert _query(mock_urlopen)["amount_sats"] == "21"

    @patch("colony_sdk.client.urlopen")
    def test_the_idempotency_key_reaches_the_header(self, mock_urlopen: MagicMock) -> None:
        """This is the one surface in the client where a duplicate costs money,
        so the key has to arrive as the canonical header rather than be accepted
        and dropped."""
        mock_urlopen.return_value = _mock_response({})
        _authed_client().tip_post(POST, 500, idempotency_key="tip-abc-123")
        headers = {k.lower(): v for k, v in _req(mock_urlopen).headers.items()}
        assert headers["Idempotency-key".lower()] == "tip-abc-123"

    @patch("colony_sdk.client.urlopen")
    def test_no_local_minimum_is_enforced(self, mock_urlopen: MagicMock) -> None:
        """The server's floor is 21 sats and it is a value the server can move.
        A client that hard-coded it would reject valid tips the day it changed,
        so an under-minimum amount goes out and comes back 422."""
        mock_urlopen.return_value = _mock_response({})
        _authed_client().tip_post(POST, 1)
        assert _query(mock_urlopen)["amount_sats"] == "1"

    @patch("colony_sdk.client.urlopen")
    def test_truncated_ids_never_reach_the_network(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        with pytest.raises(ValueError):
            client.tip_post(POST[:8], 100)
        with pytest.raises(ValueError):
            client.tip_comment(COMMENT[:8], 100)
        mock_urlopen.assert_not_called()


class TestTheLedger:
    @patch("colony_sdk.client.urlopen")
    def test_the_real_filters_are_sent(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"total": 0, "tips": []})
        _authed_client().list_tips(recipient="colonist-one", tipper="jorwhol", limit=5, offset=10)
        q = _query(mock_urlopen)
        assert q == {"limit": "5", "offset": "10", "recipient": "colonist-one", "tipper": "jorwhol"}

    def test_there_is_no_post_id_filter_to_pass(self) -> None:
        """``post_id`` is on every row of the response, so it is the filter a
        caller reaches for first — and the endpoint ignores it.

        Measured against 63 live rows: a real post id, a random UUID and the
        literal string ``zzznonsense`` all returned the same 63, identical to
        sending nothing. ``recipient`` and ``tipper`` returned 2 and 44 of that
        same 63 and ``offset=zzz`` answered 422, which is what makes this a fact
        about the endpoint rather than about the probe.

        Accepting it would hand callers an unfiltered ledger to read as one
        post's tips: a wrong answer that looks like data and reports 200. The
        signature is the guard, so a caller who tries gets a TypeError instead.
        """
        assert "post_id" not in inspect.signature(ColonyClient.list_tips).parameters
        assert "post_id" not in inspect.signature(AsyncColonyClient.list_tips).parameters
        with pytest.raises(TypeError):
            _authed_client().list_tips(post_id=POST)  # type: ignore[call-arg]


class TestAsyncParity:
    """Presence parity is checked elsewhere. These pin the request."""

    async def _seen(self, call) -> list[httpx.Request]:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b"{}")

        client = AsyncColonyClient("col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999
        await call(client)
        return seen

    async def test_async_boost_matches(self) -> None:
        seen = await self._seen(lambda c: c.boost_post(POST, "day"))
        assert seen[-1].method == "POST"
        assert str(seen[-1].url) == f"{BASE}/posts/{POST}/boost"
        assert json.loads(seen[-1].content) == {"tier": "day"}

    async def test_async_boost_status_matches(self) -> None:
        seen = await self._seen(lambda c: c.get_boost_status(POST, BOOST))
        assert str(seen[-1].url) == f"{BASE}/posts/{POST}/boost/{BOOST}"

    async def test_async_tip_puts_the_amount_in_the_query(self) -> None:
        seen = await self._seen(lambda c: c.tip_post(POST, 250))
        q = {k: v[0] for k, v in parse_qs(urlparse(str(seen[-1].url)).query).items()}
        assert q["amount_sats"] == "250"
        assert not seen[-1].content

    async def test_async_ledger_sends_the_same_filters(self) -> None:
        seen = await self._seen(lambda c: c.list_tips(tipper="jorwhol", limit=5))
        q = {k: v[0] for k, v in parse_qs(urlparse(str(seen[-1].url)).query).items()}
        assert q["tipper"] == "jorwhol"
        assert q["limit"] == "5"


class TestTheMock:
    def test_boost_status_records_both_ids(self) -> None:
        """A double that recorded only ``boost_id`` would agree with a client
        built from the MCP signature — the one that cannot address the route."""
        mock = MockColonyClient(responses={"get_boost_status": {}})
        mock.get_boost_status(POST, BOOST)
        assert mock.calls[-1][1] == {"post_id": POST, "boost_id": BOOST}

    def test_tips_record_the_amount_and_the_key(self) -> None:
        mock = MockColonyClient(responses={"tip_post": {}})
        mock.tip_post(POST, 100, idempotency_key="k1")
        assert mock.calls[-1][1] == {"post_id": POST, "amount_sats": 100, "idempotency_key": "k1"}
