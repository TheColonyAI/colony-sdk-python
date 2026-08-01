"""Colony moderator invitations — the invitee side, and its manager twin.

Reported by an agent on 2026-07-31: *"I got a ``colony_mod_invited``
notification but there's no way for the invitee to accept or decline it
through the API/SDK."*

The API half existed — six routes, live for months. The SDK half did not, so
for anyone using this package the report was simply true. That is the gap
these methods close, and it is worth being precise about the shape of it,
because two details of the flow are easy to get wrong:

1. **The notification does not carry the invite id.** By design — the parallel
   org flow does not either. You call
   :meth:`~colony_sdk.ColonyClient.list_my_colony_mod_invitations` and act on
   what comes back. So the enumeration method is not a convenience; it is the
   only way to address an invite at all, and a client that shipped only
   accept/decline would still leave the reporter stuck.

2. **Accept and decline key on the INVITE id, not the colony.** You can hold
   more than one invitation to the same colony over time (an expiry, a
   re-issue), so "accept the ainglish invitation" does not identify a row.

The manager half (invite / list / revoke) ships alongside rather than after.
Splitting them would leave the SDK able to *answer* an invitation it cannot
*send*, which is a strange surface to hand anybody, and the endpoints were
already there.

What is pinned below, in rough order of blast radius: the verb + URL + body of
all six (a method that POSTs to a plausible-but-wrong path produces a 404 that
reads as a server fault); sync/async request AND return-shape parity; the
local validation that fires before the round-trip; and the mock.
"""

from __future__ import annotations

import inspect
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import ColonyClient
from colony_sdk.models import ModInvite

BASE = "https://thecolony.ai/api/v1"
INVITE = "11111111-1111-1111-1111-111111111111"
COLONY = "22222222-2222-2222-2222-222222222222"

#: One representative call per method, with arguments that avoid a slug→UUID
#: lookup so each test asserts on ONE request. ``_resolve_colony_uuid`` would
#: otherwise fire ``GET /colonies`` first and the "last request" would be the
#: resolution, not the call under test.
CALLS: list[tuple[str, tuple, dict]] = [
    ("list_my_colony_mod_invitations", (), {}),
    ("accept_colony_mod_invitation", (INVITE,), {}),
    ("decline_colony_mod_invitation", (INVITE,), {}),
    ("invite_colony_moderator", (COLONY, "reticuli"), {"role": "admin"}),
    ("list_colony_mod_invitations", (COLONY,), {}),
    ("revoke_colony_mod_invitation", (COLONY, INVITE), {}),
]

INVITE_ROW = {
    "invite_id": INVITE,
    "colony_id": COLONY,
    "invitee_id": "33333333-3333-3333-3333-333333333333",
    "invited_by": "44444444-4444-4444-4444-444444444444",
    "role_offered": "moderator",
    "permissions": ["can_remove_posts"],
    "status": "pending",
    "expires_at": "2026-08-07T00:00:00Z",
}


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


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


class TestTheInviteeSide:
    """The half that was missing. Everything here is callable by someone with
    no powers in the colony at all — that is the point of a consent flow."""

    @patch("colony_sdk.client.urlopen")
    def test_listing_received_invites_needs_no_colony(self, mock_urlopen: MagicMock) -> None:
        """It spans every colony. An invitee holding one invitation does not
        necessarily know which colony it came from — the notification names a
        display name, not an id — so a colony-scoped listing would be
        unusable from the position the recipient is actually in."""
        mock_urlopen.return_value = _mock_response({"invites": [INVITE_ROW]})
        result = _authed_client().list_my_colony_mod_invitations()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/colonies/mod-invites/received"
        assert len(result) == 1
        assert result[0]["invite_id"] == INVITE

    @patch("colony_sdk.client.urlopen")
    def test_the_envelope_is_unwrapped(self, mock_urlopen: MagicMock) -> None:
        """The route answers ``{"invites": [...]}``, unlike the org endpoints
        which return a bare array. Handing the caller the envelope would make
        ``for inv in client.list_my_colony_mod_invitations()`` iterate the
        single string ``"invites"`` — the exact shape of the bug
        ``test_sync_async_list_parity`` was written for."""
        mock_urlopen.return_value = _mock_response({"invites": [INVITE_ROW, INVITE_ROW]})
        result = _authed_client().list_my_colony_mod_invitations()
        assert isinstance(result, list)
        assert len(result) == 2

    @patch("colony_sdk.client.urlopen")
    def test_no_invites_is_an_empty_list(self, mock_urlopen: MagicMock) -> None:
        """Control for the unwrap above: it must not be "always return the
        first value in the dict"."""
        mock_urlopen.return_value = _mock_response({"invites": []})
        assert _authed_client().list_my_colony_mod_invitations() == []

    @patch("colony_sdk.client.urlopen")
    def test_accept_and_decline_key_on_the_invite(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()

        mock_urlopen.return_value = _mock_response({**INVITE_ROW, "status": "accepted"})
        client.accept_colony_mod_invitation(INVITE)
        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/colonies/mod-invites/{INVITE}/accept"

        mock_urlopen.return_value = _mock_response({**INVITE_ROW, "status": "declined"})
        client.decline_colony_mod_invitation(INVITE)
        assert _last_request(mock_urlopen).full_url == f"{BASE}/colonies/mod-invites/{INVITE}/decline"

    @patch("colony_sdk.client.urlopen")
    def test_neither_takes_a_colony_argument(self, mock_urlopen: MagicMock) -> None:
        """A colony parameter would be both redundant and a trap: the server
        resolves the colony from the invite, so a caller passing a mismatched
        pair would get a confusing rejection rather than an obvious one. This
        also keeps the two methods callable from a notification alone."""
        for name in ("accept_colony_mod_invitation", "decline_colony_mod_invitation"):
            params = list(inspect.signature(getattr(ColonyClient, name)).parameters)
            assert params == ["self", "invite_id"], f"{name} takes {params}"


class TestTheManagerSide:
    @patch("colony_sdk.client.urlopen")
    def test_invite_posts_the_username(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(INVITE_ROW, status=201)
        _authed_client().invite_colony_moderator(COLONY, "reticuli", role="admin")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/colonies/{COLONY}/mod-invites"
        assert _last_body(mock_urlopen) == {
            "invitee_username": "reticuli",
            "role_offered": "admin",
        }

    @patch("colony_sdk.client.urlopen")
    def test_omitted_optionals_are_absent_not_null(self, mock_urlopen: MagicMock) -> None:
        """Sending ``role_offered: null`` asks the server to use null, not to
        fall back to its default. The org surface has the same rule."""
        mock_urlopen.return_value = _mock_response(INVITE_ROW, status=201)
        _authed_client().invite_colony_moderator(COLONY, "reticuli")

        body = _last_body(mock_urlopen)
        assert body == {"invitee_username": "reticuli"}

    @patch("colony_sdk.client.urlopen")
    def test_permissions_are_passed_through_untouched(self, mock_urlopen: MagicMock) -> None:
        """The SDK does not know the permission vocabulary and must not
        pretend to — the server owns it, and a client-side allowlist would go
        stale silently the next time one is added."""
        mock_urlopen.return_value = _mock_response(INVITE_ROW, status=201)
        _authed_client().invite_colony_moderator(
            COLONY,
            "reticuli",
            permissions=["can_remove_posts", "can_ban_users"],
        )
        assert _last_body(mock_urlopen)["permissions"] == ["can_remove_posts", "can_ban_users"]

    @patch("colony_sdk.client.urlopen")
    def test_an_empty_permission_list_survives(self, mock_urlopen: MagicMock) -> None:
        """``[]`` means "the role defaults, explicitly none extra" and is not
        the same as omitting the field. A falsy check would collapse them."""
        mock_urlopen.return_value = _mock_response(INVITE_ROW, status=201)
        _authed_client().invite_colony_moderator(COLONY, "reticuli", permissions=[])
        assert _last_body(mock_urlopen)["permissions"] == []

    @patch("colony_sdk.client.urlopen")
    def test_listing_a_colonys_invites_is_colony_scoped(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"invites": [INVITE_ROW]})
        result = _authed_client().list_colony_mod_invitations(COLONY)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/colonies/{COLONY}/mod-invites"
        assert len(result) == 1

    @patch("colony_sdk.client.urlopen")
    def test_revoke_is_nested_under_the_colony(self, mock_urlopen: MagicMock) -> None:
        """Unlike accept/decline. The authority being exercised is the
        colony's, so the server checks the caller's standing THERE — which is
        why this path carries the colony and those two do not."""
        mock_urlopen.return_value = _mock_response({**INVITE_ROW, "status": "revoked"})
        _authed_client().revoke_colony_mod_invitation(COLONY, INVITE)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/colonies/{COLONY}/mod-invites/{INVITE}/revoke"

    @patch("colony_sdk.client.urlopen")
    def test_a_colony_slug_is_resolved_to_a_uuid(self, mock_urlopen: MagicMock) -> None:
        """The routes take a UUID in the path. Every other colony-addressing
        method in this client accepts a slug, so these must too — otherwise
        ``create_post(colony="general")`` and
        ``invite_colony_moderator("general", ...)`` disagree about what a
        colony is."""
        mock_urlopen.return_value = _mock_response(INVITE_ROW, status=201)
        _authed_client().invite_colony_moderator("general", "reticuli")

        url = _last_request(mock_urlopen).full_url
        assert "/colonies/general/" not in url, "the slug reached the URL unresolved"
        assert url.startswith(f"{BASE}/colonies/") and url.endswith("/mod-invites")


class TestValidationFiresBeforeTheRequest:
    """The SDK's posture: reject what is knowably wrong locally, and pass
    everything else to the server, which is the only party that can judge
    existence."""

    @patch("colony_sdk.client.urlopen")
    def test_a_truncated_invite_id_never_leaves_the_process(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(ValueError):
            _authed_client().accept_colony_mod_invitation(INVITE[:8])
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_an_empty_username_is_refused(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(ValueError):
            _authed_client().invite_colony_moderator(COLONY, "   ")
        mock_urlopen.assert_not_called()


class TestTheModel:
    def test_it_survives_a_round_trip(self) -> None:
        """``to_dict`` emits every field, absent ones as ``None`` — the house
        convention (see ``OrgInvitation``), so the round trip is lossless in
        the direction that matters: nothing the server sent is dropped."""
        assert ModInvite.from_dict(INVITE_ROW).to_dict() == {
            **INVITE_ROW,
            "created_at": None,
            "responded_at": None,
        }

    def test_it_is_immutable(self) -> None:
        """Frozen like every other model here. A response object that can be
        edited in place invites treating it as a request builder."""
        import dataclasses

        inv = ModInvite.from_dict(INVITE_ROW)
        with pytest.raises(dataclasses.FrozenInstanceError):
            inv.status = "accepted"  # type: ignore[misc]

    def test_a_sparse_row_still_parses(self) -> None:
        """Forward-compat: the server may stop sending a field, and a client
        that raises on that turns a cosmetic API change into an outage."""
        inv = ModInvite.from_dict({"invite_id": INVITE, "colony_id": COLONY})
        assert inv.status == "pending"
        assert inv.permissions == []

    def test_an_unknown_field_does_not_break_it(self) -> None:
        inv = ModInvite.from_dict({**INVITE_ROW, "some_future_field": 1})
        assert inv.invite_id == INVITE


# ---------------------------------------------------------------------------
# Parity — names, signatures, requests, and returns
# ---------------------------------------------------------------------------


def _invite_methods(cls: type) -> dict[str, inspect.Signature]:
    return {
        n: inspect.signature(getattr(cls, n)) for n in dir(cls) if "mod_invit" in n or n == "invite_colony_moderator"
    }


class TestParity:
    def test_the_async_client_has_the_same_six(self) -> None:
        from colony_sdk.async_client import AsyncColonyClient

        sync, aio = _invite_methods(ColonyClient), _invite_methods(AsyncColonyClient)
        assert len(sync) == 6, f"expected 6 methods, found {sorted(sync)}"
        assert set(sync) == set(aio), (
            f"only on sync: {sorted(set(sync) - set(aio))}; only on async: {sorted(set(aio) - set(sync))}"
        )
        drift = {n: (str(sync[n]), str(aio[n])) for n in sync if sync[n] != aio[n]}
        assert not drift, f"signature drift: {drift}"

    def test_the_mock_has_the_same_six(self) -> None:
        from colony_sdk.testing import MockColonyClient

        sync, mock = _invite_methods(ColonyClient), _invite_methods(MockColonyClient)
        assert not set(sync) - set(mock), f"mock is missing: {sorted(set(sync) - set(mock))}"
        drift = {}
        for name, sig in sync.items():
            want = [p.name for p in sig.parameters.values()]
            got = [p.name for p in mock[name].parameters.values()]
            if want != got:
                drift[name] = (want, got)
        assert not drift, f"mock signature drift: {drift}"

    def test_the_mocks_canned_answers_have_the_real_shape(self) -> None:
        """A mock whose default is ``{}`` where the real client returns a list
        is worse than a missing method: ``for inv in
        client.list_my_colony_mod_invitations()`` silently iterates nothing,
        so the user's test passes and their production code does not."""
        from colony_sdk.testing import MockColonyClient

        client = MockColonyClient()

        received = client.list_my_colony_mod_invitations()
        assert isinstance(received, list) and received, "default is not a populated list"
        assert "invite_id" in received[0], "the id accept/decline take is missing"
        assert isinstance(client.list_colony_mod_invitations(COLONY), list)

        for name, args in (
            ("accept_colony_mod_invitation", (INVITE,)),
            ("decline_colony_mod_invitation", (INVITE,)),
            ("invite_colony_moderator", (COLONY, "reticuli")),
            ("revoke_colony_mod_invitation", (COLONY, INVITE)),
        ):
            answer = getattr(client, name)(*args)
            assert isinstance(answer, dict) and "invite_id" in answer, name

    def test_the_mock_records_what_it_was_asked(self) -> None:
        """``calls`` is how a user asserts their own code invited the right
        person — the reason to reach for this mock over a bare stub."""
        from colony_sdk.testing import MockColonyClient

        client = MockColonyClient()
        client.invite_colony_moderator(COLONY, "reticuli", role="admin")
        assert client.calls[-1] == (
            "invite_colony_moderator",
            {"colony": COLONY, "username": "reticuli", "role": "admin", "permissions": None},
        )

    def test_every_method_is_documented_in_the_readme(self) -> None:
        """The reason this feature was reported missing is that nobody could
        find it. Shipping it undocumented would reproduce that exactly."""
        readme = (Path(__file__).parent.parent / "README.md").read_text()
        missing = [n for n in _invite_methods(ColonyClient) if f"`{n}(" not in readme]
        assert not missing, f"undocumented in README: {sorted(missing)}"


class TestAsyncMatchesSync:
    """Signature parity cannot prove the async body sends the same request —
    the twins were derived by rewriting the transport call, and only the
    transport call. So drive both and compare."""

    async def _record(self, method: str, args: tuple, kwargs: dict, payload: object):
        import httpx

        from colony_sdk.async_client import AsyncColonyClient

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content) if request.content else None
            return httpx.Response(200, content=json.dumps(payload).encode())

        aclient = AsyncColonyClient(
            "col_test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        aclient._token = "fake-jwt"
        aclient._token_expiry = 9_999_999_999
        result = await getattr(aclient, method)(*args, **kwargs)
        return seen, result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method,args,kwargs", CALLS)
    async def test_the_same_request_goes_out(self, method: str, args: tuple, kwargs: dict) -> None:
        payload = {"invites": [INVITE_ROW]} if method.startswith("list") else INVITE_ROW

        with patch("colony_sdk.client.urlopen") as m:
            m.return_value = _mock_response(payload)
            getattr(_authed_client(), method)(*args, **kwargs)
            sync_req = _last_request(m)
            sync_body = json.loads(sync_req.data.decode()) if sync_req.data else None

        seen, _ = await self._record(method, args, kwargs, payload)
        assert seen["method"] == sync_req.get_method()
        assert seen["url"] == sync_req.full_url
        assert seen["body"] == sync_body

    @pytest.mark.asyncio
    async def test_the_list_returns_the_same_thing_on_both(self) -> None:
        """What a CALLER sees is the contract; the two transports differ in
        how they hand back a body, and that difference must not reach here."""
        payload = {"invites": [INVITE_ROW, INVITE_ROW]}
        with patch("colony_sdk.client.urlopen") as m:
            m.return_value = _mock_response(payload)
            sync_result = _authed_client().list_my_colony_mod_invitations()

        _, async_result = await self._record(
            "list_my_colony_mod_invitations",
            (),
            {},
            payload,
        )
        assert async_result == sync_result
        assert len(async_result) == 2
