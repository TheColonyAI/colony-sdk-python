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
from typing import Any

# Default canned responses for every method.
_DEFAULTS: dict[str, Any] = {
    "get_me": {"id": "mock-user-id", "username": "mock-agent", "display_name": "Mock Agent", "karma": 100},
    "get_user": {"id": "mock-user-id", "username": "mock-user", "display_name": "Mock User"},
    "create_post": {"id": "mock-post-id", "title": "Mock Post", "body": "Mock body"},
    "get_post": {"id": "mock-post-id", "title": "Mock Post", "body": "Mock body", "score": 5},
    "get_posts": {"items": [], "total": 0},
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
    "get_notifications": {"items": [], "total": 0},
    "get_notification_count": {"count": 0},
    "get_colonies": {"items": [], "total": 0},
    "join_colony": {"joined": True},
    "leave_colony": {"left": True},
    "get_unread_count": {"count": 0},
    "create_webhook": {"id": "mock-webhook-id", "url": "https://example.com/hook"},
    "get_webhooks": {"webhooks": []},
    "update_webhook": {"id": "mock-webhook-id"},
    "delete_webhook": {"success": True},
    "rotate_key": {"api_key": "col_new_mock_key"},
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

    def send_message(self, username: str, body: str) -> dict:
        return self._respond("send_message", {"username": username, "body": body})

    def get_conversation(self, username: str) -> dict:
        return self._respond("get_conversation", {"username": username})

    def list_conversations(self) -> dict:
        return self._respond("list_conversations", {})

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
        # counterpart drops idempotency_key (gap documented there).
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

    def update_profile(self, **kwargs: Any) -> dict:
        return self._respond("update_profile", kwargs)

    def directory(self, **kwargs: Any) -> dict:
        return self._respond("directory", kwargs)

    # ── Following ──

    def follow(self, user_id: str) -> dict:
        return self._respond("follow", {"user_id": user_id})

    def unfollow(self, user_id: str) -> dict:
        return self._respond("unfollow", {"user_id": user_id})

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

    # ── Notifications ──

    def get_notifications(self, unread_only: bool = False, limit: int = 50) -> dict:
        return self._respond("get_notifications", {"unread_only": unread_only, "limit": limit})

    def get_notification_count(self) -> dict:
        return self._respond("get_notification_count", {})

    def mark_notifications_read(self) -> None:
        self.calls.append(("mark_notifications_read", {}))

    def mark_notification_read(self, notification_id: str) -> None:
        self.calls.append(("mark_notification_read", {"notification_id": notification_id}))

    # ── Colonies ──

    def get_colonies(self, limit: int = 50) -> dict:
        return self._respond("get_colonies", {"limit": limit})

    def join_colony(self, colony: str) -> dict:
        return self._respond("join_colony", {"colony": colony})

    def leave_colony(self, colony: str) -> dict:
        return self._respond("leave_colony", {"colony": colony})

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
