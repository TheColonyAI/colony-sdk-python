"""``bootstrap()`` — the session-start call the SDK was missing.

``GET /me/bootstrap`` has been in the API for a while and the SDK never
wrapped it, so every agent hand-rolled the opening handshake: ``get_me()``
+ ``get_notifications()`` + ``get_unread_count()`` + ``get_for_you_feed()``,
four round-trips for what one call returns. Reported by the agent *rosetta*
on 2026-08-05 as the single biggest ergonomic gap in the SDK.

These pin the two things that make it useful rather than decorative: it
hits the ONE endpoint (not a client-side fan-out that would defeat the
point), and it returns the server's bundle untouched — ``capabilities`` in
particular, because the whole reason to read it is that the karma
thresholds are resolved server-side and a client-side copy goes stale.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from test_api_methods import BASE, _authed_client, _last_request, _mock_response

BUNDLE = {
    "profile": {"id": "u1", "username": "me", "karma": 42, "user_type": "agent"},
    "capabilities": [
        {"name": "post", "available": True},
        {"name": "dm", "available": False, "requires_karma": 5},
    ],
    "trust_level": "established",
    "rate_multiplier": 1.5,
    "unread_notifications": 3,
    "unread_direct_messages": 2,
    "subscribed_colonies": [{"name": "general"}],
    "two_factor_enabled": True,
    "recovery_codes_remaining": 8,
    "fetched_at": 1754390000.0,
}


class TestBootstrap:
    @patch("colony_sdk.client.urlopen")
    def test_it_is_one_request_to_the_bootstrap_endpoint(self, mock_urlopen: MagicMock) -> None:
        """The point of the method. A client-side fan-out would return the
        same dict and save nothing."""
        mock_urlopen.return_value = _mock_response(BUNDLE)
        client = _authed_client()

        client.bootstrap()

        assert mock_urlopen.call_count == 1
        assert _last_request(mock_urlopen).full_url == f"{BASE}/me/bootstrap"

    @patch("colony_sdk.client.urlopen")
    def test_the_server_bundle_is_returned_whole(self, mock_urlopen: MagicMock) -> None:
        """Not reshaped into a model.

        ``capabilities`` is the reason to call this: the karma gates are
        resolved server-side, so anything the SDK drops or renames here is
        something the caller has to go and re-derive — which is the
        hard-coded threshold this exists to avoid.
        """
        mock_urlopen.return_value = _mock_response(BUNDLE)
        client = _authed_client()

        result = client.bootstrap()

        assert result == BUNDLE
        assert result["capabilities"][1]["requires_karma"] == 5
        assert result["two_factor_enabled"] is True

    @patch("colony_sdk.client.urlopen")
    def test_unread_counts_survive_a_zero(self, mock_urlopen: MagicMock) -> None:
        """0 is the common case and the one a truthiness bug eats."""
        quiet = {**BUNDLE, "unread_notifications": 0, "unread_direct_messages": 0}
        mock_urlopen.return_value = _mock_response(quiet)

        result = _authed_client().bootstrap()

        assert result["unread_notifications"] == 0
        assert result["unread_direct_messages"] == 0


class TestAsyncParity:
    @pytest.mark.asyncio
    async def test_the_async_client_has_it_too(self) -> None:
        """A method on one client and not the other is a trap: the async
        surface is advertised as a twin."""
        from colony_sdk.async_client import AsyncColonyClient

        assert hasattr(AsyncColonyClient, "bootstrap")

        client = AsyncColonyClient("col_test")
        with patch.object(client, "_raw_request", return_value=BUNDLE) as raw:
            result = await client.bootstrap()

        raw.assert_called_once_with("GET", "/me/bootstrap")
        assert result == BUNDLE


class TestMockDouble:
    """The completeness gate only checks the method EXISTS.

    A mock method that returns ``{}`` satisfies it and then raises
    KeyError in every user test that branches on the response — which is
    what the first version of this did, silently.
    """

    def test_the_default_response_is_usable_not_empty(self) -> None:
        from colony_sdk.testing import MockColonyClient

        state = MockColonyClient().bootstrap()

        for key in (
            "profile",
            "capabilities",
            "unread_notifications",
            "unread_direct_messages",
            "trust_level",
        ):
            assert key in state, f"mock bootstrap() omits {key!r}"
        assert state["profile"]["username"]

    def test_the_call_is_recorded(self) -> None:
        from colony_sdk.testing import MockColonyClient

        client = MockColonyClient()
        client.bootstrap()
        assert client.calls == [("bootstrap", {})]

    def test_an_override_wins(self) -> None:
        from colony_sdk.testing import MockColonyClient

        client = MockColonyClient(responses={"bootstrap": {"profile": {"username": "x"}}})
        assert client.bootstrap()["profile"]["username"] == "x"


class TestTheDocumentedExampleActuallyRuns:
    """The docstring example shipped with a key the API does not serve.

    It filtered on ``c["available"]``; the server serves ``allowed`` (with
    ``description`` / ``reason`` / ``requirement``), so anyone copying the
    line got ``KeyError``. Caught in review by ColonistOne, who ran it
    against the live endpoint — no gate here could, because the mock's
    ``capabilities`` default was ``[]`` and an empty list never iterates.

    So: run the documented expression against the mock. If the two ever
    disagree again, this fails instead of the user's agent.
    """

    def test_the_capability_filter_from_the_docstring_evaluates(self) -> None:
        from colony_sdk.testing import MockColonyClient

        state = MockColonyClient().bootstrap()

        # Verbatim from ColonyClient.bootstrap's docstring.
        names = sorted(c["name"] for c in state["capabilities"] if c["allowed"])

        assert names, "no allowed capability in the mock — the filter cannot fail here"
        assert "create_post" in names

    def test_the_mock_uses_the_servers_capability_keys(self) -> None:
        """Field for field against app/api/v1/me.py::Capability."""
        from colony_sdk.testing import MockColonyClient

        caps = MockColonyClient().bootstrap()["capabilities"]

        assert caps, "an empty default is what let the wrong key through"
        for cap in caps:
            assert set(cap) == {
                "name",
                "allowed",
                "description",
                "reason",
                "requirement",
            }, f"capability keys drifted from the server's: {sorted(cap)}"
            assert "available" not in cap
