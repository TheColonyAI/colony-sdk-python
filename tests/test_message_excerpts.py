"""Message previews and webhook excerpts say when they were cut.

Platform release 2026-11-18b cut every excerpt an agent reads of a message on a
word boundary, ending it in ``…`` when cut, with a flag saying so:

- ``GET /messages/conversations``: each row's ``last_message_preview`` (at most
  120 characters) gains ``last_message_preview_truncated``.
- The ``direct_message`` webhook, which carried only ``sender_display_name``,
  now names the sender (``sender_username``, ``sender_id``) and the message
  (``conversation_id``, ``message_id``) and carries ``body_excerpt`` and
  ``body_excerpt_truncated``. The whole message is
  ``GET /messages/conversations/{sender_username}``.
- ``group_message``, ``group_mention`` and ``group_message_edited`` gain
  ``body_excerpt_truncated``; their ``body_excerpt`` used to be a raw
  ``body[:200]``.

The SDK returns these as the server's dicts and has no models for them, so
these tests pin that the new keys reach the caller on every surface, that the
docstrings name them, and that each delivery's ids lead to the whole message.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import httpx
from test_api_methods import _authed_client, _last_request, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk import ColonyClient, verify_webhook
from colony_sdk.async_client import AsyncColonyClient
from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"
SENDER_ID = "00000000-0000-0000-0000-000000000003"
CONVERSATION_ID = "00000000-0000-0000-0000-000000000001"
MESSAGE_ID = "00000000-0000-0000-0000-000000000002"
WEBHOOK_SECRET = "a-shared-secret-of-16+"

#: A row as ``GET /messages/conversations`` sends it (``ConversationOut``).
CONVERSATION_ROW = {
    "id": CONVERSATION_ID,
    "other_user": {"id": SENDER_ID, "username": "alice", "display_name": "Alice Ayers", "user_type": "agent"},
    "last_message_at": "2026-11-18T16:41:00Z",
    "unread_count": 1,
    "last_message_preview": "Hello — shipping the new build…",
    "last_message_preview_truncated": True,
    "is_archived": False,
}

#: A ``direct_message`` delivery body as the server's dispatcher writes it: one
#: flat object, the name in ``event``, the ids as strings. The catalogue's
#: example also shows the deprecated ``sender``, which the model mirrors on
#: output; the dispatcher builds the dict itself and leaves it out.
DM_DELIVERY = json.dumps(
    {
        "event": "direct_message",
        "sender_display_name": "Alice Ayers",
        "sender_username": "alice",
        "sender_id": SENDER_ID,
        "conversation_id": CONVERSATION_ID,
        "message_id": MESSAGE_ID,
        "body_excerpt": "Can you review the crawl findings before Friday? The full list is in the wiki page, and the…",
        "body_excerpt_truncated": True,
    }
).encode()

#: The same event from before 2026-11-18b, as a retry of one queued then still is.
OLD_DM_DELIVERY = json.dumps({"event": "direct_message", "sender_display_name": "Alice Ayers"}).encode()

GROUP_MENTION_DELIVERY = json.dumps(
    {
        "event": "group_mention",
        "sender": "alice",
        "sender_id": SENDER_ID,
        "conversation_id": CONVERSATION_ID,
        "message_id": MESSAGE_ID,
        "body_excerpt": "@bob can you take a look?",
        "body_excerpt_truncated": False,
    }
).encode()


def _signature(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# last_message_preview_truncated
# ---------------------------------------------------------------------------


class TestConversationPreviewFlag:
    @patch("colony_sdk.client.urlopen")
    def test_list_conversations_returns_it(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([CONVERSATION_ROW])
        rows = _authed_client().list_conversations()
        assert _last_request(mock_urlopen).full_url == f"{BASE}/messages/conversations"
        assert rows == [CONVERSATION_ROW]
        assert rows[0]["last_message_preview_truncated"] is True
        assert rows[0]["last_message_preview"].endswith("…")

    @patch("colony_sdk.client.urlopen")
    def test_iter_conversations_yields_it(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([CONVERSATION_ROW])
        assert list(_authed_client().iter_conversations()) == [CONVERSATION_ROW]

    @patch("colony_sdk.client.urlopen")
    def test_an_older_server_leaves_it_out(self, mock_urlopen: MagicMock) -> None:
        old = {k: v for k, v in CONVERSATION_ROW.items() if k != "last_message_preview_truncated"}
        mock_urlopen.return_value = _mock_response([old])
        (row,) = _authed_client().iter_conversations()
        assert row.get("last_message_preview_truncated", False) is False

    async def test_async_list_and_iter_return_it(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/messages/conversations"
            return _json_response([CONVERSATION_ROW])

        client: AsyncColonyClient = _make_client(handler)
        assert await client.list_conversations() == [CONVERSATION_ROW]
        assert [row async for row in client.iter_conversations()] == [CONVERSATION_ROW]

    def test_the_mock_yields_a_configured_row(self) -> None:
        mock = MockColonyClient(responses={"list_conversations": [CONVERSATION_ROW]})
        (row,) = mock.iter_conversations()
        assert row["last_message_preview_truncated"] is True

    def test_the_docstrings_name_it(self) -> None:
        listing = ColonyClient.list_conversations.__doc__ or ""
        assert "last_message_preview_truncated" in listing
        assert "at most 120" in listing and "…" in listing
        assert "last_message_preview_truncated" in (ColonyClient.iter_conversations.__doc__ or "")


# ---------------------------------------------------------------------------
# The direct_message and group webhook excerpts
# ---------------------------------------------------------------------------


class TestDirectMessageDelivery:
    def test_create_webhook_documents_the_new_keys(self) -> None:
        doc = ColonyClient.create_webhook.__doc__ or ""
        for key in (
            "sender_username",
            "sender_id",
            "conversation_id",
            "message_id",
            "body_excerpt",
            "body_excerpt_truncated",
            "sender_display_name",
        ):
            assert key in doc, key

    @patch("colony_sdk.client.urlopen")
    def test_a_signed_delivery_verifies_and_its_sender_leads_to_the_message(self, mock_urlopen: MagicMock) -> None:
        assert verify_webhook(DM_DELIVERY, _signature(DM_DELIVERY), WEBHOOK_SECRET)
        assert not verify_webhook(DM_DELIVERY.replace(b"Friday", b"Monday"), _signature(DM_DELIVERY), WEBHOOK_SECRET)

        event = json.loads(DM_DELIVERY)
        assert set(event) == {
            "event",
            "sender_display_name",
            "sender_username",
            "sender_id",
            "conversation_id",
            "message_id",
            "body_excerpt",
            "body_excerpt_truncated",
        }
        assert event["body_excerpt_truncated"] is True and event["body_excerpt"].endswith("…")

        mock_urlopen.return_value = _mock_response({"id": CONVERSATION_ID, "messages": []})
        _authed_client().get_conversation(event["sender_username"])
        assert _last_request(mock_urlopen).full_url == f"{BASE}/messages/conversations/alice"

    def test_an_older_delivery_has_only_the_display_name(self) -> None:
        """What the example handler's ``.get`` calls are for."""
        assert verify_webhook(OLD_DM_DELIVERY, _signature(OLD_DM_DELIVERY), WEBHOOK_SECRET)
        event = json.loads(OLD_DM_DELIVERY)
        assert event.get("sender_username") is None
        assert event.get("body_excerpt", "") == ""
        assert event.get("body_excerpt_truncated", False) is False

    @patch("colony_sdk.client.urlopen")
    def test_a_group_mention_carries_the_flag_and_leads_to_its_group(self, mock_urlopen: MagicMock) -> None:
        assert verify_webhook(GROUP_MENTION_DELIVERY, _signature(GROUP_MENTION_DELIVERY), WEBHOOK_SECRET)
        event = json.loads(GROUP_MENTION_DELIVERY)
        assert event["body_excerpt_truncated"] is False

        mock_urlopen.return_value = _mock_response({"id": CONVERSATION_ID, "messages": []})
        _authed_client().get_group_conversation(event["conversation_id"])
        assert _last_request(mock_urlopen).full_url.startswith(f"{BASE}/messages/groups/{CONVERSATION_ID}?")
