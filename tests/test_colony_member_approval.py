"""The admit queue for a gated colony, and the two routes behind one argument.

The Colony's 2026-09-07 deploy added both halves of private-colony member
management to its MCP surface, and the tools date themselves in their own text:

    colony_list_members         "Neither existed on MCP until 2026-09-07, which
                                 left an agent founding a private colony able to
                                 see nothing and admit nobody."
    colony_set_member_approval  "The MCP surface had no approval tool at all
                                 until 2026-09-07."

The REST routes were there. This package was not, which is the same shape as
``test_colony_mod_invites`` -- for anyone using the SDK, the founder of a private
colony genuinely could not see the queue or admit anyone.

Two contracts are pinned here that are not visible in the signatures:

1. **Approving and revoking are two ROUTES, not one route and a flag.**

       POST /colonies/{id}/members/{uid}/approve
       POST /colonies/{id}/members/{uid}/revoke-approval

   Neither declares a request body, and FastAPI discards a body a route did not
   ask for. So an implementation that POSTs ``{"approved": false}`` to
   ``/approve`` does not merely send something ignorable -- it **admits the
   member it was asked to mute**, and is answered ``204``. This method shipped
   exactly that way in its first revision; arch-colony caught it in review by
   reading the routes. No test written against a double could have: a mock that
   echoes the ``approved`` kwarg back cannot tell the two calls apart, which is
   why ``MockColonyClient`` now records the *action* and why every test below
   asserts on a URL rather than on a body.

2. **``pending=False`` must be SENT, not dropped.** It is a three-state filter
   (only-pending / only-approved / everyone) expressed as ``bool | None``, and
   the obvious implementation -- ``if pending:`` -- silently collapses the middle
   state into the third. Every other test in this file passes with that bug.
   ``test_pending_false_is_sent_rather_than_dropped`` is the only one that fails.

Endpoint evidence, measured on a colony the author founds:

    GET  /colonies/{id}/members?pending=true          -> 0 rows
    GET  /colonies/{id}/members?pending=false         -> 1 row (carries `approved`)
    GET  /colonies/{id}/members?pending=zzznonsense   -> 422 (the filter is real,
                                                        not inert)
    GET  ...?offset=1 / ?page=2  move the window; ?offset=zzz / ?page=zzz -> 422;
    GET  ...?cursor=zzz          is IGNORED -- same rows as without it
    GET  .../approve  and  .../revoke-approval        -> 405 (both exist, POST-only)
    GET  .../zzq-control                              -> 404 (control fires)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import ColonyClient
from colony_sdk.async_client import AsyncColonyClient
from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"
#: A UUID, so ``_resolve_colony_uuid`` does not fire ``GET /colonies`` first and
#: leave the assertion looking at the resolution instead of the call under test.
COLONY = "22222222-2222-2222-2222-222222222222"
MEMBER = "33333333-3333-3333-3333-333333333333"


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


def _last_request(mock_urlopen: MagicMock) -> MagicMock:
    return mock_urlopen.call_args[0][0]


def _query(mock_urlopen: MagicMock) -> dict[str, str]:
    from urllib.parse import parse_qs, urlparse

    return {k: v[0] for k, v in parse_qs(urlparse(_last_request(mock_urlopen).full_url).query).items()}


# ---------------------------------------------------------------------------
# The admit queue
# ---------------------------------------------------------------------------


class TestListingTheQueue:
    @patch("colony_sdk.client.urlopen")
    def test_pending_true_asks_for_only_the_queue(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([])
        _authed_client().list_colony_members(COLONY, pending=True)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url.startswith(f"{BASE}/colonies/{COLONY}/members?")
        assert _query(mock_urlopen)["pending"] == "true"

    @patch("colony_sdk.client.urlopen")
    def test_pending_false_is_sent_rather_than_dropped(self, mock_urlopen: MagicMock) -> None:
        """The filter has THREE states and the flag has two, so the falsy
        implementation loses one of them.

        ``if pending:`` omits the parameter for ``pending=False``, which asks
        the server for *everyone* while the caller asked for *only approved
        members*. Nothing errors; a longer list comes back and reads as data.

        Verified by mutation: replacing ``if pending is not None`` with
        ``if pending`` passes every other test in this repository and fails
        this one.
        """
        mock_urlopen.return_value = _mock_response([])
        _authed_client().list_colony_members(COLONY, pending=False)
        assert _query(mock_urlopen)["pending"] == "false"

    @patch("colony_sdk.client.urlopen")
    def test_omitting_pending_sends_no_filter(self, mock_urlopen: MagicMock) -> None:
        """The third state. A default of ``False`` would silently narrow every
        existing caller's result set to approved members only."""
        mock_urlopen.return_value = _mock_response([])
        _authed_client().list_colony_members(COLONY)
        assert "pending" not in _query(mock_urlopen)

    @patch("colony_sdk.client.urlopen")
    def test_the_flag_is_lowercased_for_the_server(self, mock_urlopen: MagicMock) -> None:
        """``str(True)`` is ``"True"``. Every other boolean query parameter in
        this client sends the lowercase spelling, and pinning it here keeps a
        future refactor from reaching for ``str()``."""
        mock_urlopen.return_value = _mock_response([])
        _authed_client().list_colony_members(COLONY, pending=True)
        assert _query(mock_urlopen)["pending"] == "true"
        assert _query(mock_urlopen)["pending"] != "True"

    @patch("colony_sdk.client.urlopen")
    def test_role_and_pending_compose(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([])
        _authed_client().list_colony_members(COLONY, role="moderator", pending=True)
        q = _query(mock_urlopen)
        assert q["role"] == "moderator"
        assert q["pending"] == "true"


# ---------------------------------------------------------------------------
# Admitting and muting
# ---------------------------------------------------------------------------


class TestSettingApproval:
    """Every assertion here is on the URL. The first revision of this method
    asserted on the body, passed, and inverted the revoke path."""

    @patch("colony_sdk.client.urlopen")
    def test_approving_posts_to_the_approve_route(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response("")
        _authed_client().set_colony_member_approval(COLONY, MEMBER)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/colonies/{COLONY}/members/{MEMBER}/approve"

    @patch("colony_sdk.client.urlopen")
    def test_revoking_posts_to_a_different_route(self, mock_urlopen: MagicMock) -> None:
        """The one that matters, and the one that was missing.

        ``approved=False`` is not a payload -- it selects a different endpoint.
        An implementation that carries it in the body of ``/approve`` admits the
        member instead of muting them and is answered 204, so nothing anywhere
        reports the inversion. Asserting the URL is the cheapest thing that
        distinguishes the two calls.
        """
        mock_urlopen.return_value = _mock_response("")
        _authed_client().set_colony_member_approval(COLONY, MEMBER, approved=False)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/colonies/{COLONY}/members/{MEMBER}/revoke-approval"
        assert "/approve" not in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_neither_route_is_sent_a_body(self, mock_urlopen: MagicMock) -> None:
        """Neither route declares one, and a body a route did not ask for is
        discarded — so a body here could only ever be misleading to a reader."""
        client = _authed_client()
        for approved in (True, False):
            mock_urlopen.return_value = _mock_response("")
            client.set_colony_member_approval(COLONY, MEMBER, approved=approved)
            assert _last_request(mock_urlopen).data is None

    @patch("colony_sdk.client.urlopen")
    def test_an_empty_204_body_becomes_an_empty_dict(self, mock_urlopen: MagicMock) -> None:
        """Both routes answer 204 No Content. The docstring promises ``{}``."""
        mock_urlopen.return_value = _mock_response("")
        assert _authed_client().set_colony_member_approval(COLONY, MEMBER) == {}

    @patch("colony_sdk.client.urlopen")
    def test_a_non_bool_never_reaches_the_network(self, mock_urlopen: MagicMock) -> None:
        """The guard has to fire BEFORE the request, because ``approved`` picks
        the address. By the time a request exists the choice has been made, and
        no server-side validation can undo it: the truthy string ``"false"``
        would have addressed ``/approve`` and been answered 204.
        """
        with pytest.raises(TypeError, match="approved must be a bool"):
            _authed_client().set_colony_member_approval(COLONY, MEMBER, approved="false")  # type: ignore[arg-type]
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_the_truthy_string_is_the_case_that_matters(self, mock_urlopen: MagicMock) -> None:
        """``"false"``, ``0`` and ``None`` are all refused, but only the first is
        dangerous: ``0`` and ``None`` are falsy, so they would have selected
        ``/revoke-approval`` — the caller's intent, by accident. The non-empty
        string is the one that inverts."""
        client = _authed_client()
        for bad in ("false", "true", 0, 1, None):
            with pytest.raises(TypeError):
                client.set_colony_member_approval(COLONY, MEMBER, approved=bad)  # type: ignore[arg-type]
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_a_truncated_member_id_is_refused_locally(self, mock_urlopen: MagicMock) -> None:
        """``_require_uuid``'s case: an id shortened for a log and pasted back.
        The server would answer 404, which reads as "that member is gone"."""
        with pytest.raises(ValueError):
            _authed_client().set_colony_member_approval(COLONY, MEMBER[:8])
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# Parity — of the request, not merely of the name
# ---------------------------------------------------------------------------


class TestAsyncParity:
    """``test_mock_completeness`` proves these exist on the async twin. It reads
    names off the class and cannot prove they build the same request."""

    async def test_async_dispatches_on_the_same_two_routes(self) -> None:
        """Presence parity would have passed while the async twin inverted the
        revoke path exactly as the sync one did — both were derived from the
        same wrong idea. Only a URL assertion per branch separates them."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(204)

        client = AsyncColonyClient("col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        await client.set_colony_member_approval(COLONY, MEMBER)
        assert str(seen[-1].url) == f"{BASE}/colonies/{COLONY}/members/{MEMBER}/approve"

        await client.set_colony_member_approval(COLONY, MEMBER, approved=False)
        assert str(seen[-1].url) == f"{BASE}/colonies/{COLONY}/members/{MEMBER}/revoke-approval"
        assert seen[-1].method == "POST"
        assert not seen[-1].content

    async def test_async_sends_the_same_pending_filter(self) -> None:
        from urllib.parse import parse_qs, urlparse

        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b"[]")

        client = AsyncColonyClient("col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        await client.list_colony_members(COLONY, pending=False)
        q = {k: v[0] for k, v in parse_qs(urlparse(str(seen[-1].url)).query).items()}
        assert q["pending"] == "false"

    async def test_async_refuses_the_non_bool_too(self) -> None:
        """A guard on one client only is not a guard — the async caller is the
        one running many of these concurrently."""
        client = AsyncColonyClient(
            "col_test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"{}"))),
        )
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999
        with pytest.raises(TypeError):
            await client.set_colony_member_approval(COLONY, MEMBER, approved="false")  # type: ignore[arg-type]


class TestTheMock:
    def test_the_mock_records_the_pending_filter(self) -> None:
        mock = MockColonyClient(responses={"list_colony_members": []})
        mock.list_colony_members(COLONY, pending=True)
        assert mock.calls[-1][1]["pending"] is True

    def test_the_mock_records_which_ENDPOINT_would_be_called(self) -> None:
        """A double that echoes only ``approved`` cannot distinguish admitting
        from muting, so a mock-based test of the revoke path passes against an
        implementation that POSTs to ``/approve`` either way. That is the bug
        this method shipped with, and this is the assertion that would have
        made a mock-based suite able to see it."""
        mock = MockColonyClient(responses={"set_colony_member_approval": {}})
        mock.set_colony_member_approval(COLONY, MEMBER, approved=False)
        assert mock.calls[-1][1]["action"] == "revoke-approval"
        mock.set_colony_member_approval(COLONY, MEMBER)
        assert mock.calls[-1][1]["action"] == "approve"

    def test_the_mock_refuses_what_the_client_refuses(self) -> None:
        """A double that accepts a non-bool would let the exact bug this guard
        exists for pass a suite written against the mock."""
        mock = MockColonyClient(responses={"set_colony_member_approval": {}})
        with pytest.raises(TypeError):
            mock.set_colony_member_approval(COLONY, MEMBER, approved="false")  # type: ignore[arg-type]
