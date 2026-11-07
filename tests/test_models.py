"""Tests for colony_sdk.models — typed response models."""

from colony_sdk.models import (
    Colony,
    Comment,
    ForYouEntry,
    ForYouFeed,
    Message,
    Notification,
    NotificationComment,
    PollResults,
    Post,
    RateLimitInfo,
    User,
    Webhook,
)


class TestUser:
    def test_from_dict_minimal(self) -> None:
        u = User.from_dict({"id": "abc", "username": "agent1"})
        assert u.id == "abc"
        assert u.username == "agent1"
        assert u.karma == 0
        assert u.capabilities == {}

    def test_from_dict_full(self) -> None:
        u = User.from_dict(
            {
                "id": "abc",
                "username": "agent1",
                "display_name": "Agent One",
                "bio": "I'm an agent",
                "user_type": "agent",
                "karma": 42,
                "post_count": 10,
                "comment_count": 5,
                "capabilities": {"skills": ["python"]},
                "created_at": "2026-01-01T00:00:00Z",
                "avatar_url": "https://example.com/avatar.png",
                "is_following": True,
            }
        )
        assert u.karma == 42
        assert u.capabilities == {"skills": ["python"]}
        assert u.is_following is True

    def test_roundtrip(self) -> None:
        d = {"id": "abc", "username": "agent1", "karma": 10}
        u = User.from_dict(d)
        result = u.to_dict()
        assert result["id"] == "abc"
        assert result["karma"] == 10

    def test_user_id_fallback(self) -> None:
        u = User.from_dict({"user_id": "xyz", "username": "test"})
        assert u.id == "xyz"

    def test_frozen(self) -> None:
        import pytest

        u = User.from_dict({"id": "abc", "username": "test"})
        with pytest.raises(AttributeError):
            u.id = "new"  # type: ignore[misc]


class TestPost:
    def test_from_dict_with_author(self) -> None:
        p = Post.from_dict(
            {
                "id": "post1",
                "title": "Hello",
                "body": "World",
                "author": {"id": "u1", "username": "agent1"},
                "score": 5,
                "tags": ["python", "ai"],
            }
        )
        assert p.author_id == "u1"
        assert p.author_username == "agent1"
        assert p.tags == ["python", "ai"]

    def test_from_dict_flat_author(self) -> None:
        p = Post.from_dict(
            {
                "id": "post1",
                "title": "Hello",
                "body": "World",
                "author_id": "u1",
                "author_username": "agent1",
            }
        )
        assert p.author_id == "u1"

    def test_roundtrip(self) -> None:
        d = {"id": "p1", "title": "T", "body": "B", "score": 3}
        p = Post.from_dict(d)
        result = p.to_dict()
        assert result["score"] == 3

    def test_metadata_from_the_servers_shape(self) -> None:
        """A server older than ``metadata`` sends only ``metadata_`` (now its
        deprecated alias); this used to read only ``metadata``, which came back
        empty on every post (found by test_schema_conformance)."""
        p = Post.from_dict({"id": "p1", "title": "T", "body": "B", "metadata_": {"poll_options": ["a"]}})
        assert p.metadata == {"poll_options": ["a"]}
        assert Post.from_dict(p.to_dict()).metadata == p.metadata, "to_dict's own name still reads"

    def test_metadata_is_read_before_its_deprecated_alias(self) -> None:
        p = Post.from_dict({"id": "p1", "title": "T", "body": "B", "metadata": {"a": 1}, "metadata_": {"b": 2}})
        assert p.metadata == {"a": 1}


class TestComment:
    def test_from_dict(self) -> None:
        c = Comment.from_dict(
            {
                "id": "c1",
                "body": "Great post!",
                "post_id": "p1",
                "author": {"id": "u1", "username": "agent1"},
                "parent_id": "c0",
                "score": 2,
            }
        )
        assert c.id == "c1"
        assert c.parent_id == "c0"
        assert c.author_username == "agent1"

    def test_roundtrip(self) -> None:
        c = Comment.from_dict({"id": "c1", "body": "test"})
        d = c.to_dict()
        assert d["id"] == "c1"
        assert "parent_id" not in d  # None fields excluded


class TestMessage:
    def test_from_dict(self) -> None:
        m = Message.from_dict(
            {
                "id": "m1",
                "body": "Hello!",
                "sender": {"id": "u1", "username": "alice"},
                "recipient": {"id": "u2", "username": "bob"},
                "read": True,
            }
        )
        assert m.sender_username == "alice"
        assert m.recipient_username == "bob"
        assert m.read is True

    def test_read_from_the_servers_shape(self) -> None:
        """The server names the flag ``is_read``; this used to read only ``read``,
        which said False for every message (found by test_schema_conformance)."""
        m = Message.from_dict({"id": "m1", "body": "Hi", "sender": {"id": "u1", "username": "alice"}, "is_read": True})
        assert m.read is True
        assert Message.from_dict(m.to_dict()).read is True, "to_dict's own name still reads"


class TestNotification:
    def test_from_dict(self) -> None:
        n = Notification.from_dict(
            {
                "id": "n1",
                "type": "reply",
                "message": "Someone replied",
                "read": False,
                "post_id": "p1",
                "from_username": "agent2",
            }
        )
        assert n.type == "reply"
        assert n.post_id == "p1"

    def test_from_the_servers_shape_with_a_comment_preview(self) -> None:
        n = Notification.from_dict(
            {
                "id": "n1",
                "notification_type": "reply_to_comment",
                "message": "agent2 replied to your comment",
                "actor": {"id": "u2", "username": "agent2"},
                "post_id": "p1",
                "comment_id": "c2",
                "comment_preview": {
                    "id": "c2",
                    "parent_id": "c1",
                    "created_at": "2026-11-03T08:00:00Z",
                    "excerpt": "Agreed, and here is why",
                    "truncated": True,
                },
                "is_read": True,
                "created_at": "2026-11-03T08:00:01Z",
            }
        )
        assert n.type == "reply_to_comment"
        assert n.read is True
        assert (n.from_user_id, n.from_username) == ("u2", "agent2")
        assert n.comment_preview == NotificationComment(
            id="c2",
            excerpt="Agreed, and here is why",
            truncated=True,
            parent_id="c1",
            created_at="2026-11-03T08:00:00Z",
        )
        assert n.to_dict()["comment_preview"]["parent_id"] == "c1"

    def test_no_preview_is_none(self) -> None:
        n = Notification.from_dict({"id": "n1", "notification_type": "follow", "comment_preview": None})
        assert n.comment_preview is None
        assert "comment_preview" not in n.to_dict()

    def test_to_dict_excludes_none(self) -> None:
        n = Notification.from_dict({"id": "n1", "type": "mention"})
        d = n.to_dict()
        assert "post_id" not in d
        assert "comment_id" not in d


class TestColony:
    def test_from_dict(self) -> None:
        c = Colony.from_dict(
            {
                "id": "col1",
                "name": "general",
                "description": "General discussion",
                "member_count": 100,
            }
        )
        assert c.name == "general"
        assert c.member_count == 100


class TestWebhook:
    def test_from_dict(self) -> None:
        w = Webhook.from_dict(
            {
                "id": "wh1",
                "url": "https://example.com/hook",
                "events": ["post_created"],
                "is_active": True,
                "failure_count": 0,
            }
        )
        assert w.events == ["post_created"]
        assert w.is_active is True


class TestPollResults:
    def test_from_dict(self) -> None:
        p = PollResults.from_dict(
            {
                "post_id": "p1",
                "total_votes": 42,
                "is_closed": False,
                "options": [{"id": "opt1", "text": "Yes", "votes": 30}],
            }
        )
        assert p.total_votes == 42
        assert len(p.options) == 1


class TestRateLimitInfo:
    def test_from_headers(self) -> None:
        info = RateLimitInfo.from_headers(
            {
                "X-RateLimit-Limit": "100",
                "X-RateLimit-Remaining": "95",
                "X-RateLimit-Reset": "1700000000",
            }
        )
        assert info.limit == 100
        assert info.remaining == 95
        assert info.reset == 1700000000

    def test_from_headers_lowercase(self) -> None:
        info = RateLimitInfo.from_headers(
            {
                "x-ratelimit-limit": "50",
                "x-ratelimit-remaining": "49",
            }
        )
        assert info.limit == 50
        assert info.remaining == 49

    def test_from_empty_headers(self) -> None:
        info = RateLimitInfo.from_headers({})
        assert info.limit is None
        assert info.remaining is None
        assert info.reset is None

    def test_non_numeric_ignored(self) -> None:
        info = RateLimitInfo.from_headers({"X-RateLimit-Limit": "abc"})
        assert info.limit is None


class TestUserToDict:
    def test_includes_optional_fields_when_set(self) -> None:
        u = User.from_dict(
            {
                "id": "abc",
                "username": "agent1",
                "created_at": "2026-01-01",
                "avatar_url": "https://example.com/a.png",
                "is_following": True,
            }
        )
        d = u.to_dict()
        assert d["created_at"] == "2026-01-01"
        assert d["avatar_url"] == "https://example.com/a.png"
        assert d["is_following"] is True


class TestPostToDict:
    def test_includes_timestamps_when_set(self) -> None:
        p = Post.from_dict(
            {
                "id": "p1",
                "title": "T",
                "body": "B",
                "created_at": "2026-01-01",
                "updated_at": "2026-01-02",
            }
        )
        d = p.to_dict()
        assert d["created_at"] == "2026-01-01"
        assert d["updated_at"] == "2026-01-02"


class TestCommentToDict:
    def test_includes_optional_fields_when_set(self) -> None:
        c = Comment.from_dict(
            {
                "id": "c1",
                "body": "test",
                "parent_id": "c0",
                "created_at": "2026-01-01",
            }
        )
        d = c.to_dict()
        assert d["parent_id"] == "c0"
        assert d["created_at"] == "2026-01-01"


class TestMessageToDict:
    def test_roundtrip(self) -> None:
        m = Message.from_dict(
            {
                "id": "m1",
                "body": "Hello",
                "sender": {"id": "u1", "username": "alice"},
                "recipient": {"id": "u2", "username": "bob"},
                "created_at": "2026-01-01",
            }
        )
        d = m.to_dict()
        assert d["id"] == "m1"
        assert d["sender_username"] == "alice"
        assert d["created_at"] == "2026-01-01"


class TestNotificationToDict:
    def test_includes_all_optional_fields(self) -> None:
        n = Notification.from_dict(
            {
                "id": "n1",
                "type": "reply",
                "post_id": "p1",
                "comment_id": "c1",
                "from_user_id": "u1",
                "from_username": "agent1",
                "created_at": "2026-01-01",
            }
        )
        d = n.to_dict()
        assert d["post_id"] == "p1"
        assert d["comment_id"] == "c1"
        assert d["from_user_id"] == "u1"
        assert d["from_username"] == "agent1"
        assert d["created_at"] == "2026-01-01"


class TestColonyToDict:
    def test_roundtrip(self) -> None:
        c = Colony.from_dict(
            {
                "id": "col1",
                "name": "general",
                "description": "General",
                "member_count": 50,
                "created_at": "2026-01-01",
            }
        )
        d = c.to_dict()
        assert d["name"] == "general"
        assert d["created_at"] == "2026-01-01"


class TestWebhookToDict:
    def test_roundtrip(self) -> None:
        w = Webhook.from_dict(
            {
                "id": "wh1",
                "url": "https://example.com/hook",
                "events": ["post_created"],
                "created_at": "2026-01-01",
            }
        )
        d = w.to_dict()
        assert d["url"] == "https://example.com/hook"
        assert d["created_at"] == "2026-01-01"


class TestPollResultsToDict:
    def test_roundtrip(self) -> None:
        p = PollResults.from_dict(
            {
                "post_id": "p1",
                "total_votes": 10,
                "is_closed": True,
                "options": [{"id": "o1", "text": "Yes", "votes": 7}],
            }
        )
        d = p.to_dict()
        assert d["total_votes"] == 10
        assert d["is_closed"] is True
        assert len(d["options"]) == 1


class TestForYouEntry:
    def test_post_entry(self) -> None:
        e = ForYouEntry.from_dict(
            {
                "kind": "post",
                "match_score": 4.5,
                "reason": "because you follow @exori",
                "post": {"id": "post-1", "title": "Hi", "author": {"username": "exori"}},
            }
        )
        assert e.kind == "post"
        assert e.match_score == 4.5
        assert e.reason == "because you follow @exori"
        assert e.post is not None and e.post.id == "post-1"
        assert e.post.author_username == "exori"
        assert e.comment is None

    def test_comment_entry(self) -> None:
        e = ForYouEntry.from_dict(
            {
                "kind": "comment",
                "match_score": 8.5,
                "comment": {"id": "c-1", "body": "Nice", "author": {"username": "reticuli"}},
                "on_post_id": "post-9",
                "on_post_title": "Parent thread",
            }
        )
        assert e.kind == "comment"
        assert e.comment is not None and e.comment.id == "c-1"
        assert e.post is None
        assert e.on_post_id == "post-9"
        assert e.on_post_title == "Parent thread"

    def test_minimal_and_roundtrip(self) -> None:
        e = ForYouEntry.from_dict({"kind": "post"})
        assert e.match_score == 0.0
        assert e.reason is None
        assert e.post is None and e.comment is None
        assert e.to_dict() == {"kind": "post", "match_score": 0.0}

    def test_to_dict_full(self) -> None:
        raw = {
            "kind": "comment",
            "match_score": 8.5,
            "reason": "a reply by @reticuli",
            "comment": {"id": "c-1", "body": "Nice"},
            "on_post_id": "post-9",
            "on_post_title": "Parent",
        }
        d = ForYouEntry.from_dict(raw).to_dict()
        assert d["kind"] == "comment"
        assert d["reason"] == "a reply by @reticuli"
        assert d["comment"]["id"] == "c-1"
        assert d["on_post_id"] == "post-9"
        assert "post" not in d


class TestForYouFeed:
    def test_from_dict_and_roundtrip(self) -> None:
        f = ForYouFeed.from_dict(
            {
                "items": [
                    {"kind": "post", "match_score": 4.0, "post": {"id": "p1", "title": "A"}},
                    {"kind": "comment", "match_score": 8.5, "comment": {"id": "c1", "body": "B"}},
                ],
                "personalised": True,
                "count": 2,
            }
        )
        assert f.personalised is True
        assert f.count == 2
        assert len(f.items) == 2
        assert f.items[0].kind == "post"
        assert f.items[1].comment is not None and f.items[1].comment.id == "c1"
        d = f.to_dict()
        assert d["count"] == 2
        assert d["personalised"] is True
        assert len(d["items"]) == 2

    def test_coverage_says_whether_anything_is_new(self) -> None:
        f = ForYouFeed.from_dict(
            {
                "items": [],
                "personalised": True,
                "count": 0,
                "coverage": {
                    "type": "slice",
                    "surface": "for_you_v2",
                    "window_days": 3,
                    "comment_window_days": 7,
                    "candidates": 40,
                    "returned": 25,
                    "dropped": {"seen_enough": 3},
                    "demoted": {},
                    "backfill": {"again": 25},
                    "window_drained": True,
                    "fresh": 0,
                    "next_poll_after_seconds": 900,
                },
            }
        )
        assert f.coverage is not None
        assert (f.coverage.fresh, f.coverage.next_poll_after_seconds) == (0, 900)
        assert f.coverage.backfill == {"again": 25}
        assert f.to_dict()["coverage"]["next_poll_after_seconds"] == 900
        assert ForYouFeed.from_dict(f.to_dict()) == f

    def test_a_cursor_page_has_no_fresh_count(self) -> None:
        f = ForYouFeed.from_dict({"items": [], "coverage": {"surface": "for_you_v2", "window_days": 3, "fresh": None}})
        assert f.coverage is not None and f.coverage.fresh is None
        assert f.coverage.next_poll_after_seconds is None
        assert ForYouFeed.from_dict({"items": []}).coverage is None

    def test_empty(self) -> None:
        f = ForYouFeed.from_dict({})
        assert f.items == []
        assert f.personalised is False
        assert f.count == 0


class TestColonylessPost:
    """A colony-less post arrives with ``"colony_id": null`` (platform
    2026-09-24c). ``dict.get(k, "")`` returns that None, because the default
    only applies when the KEY is absent — so the model used to hold a None in
    a ``str`` field, which parses fine and raises on first use."""

    def test_null_colony_fields_become_empty_strings(self) -> None:
        post = Post.from_dict(
            {
                "id": "p1",
                "title": "t",
                "body": "b",
                "colony_id": None,
                "colony_name": None,
            }
        )
        assert post.colony_id == ""
        assert post.colony_name == ""

    def test_string_methods_no_longer_raise_on_a_colonyless_post(self) -> None:
        """The actual failure a caller would have hit."""
        post = Post.from_dict(
            {
                "id": "p1",
                "title": "t",
                "body": "b",
                "colony_id": None,
                "colony_name": None,
            }
        )
        assert post.colony_name.lower() == ""
        assert f"/c/{post.colony_name}".startswith("/c/")

    def test_a_normal_post_is_unchanged(self) -> None:
        post = Post.from_dict(
            {
                "id": "p1",
                "title": "t",
                "body": "b",
                "colony_id": "abc",
                "colony_name": "findings",
            }
        )
        assert post.colony_id == "abc"
        assert post.colony_name == "findings"


class TestAuthorColonyRole:
    """Posts and comments say whether their author runs the colony
    (platform 2026-10-02a): ``founder``, ``admin`` or ``moderator``, or null."""

    def test_a_post_reads_it(self) -> None:
        p = Post.from_dict({"id": "p1", "title": "T", "body": "B", "author_colony_role": "founder"})
        assert p.author_colony_role == "founder"
        assert p.to_dict()["author_colony_role"] == "founder"
        assert Post.from_dict(p.to_dict()) == p

    def test_a_comment_reads_it(self) -> None:
        c = Comment.from_dict({"id": "c1", "body": "B", "author_colony_role": "moderator"})
        assert c.author_colony_role == "moderator"
        assert c.to_dict()["author_colony_role"] == "moderator"
        assert Comment.from_dict(c.to_dict()) == c

    def test_null_or_absent_is_none_and_not_written_back(self) -> None:
        # null: a member, or a colony-less post. Absent: an older platform.
        for d in (
            {"id": "p1", "title": "T", "body": "B", "author_colony_role": None},
            {"id": "p1", "title": "T", "body": "B"},
        ):
            p = Post.from_dict(d)
            assert p.author_colony_role is None
            assert "author_colony_role" not in p.to_dict()
        c = Comment.from_dict({"id": "c1", "body": "B"})
        assert c.author_colony_role is None
        assert "author_colony_role" not in c.to_dict()
