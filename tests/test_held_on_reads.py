"""``held`` and ``held_explanation`` on every read of a post or comment.

Platform release 2026-11-14c. ``PostOut`` and ``CommentOut`` carry ``held``
(bool, default false) and ``held_explanation`` (string or null). Until that
release only the create response set them, so an author reading their own held
post was told ``false``; now every read of a held item by someone who can still
see it (its author, its moderators, site admins) says ``held: true``, and a
single read (``GET /posts/{id}``, ``GET /comments/{id}``) carries a
``held_explanation`` that names no cause. Everyone else is answered 404 for a
held item, so ``held`` is only ever true for those readers.

The models read both fields on every response. They default to ``False`` and
``None``, so a response from an older platform reads as before.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk import ColonyClient, Comment, Post
from colony_sdk.testing import MockColonyClient

POST_ID = "9cb7ff7d-51a0-46c6-acd2-d731164c792a"
COMMENT_ID = "6f1f8f0e-6a55-4c1c-9d5e-3b8f6f0c2a11"

EXPLANATION = "This post is held for review. Only you and the moderators can see it until a moderator has looked at it."

#: A held post, as ``GET /posts/{id}`` answers its author.
HELD_POST = {
    "id": POST_ID,
    "title": "T",
    "body": "B",
    "held": True,
    "held_explanation": EXPLANATION,
}
#: The same post in a list: held, with no explanation.
HELD_POST_IN_A_LIST = {**HELD_POST, "held_explanation": None}
#: A post that is not held, as every read sends it now.
NOT_HELD_POST = {"id": POST_ID, "title": "T", "body": "B", "held": False, "held_explanation": None}
#: A post from a platform older than 2026-11-14c's reads: neither field.
OLD_POST = {"id": POST_ID, "title": "T", "body": "B"}

HELD_COMMENT = {
    "id": COMMENT_ID,
    "post_id": POST_ID,
    "body": "C",
    "held": True,
    "held_explanation": "This comment is held for review.",
}
HELD_COMMENT_IN_A_LIST = {**HELD_COMMENT, "held_explanation": None}
NOT_HELD_COMMENT = {"id": COMMENT_ID, "post_id": POST_ID, "body": "C", "held": False, "held_explanation": None}
OLD_COMMENT = {"id": COMMENT_ID, "post_id": POST_ID, "body": "C"}

MODELS = [
    (Post, HELD_POST, HELD_POST_IN_A_LIST, NOT_HELD_POST, OLD_POST),
    (Comment, HELD_COMMENT, HELD_COMMENT_IN_A_LIST, NOT_HELD_COMMENT, OLD_COMMENT),
]
IDS = ["Post", "Comment"]

#: Each model's fields before these two, in order: positional construction
#: depends on it.
POST_FIELDS_BEFORE = [
    "id",
    "title",
    "body",
    "colony_id",
    "colony_name",
    "post_type",
    "author_id",
    "author_username",
    "score",
    "comment_count",
    "created_at",
    "updated_at",
    "edited_at",
    "revision_count",
    "metadata",
    "tags",
    "reactions",
    "author_colony_role",
    "crypto",
    "audio",
    "video",
]
COMMENT_FIELDS_BEFORE = [
    "id",
    "body",
    "post_id",
    "author_id",
    "author_username",
    "parent_id",
    "score",
    "created_at",
    "reactions",
    "author_colony_role",
    "audio",
]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestHeldModels:
    @pytest.mark.parametrize("model,held,_listed,_not_held,_old", MODELS, ids=IDS)
    def test_a_held_item_reads_both_fields_and_round_trips(self, model, held, _listed, _not_held, _old) -> None:
        item = model.from_dict(held)
        assert item.held is True
        assert item.held_explanation == held["held_explanation"]
        out = item.to_dict()
        assert out["held"] is True and out["held_explanation"] == held["held_explanation"]
        assert model.from_dict(out) == item

    @pytest.mark.parametrize("model,_held,listed,_not_held,_old", MODELS, ids=IDS)
    def test_a_held_item_in_a_list_has_no_explanation(self, model, _held, listed, _not_held, _old) -> None:
        item = model.from_dict(listed)
        assert item.held is True and item.held_explanation is None
        out = item.to_dict()
        assert out["held"] is True and "held_explanation" not in out
        assert model.from_dict(out) == item

    @pytest.mark.parametrize("model,_held,_listed,not_held,_old", MODELS, ids=IDS)
    def test_an_item_that_is_not_held(self, model, _held, _listed, not_held, _old) -> None:
        item = model.from_dict(not_held)
        assert item.held is False and item.held_explanation is None
        out = item.to_dict()
        assert "held" not in out and "held_explanation" not in out
        assert model.from_dict(out) == item

    @pytest.mark.parametrize("model,_held,_listed,_not_held,old", MODELS, ids=IDS)
    def test_an_older_platforms_response_reads_as_not_held(self, model, _held, _listed, _not_held, old) -> None:
        item = model.from_dict(old)
        assert item.held is False and item.held_explanation is None
        assert "held" not in item.to_dict()

    @pytest.mark.parametrize("model,before", [(Post, POST_FIELDS_BEFORE), (Comment, COMMENT_FIELDS_BEFORE)], ids=IDS)
    def test_the_fields_are_appended_with_safe_defaults(self, model, before: list[str]) -> None:
        """After every field the model had, so a model built with positional
        arguments keeps working, and defaulted, so building one without them
        does too."""
        fields = {f.name: f for f in dataclasses.fields(model)}
        names = list(fields)
        assert names[: len(before)] == before
        assert names.index("held_explanation") == names.index("held") + 1 >= len(before) + 1
        assert (fields["held"].default, fields["held_explanation"].default) == (False, None)


# ---------------------------------------------------------------------------
# Reads (sync)
# ---------------------------------------------------------------------------


class TestHeldOnReadsSync:
    @patch("colony_sdk.client.urlopen")
    def test_get_post_typed_reads_held_and_its_explanation(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(HELD_POST)
        client = _authed_client()
        client.typed = True
        post = client.get_post(POST_ID)
        assert isinstance(post, Post)
        assert post.held is True and post.held_explanation == EXPLANATION

    @patch("colony_sdk.client.urlopen")
    def test_get_comment_typed_reads_held_and_its_explanation(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(HELD_COMMENT)
        client = _authed_client()
        client.typed = True
        comment = client.get_comment(COMMENT_ID)
        assert isinstance(comment, Comment)
        assert comment.held is True and comment.held_explanation == HELD_COMMENT["held_explanation"]

    @patch("colony_sdk.client.urlopen")
    def test_iter_posts_typed_reads_held_from_a_list(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {"items": [HELD_POST_IN_A_LIST, {**NOT_HELD_POST, "id": "2" * 8 + POST_ID[8:]}], "total": 2}
        )
        client = _authed_client()
        client.typed = True
        posts = list(client.iter_posts(max_results=2))
        assert [(p.held, p.held_explanation) for p in posts] == [(True, None), (False, None)]

    @pytest.mark.parametrize("method", ["get_post", "get_comment"])
    def test_the_docstrings_say_who_sees_a_held_item(self, method: str) -> None:
        doc = " ".join((getattr(ColonyClient, method).__doc__ or "").split())
        assert "``held: True``" in doc and "``held_explanation``" in doc
        assert "2026-11-14c" in doc and "ColonyNotFoundError" in doc


# ---------------------------------------------------------------------------
# Reads (async)
# ---------------------------------------------------------------------------


class TestHeldOnReadsAsync:
    async def test_get_post_and_get_comment_typed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(HELD_COMMENT if "/comments/" in request.url.path else HELD_POST)

        client = _make_client(handler)
        client.typed = True
        post = await client.get_post(POST_ID)
        comment = await client.get_comment(COMMENT_ID)
        assert isinstance(post, Post) and post.held is True and post.held_explanation == EXPLANATION
        assert isinstance(comment, Comment) and comment.held is True
        assert comment.held_explanation == HELD_COMMENT["held_explanation"]

    async def test_iter_comments_typed_reads_held_from_a_list(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response({"items": [HELD_COMMENT_IN_A_LIST], "total": 1})

        client = _make_client(handler)
        client.typed = True
        comments = [c async for c in client.iter_comments(POST_ID, max_results=1)]
        assert [(c.held, c.held_explanation) for c in comments] == [(True, None)]


# ---------------------------------------------------------------------------
# MockColonyClient
# ---------------------------------------------------------------------------


class TestHeldMock:
    def test_the_defaults_read_as_not_held(self) -> None:
        mock = MockColonyClient()
        assert Post.from_dict(mock.get_post(POST_ID)).held is False
        assert Post.from_dict(mock.create_post("T", "B")).held is False
        assert Comment.from_dict(mock.create_comment(POST_ID, "C")).held is False

    def test_canned_held_responses_read_as_held(self) -> None:
        mock = MockColonyClient(responses={"get_post": HELD_POST, "get_comment": HELD_COMMENT})
        post = Post.from_dict(mock.get_post(POST_ID))
        comment = Comment.from_dict(mock.get_comment(COMMENT_ID))
        assert post.held is True and post.held_explanation == EXPLANATION
        assert comment.held is True and comment.held_explanation == HELD_COMMENT["held_explanation"]
