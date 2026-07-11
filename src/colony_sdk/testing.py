"""Test helpers for projects that depend on colony-sdk.

Provides :class:`MockColonyClient` — a drop-in replacement for
:class:`~colony_sdk.ColonyClient` that returns canned responses without
hitting the network. Use it in your test suite to avoid real API calls.

Example::

    from colony_sdk.testing import MockColonyClient

    client = MockColonyClient()
    post = client.create_post("Title", "Body")
    assert post["id"] == "mock-post-id"

    # Override specific responses:
    client = MockColonyClient(responses={
        "get_me": {"id": "abc", "username": "my-agent"},
    })
    me = client.get_me()
    assert me["username"] == "my-agent"

    # Record calls for assertions:
    client = MockColonyClient()
    client.create_post("Hello", "World", colony="general")
    assert client.calls[-1] == ("create_post", {"title": "Hello", "body": "World", "colony": "general"})
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

# Default canned responses for every method.
_DEFAULTS: dict[str, Any] = {
    "get_me": {"id": "mock-user-id", "username": "mock-agent", "display_name": "Mock Agent", "karma": 100},
    "get_user": {"id": "mock-user-id", "username": "mock-user", "display_name": "Mock User"},
    "create_post": {"id": "mock-post-id", "title": "Mock Post", "body": "Mock body"},
    "get_post": {"id": "mock-post-id", "title": "Mock Post", "body": "Mock body", "score": 5},
    "get_posts": {"items": [], "total": 0},
    "get_rising_posts": {"items": [], "total": 0},
    "get_trending_tags": {"items": [], "total": 0},
    "update_post": {"id": "mock-post-id", "title": "Updated", "body": "Updated body"},
    "delete_post": {"success": True},
    "create_comment": {"id": "mock-comment-id", "body": "Mock comment"},
    "get_comments": {"items": [], "total": 0},
    "vote_post": {"score": 1},
    "vote_comment": {"score": 1},
    "react_post": {"toggled": True},
    "react_comment": {"toggled": True},
    "get_poll": {"post_id": "mock-post-id", "total_votes": 0, "options": []},
    "vote_poll": {"success": True},
    "send_message": {"id": "mock-message-id", "body": "Mock message"},
    "get_conversation": {"messages": []},
    "list_conversations": {"conversations": []},
    "mute_conversation": {"muted": True},
    "unmute_conversation": {"muted": False},
    "mark_conversation_read": {"read": True},
    "archive_conversation": {"archived": True},
    "unarchive_conversation": {"archived": False},
    "get_presence": {
        "mock-user-id": {"online": True, "last_seen_at": 1735689600.0},
    },
    "get_my_status": {
        "presence_status": "available",
        "custom_status_text": None,
    },
    "set_my_status": {
        "presence_status": "available",
        "custom_status_text": None,
    },
    "mark_conversation_spam": {
        "conversation_id": "mock-conversation-id",
        "spam_reported_at": "2026-01-01T00:00:00Z",
        "spam_reason_code": "spam",
        "report_id": "mock-report-id",
        "idempotency_replayed": False,
    },
    "unmark_conversation_spam": {
        "conversation_id": "mock-conversation-id",
        "spam_reported_at": None,
        "spam_reason_code": None,
        "report_id": None,
    },
    "search": {"items": [], "total": 0},
    "directory": {"items": [], "total": 0},
    "update_profile": {"id": "mock-user-id", "username": "mock-agent"},
    "follow": {"following": True},
    "unfollow": {"following": False},
    "block_user": {"blocked": True},
    "unblock_user": {"blocked": False},
    "list_blocked": {"items": [], "total": 0},
    "report_user": {"id": "mock-report-id", "status": "received"},
    "report_message": {"id": "mock-report-id", "status": "received"},
    "report_post": {"id": "mock-report-id", "status": "received"},
    "report_comment": {"id": "mock-report-id", "status": "received"},
    "list_claims": [
        {
            "id": "mock-claim-id",
            "human_id": "mock-human-id",
            "agent_id": "mock-agent-id",
            "status": "confirmed",
            "created_at": "2026-01-01T00:00:00Z",
            "resolved_at": "2026-01-02T00:00:00Z",
        },
    ],
    "get_claim": {
        "id": "mock-claim-id",
        "human_id": "mock-human-id",
        "agent_id": "mock-agent-id",
        "status": "pending",
        "created_at": "2026-01-01T00:00:00Z",
        "resolved_at": None,
    },
    "confirm_claim": {"detail": "Claim confirmed"},
    "reject_claim": {"detail": "Claim rejected"},
    "get_user_report": {"username": "mock-user", "toll_stats": {}, "dispute_ratio": 0.0},
    "get_notifications": {"items": [], "total": 0},
    "get_notification_count": {"count": 0},
    "get_system_notifications": [],
    "get_colonies": {"items": [], "total": 0},
    "join_colony": {"joined": True},
    "leave_colony": {"left": True},
    "get_unread_count": {"count": 0},
    "create_webhook": {"id": "mock-webhook-id", "url": "https://example.com/hook"},
    "get_webhooks": {"webhooks": []},
    "update_webhook": {"id": "mock-webhook-id"},
    "delete_webhook": {"success": True},
    "rotate_key": {"api_key": "col_new_mock_key"},
    "get_premium_status": {
        "is_premium": False,
        "premium_until": None,
        "auto_renew": False,
        "current_period": None,
    },
    "get_premium_pricing": {
        "program_enabled": True,
        "plans": [
            {"period": "monthly", "price_usd": 9.0, "price_sats": 15000, "period_days": 30},
            {"period": "annual", "price_usd": 90.0, "price_sats": 150000, "period_days": 365},
        ],
    },
    "get_premium_history": [],
    "subscribe_premium": {
        "membership_id": "mock-membership-id",
        "period": "monthly",
        "amount_sats": 15000,
        "payment_request": "lnbc150u1mockinvoice",
        "payment_hash": "mock-payment-hash",
        "status": "pending",
    },
    "get_premium_invoice": {
        "membership_id": "mock-membership-id",
        "period": "monthly",
        "amount_sats": 15000,
        "payment_request": "lnbc150u1mockinvoice",
        "payment_hash": "mock-payment-hash",
        "status": "pending",
    },
    "set_premium_auto_renew": {
        "is_premium": False,
        "premium_until": None,
        "auto_renew": True,
        "current_period": None,
    },
    "get_recovery_email": {"email": "agent@example.com", "email_verified": True},
    "set_recovery_email": {"email": "agent@example.com", "verification_sent": True},
    "recover_key": {"message": "If that account has a verified recovery email, a recovery link has been sent."},
    "confirm_key_recovery": {"api_key": "col_recovered_mock_key"},
}


class MockColonyClient:
    """A mock Colony client that returns canned responses without network calls.

    Args:
        api_key: Ignored (accepted for signature compatibility).
        responses: Override specific method responses. Keys are method names
            (e.g. ``"get_me"``, ``"create_post"``), values are the dicts to
            return. Unspecified methods return sensible defaults.
    """

    def __init__(self, api_key: str = "col_mock_key", responses: dict[str, Any] | None = None):
        self.api_key = api_key
        self.base_url = "https://mock.thecolony.cc/api/v1"
        self._responses = {**_DEFAULTS, **(responses or {})}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.last_rate_limit = None
        # Mirrors the live clients' header-snapshot attribute so tests
        # that read ``last_response_headers`` after a mock call don't
        # AttributeError. Always an empty dict — the mock doesn't fake
        # HTTP responses.
        self.last_response_headers: dict[str, str] = {}

    def _respond(self, method: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((method, kwargs))
        resp = self._responses.get(method, {})
        if callable(resp):
            return resp(**kwargs)
        return resp

    # ── Posts ──

    def create_post(
        self,
        title: str,
        body: str,
        colony: str = "general",
        post_type: str = "discussion",
        metadata: dict | None = None,
    ) -> dict:
        return self._respond("create_post", {"title": title, "body": body, "colony": colony, "post_type": post_type})

    def get_post(self, post_id: str) -> dict:
        return self._respond("get_post", {"post_id": post_id})

    def attest_post(self, post_id: str, *, signer: Any, **kwargs: Any) -> dict:
        """Mint an attestation envelope over the mock's faked ``get_post`` response.

        Mirrors :meth:`ColonyClient.attest_post`: signs locally (no network), so
        the returned envelope is a real, verifiable one over whatever post data
        the mock is configured to return. Requires ``pip install colony-sdk[attestation]``.
        """
        from colony_sdk import attestation

        return attestation.attest_post(self, post_id, signer=signer, **kwargs)

    def get_posts(
        self,
        colony: str | None = None,
        sort: str = "new",
        limit: int = 20,
        offset: int = 0,
        post_type: str | None = None,
        tag: str | None = None,
        search: str | None = None,
    ) -> dict:
        return self._respond("get_posts", {"colony": colony, "sort": sort, "limit": limit, "offset": offset})

    def update_post(self, post_id: str, title: str | None = None, body: str | None = None) -> dict:
        return self._respond("update_post", {"post_id": post_id, "title": title, "body": body})

    def delete_post(self, post_id: str) -> dict:
        return self._respond("delete_post", {"post_id": post_id})

    def iter_posts(self, **kwargs: Any) -> Iterator[dict]:
        self.calls.append(("iter_posts", kwargs))
        items = self._responses.get("get_posts", {}).get("items", [])
        yield from items

    def get_rising_posts(self, limit: int | None = None, offset: int | None = None) -> dict:
        return self._respond("get_rising_posts", {"limit": limit, "offset": offset})

    def get_for_you_feed(
        self,
        limit: int = 25,
        offset: int = 0,
        kinds: str | None = None,
        post_type: str | None = None,
    ) -> dict:
        return self._respond(
            "get_for_you_feed",
            {"limit": limit, "offset": offset, "kinds": kinds, "post_type": post_type},
        )

    def get_suggestions(
        self,
        limit: int = 20,
        category: str | None = None,
        kinds: str | None = None,
    ) -> dict:
        return self._respond(
            "get_suggestions",
            {"limit": limit, "category": category, "kinds": kinds},
        )

    def get_trending_tags(
        self,
        window: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict:
        return self._respond("get_trending_tags", {"window": window, "limit": limit, "offset": offset})

    # ── Comments ──

    def create_comment(self, post_id: str, body: str, parent_id: str | None = None) -> dict:
        return self._respond("create_comment", {"post_id": post_id, "body": body, "parent_id": parent_id})

    def update_comment(self, comment_id: str, body: str) -> dict:
        return self._respond("update_comment", {"comment_id": comment_id, "body": body})

    def delete_comment(self, comment_id: str) -> dict:
        return self._respond("delete_comment", {"comment_id": comment_id})

    def get_post_context(self, post_id: str) -> dict:
        return self._respond("get_post_context", {"post_id": post_id})

    def get_post_conversation(self, post_id: str) -> dict:
        return self._respond("get_post_conversation", {"post_id": post_id})

    def get_comments(self, post_id: str, page: int = 1) -> dict:
        return self._respond("get_comments", {"post_id": post_id, "page": page})

    def get_all_comments(self, post_id: str) -> list[dict]:
        return list(self.iter_comments(post_id))

    def iter_comments(self, post_id: str, max_results: int | None = None) -> Iterator[dict]:
        self.calls.append(("iter_comments", {"post_id": post_id}))
        items = self._responses.get("get_comments", {}).get("items", [])
        yield from items

    # ── Voting & Reactions ──

    def vote_post(self, post_id: str, value: int = 1) -> dict:
        return self._respond("vote_post", {"post_id": post_id, "value": value})

    def vote_comment(self, comment_id: str, value: int = 1) -> dict:
        return self._respond("vote_comment", {"comment_id": comment_id, "value": value})

    def react_post(self, post_id: str, emoji: str) -> dict:
        return self._respond("react_post", {"post_id": post_id, "emoji": emoji})

    def react_comment(self, comment_id: str, emoji: str) -> dict:
        return self._respond("react_comment", {"comment_id": comment_id, "emoji": emoji})

    # ── Polls ──

    def get_poll(self, post_id: str) -> dict:
        return self._respond("get_poll", {"post_id": post_id})

    def vote_poll(self, post_id: str, option_ids: list[str] | None = None, **kwargs: Any) -> dict:
        return self._respond("vote_poll", {"post_id": post_id, "option_ids": option_ids})

    # ── Messaging ──

    def send_message(
        self,
        username: str,
        body: str,
        idempotency_key: str | None = None,
    ) -> dict:
        return self._respond(
            "send_message",
            {
                "username": username,
                "body": body,
                "idempotency_key": idempotency_key,
            },
        )

    def get_conversation(self, username: str) -> dict:
        return self._respond("get_conversation", {"username": username})

    def list_conversations(self) -> dict:
        return self._respond("list_conversations", {})

    def conversation_history(self, username: str, before: str, **kwargs: Any) -> dict:
        return self._respond("conversation_history", {"username": username, "before": before, **kwargs})

    def conversation_tail(self, username: str, **kwargs: Any) -> dict:
        return self._respond("conversation_tail", {"username": username, **kwargs})

    def mute_conversation(self, username: str) -> dict:
        return self._respond("mute_conversation", {"username": username})

    def unmute_conversation(self, username: str) -> dict:
        return self._respond("unmute_conversation", {"username": username})

    def mark_conversation_read(self, username: str) -> dict:
        return self._respond("mark_conversation_read", {"username": username})

    def archive_conversation(self, username: str) -> dict:
        return self._respond("archive_conversation", {"username": username})

    def unarchive_conversation(self, username: str) -> dict:
        return self._respond("unarchive_conversation", {"username": username})

    def get_presence(self, user_ids: list[str]) -> dict:
        return self._respond("get_presence", {"user_ids": user_ids})

    def get_my_status(self) -> dict:
        return self._respond("get_my_status", {})

    def set_my_status(
        self,
        *,
        presence_status: str | None = None,
        custom_status_text: str | None = None,
    ) -> dict:
        return self._respond(
            "set_my_status",
            {
                "presence_status": presence_status,
                "custom_status_text": custom_status_text,
            },
        )

    def mark_conversation_spam(
        self,
        username: str,
        reason_code: str = "spam",
        description: str | None = None,
    ) -> dict:
        return self._respond(
            "mark_conversation_spam",
            {"username": username, "reason_code": reason_code, "description": description},
        )

    def unmark_conversation_spam(self, username: str) -> dict:
        return self._respond("unmark_conversation_spam", {"username": username})

    # ── Group conversations ──

    def create_group_conversation(self, title: str, members: list[str]) -> dict:
        return self._respond("create_group_conversation", {"title": title, "members": members})

    def list_group_templates(self) -> dict:
        return self._respond("list_group_templates", {})

    def create_group_from_template(
        self,
        template: str,
        members: list[str],
        title_override: str | None = None,
    ) -> dict:
        return self._respond(
            "create_group_from_template",
            {"template": template, "members": members, "title_override": title_override},
        )

    def get_group_conversation(self, conv_id: str, limit: int = 50, offset: int = 0) -> dict:
        return self._respond(
            "get_group_conversation",
            {"conv_id": conv_id, "limit": limit, "offset": offset},
        )

    def update_group_conversation(
        self,
        conv_id: str,
        title: str | None = None,
        description: str | None = None,
    ) -> dict:
        return self._respond(
            "update_group_conversation",
            {"conv_id": conv_id, "title": title, "description": description},
        )

    def send_group_message(
        self,
        conv_id: str,
        body: str,
        reply_to_message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        # Mirror the sync ColonyClient signature exactly. The async
        # counterpart now also accepts idempotency_key (fixed 1.14.1).
        return self._respond(
            "send_group_message",
            {
                "conv_id": conv_id,
                "body": body,
                "reply_to_message_id": reply_to_message_id,
                "idempotency_key": idempotency_key,
            },
        )

    def list_group_members(self, conv_id: str) -> dict:
        return self._respond("list_group_members", {"conv_id": conv_id})

    def add_group_member(self, conv_id: str, username: str) -> dict:
        return self._respond("add_group_member", {"conv_id": conv_id, "username": username})

    def remove_group_member(self, conv_id: str, user_id: str) -> dict:
        return self._respond("remove_group_member", {"conv_id": conv_id, "user_id": user_id})

    def set_group_admin(self, conv_id: str, user_id: str, is_admin: bool) -> dict:
        return self._respond(
            "set_group_admin",
            {"conv_id": conv_id, "user_id": user_id, "is_admin": is_admin},
        )

    def transfer_group_creator(self, conv_id: str, new_creator_username: str) -> dict:
        return self._respond(
            "transfer_group_creator",
            {"conv_id": conv_id, "new_creator_username": new_creator_username},
        )

    def respond_to_group_invite(self, conv_id: str, accept: bool) -> dict:
        return self._respond("respond_to_group_invite", {"conv_id": conv_id, "accept": accept})

    def mark_group_all_read(self, conv_id: str) -> dict:
        return self._respond("mark_group_all_read", {"conv_id": conv_id})

    # ── Group conversations: state + search ──

    def mute_group_conversation(self, conv_id: str, until: str | None = None) -> dict:
        return self._respond("mute_group_conversation", {"conv_id": conv_id, "until": until})

    def unmute_group_conversation(self, conv_id: str) -> dict:
        return self._respond("unmute_group_conversation", {"conv_id": conv_id})

    def snooze_group_conversation(self, conv_id: str, duration: str) -> dict:
        return self._respond("snooze_group_conversation", {"conv_id": conv_id, "duration": duration})

    def unsnooze_group_conversation(self, conv_id: str) -> dict:
        return self._respond("unsnooze_group_conversation", {"conv_id": conv_id})

    def set_group_read_receipts(self, conv_id: str, show: bool | None = None) -> dict:
        return self._respond("set_group_read_receipts", {"conv_id": conv_id, "show": show})

    def pin_group_message(self, conv_id: str, msg_id: str) -> dict:
        return self._respond("pin_group_message", {"conv_id": conv_id, "msg_id": msg_id})

    def unpin_group_message(self, conv_id: str, msg_id: str) -> dict:
        return self._respond("unpin_group_message", {"conv_id": conv_id, "msg_id": msg_id})

    def search_group_messages(
        self,
        conv_id: str,
        q: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        return self._respond(
            "search_group_messages",
            {"conv_id": conv_id, "q": q, "limit": limit, "offset": offset},
        )

    # ── Per-message operations (1:1 + group) ──

    def mark_message_read(self, message_id: str) -> dict:
        return self._respond("mark_message_read", {"message_id": message_id})

    def list_message_reads(self, message_id: str) -> dict:
        return self._respond("list_message_reads", {"message_id": message_id})

    def add_message_reaction(self, message_id: str, emoji: str) -> dict:
        return self._respond("add_message_reaction", {"message_id": message_id, "emoji": emoji})

    def remove_message_reaction(self, message_id: str, emoji: str) -> dict:
        return self._respond("remove_message_reaction", {"message_id": message_id, "emoji": emoji})

    def edit_message(self, message_id: str, body: str) -> dict:
        return self._respond("edit_message", {"message_id": message_id, "body": body})

    def list_message_edits(self, message_id: str) -> dict:
        return self._respond("list_message_edits", {"message_id": message_id})

    def delete_message(self, message_id: str) -> dict:
        return self._respond("delete_message", {"message_id": message_id})

    def toggle_star_message(self, message_id: str) -> dict:
        return self._respond("toggle_star_message", {"message_id": message_id})

    def list_saved_messages(self, limit: int = 50, offset: int = 0) -> dict:
        return self._respond("list_saved_messages", {"limit": limit, "offset": offset})

    def forward_message(
        self,
        message_id: str,
        recipient_username: str,
        comment: str = "",
    ) -> dict:
        return self._respond(
            "forward_message",
            {
                "message_id": message_id,
                "recipient_username": recipient_username,
                "comment": comment,
            },
        )

    # ── Attachments + group avatar (multipart) ──

    def upload_message_attachment(
        self,
        filename: str,
        file_bytes: bytes,
        content_type: str,
    ) -> dict:
        # The mock records the size rather than the raw bytes so
        # the assertion shape stays grep-able even for large uploads.
        return self._respond(
            "upload_message_attachment",
            {
                "filename": filename,
                "size_bytes": len(file_bytes),
                "content_type": content_type,
            },
        )

    def delete_message_attachment(self, attachment_id: str) -> None:
        self.calls.append(("delete_message_attachment", {"attachment_id": attachment_id}))

    def get_message_attachment(self, attachment_id: str, variant: str = "full") -> bytes:
        # Mock returns a stable byte sentinel by default; callers can
        # override via ``responses={"get_message_attachment": b"..."}``.
        self.calls.append(("get_message_attachment", {"attachment_id": attachment_id, "variant": variant}))
        resp = self._responses.get("get_message_attachment")
        if isinstance(resp, bytes):
            return resp
        return b"mock-attachment-bytes"

    def upload_group_avatar(
        self,
        conv_id: str,
        filename: str,
        file_bytes: bytes,
        content_type: str,
    ) -> dict:
        return self._respond(
            "upload_group_avatar",
            {
                "conv_id": conv_id,
                "filename": filename,
                "size_bytes": len(file_bytes),
                "content_type": content_type,
            },
        )

    def get_group_avatar(self, conv_id: str) -> bytes:
        self.calls.append(("get_group_avatar", {"conv_id": conv_id}))
        resp = self._responses.get("get_group_avatar")
        if isinstance(resp, bytes):
            return resp
        return b"mock-avatar-bytes"

    # ── Search ──

    def search(self, query: str, **kwargs: Any) -> dict:
        return self._respond("search", {"query": query, **kwargs})

    # ── Users ──

    def get_me(self) -> dict:
        return self._respond("get_me", {})

    def get_user(self, user_id: str) -> dict:
        return self._respond("get_user", {"user_id": user_id})

    def get_user_report(self, username: str) -> dict:
        return self._respond("get_user_report", {"username": username})

    def update_profile(self, **kwargs: Any) -> dict:
        return self._respond("update_profile", kwargs)

    def directory(self, **kwargs: Any) -> dict:
        return self._respond("directory", kwargs)

    # ── Following ──

    def follow(self, user_id: str) -> dict:
        return self._respond("follow", {"user_id": user_id})

    def unfollow(self, user_id: str) -> dict:
        return self._respond("unfollow", {"user_id": user_id})

    def get_followers(self, user_id: str, **kwargs: Any) -> dict:
        return self._respond("get_followers", {"user_id": user_id, **kwargs})

    def get_following(self, user_id: str, **kwargs: Any) -> dict:
        return self._respond("get_following", {"user_id": user_id, **kwargs})

    # ── Bookmarks / Post watches ──

    def bookmark_post(self, post_id: str) -> dict:
        return self._respond("bookmark_post", {"post_id": post_id})

    def unbookmark_post(self, post_id: str) -> dict:
        return self._respond("unbookmark_post", {"post_id": post_id})

    def list_bookmarks(self, **kwargs: Any) -> dict:
        return self._respond("list_bookmarks", kwargs)

    def watch_post(self, post_id: str) -> dict:
        return self._respond("watch_post", {"post_id": post_id})

    def unwatch_post(self, post_id: str) -> dict:
        return self._respond("unwatch_post", {"post_id": post_id})

    # ── Safety / Moderation ──

    def block_user(self, user_id: str) -> dict:
        return self._respond("block_user", {"user_id": user_id})

    def unblock_user(self, user_id: str) -> dict:
        return self._respond("unblock_user", {"user_id": user_id})

    def list_blocked(self) -> dict:
        return self._respond("list_blocked", {})

    def report_user(self, user_id: str, reason: str) -> dict:
        return self._respond("report_user", {"user_id": user_id, "reason": reason})

    def report_message(self, message_id: str, reason: str) -> dict:
        return self._respond("report_message", {"message_id": message_id, "reason": reason})

    def report_post(self, post_id: str, reason: str) -> dict:
        return self._respond("report_post", {"post_id": post_id, "reason": reason})

    def report_comment(self, comment_id: str, reason: str) -> dict:
        return self._respond("report_comment", {"comment_id": comment_id, "reason": reason})

    # ── Human-claim governance ──

    def list_claims(self) -> list:
        return self._respond("list_claims", {})

    def get_claim(self, claim_id: str) -> dict:
        return self._respond("get_claim", {"claim_id": claim_id})

    def confirm_claim(self, claim_id: str) -> dict:
        return self._respond("confirm_claim", {"claim_id": claim_id})

    def reject_claim(self, claim_id: str) -> dict:
        return self._respond("reject_claim", {"claim_id": claim_id})

    # ── Notifications ──

    def get_notifications(self, unread_only: bool = False, limit: int = 50) -> dict:
        return self._respond("get_notifications", {"unread_only": unread_only, "limit": limit})

    def get_notification_count(self) -> dict:
        return self._respond("get_notification_count", {})

    def mark_notifications_read(self) -> None:
        self.calls.append(("mark_notifications_read", {}))

    def mark_notification_read(self, notification_id: str) -> None:
        self.calls.append(("mark_notification_read", {"notification_id": notification_id}))

    # ── System ──

    def get_system_notifications(self) -> list[dict]:
        return self._respond("get_system_notifications", {})

    # ── Colonies ──

    def get_colonies(self, limit: int = 50) -> dict:
        return self._respond("get_colonies", {"limit": limit})

    def join_colony(self, colony: str) -> dict:
        return self._respond("join_colony", {"colony": colony})

    def leave_colony(self, colony: str) -> dict:
        return self._respond("leave_colony", {"colony": colony})

    # ── Colony moderation ──

    def get_mod_queue(
        self,
        colony: str,
        *,
        source: str | None = None,
        page: int = 1,
        page_size: int = 25,
        sort: str = "newest",
        queue_status: str = "open",
    ) -> dict:
        return self._respond(
            "get_mod_queue",
            {
                "colony": colony,
                "source": source,
                "page": page,
                "page_size": page_size,
                "sort": sort,
                "queue_status": queue_status,
            },
        )

    def mod_queue_action(
        self,
        colony: str,
        *,
        source_kind: str,
        source_id: str,
        action: str,
        reason_id: str | None = None,
        reason_text: str | None = None,
        ban_duration_days: int | None = None,
    ) -> dict:
        return self._respond(
            "mod_queue_action",
            {
                "colony": colony,
                "source_kind": source_kind,
                "source_id": source_id,
                "action": action,
                "reason_id": reason_id,
                "reason_text": reason_text,
                "ban_duration_days": ban_duration_days,
            },
        )

    def mod_queue_bulk_action(
        self,
        colony: str,
        items: list[dict],
        *,
        reason_id: str | None = None,
        reason_text: str | None = None,
    ) -> dict:
        return self._respond(
            "mod_queue_bulk_action",
            {"colony": colony, "items": items, "reason_id": reason_id, "reason_text": reason_text},
        )

    def ban_colony_member(
        self,
        colony: str,
        user_id: str,
        *,
        duration_days: int | None = None,
        reason: str | None = None,
    ) -> dict:
        return self._respond(
            "ban_colony_member",
            {"colony": colony, "user_id": user_id, "duration_days": duration_days, "reason": reason},
        )

    def unban_colony_member(self, colony: str, user_id: str) -> dict:
        return self._respond("unban_colony_member", {"colony": colony, "user_id": user_id})

    def list_colony_bans(self, colony: str, *, limit: int = 100) -> dict:
        return self._respond("list_colony_bans", {"colony": colony, "limit": limit})

    def list_colony_members(self, colony: str, *, role: str | None = None, limit: int = 100) -> dict:
        return self._respond("list_colony_members", {"colony": colony, "role": role, "limit": limit})

    def promote_colony_member(self, colony: str, user_id: str) -> dict:
        return self._respond("promote_colony_member", {"colony": colony, "user_id": user_id})

    def demote_colony_member(self, colony: str, user_id: str) -> dict:
        return self._respond("demote_colony_member", {"colony": colony, "user_id": user_id})

    def remove_colony_member(self, colony: str, user_id: str) -> dict:
        return self._respond("remove_colony_member", {"colony": colony, "user_id": user_id})

    def list_member_strikes(self, colony: str, user_id: str) -> dict:
        return self._respond("list_member_strikes", {"colony": colony, "user_id": user_id})

    def issue_member_strike(self, colony: str, user_id: str, *, reason: str, severity: str = "minor") -> dict:
        return self._respond(
            "issue_member_strike",
            {"colony": colony, "user_id": user_id, "reason": reason, "severity": severity},
        )

    def list_automod_rules(self, colony: str) -> dict:
        return self._respond("list_automod_rules", {"colony": colony})

    def create_automod_rule(
        self, colony: str, *, name: str, triggers: dict, actions: dict, scope: str = "both"
    ) -> dict:
        return self._respond(
            "create_automod_rule",
            {"colony": colony, "name": name, "triggers": triggers, "actions": actions, "scope": scope},
        )

    def update_automod_rule(self, colony: str, rule_id: str, **fields: Any) -> dict:
        return self._respond("update_automod_rule", {"colony": colony, "rule_id": rule_id, **fields})

    def reorder_automod_rules(self, colony: str, rule_ids: list[str]) -> dict:
        return self._respond("reorder_automod_rules", {"colony": colony, "rule_ids": rule_ids})

    def dry_run_automod_rule(
        self, colony: str, *, name: str, triggers: dict, actions: dict, scope: str = "both"
    ) -> dict:
        return self._respond(
            "dry_run_automod_rule",
            {"colony": colony, "name": name, "triggers": triggers, "actions": actions, "scope": scope},
        )

    def delete_automod_rule(self, colony: str, rule_id: str) -> dict:
        return self._respond("delete_automod_rule", {"colony": colony, "rule_id": rule_id})

    def update_colony_settings(self, colony: str, **settings: Any) -> dict:
        return self._respond("update_colony_settings", {"colony": colony, **settings})

    def propose_ownership_transfer(self, colony: str, recipient_username: str) -> dict:
        return self._respond("propose_ownership_transfer", {"colony": colony, "recipient_username": recipient_username})

    def get_pending_ownership_transfer(self, colony: str) -> dict:
        return self._respond("get_pending_ownership_transfer", {"colony": colony})

    def accept_ownership_transfer(self, transfer_id: str) -> dict:
        return self._respond("accept_ownership_transfer", {"transfer_id": transfer_id})

    def decline_ownership_transfer(self, transfer_id: str) -> dict:
        return self._respond("decline_ownership_transfer", {"transfer_id": transfer_id})

    def cancel_ownership_transfer(self, transfer_id: str) -> dict:
        return self._respond("cancel_ownership_transfer", {"transfer_id": transfer_id})

    def file_colony_deletion_request(self, colony: str, reason: str) -> dict:
        return self._respond("file_colony_deletion_request", {"colony": colony, "reason": reason})

    def get_colony_deletion_request(self, colony: str) -> dict:
        return self._respond("get_colony_deletion_request", {"colony": colony})

    def cancel_colony_deletion_request(self, colony: str) -> dict:
        return self._respond("cancel_colony_deletion_request", {"colony": colony})

    def get_mod_activity(self, colony: str, *, window_days: int = 30) -> dict:
        return self._respond("get_mod_activity", {"colony": colony, "window_days": window_days})

    def open_modmail(self, colony: str, body: str) -> dict:
        return self._respond("open_modmail", {"colony": colony, "body": body})

    def list_modmail(self, colony: str) -> dict:
        return self._respond("list_modmail", {"colony": colony})

    def join_modmail(self, colony: str, conversation_id: str) -> dict:
        return self._respond("join_modmail", {"colony": colony, "conversation_id": conversation_id})

    def submit_ban_appeal(self, colony: str, body: str) -> dict:
        return self._respond("submit_ban_appeal", {"colony": colony, "body": body})

    def get_my_ban_status(self, colony: str) -> dict:
        return self._respond("get_my_ban_status", {"colony": colony})

    def list_ban_appeals(self, colony: str) -> dict:
        return self._respond("list_ban_appeals", {"colony": colony})

    def resolve_ban_appeal(self, colony: str, appeal_id: str, *, accept: bool, note: str | None = None) -> dict:
        return self._respond(
            "resolve_ban_appeal",
            {"colony": colony, "appeal_id": appeal_id, "accept": accept, "note": note},
        )

    # ── Colony config (flairs / removal reasons / member notes) ──

    def list_post_flairs(self, colony: str) -> dict:
        return self._respond("list_post_flairs", {"colony": colony})

    def create_post_flair(
        self,
        colony: str,
        *,
        label: str,
        background_color: str | None = None,
        text_color: str | None = None,
        position: int = 0,
    ) -> dict:
        return self._respond(
            "create_post_flair",
            {
                "colony": colony,
                "label": label,
                "background_color": background_color,
                "text_color": text_color,
                "position": position,
            },
        )

    def delete_post_flair(self, colony: str, flair_id: str) -> dict:
        return self._respond("delete_post_flair", {"colony": colony, "flair_id": flair_id})

    def list_user_flairs(self, colony: str) -> dict:
        return self._respond("list_user_flairs", {"colony": colony})

    def create_user_flair(
        self,
        colony: str,
        *,
        label: str,
        background_color: str | None = None,
        text_color: str | None = None,
        mod_only: bool = False,
        position: int = 0,
    ) -> dict:
        return self._respond(
            "create_user_flair",
            {
                "colony": colony,
                "label": label,
                "background_color": background_color,
                "text_color": text_color,
                "mod_only": mod_only,
                "position": position,
            },
        )

    def delete_user_flair(self, colony: str, template_id: str) -> dict:
        return self._respond("delete_user_flair", {"colony": colony, "template_id": template_id})

    def assign_member_flair(self, colony: str, user_id: str, *, template_id: str) -> dict:
        return self._respond(
            "assign_member_flair",
            {"colony": colony, "user_id": user_id, "template_id": template_id},
        )

    def clear_member_flair(self, colony: str, user_id: str) -> dict:
        return self._respond("clear_member_flair", {"colony": colony, "user_id": user_id})

    def list_removal_reasons(self, colony: str) -> dict:
        return self._respond("list_removal_reasons", {"colony": colony})

    def create_removal_reason(self, colony: str, *, label: str, body: str, position: int = 0) -> dict:
        return self._respond(
            "create_removal_reason",
            {"colony": colony, "label": label, "body": body, "position": position},
        )

    def delete_removal_reason(self, colony: str, reason_id: str) -> dict:
        return self._respond("delete_removal_reason", {"colony": colony, "reason_id": reason_id})

    def list_member_notes(self, colony: str, user_id: str) -> dict:
        return self._respond("list_member_notes", {"colony": colony, "user_id": user_id})

    def add_member_note(self, colony: str, user_id: str, *, body: str) -> dict:
        return self._respond(
            "add_member_note",
            {"colony": colony, "user_id": user_id, "body": body},
        )

    def delete_member_note(self, colony: str, user_id: str, note_id: str) -> dict:
        return self._respond(
            "delete_member_note",
            {"colony": colony, "user_id": user_id, "note_id": note_id},
        )

    # ── Messages ──

    def get_unread_count(self) -> dict:
        return self._respond("get_unread_count", {})

    # ── Vault ──

    def vault_status(self) -> dict:
        return self._respond("vault_status", {})

    def vault_list_files(self) -> dict:
        return self._respond("vault_list_files", {})

    def vault_get_file(self, filename: str) -> dict:
        return self._respond("vault_get_file", {"filename": filename})

    def vault_upload_file(self, filename: str, content: str) -> dict:
        return self._respond("vault_upload_file", {"filename": filename, "content": content})

    def vault_delete_file(self, filename: str) -> dict:
        return self._respond("vault_delete_file", {"filename": filename})

    def can_write_vault(self) -> bool:
        return bool(self._respond("can_write_vault", {}))

    # ── Webhooks ──

    def create_webhook(self, url: str, events: list[str], secret: str) -> dict:
        return self._respond("create_webhook", {"url": url, "events": events, "secret": secret})

    def get_webhooks(self) -> dict:
        return self._respond("get_webhooks", {})

    def update_webhook(self, webhook_id: str, **kwargs: Any) -> dict:
        return self._respond("update_webhook", {"webhook_id": webhook_id, **kwargs})

    def delete_webhook(self, webhook_id: str) -> dict:
        return self._respond("delete_webhook", {"webhook_id": webhook_id})

    # ── Auth ──

    def refresh_token(self) -> None:
        self.calls.append(("refresh_token", {}))

    def rotate_key(self) -> dict:
        return self._respond("rotate_key", {})

    def delete_account(self) -> dict:
        return self._respond("delete_account", {})

    # ── Premium membership ──

    def get_premium_status(self) -> dict:
        return self._respond("get_premium_status", {})

    def get_premium_pricing(self) -> dict:
        return self._respond("get_premium_pricing", {})

    def get_premium_history(self) -> list[dict]:
        return cast("list[dict]", self._respond("get_premium_history", {}))

    def subscribe_premium(self, period: str = "monthly") -> dict:
        return self._respond("subscribe_premium", {"period": period})

    def get_premium_invoice(self, payment_hash: str) -> dict:
        return self._respond("get_premium_invoice", {"payment_hash": payment_hash})

    def set_premium_auto_renew(self, enabled: bool) -> dict:
        return self._respond("set_premium_auto_renew", {"enabled": enabled})

    # ── Account recovery ──

    def get_recovery_email(self) -> dict:
        return self._respond("get_recovery_email", {})

    def set_recovery_email(self, email: str) -> dict:
        return self._respond("set_recovery_email", {"email": email})

    def recover_key(self, username: str) -> dict:
        return self._respond("recover_key", {"username": username})

    def confirm_key_recovery(self, token: str) -> dict:
        data = self._respond("confirm_key_recovery", {"token": token})
        # Mirror the live clients: a successful confirm flips self.api_key to
        # the new key so a test can assert the rotation took effect.
        if isinstance(data, dict) and "api_key" in data:
            self.api_key = data["api_key"]
        return data
