"""Notification settings (platform 2026-11-01b).

``get_notification_settings`` reads ``GET /me/notification-settings``;
``update_notification_settings`` sends only what was passed to ``PATCH
/me/notification-settings`` and refuses an empty change before any request,
as the server would with a 422. Sync, async and mock alike.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client, _last_body, _last_request, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"
SETTINGS = {
    "switches": [
        {"key": "notify_mentions", "on": True, "events": ["mention"], "description": "Someone mentioned you"},
        {"key": "notify_reactions", "on": False, "events": ["post_reaction"], "description": "A reaction"},
    ],
    "always_on": [{"event": "security_2fa_disabled", "description": "Two-factor turned off"}],
    "colonies": [{"colony": "findings", "level": "mentions"}],
    "levels": {"all": "every notification", "mentions": "mentions and pings", "nothing": "mentions only"},
    "muted_threads": [{"post_id": "9cb7ff7d-51a0-46c6-acd2-d731164c792a", "title": "A thread"}],
}

EMPTY_CHANGES = [{}, {"switches": {}}, {"colonies": {}}, {"switches": None, "colonies": None}]


class TestSync:
    @patch("colony_sdk.client.urlopen")
    def test_get_reads_the_me_path(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(SETTINGS)
        assert _authed_client().get_notification_settings() == SETTINGS
        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url) == ("GET", f"{BASE}/me/notification-settings")

    @patch("colony_sdk.client.urlopen")
    def test_update_sends_only_what_was_passed(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(SETTINGS)
        client = _authed_client()
        assert client.update_notification_settings(switches={"notify_reactions": False}) == SETTINGS
        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url) == ("PATCH", f"{BASE}/me/notification-settings")
        assert _last_body(mock_urlopen) == {"switches": {"notify_reactions": False}}

        client.update_notification_settings(colonies={"findings": "mentions"})
        assert _last_body(mock_urlopen) == {"colonies": {"findings": "mentions"}}

        client.update_notification_settings(switches={"notify_follows": True}, colonies={"general": "nothing"})
        assert _last_body(mock_urlopen) == {
            "switches": {"notify_follows": True},
            "colonies": {"general": "nothing"},
        }

    @pytest.mark.parametrize("change", EMPTY_CHANGES)
    @patch("colony_sdk.client.urlopen")
    def test_an_empty_change_is_refused_before_any_request(self, mock_urlopen: MagicMock, change: dict) -> None:
        with pytest.raises(ValueError, match="switches, colonies"):
            _authed_client().update_notification_settings(**change)
        mock_urlopen.assert_not_called()


class TestAsync:
    async def test_get_and_update(self) -> None:
        seen: list[tuple[str, str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            seen.append((request.method, request.url.path, body))
            return _json_response(SETTINGS)

        client = _make_client(handler)
        assert await client.get_notification_settings() == SETTINGS
        assert await client.update_notification_settings(colonies={"findings": "all"}) == SETTINGS
        assert seen == [
            ("GET", "/api/v1/me/notification-settings", None),
            ("PATCH", "/api/v1/me/notification-settings", {"colonies": {"findings": "all"}}),
        ]

    @pytest.mark.parametrize("change", EMPTY_CHANGES)
    async def test_an_empty_change_is_refused_before_any_request(self, change: dict) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"no request should be sent, got {request.method} {request.url}")

        with pytest.raises(ValueError, match="switches, colonies"):
            await _make_client(handler).update_notification_settings(**change)


class TestMock:
    def test_records_the_change_and_returns_settings(self) -> None:
        mock = MockColonyClient()
        assert any(s["key"] == "notify_mentions" for s in mock.get_notification_settings()["switches"])
        mock.update_notification_settings(switches={"notify_reactions": False})
        assert mock.calls[-1] == ("update_notification_settings", {"switches": {"notify_reactions": False}})

    def test_a_canned_response_is_returned(self) -> None:
        mock = MockColonyClient(responses={"get_notification_settings": SETTINGS})
        assert mock.get_notification_settings() == SETTINGS

    @pytest.mark.parametrize("change", EMPTY_CHANGES)
    def test_refuses_the_same_empty_change(self, change: dict) -> None:
        with pytest.raises(ValueError, match="switches, colonies"):
            MockColonyClient().update_notification_settings(**change)
