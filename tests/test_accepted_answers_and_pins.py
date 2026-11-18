"""Accepted answers and pinned comments: ``accept_answer`` /
``clear_accepted_answer``, ``pin_comment`` / ``unpin_comment``, a post's
``accepted_comment_id`` and a comment's ``pinned_at``.

Platform release 2026-11-17b. The author of a question accepts one live
comment on it, a reply or not, as its answer: ``PUT
/api/v1/posts/{id}/accepted-answer`` with ``{"comment_id": ...}``, which sets
the question's ``status`` to ``answered``; ``DELETE`` takes it back and the
status returns to ``open``. Both answer ``AcceptedAnswerOut``
(``{post_id, accepted_comment_id, status}``).

The post's author, or a moderator of its colony, pins one top-level comment to
the top of the post: ``PUT /api/v1/comments/{id}/pin`` (no body), which
unpins any other and names it in ``unpinned_comment_ids``; ``DELETE`` unpins.
Both answer ``CommentPinOut`` (``{comment_id, post_id, pinned_at,
unpinned_comment_ids}``). All four are idempotent.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client, _last_body, _last_request, _make_http_error, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk import (
    ColonyAPIError,
    ColonyAuthError,
    ColonyNotFoundError,
    ColonyRateLimitError,
    ColonyValidationError,
    Comment,
    Post,
    RetryConfig,
)
from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"
POST_ID = "9cb7ff7d-51a0-46c6-acd2-d731164c792a"
COMMENT_ID = "3f2c1b9e-8d7a-4c6b-9e5f-1a2b3c4d5e6f"
OTHER_ID = "0d4e5f6a-7b8c-4d9e-8f0a-1b2c3d4e5f60"
PINNED_AT = "2026-11-17T20:30:00Z"

#: ``AcceptedAnswerOut`` after a PUT, and after a DELETE.
ACCEPTED = {"post_id": POST_ID, "accepted_comment_id": COMMENT_ID, "status": "answered"}
CLEARED = {"post_id": POST_ID, "accepted_comment_id": None, "status": "open"}

#: ``CommentPinOut`` after a PUT that unpinned another comment, and after a DELETE.
PINNED = {"comment_id": COMMENT_ID, "post_id": POST_ID, "pinned_at": PINNED_AT, "unpinned_comment_ids": [OTHER_ID]}
UNPINNED = {"comment_id": COMMENT_ID, "post_id": POST_ID, "pinned_at": None, "unpinned_comment_ids": []}

BAD_IDS = ["../users/me", "..", "a/b", POST_ID[:8]]


def _refusal(status: int, code: str) -> Exception:
    return _make_http_error(status, {"detail": {"message": code.lower(), "code": code}})


# ---------------------------------------------------------------------------
# accept_answer / clear_accepted_answer (sync)
# ---------------------------------------------------------------------------


class TestAcceptAnswerSync:
    @patch("colony_sdk.client.urlopen")
    def test_puts_the_comment_id_on_the_posts_accepted_answer(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(ACCEPTED)

        out = _authed_client().accept_answer(POST_ID, COMMENT_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/posts/{POST_ID}/accepted-answer"
        assert _last_body(mock_urlopen) == {"comment_id": COMMENT_ID}
        assert out == ACCEPTED

    @patch("colony_sdk.client.urlopen")
    def test_typed_still_returns_the_dict(self, mock_urlopen: MagicMock) -> None:
        """No model for ``AcceptedAnswerOut``: the dict, typed or not."""
        mock_urlopen.return_value = _mock_response(ACCEPTED)
        client = _authed_client()
        client.typed = True
        assert client.accept_answer(POST_ID, COMMENT_ID) == ACCEPTED

    def test_both_ids_are_required(self) -> None:
        with pytest.raises(TypeError):
            _authed_client().accept_answer(POST_ID)  # type: ignore[call-arg]

    @pytest.mark.parametrize("bad", BAD_IDS)
    @patch("colony_sdk.client.urlopen")
    def test_a_post_id_that_is_not_one_segment_or_is_truncated_is_refused(
        self, mock_urlopen: MagicMock, bad: str
    ) -> None:
        with pytest.raises(ValueError):
            _authed_client().accept_answer(bad, COMMENT_ID)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_a_truncated_comment_id_is_refused(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(ValueError, match="comment_id"):
            _authed_client().accept_answer(POST_ID, COMMENT_ID[:8])
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize(
        "status,code,error",
        [
            (403, "FORBIDDEN", ColonyAuthError),
            (400, "INVALID_INPUT", ColonyValidationError),
            (404, "POST_NOT_FOUND", ColonyNotFoundError),
            (404, "NOT_FOUND", ColonyNotFoundError),
            (429, "RATE_LIMITED", ColonyRateLimitError),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_a_refusal_raises_with_its_code(
        self, mock_urlopen: MagicMock, status: int, code: str, error: type[ColonyAPIError]
    ) -> None:
        mock_urlopen.side_effect = _refusal(status, code)
        client = _authed_client()
        client.retry = RetryConfig(max_retries=0)  # a 429 would otherwise be retried
        with pytest.raises(error) as exc:
            client.accept_answer(POST_ID, COMMENT_ID)
        assert exc.value.status == status and exc.value.code == code


class TestClearAcceptedAnswerSync:
    @patch("colony_sdk.client.urlopen")
    def test_deletes_the_posts_accepted_answer_with_no_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(CLEARED)

        out = _authed_client().clear_accepted_answer(POST_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/posts/{POST_ID}/accepted-answer"
        assert req.data is None
        assert out == CLEARED

    @pytest.mark.parametrize("bad", BAD_IDS)
    @patch("colony_sdk.client.urlopen")
    def test_the_post_id_guard(self, mock_urlopen: MagicMock, bad: str) -> None:
        with pytest.raises(ValueError):
            _authed_client().clear_accepted_answer(bad)
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize(
        "status,code,error",
        [
            (403, "FORBIDDEN", ColonyAuthError),
            (400, "INVALID_INPUT", ColonyValidationError),
            (404, "POST_NOT_FOUND", ColonyNotFoundError),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_a_refusal_raises_with_its_code(
        self, mock_urlopen: MagicMock, status: int, code: str, error: type[ColonyAPIError]
    ) -> None:
        mock_urlopen.side_effect = _refusal(status, code)
        with pytest.raises(error) as exc:
            _authed_client().clear_accepted_answer(POST_ID)
        assert exc.value.code == code


# ---------------------------------------------------------------------------
# pin_comment / unpin_comment (sync)
# ---------------------------------------------------------------------------


class TestPinCommentSync:
    @patch("colony_sdk.client.urlopen")
    def test_puts_the_comments_pin_with_no_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(PINNED)

        out = _authed_client().pin_comment(COMMENT_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/comments/{COMMENT_ID}/pin"
        assert req.data is None
        assert out == PINNED
        assert out["unpinned_comment_ids"] == [OTHER_ID]

    @pytest.mark.parametrize("bad", BAD_IDS)
    @patch("colony_sdk.client.urlopen")
    def test_the_comment_id_guard(self, mock_urlopen: MagicMock, bad: str) -> None:
        with pytest.raises(ValueError):
            _authed_client().pin_comment(bad)
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize(
        "status,code,error",
        [
            (403, "FORBIDDEN", ColonyAuthError),
            (400, "INVALID_INPUT", ColonyValidationError),
            (404, "NOT_FOUND", ColonyNotFoundError),
            (404, "POST_NOT_FOUND", ColonyNotFoundError),
            (429, "RATE_LIMITED", ColonyRateLimitError),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_a_refusal_raises_with_its_code(
        self, mock_urlopen: MagicMock, status: int, code: str, error: type[ColonyAPIError]
    ) -> None:
        mock_urlopen.side_effect = _refusal(status, code)
        client = _authed_client()
        client.retry = RetryConfig(max_retries=0)
        with pytest.raises(error) as exc:
            client.pin_comment(COMMENT_ID)
        assert exc.value.status == status and exc.value.code == code


class TestUnpinCommentSync:
    @patch("colony_sdk.client.urlopen")
    def test_deletes_the_comments_pin_with_no_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(UNPINNED)

        out = _authed_client().unpin_comment(COMMENT_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/comments/{COMMENT_ID}/pin"
        assert req.data is None
        assert out == UNPINNED

    @pytest.mark.parametrize("bad", BAD_IDS)
    @patch("colony_sdk.client.urlopen")
    def test_the_comment_id_guard(self, mock_urlopen: MagicMock, bad: str) -> None:
        with pytest.raises(ValueError):
            _authed_client().unpin_comment(bad)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_a_refusal_raises_with_its_code(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _refusal(403, "FORBIDDEN")
        with pytest.raises(ColonyAuthError) as exc:
            _authed_client().unpin_comment(COMMENT_ID)
        assert exc.value.code == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Async
# ---------------------------------------------------------------------------


class TestAsync:
    async def test_all_four_routes(self) -> None:
        seen: list[tuple[str, str, bytes]] = []
        answers = {
            ("PUT", "accepted-answer"): ACCEPTED,
            ("DELETE", "accepted-answer"): CLEARED,
            ("PUT", "pin"): PINNED,
            ("DELETE", "pin"): UNPINNED,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url), request.content))
            return _json_response(answers[(request.method, str(request.url).rsplit("/", 1)[1])])

        client = _make_client(handler)
        assert await client.accept_answer(POST_ID, COMMENT_ID) == ACCEPTED
        assert await client.clear_accepted_answer(POST_ID) == CLEARED
        assert await client.pin_comment(COMMENT_ID) == PINNED
        assert await client.unpin_comment(COMMENT_ID) == UNPINNED

        assert [(method, url) for method, url, _ in seen] == [
            ("PUT", f"{BASE}/posts/{POST_ID}/accepted-answer"),
            ("DELETE", f"{BASE}/posts/{POST_ID}/accepted-answer"),
            ("PUT", f"{BASE}/comments/{COMMENT_ID}/pin"),
            ("DELETE", f"{BASE}/comments/{COMMENT_ID}/pin"),
        ]
        assert json.loads(seen[0][2]) == {"comment_id": COMMENT_ID}
        assert [content for _, _, content in seen[1:]] == [b"", b"", b""]

    @pytest.mark.parametrize(
        "call",
        [
            lambda c, bad: c.accept_answer(bad, COMMENT_ID),
            lambda c, bad: c.clear_accepted_answer(bad),
            lambda c, bad: c.pin_comment(bad),
            lambda c, bad: c.unpin_comment(bad),
        ],
        ids=["accept", "clear", "pin", "unpin"],
    )
    @pytest.mark.parametrize("bad", BAD_IDS)
    async def test_the_id_guard(self, call, bad: str) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return _json_response({})

        with pytest.raises(ValueError):
            await call(_make_client(handler), bad)
        assert calls == []

    async def test_a_truncated_comment_id_in_the_body_is_refused(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return _json_response({})

        with pytest.raises(ValueError, match="comment_id"):
            await _make_client(handler).accept_answer(POST_ID, COMMENT_ID[:8])
        assert calls == []

    async def test_a_reply_cannot_be_pinned(self) -> None:
        detail = {"message": "Only a top-level comment can be pinned.", "code": "INVALID_INPUT"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, content=json.dumps({"detail": detail}).encode())

        with pytest.raises(ColonyValidationError) as exc:
            await _make_client(handler).pin_comment(COMMENT_ID)
        assert exc.value.code == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# MockColonyClient
# ---------------------------------------------------------------------------


class TestMock:
    def test_the_defaults_have_the_servers_shape(self) -> None:
        mock = MockColonyClient()
        accepted = mock.accept_answer(POST_ID, COMMENT_ID)
        cleared = mock.clear_accepted_answer(POST_ID)
        pinned = mock.pin_comment(COMMENT_ID)
        unpinned = mock.unpin_comment(COMMENT_ID)
        assert mock.calls == [
            ("accept_answer", {"post_id": POST_ID, "comment_id": COMMENT_ID}),
            ("clear_accepted_answer", {"post_id": POST_ID}),
            ("pin_comment", {"comment_id": COMMENT_ID}),
            ("unpin_comment", {"comment_id": COMMENT_ID}),
        ]
        assert accepted["status"] == "answered" and accepted["accepted_comment_id"]
        assert cleared["status"] == "open" and cleared["accepted_comment_id"] is None
        assert pinned["pinned_at"] and pinned["unpinned_comment_ids"] == []
        assert unpinned["pinned_at"] is None

    def test_canned_responses(self) -> None:
        mock = MockColonyClient(responses={"pin_comment": PINNED})
        assert mock.pin_comment(COMMENT_ID)["unpinned_comment_ids"] == [OTHER_ID]

    @pytest.mark.parametrize("bad", BAD_IDS)
    def test_the_id_guard(self, bad: str) -> None:
        mock = MockColonyClient()
        for call in (
            lambda: mock.accept_answer(bad, COMMENT_ID),
            lambda: mock.clear_accepted_answer(bad),
            lambda: mock.pin_comment(bad),
            lambda: mock.unpin_comment(bad),
        ):
            with pytest.raises(ValueError):
                call()
        with pytest.raises(ValueError, match="comment_id"):
            mock.accept_answer(POST_ID, COMMENT_ID[:8])
        assert mock.calls == []


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestModels:
    @pytest.mark.parametrize("payload", [{}, {"accepted_comment_id": None}], ids=["absent", "null"])
    def test_a_post_without_an_accepted_answer(self, payload: dict) -> None:
        post = Post.from_dict({"id": POST_ID, "title": "T", "body": "B", **payload})
        assert post.accepted_comment_id is None
        assert "accepted_comment_id" not in post.to_dict()

    def test_a_post_with_an_accepted_answer_reads_and_round_trips(self) -> None:
        post = Post.from_dict({"id": POST_ID, "title": "T", "body": "B", "accepted_comment_id": COMMENT_ID})
        assert post.accepted_comment_id == COMMENT_ID
        assert post.to_dict()["accepted_comment_id"] == COMMENT_ID
        assert Post.from_dict(post.to_dict()) == post

    @pytest.mark.parametrize("payload", [{}, {"pinned_at": None}], ids=["absent", "null"])
    def test_a_comment_that_is_not_pinned(self, payload: dict) -> None:
        comment = Comment.from_dict({"id": COMMENT_ID, "body": "B", **payload})
        assert comment.pinned_at is None
        assert "pinned_at" not in comment.to_dict()

    def test_a_pinned_comment_reads_and_round_trips(self) -> None:
        comment = Comment.from_dict({"id": COMMENT_ID, "body": "B", "post_id": POST_ID, "pinned_at": PINNED_AT})
        assert comment.pinned_at == PINNED_AT
        assert comment.to_dict()["pinned_at"] == PINNED_AT
        assert Comment.from_dict(comment.to_dict()) == comment

    @patch("colony_sdk.client.urlopen")
    def test_get_post_typed_reads_the_accepted_answer(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": POST_ID, "title": "T", "body": "B", **ACCEPTED})
        client = _authed_client()
        client.typed = True
        post = client.get_post(POST_ID)
        assert isinstance(post, Post) and post.accepted_comment_id == COMMENT_ID

    @patch("colony_sdk.client.urlopen")
    def test_get_comment_typed_reads_pinned_at(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": COMMENT_ID, "body": "B", "pinned_at": PINNED_AT})
        client = _authed_client()
        client.typed = True
        comment = client.get_comment(COMMENT_ID)
        assert isinstance(comment, Comment) and comment.pinned_at == PINNED_AT
