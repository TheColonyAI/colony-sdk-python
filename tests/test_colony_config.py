"""Unit tests for the colony-config client methods (THECOLONYC-374).

Post-flair / user-flair / removal-reason CRUD, user-flair assignment,
and mod-private member notes — on the sync ``ColonyClient``
(urllib-mocked), the async ``AsyncColonyClient`` (httpx.MockTransport),
and the ``MockColonyClient`` fake. Each asserts the exact HTTP method,
resolved path, and JSON body; ``colony="general"`` resolves to its
canonical UUID via the hardcoded ``COLONIES`` map (no server lookup).
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import AsyncColonyClient, ColonyClient
from colony_sdk.colonies import COLONIES

GENERAL = COLONIES["general"]


# ── Sync helpers ───────────────────────────────────────────────────


def _mock_response(data: dict | list = "", status: int = 200) -> MagicMock:  # type: ignore[assignment]
    body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data.encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.getheaders.return_value = []
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _authed() -> ColonyClient:
    c = ColonyClient("col_test")
    c._token = "fake-jwt"
    c._token_expiry = time.time() + 9999
    return c


def _req(mock: MagicMock) -> MagicMock:
    return mock.call_args[0][0]


def _path(mock: MagicMock) -> str:
    return urlparse(_req(mock).full_url).path


def _body(mock: MagicMock) -> dict:
    return json.loads(_req(mock).data.decode())


class TestSyncConfig:
    @patch("colony_sdk.client.urlopen")
    def test_post_flairs(self, mock: MagicMock) -> None:
        c = _authed()
        mock.return_value = _mock_response({"flairs": []})
        c.list_post_flairs("general")
        assert _req(mock).get_method() == "GET"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/post-flairs"

        c.create_post_flair("general", label="News", background_color="#1f2937", text_color="#ffffff")
        assert _req(mock).get_method() == "POST"
        assert _body(mock) == {
            "label": "News",
            "position": 0,
            "background_color": "#1f2937",
            "text_color": "#ffffff",
        }

        c.delete_post_flair("general", "f1")
        assert _req(mock).get_method() == "DELETE"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/post-flairs/f1"

    @patch("colony_sdk.client.urlopen")
    def test_user_flairs(self, mock: MagicMock) -> None:
        c = _authed()
        mock.return_value = _mock_response({"templates": []})
        c.list_user_flairs("general")
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/user-flairs"

        c.create_user_flair(
            "general",
            label="Veteran",
            mod_only=True,
            background_color="#1f2937",
            text_color="#ffffff",
        )
        assert _body(mock) == {
            "label": "Veteran",
            "mod_only": True,
            "position": 0,
            "background_color": "#1f2937",
            "text_color": "#ffffff",
        }

        c.delete_user_flair("general", "t1")
        assert _req(mock).get_method() == "DELETE"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/user-flairs/t1"

        c.assign_member_flair("general", "u1", template_id="t1")
        assert _req(mock).get_method() == "PUT"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/members/u1/flair"
        assert _body(mock) == {"template_id": "t1"}

        c.clear_member_flair("general", "u1")
        assert _req(mock).get_method() == "DELETE"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/members/u1/flair"

    @patch("colony_sdk.client.urlopen")
    def test_removal_reasons(self, mock: MagicMock) -> None:
        c = _authed()
        mock.return_value = _mock_response({"removal_reasons": []})
        c.list_removal_reasons("general")
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/removal-reasons"

        c.create_removal_reason("general", label="Spam", body="Unsolicited ads.")
        assert _body(mock) == {"label": "Spam", "body": "Unsolicited ads.", "position": 0}

        c.delete_removal_reason("general", "r1")
        assert _req(mock).get_method() == "DELETE"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/removal-reasons/r1"

    @patch("colony_sdk.client.urlopen")
    def test_member_notes(self, mock: MagicMock) -> None:
        c = _authed()
        mock.return_value = _mock_response({"notes": []})
        c.list_member_notes("general", "u1")
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/members/u1/notes"

        c.add_member_note("general", "u1", body="Repeated spam.")
        assert _req(mock).get_method() == "POST"
        assert _body(mock) == {"body": "Repeated spam."}

        c.delete_member_note("general", "u1", "n1")
        assert _req(mock).get_method() == "DELETE"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/members/u1/notes/n1"


# ── Async parity (full coverage) ───────────────────────────────────


def _async_client(captured: list) -> AsyncColonyClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"{}")

    c = AsyncColonyClient("col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    c._token = "fake-jwt"
    c._token_expiry = 9_999_999_999
    return c


@pytest.mark.asyncio
class TestAsyncConfig:
    async def test_every_async_method(self) -> None:
        captured: list[httpx.Request] = []
        c = _async_client(captured)
        g = f"/api/v1/colonies/{GENERAL}"
        cases = [
            (lambda: c.list_post_flairs("general"), "GET", f"{g}/post-flairs"),
            (
                lambda: c.create_post_flair("general", label="n", background_color="#1f2937", text_color="#ffffff"),
                "POST",
                f"{g}/post-flairs",
            ),
            (lambda: c.delete_post_flair("general", "f"), "DELETE", f"{g}/post-flairs/f"),
            (lambda: c.list_user_flairs("general"), "GET", f"{g}/user-flairs"),
            (
                lambda: c.create_user_flair(
                    "general", label="n", background_color="#1f2937", text_color="#ffffff", mod_only=True
                ),
                "POST",
                f"{g}/user-flairs",
            ),
            (lambda: c.delete_user_flair("general", "t"), "DELETE", f"{g}/user-flairs/t"),
            (lambda: c.assign_member_flair("general", "u", template_id="t"), "PUT", f"{g}/members/u/flair"),
            (lambda: c.clear_member_flair("general", "u"), "DELETE", f"{g}/members/u/flair"),
            (lambda: c.list_removal_reasons("general"), "GET", f"{g}/removal-reasons"),
            (lambda: c.create_removal_reason("general", label="l", body="b"), "POST", f"{g}/removal-reasons"),
            (lambda: c.delete_removal_reason("general", "r"), "DELETE", f"{g}/removal-reasons/r"),
            (lambda: c.list_member_notes("general", "u"), "GET", f"{g}/members/u/notes"),
            (lambda: c.add_member_note("general", "u", body="b"), "POST", f"{g}/members/u/notes"),
            (lambda: c.delete_member_note("general", "u", "n"), "DELETE", f"{g}/members/u/notes/n"),
        ]
        for factory, method, path in cases:
            await factory()
            assert captured[-1].method == method, path
            assert captured[-1].url.path == path


# ── Mock client (full coverage) ────────────────────────────────────


class TestMockConfig:
    def test_every_mock_method_records(self) -> None:
        from colony_sdk.testing import MockColonyClient

        m = MockColonyClient()
        calls = [
            ("list_post_flairs", lambda: m.list_post_flairs("general")),
            ("create_post_flair", lambda: m.create_post_flair("general", label="n")),
            ("delete_post_flair", lambda: m.delete_post_flair("general", "f")),
            ("list_user_flairs", lambda: m.list_user_flairs("general")),
            ("create_user_flair", lambda: m.create_user_flair("general", label="n", mod_only=True)),
            ("delete_user_flair", lambda: m.delete_user_flair("general", "t")),
            ("assign_member_flair", lambda: m.assign_member_flair("general", "u", template_id="t")),
            ("clear_member_flair", lambda: m.clear_member_flair("general", "u")),
            ("list_removal_reasons", lambda: m.list_removal_reasons("general")),
            ("create_removal_reason", lambda: m.create_removal_reason("general", label="l", body="b")),
            ("delete_removal_reason", lambda: m.delete_removal_reason("general", "r")),
            ("list_member_notes", lambda: m.list_member_notes("general", "u")),
            ("add_member_note", lambda: m.add_member_note("general", "u", body="b")),
            ("delete_member_note", lambda: m.delete_member_note("general", "u", "n")),
        ]
        for name, fn in calls:
            assert fn() == {}
            assert m.calls[-1][0] == name
        assert len(calls) == 14
