"""Sync and async must return the SAME shape for bare-array endpoints.

``AsyncColonyClient._raw_request`` used to wrap a non-dict JSON body as
``{"data": parsed}``, so that its ``-> dict`` annotation stayed true. The
sync client never did. Around 38 API endpoints return a bare JSON array
(``GET /colonies``, ``/notifications``, ``/orgs``, ``/webhooks``,
``/users/{id}/followers`` …), so for every one of them the two clients
returned **different types for the same call**:

    client.get_colonies()          -> [{...}, {...}]      # sync
    await client.get_colonies()    -> {"data": [{...}]}   # async

A caller doing ``for c in await client.get_colonies()`` iterated the single
string ``"data"``. The README documents these as returning lists and makes no
sync/async distinction, so the async client was simply wrong — and had been
through at least 1.29.0.

Nothing caught it because no test compared the two clients' *return values*
on the same payload. Everything here does exactly that. The parity assertion
is the point: an individual client can look self-consistent while disagreeing
with its twin, and the twin is the documented contract.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import ColonyClient
from colony_sdk.async_client import AsyncColonyClient

UUID = "11111111-1111-1111-1111-111111111111"

#: One representative call per released method that hits a bare-array
#: endpoint. Kept explicit rather than derived: the point is to name the
#: methods a user actually calls.
BARE_ARRAY_CALLS: list[tuple[str, tuple]] = [
    ("get_colonies", ()),
    ("get_notifications", ()),
    ("list_conversations", ()),
    ("get_webhooks", ()),
    ("list_blocked", ()),
    ("get_followers", (UUID,)),
    ("get_following", (UUID,)),
    # Organisations (added in the same release; they route through
    # _require_list_response, so they also prove that path agrees).
    ("list_my_orgs", ()),
    ("list_my_org_invitations", ()),
    ("list_org_members", ("acme",)),
    ("list_org_resources", ("acme",)),
    ("list_org_delegation_grants", ("acme",)),
    ("list_org_domain_challenges", ("acme",)),
    ("list_org_pending_invitations", ("acme",)),
    ("list_org_disclosure_recipients", ()),
]

ROWS: list[dict[str, Any]] = [
    {"id": "11111111-1111-1111-1111-111111111111", "name": "one", "slug": "one"},
    {"id": "22222222-2222-2222-2222-222222222222", "name": "two", "slug": "two"},
]


def _sync_client() -> ColonyClient:
    c = ColonyClient("col_test")
    c._token = "fake-jwt"
    c._token_expiry = time.time() + 9999
    return c


def _async_client(payload: object) -> AsyncColonyClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    c = AsyncColonyClient("col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    c._token = "fake-jwt"
    c._token_expiry = 9_999_999_999
    return c


def _mock_response(data: object) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode()
    resp.status = 200
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestRawRequestShape:
    """The root cause, at the transport layer."""

    @pytest.mark.asyncio
    async def test_async_passes_a_bare_array_through_unwrapped(self) -> None:
        result = await _async_client(ROWS)._raw_request("GET", "/anything")
        assert result == ROWS
        assert isinstance(result, list), (
            "a bare array must not be re-wrapped as {'data': [...]} — that is "
            "what made the async client disagree with the sync one"
        )

    def test_sync_passes_a_bare_array_through_unwrapped(self) -> None:
        with patch("colony_sdk.client.urlopen") as m:
            m.return_value = _mock_response(ROWS)
            assert _sync_client()._raw_request("GET", "/anything") == ROWS

    @pytest.mark.asyncio
    async def test_a_dict_body_is_still_a_dict_on_both(self) -> None:
        """The control. Only the non-dict branch changed; ordinary object
        responses must be untouched."""
        body = {"id": "x", "nested": {"a": 1}}
        assert await _async_client(body)._raw_request("GET", "/anything") == body
        with patch("colony_sdk.client.urlopen") as m:
            m.return_value = _mock_response(body)
            assert _sync_client()._raw_request("GET", "/anything") == body


class TestClientParity:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,args", BARE_ARRAY_CALLS)
    async def test_sync_and_async_return_the_same_value(self, method: str, args: tuple) -> None:
        """The assertion that would have caught this from the start."""
        assert hasattr(ColonyClient, method), f"{method} missing on sync client"
        assert hasattr(AsyncColonyClient, method), f"{method} missing on async client"

        with patch("colony_sdk.client.urlopen") as m:
            m.return_value = _mock_response(ROWS)
            sync_result = getattr(_sync_client(), method)(*args)

        async_result = await getattr(_async_client(ROWS), method)(*args)
        assert async_result == sync_result, (
            f"{method}: async returned {async_result!r}, sync returned "
            f"{sync_result!r} — the two clients must not disagree on shape"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,args", BARE_ARRAY_CALLS)
    async def test_both_return_an_actual_list_of_the_rows(self, method: str, args: tuple) -> None:
        """Parity alone is not enough — both could be wrong in the same way.
        Pin the shape a caller relies on: iterating yields the rows, not the
        single string ``"data"``."""
        async_result = await getattr(_async_client(ROWS), method)(*args)
        assert isinstance(async_result, list), f"{method} returned {type(async_result).__name__}"
        assert len(async_result) == 2, f"{method} lost rows: {async_result!r}"
        assert async_result[0]["id"] == ROWS[0]["id"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,args", BARE_ARRAY_CALLS)
    async def test_an_empty_result_is_an_empty_list_on_both(self, method: str, args: tuple) -> None:
        """A real empty response must stay an ordinary empty list — the fix
        must not turn 'nothing found' into something exotic."""
        with patch("colony_sdk.client.urlopen") as m:
            m.return_value = _mock_response([])
            sync_result = getattr(_sync_client(), method)(*args)
        async_result = await getattr(_async_client([]), method)(*args)
        assert sync_result == [] and async_result == []


class TestColonyResolverStillWorks:
    """``_resolve_colony_uuid`` read ``{"data": [...]}`` out of the old
    wrapping. It also handled a bare list, so removing the wrapping should be
    invisible to it — but it is the one caller that touched the envelope by
    name, so it gets a direct test rather than an assumption."""

    @pytest.mark.asyncio
    async def test_async_resolves_a_colony_slug_from_a_bare_array(self) -> None:
        payload = [{"id": UUID, "name": "my-colony"}]
        client = _async_client(payload)
        assert await client._resolve_colony_uuid("my-colony") == UUID

    def test_sync_resolves_a_colony_slug_from_a_bare_array(self) -> None:
        with patch("colony_sdk.client.urlopen") as m:
            m.return_value = _mock_response([{"id": UUID, "name": "my-colony"}])
            assert _sync_client()._resolve_colony_uuid("my-colony") == UUID
