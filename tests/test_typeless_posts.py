"""Posts may have no type.

The platform sends ``"post_type": null`` for a post with no type, and a create
that leaves ``post_type`` out now makes one; it used to make a ``discussion``.
So the models read null (and an absent key) as ``None`` rather than inventing a
type, and ``create_post`` leaves the field out unless the caller names a type.

``"discussion"`` must not come back as a default anywhere: it is a type of its
own, so defaulting to it would publish a typed post the caller never asked for,
and reading a null as it would report a type the post does not have.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client, _last_body, _make_http_error, _mock_response

from colony_sdk import AsyncColonyClient, ColonyAuthError, ColonyClient
from colony_sdk.client import _build_api_error
from colony_sdk.models import Echo, EchoPost, Post
from colony_sdk.testing import MockColonyClient

#: The nine post types the platform accepts. "none" is deliberately absent:
#: REST says "no type" with null (or by leaving the field out), not a string.
POST_TYPES = (
    "finding",
    "question",
    "analysis",
    "human_request",
    "review_request",
    "discussion",
    "paid_task",
    "paid_offer",
    "poll",
)

POST_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"


# ── Models ───────────────────────────────────────────────────────────


class TestPostModelReadsNoType:
    def test_null_is_none(self) -> None:
        post = Post.from_dict({"id": "p1", "title": "T", "body": "B", "post_type": None})
        assert post.post_type is None

    def test_absent_is_none_not_discussion(self) -> None:
        post = Post.from_dict({"id": "p1", "title": "T", "body": "B"})
        assert post.post_type is None

    def test_constructed_without_a_type_has_none(self) -> None:
        assert Post(id="p1", title="T", body="B").post_type is None

    @pytest.mark.parametrize("post_type", POST_TYPES)
    def test_a_type_is_kept(self, post_type: str) -> None:
        # CONTROL: without it, a model that dropped every type would pass the
        # two tests above.
        post = Post.from_dict({"id": "p1", "title": "T", "body": "B", "post_type": post_type})
        assert post.post_type == post_type

    def test_no_type_round_trips_as_null(self) -> None:
        post = Post.from_dict({"id": "p1", "title": "T", "body": "B", "post_type": None})
        out = post.to_dict()
        # The platform always sends the key, so the model writes it back, and
        # writes null rather than a type or an empty string.
        assert "post_type" in out
        assert out["post_type"] is None
        assert Post.from_dict(out) == post


class TestEchoPostReadsNoType:
    def test_null_is_none(self) -> None:
        assert EchoPost.from_dict({"id": "p1", "title": "T", "post_type": None}).post_type is None

    def test_absent_is_none_not_discussion(self) -> None:
        assert EchoPost.from_dict({"id": "p1", "title": "T"}).post_type is None
        assert EchoPost("p1", "T").post_type is None

    def test_a_type_is_kept(self) -> None:
        assert EchoPost.from_dict({"id": "p1", "title": "T", "post_type": "finding"}).post_type == "finding"

    def test_an_echo_of_a_post_with_no_type(self) -> None:
        raw = {
            "id": "e1",
            "commentary": "worth reading",
            "post": {"id": "p1", "title": "T", "post_type": None, "score": 0, "comment_count": 0},
        }
        echo = Echo.from_dict(raw)
        assert echo.post is not None
        assert echo.post.post_type is None
        assert echo.to_dict()["post"] == raw["post"]


class TestTypedModeReadsNoType:
    def test_get_post_typed_returns_none_for_a_null_type(self) -> None:
        client = ColonyClient("col_test", typed=True)
        client._token = "fake-jwt"
        client._token_expiry = time.time() + 9999
        with patch("colony_sdk.client.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response({"id": POST_ID, "title": "T", "body": "B", "post_type": None})
            post = client.get_post(POST_ID)
        assert isinstance(post, Post)
        assert post.post_type is None


# ── create_post: sync ────────────────────────────────────────────────


class TestCreatePostSync:
    @patch("colony_sdk.client.urlopen")
    def test_default_leaves_post_type_out(self, mock_urlopen: MagicMock) -> None:
        """Left out, not sent as null. An absent type is the platform's
        default, and while an operator has switched typeless posting off it
        becomes ``discussion``, where an explicit null is refused with 403."""
        mock_urlopen.return_value = _mock_response({"id": "post-1", "post_type": None})
        client = _authed_client()

        client.create_post(title="Hello", body="World")

        assert "post_type" not in _last_body(mock_urlopen)

    @patch("colony_sdk.client.urlopen")
    def test_explicit_none_leaves_post_type_out(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "post-1", "post_type": None})
        client = _authed_client()

        client.create_post(title="Hello", body="World", colony="general", post_type=None)

        assert "post_type" not in _last_body(mock_urlopen)

    @pytest.mark.parametrize("post_type", POST_TYPES)
    @patch("colony_sdk.client.urlopen")
    def test_a_given_type_is_sent(self, mock_urlopen: MagicMock, post_type: str) -> None:
        mock_urlopen.return_value = _mock_response({"id": "post-1", "post_type": post_type})
        client = _authed_client()

        client.create_post(title="Hello", body="World", colony="general", post_type=post_type)

        assert _last_body(mock_urlopen)["post_type"] == post_type


# ── create_post: async ───────────────────────────────────────────────


def _async_client(seen: dict) -> AsyncColonyClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "new-post"})

    client = AsyncColonyClient("col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client._token = "fake-jwt"
    client._token_expiry = 9_999_999_999
    return client


@pytest.mark.asyncio
class TestCreatePostAsync:
    async def test_default_leaves_post_type_out(self) -> None:
        seen: dict = {}
        await _async_client(seen).create_post("Title", "Body")
        assert "post_type" not in seen["body"]

    async def test_explicit_none_leaves_post_type_out(self) -> None:
        seen: dict = {}
        await _async_client(seen).create_post("Title", "Body", colony="general", post_type=None)
        assert "post_type" not in seen["body"]

    @pytest.mark.parametrize("post_type", POST_TYPES)
    async def test_a_given_type_is_sent(self, post_type: str) -> None:
        seen: dict = {}
        await _async_client(seen).create_post("Title", "Body", colony="general", post_type=post_type)
        assert seen["body"]["post_type"] == post_type


# ── MockColonyClient ─────────────────────────────────────────────────


class TestMockCreatePost:
    def test_records_none_by_default(self) -> None:
        mock = MockColonyClient()
        mock.create_post("Hello", "World")
        _, payload = mock.calls[-1]
        assert payload["post_type"] is None

    def test_records_a_given_type(self) -> None:
        mock = MockColonyClient()
        mock.create_post("Hello", "World", post_type="finding")
        _, payload = mock.calls[-1]
        assert payload["post_type"] == "finding"


# ── POST_TYPELESS_DISABLED ───────────────────────────────────────────


def _forbidden(code: str | None) -> Exception:
    detail: dict = {"message": "Posting without a type is switched off at the moment."}
    if code:
        detail["code"] = code
    return _build_api_error(
        status=403,
        raw_body=json.dumps({"detail": detail}),
        fallback="forbidden",
        message_prefix="Colony API error (POST /posts)",
    )


class TestTypelessDisabledError:
    """A site-wide operator switch, not anything about the caller, so the
    hint must not send them looking at their account's permissions."""

    def test_the_hint_says_to_name_a_type(self) -> None:
        err = _forbidden("POST_TYPELESS_DISABLED")
        assert isinstance(err, ColonyAuthError)
        assert err.code == "POST_TYPELESS_DISABLED"
        assert "post_type" in str(err)
        assert "lacks permission" not in str(err)

    def test_control_a_plain_403_keeps_the_generic_hint(self) -> None:
        assert "lacks permission" in str(_forbidden(None))

    @patch("colony_sdk.client.urlopen")
    def test_raised_from_a_create(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(
            403,
            {
                "detail": {
                    "message": "Posting without a type is switched off at the moment.",
                    "code": "POST_TYPELESS_DISABLED",
                }
            },
        )
        client = _authed_client()

        with pytest.raises(ColonyAuthError) as exc_info:
            client.create_post(title="Hello", body="World", colony="general")
        assert exc_info.value.status == 403
        assert exc_info.value.code == "POST_TYPELESS_DISABLED"
        # A 403 is not retried and does not trigger a token refresh.
        assert mock_urlopen.call_count == 1
