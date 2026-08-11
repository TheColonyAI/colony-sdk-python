"""Free-text URL path segments are escaped, on BOTH clients.

WHY THIS EXISTS
===============
#141 escaped vault filenames and left every other free-text path segment
raw. The class it fixed still lived at 80 sites: ``username`` (15 per
client), ``slug`` (24) and ``variant`` (1).

Two ways a raw segment fails, both silent:

* a space builds an invalid URL;
* a ``#`` truncates the path at the fragment, so the request addresses a
  DIFFERENT resource and nothing raises anywhere.
  ``get_conversation("bob#admin")`` asks the server about ``bob``.

Both surfaces are covered here on purpose. Reviewing #141 turned up that
its async escaping had no test at all -- removing ``quote()`` from all
four async sites left the whole suite green -- so a fix tested on one
client only is the specific mistake this file is written against.

The controls matter more than the assertions:

* ``test_valid_values_are_untouched`` is what makes the change safe to
  ship. For every legal username, slug and UUID the escape is the
  identity, so nothing that works today changes.
* ``test_prebuilt_query_suffix_is_not_escaped`` guards the other
  direction. ``suffix`` is a pre-built query string at 5 sites; a
  blanket sweep over every ``{...}`` in every path would have escaped it
  and broken every call. Over-escaping is as real a bug as under-.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx

from colony_sdk import ColonyClient
from colony_sdk.async_client import AsyncColonyClient
from colony_sdk.client import _path_segment

BASE = "https://thecolony.ai/api/v1"


def _mock_response(data: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode()
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _authed_client() -> ColonyClient:
    """Pre-seeded token so _ensure_token is a no-op. Mirrors test_api_methods."""
    client = ColonyClient("col_test")
    client._token = "fake-jwt"
    client._token_expiry = time.time() + 9999
    return client


def _async_client(handler) -> AsyncColonyClient:
    """Mirrors _make_client in test_async_client."""
    httpx_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AsyncColonyClient("col_test", client=httpx_client)
    client._token = "fake-jwt"
    client._token_expiry = 9_999_999_999
    return client


def _url(mock_urlopen: MagicMock) -> str:
    return mock_urlopen.call_args[0][0].full_url


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------


class TestPathSegmentHelper:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("bob smith", "bob%20smith"),
            ("bob#admin", "bob%23admin"),
            ("a/b", "a%2Fb"),
            ("a?b", "a%3Fb"),
            ("100%", "100%25"),
            ("café", "caf%C3%A9"),
        ],
    )
    def test_escapes_the_characters_that_break_a_path(self, raw: str, expected: str) -> None:
        assert _path_segment(raw) == expected

    def test_the_separator_is_NOT_preserved(self) -> None:
        """Deliberately unlike the vault's ``safe="/"``.

        A vault filename may contain ``/`` because the vault has folders.
        A username with a ``/`` in it is a different path, not a nested
        one, so here the separator must be encoded.
        """
        assert _path_segment("logs/day") == "logs%2Fday"

    @pytest.mark.parametrize(
        "value",
        [
            "colonist-one",
            "arch_colony",
            "Agent42",
            "the-colony",
            "b01f554e-769b-43d0-89d7-1a2eeb7bd912",
        ],
    )
    def test_valid_values_are_untouched(self, value: str) -> None:
        """THE control that makes this change safe to ship.

        Every legal username, org slug and UUID escapes to itself, so no
        request that works today changes shape. The sweep can only alter
        requests that were already malformed.
        """
        assert _path_segment(value) == value


# ---------------------------------------------------------------------------
# Sync surface
# ---------------------------------------------------------------------------


class TestSyncPathEscaping:
    @patch("colony_sdk.client.urlopen")
    def test_username_with_a_fragment_addresses_the_right_agent(self, mock_urlopen: MagicMock) -> None:
        """Unescaped, this asked the server about ``bob`` and returned
        somebody else's conversation with a 200."""
        mock_urlopen.return_value = _mock_response({"messages": []})
        _authed_client().get_conversation("bob#admin")
        assert _url(mock_urlopen) == f"{BASE}/messages/conversations/bob%23admin"

    @patch("colony_sdk.client.urlopen")
    def test_username_with_a_space(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})
        _authed_client().get_user_by_username("bob smith")
        assert _url(mock_urlopen) == f"{BASE}/users/by-username/bob%20smith"

    @patch("colony_sdk.client.urlopen")
    def test_org_slug_is_escaped(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})
        _authed_client().get_org("my org#2")
        assert _url(mock_urlopen) == f"{BASE}/orgs/my%20org%232"

    @patch("colony_sdk.client.urlopen")
    def test_a_valid_username_produces_an_unchanged_url(self, mock_urlopen: MagicMock) -> None:
        """Companion to the helper-level control, at the call site."""
        mock_urlopen.return_value = _mock_response({"messages": []})
        _authed_client().get_conversation("colonist-one")
        assert _url(mock_urlopen) == f"{BASE}/messages/conversations/colonist-one"

    @patch("colony_sdk.client.urlopen")
    def test_attachment_variant_is_escaped(self, mock_urlopen: MagicMock) -> None:
        """``variant`` is caller-supplied and reaches a path segment.

        Only one site, which is exactly why it was the one my own
        mutation run caught as untested after I had already escaped it.
        """
        resp = MagicMock()
        resp.read.return_value = b"bytes"
        resp.status = 200
        resp.getheaders.return_value = []
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        aid = "b01f554e-769b-43d0-89d7-1a2eeb7bd912"

        _authed_client().get_message_attachment(aid, variant="thumb#2")

        assert _url(mock_urlopen) == f"{BASE}/messages/attachments/{aid}/thumb%232"

    @patch("colony_sdk.client.urlopen")
    def test_prebuilt_query_suffix_is_not_escaped(self, mock_urlopen: MagicMock) -> None:
        """CONTROL, and the reason this sweep was scoped rather than blanket.

        ``suffix`` is an already-built query string interpolated into the
        path at 5 sites. Escaping every ``{...}`` in every path -- the
        obvious way to write this fix -- turns ``?limit=5`` into
        ``%3Flimit%3D5`` and breaks the call.
        """
        mock_urlopen.return_value = _mock_response({"posts": []})
        _authed_client().get_rising_posts(limit=5)
        url = _url(mock_urlopen)
        assert "?" in url and "%3F" not in url, url


# ---------------------------------------------------------------------------
# Async surface — the leg #141 was missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAsyncPathEscaping:
    async def test_username_with_a_fragment(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"messages": []})

        await _async_client(handler).get_conversation("bob#admin")
        assert seen["url"] == f"{BASE}/messages/conversations/bob%23admin"

    async def test_org_slug_is_escaped(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={})

        await _async_client(handler).get_org("my org#2")
        assert seen["url"] == f"{BASE}/orgs/my%20org%232"

    async def test_attachment_variant_is_escaped(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, content=b"bytes")

        aid = "b01f554e-769b-43d0-89d7-1a2eeb7bd912"
        await _async_client(handler).get_message_attachment(aid, variant="thumb#2")
        assert seen["url"] == f"{BASE}/messages/attachments/{aid}/thumb%232"

    async def test_a_valid_username_produces_an_unchanged_url(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"messages": []})

        await _async_client(handler).get_conversation("colonist-one")
        assert seen["url"] == f"{BASE}/messages/conversations/colonist-one"
