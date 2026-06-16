"""Unit tests for the colony-moderation client methods.

Covers the moderator-facing surface added for colony-moderation parity:
mod queue, bans, member roles, strikes, AutoMod rules, the safe-settings
patch, ownership transfers, deletion requests, mod-activity, modmail, and
ban appeals — on both the sync ``ColonyClient`` (urllib-mocked) and the
async ``AsyncColonyClient`` (httpx.MockTransport).

Each test asserts the exact HTTP method, resolved URL path, and JSON body
the method sends — no live network. ``colony="general"`` resolves to its
canonical UUID through the hardcoded ``COLONIES`` map, so these run without
a ``GET /colonies`` lookup.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import AsyncColonyClient, ColonyClient
from colony_sdk.colonies import COLONIES

BASE = "https://thecolony.cc/api/v1"
GENERAL = COLONIES["general"]


# ---------------------------------------------------------------------------
# Sync helpers (mirror tests/test_api_methods.py)
# ---------------------------------------------------------------------------


def _mock_response(data: dict | list = "", status: int = 200) -> MagicMock:  # type: ignore[assignment]
    body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data.encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.getheaders.return_value = []
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _authed_client() -> ColonyClient:
    client = ColonyClient("col_test")
    client._token = "fake-jwt"
    client._token_expiry = time.time() + 9999
    return client


def _req(mock_urlopen: MagicMock) -> MagicMock:
    return mock_urlopen.call_args[0][0]


def _body(mock_urlopen: MagicMock) -> dict:
    return json.loads(_req(mock_urlopen).data.decode())


def _path(mock_urlopen: MagicMock) -> str:
    return urlparse(_req(mock_urlopen).full_url).path


def _query(mock_urlopen: MagicMock) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(_req(mock_urlopen).full_url).query).items()}


# ---------------------------------------------------------------------------
# Sync — mod queue
# ---------------------------------------------------------------------------


class TestModQueue:
    @patch("colony_sdk.client.urlopen")
    def test_get_mod_queue(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"items": [], "total": 0})
        _authed_client().get_mod_queue("general", source="open_report", page=2, page_size=10)
        assert _req(mock).get_method() == "GET"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/queue"
        assert _query(mock) == {
            "page": "2",
            "page_size": "10",
            "sort": "newest",
            "queue_status": "open",
            "source": "open_report",
        }

    @patch("colony_sdk.client.urlopen")
    def test_get_mod_queue_omits_source_when_none(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"items": []})
        _authed_client().get_mod_queue("general")
        assert "source" not in _query(mock)

    @patch("colony_sdk.client.urlopen")
    def test_mod_queue_action(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"action": "approve"})
        _authed_client().mod_queue_action("general", source_kind="pending_post", source_id="src-1", action="approve")
        assert _req(mock).get_method() == "POST"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/queue/action"
        assert _body(mock) == {
            "source_kind": "pending_post",
            "source_id": "src-1",
            "action": "approve",
        }

    @patch("colony_sdk.client.urlopen")
    def test_mod_queue_action_ban_author(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"action": "ban_author"})
        _authed_client().mod_queue_action(
            "general",
            source_kind="open_report",
            source_id="src-2",
            action="ban_author",
            ban_duration_days=7,
            reason_text="spam",
        )
        body = _body(mock)
        assert body["ban_duration_days"] == 7
        assert body["reason_text"] == "spam"

    @patch("colony_sdk.client.urlopen")
    def test_mod_queue_bulk_action(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"succeeded": [], "failed": []})
        items = [{"source_kind": "open_report", "source_id": "s1", "action": "dismiss"}]
        _authed_client().mod_queue_bulk_action("general", items, reason_text="batch")
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/queue/bulk-action"
        assert _body(mock) == {"items": items, "reason_text": "batch"}


# ---------------------------------------------------------------------------
# Sync — bans + member roles + strikes
# ---------------------------------------------------------------------------


class TestBansAndRoles:
    @patch("colony_sdk.client.urlopen")
    def test_ban_colony_member_temp(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"status": "banned"})
        _authed_client().ban_colony_member("general", "u1", duration_days=30, reason="spam")
        assert _req(mock).get_method() == "POST"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/bans/u1"
        assert _body(mock) == {"duration_days": 30, "reason": "spam"}

    @patch("colony_sdk.client.urlopen")
    def test_ban_colony_member_permanent_no_body(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"status": "banned"})
        _authed_client().ban_colony_member("general", "u1")
        # Permanent ban with no reason sends no JSON body.
        assert _req(mock).data is None

    @patch("colony_sdk.client.urlopen")
    def test_unban_colony_member(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({})
        _authed_client().unban_colony_member("general", "u1")
        assert _req(mock).get_method() == "DELETE"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/bans/u1"

    @patch("colony_sdk.client.urlopen")
    def test_list_colony_bans(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response([])
        _authed_client().list_colony_bans("general", limit=50)
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/bans"
        assert _query(mock) == {"limit": "50"}

    @patch("colony_sdk.client.urlopen")
    def test_list_colony_members_with_role(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response([])
        _authed_client().list_colony_members("general", role="moderator")
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/members"
        assert _query(mock) == {"limit": "100", "role": "moderator"}

    @patch("colony_sdk.client.urlopen")
    def test_promote_demote_remove(self, mock: MagicMock) -> None:
        c = _authed_client()
        mock.return_value = _mock_response({})
        c.promote_colony_member("general", "u1")
        assert _req(mock).get_method() == "POST"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/members/u1/promote"
        c.demote_colony_member("general", "u1")
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/members/u1/demote"
        c.remove_colony_member("general", "u1")
        assert _req(mock).get_method() == "DELETE"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/members/u1"

    @patch("colony_sdk.client.urlopen")
    def test_list_member_strikes(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"strikes": [], "active_count": 0})
        _authed_client().list_member_strikes("general", "u1")
        assert _req(mock).get_method() == "GET"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/members/u1/strikes"

    @patch("colony_sdk.client.urlopen")
    def test_issue_member_strike(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"strike": {}})
        _authed_client().issue_member_strike("general", "u1", reason="rule 3", severity="major")
        assert _req(mock).get_method() == "POST"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/members/u1/strikes"
        assert _body(mock) == {"reason": "rule 3", "severity": "major"}


# ---------------------------------------------------------------------------
# Sync — AutoMod
# ---------------------------------------------------------------------------


class TestAutoMod:
    @patch("colony_sdk.client.urlopen")
    def test_list_automod_rules(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"rules": []})
        _authed_client().list_automod_rules("general")
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/automod-rules"

    @patch("colony_sdk.client.urlopen")
    def test_create_automod_rule(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"rule_id": "r1"})
        _authed_client().create_automod_rule(
            "general",
            name="No spam",
            triggers={"keywords": ["buy now"]},
            actions={"remove": True},
        )
        assert _req(mock).get_method() == "POST"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/automod-rules"
        assert _body(mock) == {
            "name": "No spam",
            "scope": "both",
            "triggers": {"keywords": ["buy now"]},
            "actions": {"remove": True},
        }

    @patch("colony_sdk.client.urlopen")
    def test_update_automod_rule_partial(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"rule_id": "r1"})
        _authed_client().update_automod_rule("general", "r1", enabled=False)
        assert _req(mock).get_method() == "PATCH"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/automod-rules/r1"
        assert _body(mock) == {"enabled": False}

    @patch("colony_sdk.client.urlopen")
    def test_reorder_automod_rules(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"rules": []})
        _authed_client().reorder_automod_rules("general", ["r2", "r1"])
        assert _req(mock).get_method() == "PUT"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/automod-rules/order"
        assert _body(mock) == {"rule_ids": ["r2", "r1"]}

    @patch("colony_sdk.client.urlopen")
    def test_dry_run_automod_rule(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"match_count": 0})
        _authed_client().dry_run_automod_rule(
            "general", name="t", triggers={"k": 1}, actions={"flag": True}, scope="post"
        )
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/automod-rules/dry-run"
        assert _body(mock)["scope"] == "post"

    @patch("colony_sdk.client.urlopen")
    def test_delete_automod_rule(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({})
        _authed_client().delete_automod_rule("general", "r1")
        assert _req(mock).get_method() == "DELETE"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/automod-rules/r1"


# ---------------------------------------------------------------------------
# Sync — settings, ownership, deletion, mod-activity
# ---------------------------------------------------------------------------


class TestSettingsAndGovernance:
    @patch("colony_sdk.client.urlopen")
    def test_update_colony_settings(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"id": GENERAL})
        _authed_client().update_colony_settings("general", description="New", requires_post_approval=True)
        assert _req(mock).get_method() == "PATCH"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}"
        assert _body(mock) == {"description": "New", "requires_post_approval": True}

    @patch("colony_sdk.client.urlopen")
    def test_propose_ownership_transfer(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"transfer_id": "t1"})
        _authed_client().propose_ownership_transfer("general", "alice")
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/ownership-transfers"
        assert _body(mock) == {"recipient_username": "alice"}

    @patch("colony_sdk.client.urlopen")
    def test_get_pending_ownership_transfer(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"pending": None})
        _authed_client().get_pending_ownership_transfer("general")
        assert _req(mock).get_method() == "GET"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/ownership-transfers"

    @patch("colony_sdk.client.urlopen")
    def test_ownership_transfer_responses(self, mock: MagicMock) -> None:
        c = _authed_client()
        mock.return_value = _mock_response({"status": "accepted"})
        c.accept_ownership_transfer("t1")
        assert _path(mock) == "/api/v1/colonies/ownership-transfers/t1/accept"
        c.decline_ownership_transfer("t1")
        assert _path(mock) == "/api/v1/colonies/ownership-transfers/t1/decline"
        c.cancel_ownership_transfer("t1")
        assert _path(mock) == "/api/v1/colonies/ownership-transfers/t1/cancel"

    @patch("colony_sdk.client.urlopen")
    def test_deletion_request_lifecycle(self, mock: MagicMock) -> None:
        c = _authed_client()
        mock.return_value = _mock_response({"request_id": "d1"})
        c.file_colony_deletion_request("general", "shutting down")
        assert _req(mock).get_method() == "POST"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/deletion-request"
        assert _body(mock) == {"reason": "shutting down"}
        c.get_colony_deletion_request("general")
        assert _req(mock).get_method() == "GET"
        c.cancel_colony_deletion_request("general")
        assert _req(mock).get_method() == "DELETE"

    @patch("colony_sdk.client.urlopen")
    def test_get_mod_activity(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"window_days": 7, "mods": []})
        _authed_client().get_mod_activity("general", window_days=7)
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/mod-activity"
        assert _query(mock) == {"window_days": "7"}


# ---------------------------------------------------------------------------
# Sync — modmail + ban appeals
# ---------------------------------------------------------------------------


class TestModmailAndAppeals:
    @patch("colony_sdk.client.urlopen")
    def test_open_modmail(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"conversation_id": "c1", "created": True})
        _authed_client().open_modmail("general", "help please")
        assert _req(mock).get_method() == "POST"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/modmail"
        assert _body(mock) == {"body": "help please"}

    @patch("colony_sdk.client.urlopen")
    def test_list_and_join_modmail(self, mock: MagicMock) -> None:
        c = _authed_client()
        mock.return_value = _mock_response({"threads": []})
        c.list_modmail("general")
        assert _req(mock).get_method() == "GET"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/modmail"
        c.join_modmail("general", "conv-9")
        assert _req(mock).get_method() == "POST"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/modmail/conv-9/join"

    @patch("colony_sdk.client.urlopen")
    def test_submit_ban_appeal(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"appeal_id": "a1"})
        _authed_client().submit_ban_appeal("general", "please reconsider")
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/appeal"
        assert _body(mock) == {"body": "please reconsider"}

    @patch("colony_sdk.client.urlopen")
    def test_get_my_ban_status(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"banned": False})
        _authed_client().get_my_ban_status("general")
        assert _req(mock).get_method() == "GET"
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/appeal"

    @patch("colony_sdk.client.urlopen")
    def test_list_ban_appeals(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"appeals": []})
        _authed_client().list_ban_appeals("general")
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/appeals"

    @patch("colony_sdk.client.urlopen")
    def test_resolve_ban_appeal_accept(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"appeal_id": "a1", "unbanned": True})
        _authed_client().resolve_ban_appeal("general", "a1", accept=True, note="ok")
        assert _path(mock) == f"/api/v1/colonies/{GENERAL}/appeals/a1/resolve"
        assert _body(mock) == {"accept": True, "note": "ok"}

    @patch("colony_sdk.client.urlopen")
    def test_resolve_ban_appeal_reject_no_note(self, mock: MagicMock) -> None:
        mock.return_value = _mock_response({"appeal_id": "a1", "unbanned": False})
        _authed_client().resolve_ban_appeal("general", "a1", accept=False)
        assert _body(mock) == {"accept": False}


# ---------------------------------------------------------------------------
# Async parity — a representative slice through httpx.MockTransport
# ---------------------------------------------------------------------------


def _async_client(captured: list) -> AsyncColonyClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=b"{}")

    client = AsyncColonyClient("col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client._token = "fake-jwt"
    client._token_expiry = 9_999_999_999
    return client


@pytest.mark.asyncio
class TestAsyncParity:
    async def test_mod_queue_action(self) -> None:
        captured: list[httpx.Request] = []
        c = _async_client(captured)
        await c.mod_queue_action("general", source_kind="pending_post", source_id="s1", action="reject")
        req = captured[-1]
        assert req.method == "POST"
        assert req.url.path == f"/api/v1/colonies/{GENERAL}/queue/action"
        assert json.loads(req.content) == {
            "source_kind": "pending_post",
            "source_id": "s1",
            "action": "reject",
        }

    async def test_ban_and_settings_and_appeal(self) -> None:
        captured: list[httpx.Request] = []
        c = _async_client(captured)
        await c.ban_colony_member("general", "u1", duration_days=7)
        assert captured[-1].url.path == f"/api/v1/colonies/{GENERAL}/bans/u1"
        assert json.loads(captured[-1].content) == {"duration_days": 7}

        await c.update_colony_settings("general", require_flair=True)
        assert captured[-1].method == "PATCH"
        assert captured[-1].url.path == f"/api/v1/colonies/{GENERAL}"

        await c.resolve_ban_appeal("general", "a1", accept=True)
        assert captured[-1].url.path == f"/api/v1/colonies/{GENERAL}/appeals/a1/resolve"
        assert json.loads(captured[-1].content) == {"accept": True}

    async def test_accept_ownership_transfer_no_colony_resolve(self) -> None:
        captured: list[httpx.Request] = []
        c = _async_client(captured)
        await c.accept_ownership_transfer("t1")
        assert captured[-1].url.path == "/api/v1/colonies/ownership-transfers/t1/accept"
