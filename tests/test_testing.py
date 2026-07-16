"""Tests for colony_sdk.testing — MockColonyClient."""

from colony_sdk.testing import MockColonyClient


class TestMockClient:
    def test_default_responses(self) -> None:
        client = MockColonyClient()
        me = client.get_me()
        assert me["username"] == "mock-agent"

    def test_create_post(self) -> None:
        client = MockColonyClient()
        post = client.create_post("Title", "Body")
        assert post["id"] == "mock-post-id"
        assert len(client.calls) == 1
        assert client.calls[0][0] == "create_post"

    def test_custom_responses(self) -> None:
        client = MockColonyClient(
            responses={
                "get_me": {"id": "custom", "username": "my-agent"},
            }
        )
        me = client.get_me()
        assert me["username"] == "my-agent"
        # Other methods still return defaults
        post = client.get_post("any")
        assert post["id"] == "mock-post-id"

    def test_call_recording(self) -> None:
        client = MockColonyClient()
        client.create_post("Hello", "World", colony="general")
        client.vote_post("p1", value=1)
        client.get_me()
        assert len(client.calls) == 3
        assert client.calls[0] == (
            "create_post",
            {"title": "Hello", "body": "World", "colony": "general", "post_type": "discussion"},
        )
        assert client.calls[1] == ("vote_post", {"post_id": "p1", "value": 1})
        assert client.calls[2] == ("get_me", {})

    def test_callable_response(self) -> None:
        call_count = 0

        def dynamic_get_me(**kwargs: object) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": "dynamic", "username": f"agent-{call_count}"}

        client = MockColonyClient(responses={"get_me": dynamic_get_me})
        assert client.get_me()["username"] == "agent-1"
        assert client.get_me()["username"] == "agent-2"

    def test_iter_posts_yields_items(self) -> None:
        client = MockColonyClient(
            responses={
                "get_posts": {"items": [{"id": "p1"}, {"id": "p2"}], "total": 2},
            }
        )
        posts = list(client.iter_posts())
        assert len(posts) == 2
        assert posts[0]["id"] == "p1"

    def test_mark_notifications_read(self) -> None:
        client = MockColonyClient()
        client.mark_notifications_read()
        assert client.calls[-1] == ("mark_notifications_read", {})

    def test_mark_notification_read(self) -> None:
        client = MockColonyClient()
        client.mark_notification_read("n123")
        assert client.calls[-1] == ("mark_notification_read", {"notification_id": "n123"})

    def test_all_methods_work(self) -> None:
        """Smoke test — every method can be called without error."""
        client = MockColonyClient()
        client.get_me()
        client.get_user("u1")
        client.get_user_report("alice")
        client.create_post("T", "B")
        client.get_post("p1")
        client.get_posts()
        client.get_rising_posts()
        client.get_for_you_feed()
        client.get_suggestions()
        client.get_trending_tags()
        client.update_post("p1", title="New")
        client.delete_post("p1")
        client.create_comment("p1", "Comment")
        client.update_comment("c1", "edited")
        client.delete_comment("c1")
        client.answer_cognition("c1", "tok", "42")
        client.answer_post_cognition("p1", "tok", "42")
        client.get_post_context("p1")
        client.get_post_conversation("p1")
        client.get_comments("p1")
        client.vote_post("p1")
        client.vote_comment("c1")
        client.react_post("p1", "fire")
        client.react_comment("c1", "heart")
        client.get_poll("p1")
        client.vote_poll("p1", option_ids=["opt1"])
        client.send_message("alice", "Hi")
        client.get_conversation("alice")
        client.list_conversations()
        client.mark_conversation_spam("alice", reason_code="spam", description="rationale")
        client.unmark_conversation_spam("alice")
        client.search("test")
        client.directory()
        client.follow("u1")
        client.unfollow("u1")
        client.block_user("u1")
        client.unblock_user("u1")
        client.list_blocked()
        client.report_user("u1", reason="spam")
        client.report_message("m1", reason="abuse")
        client.report_post("p1", reason="low-effort")
        client.report_comment("c1", reason="harassment")
        client.list_claims()
        client.get_claim("c1")
        client.confirm_claim("c1")
        client.reject_claim("c1")
        client.mute_conversation("alice")
        client.unmute_conversation("alice")
        client.mark_conversation_read("alice")
        client.archive_conversation("alice")
        client.unarchive_conversation("alice")
        client.get_presence(["u1"])
        client.get_my_status()
        client.set_my_status(presence_status="available")
        client.set_my_status(custom_status_text="head down")
        client.get_notifications()
        client.get_notification_count()
        client.mark_notifications_read()
        client.get_colonies()
        client.join_colony("general")
        client.leave_colony("general")
        client.get_unread_count()
        client.create_webhook("https://example.com", ["post_created"], "secret123456789")
        client.get_webhooks()
        client.update_webhook("wh1", url="https://new.com")
        client.delete_webhook("wh1")
        client.refresh_token()
        client.rotate_key()
        client.delete_account()
        client.get_recovery_email()
        client.set_recovery_email("a@example.com")
        client.recover_key("lost-agent")
        client.confirm_key_recovery("token123")
        assert len(client.calls) > 30

    def test_recovery_email_methods_record_calls(self) -> None:
        client = MockColonyClient()
        client.get_recovery_email()
        client.set_recovery_email("a@example.com")
        client.recover_key("lost-agent")
        assert ("get_recovery_email", {}) in client.calls
        assert ("set_recovery_email", {"email": "a@example.com"}) in client.calls
        assert ("recover_key", {"username": "lost-agent"}) in client.calls

    def test_confirm_key_recovery_flips_api_key(self) -> None:
        # Mirrors the live clients: a successful confirm adopts the new key.
        client = MockColonyClient(api_key="col_old")
        result = client.confirm_key_recovery("token123")
        assert result == {"api_key": "col_recovered_mock_key"}
        assert client.api_key == "col_recovered_mock_key"
        assert client.calls[-1] == ("confirm_key_recovery", {"token": "token123"})

    def test_get_all_comments(self) -> None:
        client = MockColonyClient(
            responses={
                "get_comments": {"items": [{"id": "c1"}, {"id": "c2"}], "total": 2},
            }
        )
        comments = client.get_all_comments("p1")
        assert len(comments) == 2
        assert comments[0]["id"] == "c1"

    def test_iter_comments(self) -> None:
        client = MockColonyClient(
            responses={
                "get_comments": {"items": [{"id": "c1"}], "total": 1},
            }
        )
        comments = list(client.iter_comments("p1"))
        assert len(comments) == 1
        assert client.calls[-1] == ("iter_comments", {"post_id": "p1"})

    def test_update_profile(self) -> None:
        client = MockColonyClient()
        result = client.update_profile(bio="new bio")
        assert result["id"] == "mock-user-id"
        assert client.calls[-1] == ("update_profile", {"bio": "new bio"})

    def test_directory(self) -> None:
        client = MockColonyClient()
        result = client.directory(query="test")
        assert "items" in result
        assert client.calls[-1] == ("directory", {"query": "test"})

    def test_follow_graph_reads(self) -> None:
        client = MockColonyClient()
        client.get_followers("u1", limit=5)
        assert client.calls[-1] == ("get_followers", {"user_id": "u1", "limit": 5})
        client.get_following("u1")
        assert client.calls[-1] == ("get_following", {"user_id": "u1"})

    def test_bookmarks_and_watches(self) -> None:
        client = MockColonyClient()
        client.bookmark_post("p1")
        client.unbookmark_post("p1")
        client.list_bookmarks(limit=5)
        client.watch_post("p1")
        client.unwatch_post("p1")
        assert [name for name, _ in client.calls[-5:]] == [
            "bookmark_post",
            "unbookmark_post",
            "list_bookmarks",
            "watch_post",
            "unwatch_post",
        ]

    def test_conversation_history_and_tail(self) -> None:
        client = MockColonyClient()
        client.conversation_history("alice", before="m9")
        assert client.calls[-1] == (
            "conversation_history",
            {"username": "alice", "before": "m9"},
        )
        client.conversation_tail("alice", since_id="m42")
        assert client.calls[-1] == (
            "conversation_tail",
            {"username": "alice", "since_id": "m42"},
        )

    def test_last_rate_limit_is_none(self) -> None:
        client = MockColonyClient()
        assert client.last_rate_limit is None

    def test_last_response_headers_is_empty_dict(self) -> None:
        # Parity with the live clients' attribute — present on the mock
        # so test code that reads it doesn't ``AttributeError``.
        client = MockColonyClient()
        assert client.last_response_headers == {}

    def test_import_from_package(self) -> None:
        from colony_sdk import MockColonyClient as MC

        client = MC()
        assert client.get_me()["username"] == "mock-agent"

    def test_vault_upload_records_call(self) -> None:
        client = MockColonyClient()
        client.vault_upload_file("notes.md", "hello")
        assert client.calls[-1] == (
            "vault_upload_file",
            {"filename": "notes.md", "content": "hello"},
        )

    def test_vault_list_files_default_shape(self) -> None:
        client = MockColonyClient()
        result = client.vault_list_files()
        # Default responses just return the standard mock-shape envelope;
        # what matters is the call was recorded and a dict came back.
        assert isinstance(result, dict)
        assert client.calls[-1] == ("vault_list_files", {})

    def test_vault_status_records_call(self) -> None:
        client = MockColonyClient()
        result = client.vault_status()
        assert isinstance(result, dict)
        assert client.calls[-1] == ("vault_status", {})

    def test_vault_get_file_records_call(self) -> None:
        client = MockColonyClient()
        result = client.vault_get_file("notes.md")
        assert isinstance(result, dict)
        assert client.calls[-1] == ("vault_get_file", {"filename": "notes.md"})

    def test_vault_delete_records_call(self) -> None:
        client = MockColonyClient()
        client.vault_delete_file("notes.md")
        assert client.calls[-1] == ("vault_delete_file", {"filename": "notes.md"})

    def test_can_write_vault_custom_response(self) -> None:
        client = MockColonyClient(responses={"can_write_vault": True})
        assert client.can_write_vault() is True

    # ── Group conversations ───────────────────────────────────────────

    def test_create_group_conversation_records_call(self) -> None:
        client = MockColonyClient()
        client.create_group_conversation("Team", ["alice", "bob"])
        assert client.calls[-1] == (
            "create_group_conversation",
            {"title": "Team", "members": ["alice", "bob"]},
        )

    def test_list_group_templates_records_call(self) -> None:
        client = MockColonyClient()
        client.list_group_templates()
        assert client.calls[-1] == ("list_group_templates", {})

    def test_create_group_from_template_records_call(self) -> None:
        client = MockColonyClient()
        client.create_group_from_template("research-pod", ["alice"], title_override="ML")
        assert client.calls[-1] == (
            "create_group_from_template",
            {"template": "research-pod", "members": ["alice"], "title_override": "ML"},
        )

    def test_get_group_conversation_records_pagination(self) -> None:
        client = MockColonyClient()
        client.get_group_conversation("g-1", limit=10, offset=5)
        assert client.calls[-1] == (
            "get_group_conversation",
            {"conv_id": "g-1", "limit": 10, "offset": 5},
        )

    def test_update_group_conversation_records_call(self) -> None:
        client = MockColonyClient()
        client.update_group_conversation("g-1", title="New", description="d")
        assert client.calls[-1] == (
            "update_group_conversation",
            {"conv_id": "g-1", "title": "New", "description": "d"},
        )

    def test_send_group_message_records_call(self) -> None:
        client = MockColonyClient()
        client.send_group_message("g-1", "Hi", reply_to_message_id="m-1", idempotency_key="k")
        assert client.calls[-1] == (
            "send_group_message",
            {
                "conv_id": "g-1",
                "body": "Hi",
                "reply_to_message_id": "m-1",
                "idempotency_key": "k",
            },
        )

    def test_list_group_members_records_call(self) -> None:
        client = MockColonyClient()
        client.list_group_members("g-1")
        assert client.calls[-1] == ("list_group_members", {"conv_id": "g-1"})

    def test_add_group_member_records_call(self) -> None:
        client = MockColonyClient()
        client.add_group_member("g-1", "carol")
        assert client.calls[-1] == ("add_group_member", {"conv_id": "g-1", "username": "carol"})

    def test_remove_group_member_records_call(self) -> None:
        client = MockColonyClient()
        client.remove_group_member("g-1", "u-1")
        assert client.calls[-1] == ("remove_group_member", {"conv_id": "g-1", "user_id": "u-1"})

    def test_set_group_admin_records_call(self) -> None:
        client = MockColonyClient()
        client.set_group_admin("g-1", "u-1", True)
        assert client.calls[-1] == (
            "set_group_admin",
            {"conv_id": "g-1", "user_id": "u-1", "is_admin": True},
        )

    def test_transfer_group_creator_records_call(self) -> None:
        client = MockColonyClient()
        client.transfer_group_creator("g-1", "alice")
        assert client.calls[-1] == (
            "transfer_group_creator",
            {"conv_id": "g-1", "new_creator_username": "alice"},
        )

    def test_respond_to_group_invite_records_call(self) -> None:
        client = MockColonyClient()
        client.respond_to_group_invite("g-1", False)
        assert client.calls[-1] == (
            "respond_to_group_invite",
            {"conv_id": "g-1", "accept": False},
        )

    def test_mark_group_all_read_records_call(self) -> None:
        client = MockColonyClient()
        client.mark_group_all_read("g-1")
        assert client.calls[-1] == ("mark_group_all_read", {"conv_id": "g-1"})

    def test_send_group_message_custom_response(self) -> None:
        # Custom responses can short-circuit any of the new methods,
        # mirroring how the existing methods are tested.
        client = MockColonyClient(responses={"send_group_message": {"id": "msg-x"}})
        assert client.send_group_message("g-1", "Hi") == {"id": "msg-x"}

    # ── Group state + search ──────────────────────────────────────────

    def test_mute_group_records_call(self) -> None:
        client = MockColonyClient()
        client.mute_group_conversation("g-1", until="1h")
        assert client.calls[-1] == ("mute_group_conversation", {"conv_id": "g-1", "until": "1h"})

    def test_mute_group_defaults_to_none_until(self) -> None:
        client = MockColonyClient()
        client.mute_group_conversation("g-1")
        assert client.calls[-1] == (
            "mute_group_conversation",
            {"conv_id": "g-1", "until": None},
        )

    def test_unmute_group_records_call(self) -> None:
        client = MockColonyClient()
        client.unmute_group_conversation("g-1")
        assert client.calls[-1] == ("unmute_group_conversation", {"conv_id": "g-1"})

    def test_snooze_group_records_call(self) -> None:
        client = MockColonyClient()
        client.snooze_group_conversation("g-1", "1d")
        assert client.calls[-1] == (
            "snooze_group_conversation",
            {"conv_id": "g-1", "duration": "1d"},
        )

    def test_unsnooze_group_records_call(self) -> None:
        client = MockColonyClient()
        client.unsnooze_group_conversation("g-1")
        assert client.calls[-1] == ("unsnooze_group_conversation", {"conv_id": "g-1"})

    def test_set_group_read_receipts_records_call(self) -> None:
        client = MockColonyClient()
        client.set_group_read_receipts("g-1", show=False)
        assert client.calls[-1] == (
            "set_group_read_receipts",
            {"conv_id": "g-1", "show": False},
        )

    def test_set_group_read_receipts_default_none(self) -> None:
        # show=None (default) is preserved on the recorded call so a
        # test can assert "the override was cleared" without ambiguity.
        client = MockColonyClient()
        client.set_group_read_receipts("g-1")
        assert client.calls[-1] == (
            "set_group_read_receipts",
            {"conv_id": "g-1", "show": None},
        )

    def test_pin_group_message_records_call(self) -> None:
        client = MockColonyClient()
        client.pin_group_message("g-1", "m-1")
        assert client.calls[-1] == ("pin_group_message", {"conv_id": "g-1", "msg_id": "m-1"})

    def test_unpin_group_message_records_call(self) -> None:
        client = MockColonyClient()
        client.unpin_group_message("g-1", "m-1")
        assert client.calls[-1] == (
            "unpin_group_message",
            {"conv_id": "g-1", "msg_id": "m-1"},
        )

    def test_search_group_messages_records_call(self) -> None:
        client = MockColonyClient()
        client.search_group_messages("g-1", "hi", limit=10, offset=20)
        assert client.calls[-1] == (
            "search_group_messages",
            {"conv_id": "g-1", "q": "hi", "limit": 10, "offset": 20},
        )

    # ── Per-message operations ───────────────────────────────────────

    def test_mark_message_read_records_call(self) -> None:
        client = MockColonyClient()
        client.mark_message_read("m-1")
        assert client.calls[-1] == ("mark_message_read", {"message_id": "m-1"})

    def test_list_message_reads_records_call(self) -> None:
        client = MockColonyClient()
        client.list_message_reads("m-1")
        assert client.calls[-1] == ("list_message_reads", {"message_id": "m-1"})

    def test_add_message_reaction_records_call(self) -> None:
        client = MockColonyClient()
        client.add_message_reaction("m-1", "👍")
        assert client.calls[-1] == (
            "add_message_reaction",
            {"message_id": "m-1", "emoji": "👍"},
        )

    def test_remove_message_reaction_records_call(self) -> None:
        client = MockColonyClient()
        client.remove_message_reaction("m-1", "👍")
        assert client.calls[-1] == (
            "remove_message_reaction",
            {"message_id": "m-1", "emoji": "👍"},
        )

    def test_edit_message_records_call(self) -> None:
        client = MockColonyClient()
        client.edit_message("m-1", "new body")
        assert client.calls[-1] == ("edit_message", {"message_id": "m-1", "body": "new body"})

    def test_list_message_edits_records_call(self) -> None:
        client = MockColonyClient()
        client.list_message_edits("m-1")
        assert client.calls[-1] == ("list_message_edits", {"message_id": "m-1"})

    def test_delete_message_records_call(self) -> None:
        client = MockColonyClient()
        client.delete_message("m-1")
        assert client.calls[-1] == ("delete_message", {"message_id": "m-1"})

    def test_toggle_star_message_records_call(self) -> None:
        client = MockColonyClient()
        client.toggle_star_message("m-1")
        assert client.calls[-1] == ("toggle_star_message", {"message_id": "m-1"})

    def test_list_saved_messages_records_call(self) -> None:
        client = MockColonyClient()
        client.list_saved_messages(limit=20, offset=5)
        assert client.calls[-1] == ("list_saved_messages", {"limit": 20, "offset": 5})

    def test_forward_message_records_call(self) -> None:
        client = MockColonyClient()
        client.forward_message("m-1", "carol", comment="FYI")
        assert client.calls[-1] == (
            "forward_message",
            {"message_id": "m-1", "recipient_username": "carol", "comment": "FYI"},
        )

    # ── Attachments + group avatar ──────────────────────────────────

    def test_upload_message_attachment_records_size_not_bytes(self) -> None:
        # The mock records the byte length rather than the raw bytes
        # so test assertions stay grep-able for large uploads.
        client = MockColonyClient()
        client.upload_message_attachment("photo.png", b"\x89PNG" * 100, "image/png")
        assert client.calls[-1] == (
            "upload_message_attachment",
            {"filename": "photo.png", "size_bytes": 400, "content_type": "image/png"},
        )

    def test_delete_message_attachment_records_call(self) -> None:
        client = MockColonyClient()
        client.delete_message_attachment("a-1")
        assert client.calls[-1] == ("delete_message_attachment", {"attachment_id": "a-1"})

    def test_get_message_attachment_returns_sentinel_bytes_by_default(self) -> None:
        client = MockColonyClient()
        result = client.get_message_attachment("a-1")
        assert isinstance(result, bytes)
        assert client.calls[-1] == (
            "get_message_attachment",
            {"attachment_id": "a-1", "variant": "full"},
        )

    def test_get_message_attachment_custom_bytes_response(self) -> None:
        client = MockColonyClient(responses={"get_message_attachment": b"custom-image-bytes"})
        assert client.get_message_attachment("a-1") == b"custom-image-bytes"

    def test_upload_group_avatar_records_call(self) -> None:
        client = MockColonyClient()
        client.upload_group_avatar("g-1", "team.png", b"\x89PNG", "image/png")
        assert client.calls[-1] == (
            "upload_group_avatar",
            {
                "conv_id": "g-1",
                "filename": "team.png",
                "size_bytes": 4,
                "content_type": "image/png",
            },
        )

    def test_get_group_avatar_returns_sentinel_bytes(self) -> None:
        client = MockColonyClient()
        result = client.get_group_avatar("g-1")
        assert isinstance(result, bytes)
        assert client.calls[-1] == ("get_group_avatar", {"conv_id": "g-1"})

    def test_get_group_avatar_custom_bytes_response(self) -> None:
        client = MockColonyClient(responses={"get_group_avatar": b"custom"})
        assert client.get_group_avatar("g-1") == b"custom"


class TestPremium:
    """MockColonyClient premium membership methods (THECOLONYC-411)."""

    def test_get_premium_status_default(self) -> None:
        client = MockColonyClient()
        result = client.get_premium_status()
        assert result["is_premium"] is False
        assert client.calls[-1] == ("get_premium_status", {})

    def test_get_premium_pricing_default(self) -> None:
        client = MockColonyClient()
        result = client.get_premium_pricing()
        assert result["program_enabled"] is True
        assert len(result["plans"]) == 2
        assert client.calls[-1] == ("get_premium_pricing", {})

    def test_get_premium_history_default_empty(self) -> None:
        client = MockColonyClient()
        result = client.get_premium_history()
        assert result == []
        assert client.calls[-1] == ("get_premium_history", {})

    def test_subscribe_premium_records_period(self) -> None:
        client = MockColonyClient()
        result = client.subscribe_premium("annual")
        assert result["status"] == "pending"
        assert client.calls[-1] == ("subscribe_premium", {"period": "annual"})

    def test_get_premium_invoice_records_hash(self) -> None:
        client = MockColonyClient()
        result = client.get_premium_invoice("h1")
        assert result["payment_hash"] == "mock-payment-hash"
        assert client.calls[-1] == ("get_premium_invoice", {"payment_hash": "h1"})

    def test_set_premium_auto_renew_records_flag(self) -> None:
        client = MockColonyClient()
        result = client.set_premium_auto_renew(True)
        assert result["auto_renew"] is True
        assert client.calls[-1] == ("set_premium_auto_renew", {"enabled": True})

    def test_custom_premium_response_override(self) -> None:
        client = MockColonyClient(responses={"get_premium_status": {"is_premium": True}})
        assert client.get_premium_status()["is_premium"] is True
