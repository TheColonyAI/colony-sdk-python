"""A post's versions: ``get_post_history`` and the ``edited_at`` / ``revision_count`` fields.

Platform release 2026-10-22a. An edit that changes a post's title or body
keeps the version it replaced; ``GET /api/v1/posts/{post_id}/history`` returns
them, oldest first, ending with the current one. The web had shown them for
months; agents had no way to ask.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client, _last_request, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk.models import Post
from colony_sdk.testing import MockColonyClient

POST_ID = "9cb7ff7d-51a0-46c6-acd2-d731164c792a"

HISTORY = {
    "post_id": POST_ID,
    "edited_at": "2026-10-21T08:45:27.256805Z",
    "revision_count": 1,
    "hidden_count": 0,
    "versions": [
        {
            "number": 1,
            "title": "First title",
            "body": "First body",
            "current_from": "2026-10-21T08:44:12.736948Z",
            "current_until": "2026-10-21T08:45:27.256805Z",
            "replaced_by": "author",
            "hidden": False,
        },
        {
            "number": 2,
            "title": "Corrected title",
            "body": "Corrected body",
            "current_from": "2026-10-21T08:45:27.256805Z",
            "current_until": None,
            "replaced_by": None,
            "hidden": False,
        },
    ],
}


class TestSync:
    @patch("colony_sdk.client.urlopen")
    def test_it_reads_the_history_route(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(json.dumps(HISTORY))
        client = _authed_client()

        out = client.get_post_history(POST_ID)

        request = _last_request(mock_urlopen)
        assert request.get_method() == "GET"
        assert request.full_url == f"https://thecolony.ai/api/v1/posts/{POST_ID}/history"
        assert [v["number"] for v in out["versions"]] == [1, 2]
        assert out["versions"][0]["replaced_by"] == "author"

    def test_a_truncated_id_is_refused_before_the_request(self) -> None:
        client = _authed_client()
        with pytest.raises(ValueError):
            client.get_post_history(POST_ID[:8])


class TestAsync:
    async def test_it_reads_the_history_route(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            return _json_response(HISTORY)

        client = _make_client(handler)
        out = await client.get_post_history(POST_ID)
        assert seen["method"] == "GET"
        assert seen["url"].endswith(f"/posts/{POST_ID}/history")
        assert out["revision_count"] == 1


def test_the_mock_records_the_call_and_returns_what_it_was_given() -> None:
    mock = MockColonyClient(responses={"get_post_history": HISTORY})
    assert mock.get_post_history(POST_ID) == HISTORY
    assert mock.calls[-1] == ("get_post_history", {"post_id": POST_ID})


class TestPostFields:
    def test_an_edited_post_carries_both(self) -> None:
        post = Post.from_dict(
            {"id": POST_ID, "title": "t", "body": "b", "edited_at": HISTORY["edited_at"], "revision_count": 1}
        )
        assert post.edited_at == HISTORY["edited_at"] and post.revision_count == 1
        assert post.to_dict()["edited_at"] == HISTORY["edited_at"]
        assert post.to_dict()["revision_count"] == 1

    def test_an_unedited_post_and_an_older_platform_read_as_never_edited(self) -> None:
        for payload in (
            {"id": POST_ID, "title": "t", "body": "b", "edited_at": None, "revision_count": 0},
            {"id": POST_ID, "title": "t", "body": "b"},
        ):
            post = Post.from_dict(payload)
            assert post.edited_at is None and post.revision_count == 0
            assert "edited_at" not in post.to_dict() and "revision_count" not in post.to_dict()
