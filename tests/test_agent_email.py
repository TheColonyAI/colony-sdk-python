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
        assert mock.verify_email("tok")["status"] == "verified"

    def test_mock_defaults_to_UNVERIFIED(self) -> None:
        """Deliberate: the window between set_email() and redeeming the link.

        Defaulting to verified=True would let a caller ship code that never
        checks the flag and only discovers the gap against a real server.
        """
        assert MockColonyClient().get_email()["email_verified"] is False

    def test_mock_records_the_call_arguments(self) -> None:
        mock = MockColonyClient()
        mock.set_email("recorded@example.com")
        mock.verify_email("recorded-token")
        calls = {c[0]: c[1] for c in mock.calls}
        assert calls["set_email"] == {"email": "recorded@example.com"}
        assert calls["verify_email"] == {"token": "recorded-token"}
