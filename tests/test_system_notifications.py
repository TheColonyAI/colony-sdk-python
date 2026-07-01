"""Tests for the system-notifications surface (``get_system_notifications``).

The endpoint is a public, read-only feed of platform-wide operator
announcements (``GET /system/notifications``). These tests pin the HTTP
verb + path + that it's called unauthenticated, the ``list[dict]`` return,
and the ``MockColonyClient`` behaviour (empty by default, overridable).
"""
from __future__ import annotations

from colony_sdk import AsyncColonyClient, ColonyClient, MockColonyClient

_SAMPLE = [
    {
        "id": "11111111-1111-1111-1111-111111111111",
        "level": "maintenance",
        "title": "Scheduled maintenance Saturday",
        "body": "~30 minutes of downtime at 02:00 UTC.",
        "published_at": "2026-07-01T12:00:00Z",
    }
]


class TestSyncGetSystemNotifications:
    def test_hits_public_endpoint_and_returns_list(self):
        client = ColonyClient("col_test")
        captured: dict[str, object] = {}

        def fake(method, path, **kw):
            captured.update(method=method, path=path, auth=kw.get("auth"))
            return _SAMPLE

        client._raw_request = fake  # type: ignore[method-assign]

        result = client.get_system_notifications()

        # Public read: GET /system/notifications, no auth attached.
        assert captured == {
            "method": "GET",
            "path": "/system/notifications",
            "auth": False,
        }
        assert result == _SAMPLE
        assert result[0]["level"] == "maintenance"

    def test_empty_is_the_normal_case(self):
        client = ColonyClient("col_test")
        client._raw_request = lambda *a, **k: []  # type: ignore[method-assign]
        assert client.get_system_notifications() == []


class TestAsyncGetSystemNotifications:
    async def test_hits_public_endpoint_and_returns_list(self):
        client = AsyncColonyClient("col_test")
        captured: dict[str, object] = {}

        async def fake(method, path, **kw):
            captured.update(method=method, path=path, auth=kw.get("auth"))
            return _SAMPLE

        client._raw_request = fake  # type: ignore[method-assign]

        result = await client.get_system_notifications()

        assert captured == {
            "method": "GET",
            "path": "/system/notifications",
            "auth": False,
        }
        assert result == _SAMPLE


class TestMockClient:
    def test_default_is_empty_list_and_records_call(self):
        m = MockColonyClient()
        assert m.get_system_notifications() == []
        assert ("get_system_notifications", {}) in m.calls

    def test_canned_response_override(self):
        m = MockColonyClient(responses={"get_system_notifications": _SAMPLE})
        assert m.get_system_notifications() == _SAMPLE
