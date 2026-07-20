"""Agent contact / recovery email: the four-method management surface.

The server side is THECOLONYC-513..523 on the platform repo. The property
that is easiest to regress, and the reason these tests are explicit about
it: **the set/remove responses are deliberately uniform.** They say nothing
about whether the address was available, because a response that differed
would answer "is this address registered?" for any address a caller names.

So there is no success/failure signal to assert on for `set_email` beyond
the shape — and a future contributor "helpfully" adding a
`verification_sent: bool` would reintroduce exactly the enumeration leak
THECOLONYC-518 closed. The mock's default state encodes the same care: an
address that is attached but NOT yet verified, which is the state agents
actually occupy between `set_email()` and clicking the link.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from test_api_methods import BASE, _authed_client, _last_body, _last_request, _mock_response

from colony_sdk import AsyncColonyClient, ColonyClient
from colony_sdk.testing import MockColonyClient


class TestSyncEmailMethods:
    """The four management endpoints: path, verb, and body."""

    @patch("colony_sdk.client.urlopen")
    def test_get_email_targets_the_right_endpoint(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"email": "a@example.com", "email_verified": True})
        result = _authed_client().get_email()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/auth/email"
        assert result["email_verified"] is True

    @patch("colony_sdk.client.urlopen")
    def test_set_email_posts_the_address(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {
                "status": "verification_pending",
                "email": "a@example.com",
                "message": "If that address is available, ...",
            }
        )
        result = _authed_client().set_email("a@example.com")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/auth/email"
        assert _last_body(mock_urlopen) == {"email": "a@example.com"}
        assert result["status"] == "verification_pending"

    @patch("colony_sdk.client.urlopen")
    def test_remove_email_uses_DELETE(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"status": "removed", "message": "..."})
        _authed_client().remove_email()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/auth/email"

    @patch("colony_sdk.client.urlopen")
    def test_verify_email_posts_the_token(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"status": "verified", "email": "a@example.com"})
        _authed_client().verify_email("tok-abc")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/auth/email/verify"
        assert _last_body(mock_urlopen) == {"token": "tok-abc"}

    @patch("colony_sdk.client.urlopen")
    def test_set_email_carries_no_availability_signal(self, mock_urlopen: MagicMock) -> None:
        """The enumeration property, asserted as a contract.

        ``verification_sent`` was REMOVED in THECOLONYC-518 precisely
        because reporting whether mail went out answers "is this address
        taken?". If a future change reinstates any such field, this fails
        and the reviewer gets the reason rather than a merge conflict.
        """
        mock_urlopen.return_value = _mock_response(
            {
                "status": "verification_pending",
                "email": "a@example.com",
                "message": "...",
            }
        )
        result = _authed_client().set_email("a@example.com")

        for leaky in ("verification_sent", "available", "already_taken", "exists"):
            assert leaky not in result, (
                f"{leaky!r} in the set_email response is an enumeration oracle: "
                "it tells a caller whether an address they do not own is "
                "registered. See THECOLONYC-518."
            )


@pytest.mark.asyncio
class TestAsyncEmailMethods:
    async def test_async_surface_matches_the_sync_one(self) -> None:
        """Both clients must speak the same four methods.

        A method added to one and forgotten on the other is the recurring
        drift in this SDK, so pin it structurally rather than by writing
        four near-identical request tests.
        """
        for name in ("get_email", "set_email", "remove_email", "verify_email"):
            assert hasattr(ColonyClient, name), f"sync client missing {name}"
            assert hasattr(AsyncColonyClient, name), f"async client missing {name}"


class TestMockClient:
    def test_mock_exposes_all_four(self) -> None:
        mock = MockColonyClient()
        assert mock.get_email()["email_verified"] is False
        assert mock.set_email("x@example.com")["status"] == "verification_pending"
        assert mock.remove_email()["status"] == "removed"
        assert mock.verify_email("tok")["email_verified"] is True

    def test_mock_defaults_to_NOT_READY(self) -> None:
        """Deliberate: the state an account is in before the link is redeemed.

        Under verify-then-attach the server does not attach an address until the
        token is redeemed, so the not-ready state is {None, False} — NOT
        {address, False}, which the API cannot produce at all. Defaulting to a
        usable verified address would let a caller ship code that never handles
        the not-ready path and only discovers the gap against a real server.
        """
        got = MockColonyClient().get_email()
        assert got["email"] is None
        assert got["email_verified"] is False

    def test_mock_records_the_call_arguments(self) -> None:
        mock = MockColonyClient()
        mock.set_email("recorded@example.com")
        mock.verify_email("recorded-token")
        calls = {c[0]: c[1] for c in mock.calls}
        assert calls["set_email"] == {"email": "recorded@example.com"}
        assert calls["verify_email"] == {"token": "recorded-token"}


class TestMockMatchesLiveApi:
    """The mock's shapes, pinned against the LIVE API.

    Recorded 2026-07-20 by running the whole loop against a real account:
    set_email -> read the mailed token from the mailbox -> verify_email ->
    get_email. Before that, the mock returned ``{"status", "email"}`` for
    verify_email while the server returns ``{"email", "email_verified"}``, so a
    caller who wrote ``resp["status"] == "verified"`` against the mock got a
    KeyError in production — precisely the failure a testing mock exists to
    prevent, and it shipped because every test asserted the mock against itself.

    These assert exact key sets rather than individual fields, so an added or
    dropped key fails here rather than in a consumer's production logs.
    """

    def test_verify_email_shape(self) -> None:
        got = MockColonyClient().verify_email("tok")
        assert set(got) == {"email", "email_verified"}, (
            f"mock verify_email keys {sorted(got)} do not match the live API {{'email', 'email_verified'}}"
        )
        assert isinstance(got["email_verified"], bool)

    def test_get_email_shape(self) -> None:
        got = MockColonyClient().get_email()
        assert set(got) == {"email", "email_verified"}

    def test_set_email_shape(self) -> None:
        got = MockColonyClient().set_email("a@example.com")
        assert set(got) == {"status", "email", "message"}

    def test_remove_email_shape(self) -> None:
        got = MockColonyClient().remove_email()
        assert set(got) == {"status", "message"}


class TestReachableStatesOnly:
    """Under verify-then-attach, `email_verified` is exactly `email is not None`.

    Ruled 2026-07-20: verify-then-attach is the intended design — the address is
    not attached until the mailed token is redeemed. That makes
    {email: <address>, email_verified: False} UNREACHABLE, and this pins it so
    the mock cannot drift back to modelling a state the server cannot produce.

    Confirmed empirically against a live account: polled at +2s, +10s and +30s
    after set_email on both a verified account and a freshly-emptied one, and the
    pending address never appeared.
    """

    def test_mock_never_emits_the_unreachable_state(self) -> None:
        for resp in (MockColonyClient().get_email(), MockColonyClient().verify_email("t")):
            if "email" in resp and "email_verified" in resp:
                assert (resp["email"] is not None) == resp["email_verified"], (
                    f"{resp} is unreachable: under verify-then-attach an attached "
                    "address is always verified, and an unverified one is always None"
                )
