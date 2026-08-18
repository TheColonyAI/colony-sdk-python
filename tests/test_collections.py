"""Collections — the curated, publishable post list.

The Colony has had a complete ``/api/v1/collections`` surface for months and
one public collection network-wide. The SDK had no methods for it at all, so
neither ``ColonyClient`` nor anything built on it (the MCP-less scripts, the
skill wrapper that introspects this class) could reach the feature. These
methods close that.

Signature parity with the async client is pinned by
``test_sync_async_parity``; mock parity by ``test_mock_completeness``. What
neither of those can check is whether a method sends the RIGHT request — a
method that exists on all three clients and PUTs to the wrong path passes both.
That is what this file is for, on both transports.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import ColonyClient
from colony_sdk.async_client import AsyncColonyClient

BASE = "https://thecolony.ai/api/v1"

CID = "11111111-1111-4111-8111-111111111111"
PID = "22222222-2222-4222-8222-222222222222"
UID = "33333333-3333-4333-8333-333333333333"


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


def _last_body(mock_urlopen: MagicMock) -> dict:
    return json.loads(_last_request(mock_urlopen).data.decode())


class TestReads:
    @patch("colony_sdk.client.urlopen")
    def test_list_collections(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []})
        _authed_client().list_collections()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/collections?limit=20&offset=0"

    @patch("colony_sdk.client.urlopen")
    def test_list_collections_scoped_to_a_curator(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []})
        _authed_client().list_collections(user_id=UID, limit=5, offset=10)

        req = _last_request(mock_urlopen)
        assert req.full_url == f"{BASE}/collections?limit=5&offset=10&user_id={UID}"

    @patch("colony_sdk.client.urlopen")
    def test_user_id_is_omitted_when_not_given(self, mock_urlopen: MagicMock) -> None:
        """A ``user_id=None`` sent as a literal would scope the list to a
        curator called "None" — or, worse on some servers, be dropped and
        silently widen. Assert it is absent, not merely falsy."""
        mock_urlopen.return_value = _mock_response({"items": []})
        _authed_client().list_collections()
        assert "user_id" not in _last_request(mock_urlopen).full_url

    @patch("colony_sdk.client.urlopen")
    def test_get_collection(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": CID})
        _authed_client().get_collection(CID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/collections/{CID}"


class TestWrites:
    @patch("colony_sdk.client.urlopen")
    def test_create_collection(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": CID})
        _authed_client().create_collection("Reading list", description="Why")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/collections"
        assert _last_body(mock_urlopen) == {
            "title": "Reading list",
            "is_public": True,
            "description": "Why",
        }

    @patch("colony_sdk.client.urlopen")
    def test_create_defaults_to_public(self, mock_urlopen: MagicMock) -> None:
        """The server's default is public and so is ours. If these ever
        disagree, a caller who omits the flag gets the opposite of what the
        docstring promises."""
        mock_urlopen.return_value = _mock_response({"id": CID})
        _authed_client().create_collection("No flag")
        assert _last_body(mock_urlopen)["is_public"] is True

    @patch("colony_sdk.client.urlopen")
    def test_create_omits_description_when_not_given(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": CID})
        _authed_client().create_collection("Bare")
        assert "description" not in _last_body(mock_urlopen)

    @patch("colony_sdk.client.urlopen")
    def test_update_sends_only_what_changed(self, mock_urlopen: MagicMock) -> None:
        """The server treats an omitted field as 'unchanged'. Sending
        ``{"description": None}`` for an argument the caller never passed would
        blank a blurb they meant to keep."""
        mock_urlopen.return_value = _mock_response({"id": CID})
        _authed_client().update_collection(CID, title="Renamed")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/collections/{CID}"
        assert _last_body(mock_urlopen) == {"title": "Renamed"}

    @patch("colony_sdk.client.urlopen")
    def test_update_can_unpublish(self, mock_urlopen: MagicMock) -> None:
        """``is_public=False`` is falsy, so an ``if is_public:`` guard would
        drop the one update that matters most."""
        mock_urlopen.return_value = _mock_response({"id": CID})
        _authed_client().update_collection(CID, is_public=False)
        assert _last_body(mock_urlopen) == {"is_public": False}

    @patch("colony_sdk.client.urlopen")
    def test_delete_collection(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})
        _authed_client().delete_collection(CID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/collections/{CID}"


class TestItems:
    @patch("colony_sdk.client.urlopen")
    def test_add_to_collection_with_a_note(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": PID})
        _authed_client().add_to_collection(CID, PID, note="The tail is the good bit.")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/collections/{CID}/items"
        assert _last_body(mock_urlopen) == {
            "post_id": PID,
            "note": "The tail is the good bit.",
        }

    @patch("colony_sdk.client.urlopen")
    def test_add_omits_the_note_when_not_given(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": PID})
        _authed_client().add_to_collection(CID, PID)
        assert _last_body(mock_urlopen) == {"post_id": PID}

    @patch("colony_sdk.client.urlopen")
    def test_remove_from_collection(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})
        _authed_client().remove_from_collection(CID, PID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/collections/{CID}/items/{PID}"


class TestTruncatedIdsAreRejected:
    """A UUID truncated for display and pasted back reads as 'deleted' when the
    server 404s. Same guard as everywhere else in this client."""

    @pytest.mark.parametrize(
        "call",
        [
            lambda c: c.get_collection("11111111"),
            lambda c: c.delete_collection("11111111"),
            lambda c: c.add_to_collection("11111111", PID),
            lambda c: c.remove_from_collection(CID, "22222222"),
        ],
    )
    def test_a_uuid_prefix_raises_before_the_request(self, call) -> None:
        with pytest.raises(ValueError):
            call(_authed_client())


# ── Async parity of BEHAVIOUR, not merely of signature ─────────────────
#
# test_sync_async_parity proves the async methods exist. It cannot prove they
# send the same request, which is the failure that actually reaches a user.


class TestAsyncSendsTheSameRequests:
    @pytest.mark.asyncio
    async def test_async_collection_calls_hit_the_same_paths(self) -> None:
        import httpx

        seen: list[tuple[str, str, dict | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            seen.append((request.method, str(request.url), body))
            return httpx.Response(200, json={"ok": True})

        client = AsyncColonyClient("col_test")
        client._token = "fake-jwt"
        client._token_expiry = time.time() + 9999
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=BASE,
        )

        await client.list_collections()
        await client.get_collection(CID)
        await client.create_collection("T")
        await client.update_collection(CID, title="R")
        await client.add_to_collection(CID, PID, note="n")
        await client.remove_from_collection(CID, PID)
        await client.delete_collection(CID)

        methods_paths = [(m, u.replace(BASE, "")) for m, u, _ in seen]
        assert methods_paths == [
            ("GET", "/collections?limit=20&offset=0"),
            ("GET", f"/collections/{CID}"),
            ("POST", "/collections"),
            ("PUT", f"/collections/{CID}"),
            ("POST", f"/collections/{CID}/items"),
            ("DELETE", f"/collections/{CID}/items/{PID}"),
            ("DELETE", f"/collections/{CID}"),
        ]
        assert seen[2][2] == {"title": "T", "is_public": True}
        assert seen[4][2] == {"post_id": PID, "note": "n"}
