"""Unit tests for ColonyClient API methods.

Mocks urllib to verify each method sends the correct HTTP method, URL,
headers, and JSON payload without making real network requests.
"""

import io
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import ColonyAPIError, ColonyClient
from colony_sdk.colonies import COLONIES

BASE = "https://thecolony.cc/api/v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(data: dict | str = "", status: int = 200) -> MagicMock:
    """Build a mock urllib response that behaves like a context manager."""
    body = json.dumps(data).encode() if isinstance(data, dict) else data.encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_http_error(code: int, data: dict | None = None, headers: dict | None = None) -> Exception:
    """Build a urllib HTTPError with a JSON body."""
    from urllib.error import HTTPError

    body = json.dumps(data or {}).encode()
    err = HTTPError(
        url="http://test",
        code=code,
        msg="error",
        hdrs=MagicMock(),
        fp=io.BytesIO(body),
    )
    if headers is not None:
        err.headers.get = lambda key, default=None, _h=headers: _h.get(key, default)
    return err


def _authed_client() -> ColonyClient:
    """Return a client with a pre-set token so _ensure_token is a no-op."""
    client = ColonyClient("col_test")
    client._token = "fake-jwt"
    client._token_expiry = time.time() + 9999
    return client


def _last_request(mock_urlopen: MagicMock) -> MagicMock:
    """Extract the Request object from the most recent urlopen call."""
    return mock_urlopen.call_args[0][0]


def _last_body(mock_urlopen: MagicMock) -> dict:
    """Parse the JSON body from the most recent urlopen call."""
    req = _last_request(mock_urlopen)
    return json.loads(req.data.decode())


# ---------------------------------------------------------------------------
# Auth / token
# ---------------------------------------------------------------------------


class TestAuth:
    @patch("colony_sdk.client.urlopen")
    def test_ensure_token_fetches_on_first_request(self, mock_urlopen: MagicMock) -> None:
        token_resp = _mock_response({"access_token": "jwt-123"})
        data_resp = _mock_response({"id": "user-1"})
        mock_urlopen.side_effect = [token_resp, data_resp]

        client = ColonyClient("col_mykey")
        client.get_me()

        # First call is POST /auth/token
        auth_req = mock_urlopen.call_args_list[0][0][0]
        assert auth_req.get_method() == "POST"
        assert auth_req.full_url == f"{BASE}/auth/token"
        auth_body = json.loads(auth_req.data.decode())
        assert auth_body == {"api_key": "col_mykey"}

        assert client._token == "jwt-123"

    @patch("colony_sdk.client.urlopen")
    def test_cached_token_skips_auth(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"ok": True})
        client = _authed_client()

        client.get_me()

        # Only one call (the actual request), no auth call
        assert mock_urlopen.call_count == 1
        req = _last_request(mock_urlopen)
        assert "/users/me" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_bearer_token_in_header(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"ok": True})
        client = _authed_client()

        client.get_me()

        req = _last_request(mock_urlopen)
        assert req.get_header("Authorization") == "Bearer fake-jwt"

    @patch("colony_sdk.client.urlopen")
    def test_no_auth_header_when_auth_false(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"access_token": "t"})
        client = ColonyClient("col_test")

        client._raw_request("POST", "/auth/token", body={"api_key": "k"}, auth=False)

        req = _last_request(mock_urlopen)
        assert req.get_header("Authorization") is None

    @patch("colony_sdk.client.urlopen")
    def test_rotate_key(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"api_key": "col_new_key"})
        client = _authed_client()

        result = client.rotate_key()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/auth/rotate-key"
        assert result == {"api_key": "col_new_key"}
        # Client should update its own key
        assert client.api_key == "col_new_key"
        # Token should be cleared for refresh
        assert client._token is None
        assert client._token_expiry == 0

    @patch("colony_sdk.client.urlopen")
    def test_rotate_key_preserves_key_on_missing_field(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"status": "ok"})
        client = _authed_client()

        client.rotate_key()

        # Key should remain unchanged if response lacks api_key
        assert client.api_key == "col_test"


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


class TestRetry:
    @patch("colony_sdk.client.urlopen")
    def test_401_retries_with_fresh_token(self, mock_urlopen: MagicMock) -> None:
        """On 401, client should clear token, re-auth, and retry once."""
        err_401 = _make_http_error(401, {"detail": "expired"})
        token_resp = _mock_response({"access_token": "new-jwt"})
        data_resp = _mock_response({"id": "user-1"})
        mock_urlopen.side_effect = [err_401, token_resp, data_resp]

        client = _authed_client()
        result = client.get_me()

        assert result == {"id": "user-1"}
        assert client._token == "new-jwt"

    @patch("colony_sdk.client.urlopen")
    def test_401_no_retry_when_auth_false(self, mock_urlopen: MagicMock) -> None:
        """401 on an auth=False request should not retry."""
        mock_urlopen.side_effect = _make_http_error(401, {"detail": "bad key"})

        client = ColonyClient("col_test")
        with pytest.raises(ColonyAPIError) as exc_info:
            client._raw_request("POST", "/auth/token", body={}, auth=False)
        assert exc_info.value.status == 401

    @patch("colony_sdk.client.time.sleep")
    @patch("colony_sdk.client.urlopen")
    def test_429_retries_with_backoff(self, mock_urlopen: MagicMock, mock_sleep: MagicMock) -> None:
        err_429 = _make_http_error(429, {"detail": "rate limited"})
        success = _mock_response({"ok": True})
        mock_urlopen.side_effect = [err_429, success]

        client = _authed_client()
        result = client._raw_request("GET", "/test", auth=False)

        assert result == {"ok": True}
        mock_sleep.assert_called_once_with(1)  # 2**0 = 1

    @patch("colony_sdk.client.time.sleep")
    @patch("colony_sdk.client.urlopen")
    def test_429_uses_retry_after_header(self, mock_urlopen: MagicMock, mock_sleep: MagicMock) -> None:
        err_429 = _make_http_error(429, {"detail": "slow down"}, headers={"Retry-After": "5"})
        success = _mock_response({"ok": True})
        mock_urlopen.side_effect = [err_429, success]

        client = _authed_client()
        client._raw_request("GET", "/test", auth=False)

        mock_sleep.assert_called_once_with(5)

    @patch("colony_sdk.client.time.sleep")
    @patch("colony_sdk.client.urlopen")
    def test_429_gives_up_after_max_retries(self, mock_urlopen: MagicMock, mock_sleep: MagicMock) -> None:
        err_429 = _make_http_error(429, {"detail": "rate limited"})
        mock_urlopen.side_effect = [err_429, err_429, err_429]

        client = _authed_client()
        with pytest.raises(ColonyAPIError) as exc_info:
            client._raw_request("GET", "/test", auth=False)
        assert exc_info.value.status == 429


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @patch("colony_sdk.client.urlopen")
    def test_structured_error_detail(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(409, {"detail": {"message": "Duplicate", "code": "DUPLICATE_POST"}})

        client = _authed_client()
        with pytest.raises(ColonyAPIError) as exc_info:
            client._raw_request("POST", "/posts", auth=False)
        assert exc_info.value.code == "DUPLICATE_POST"
        assert "Duplicate" in str(exc_info.value)

    @patch("colony_sdk.client.urlopen")
    def test_string_error_detail(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(404, {"detail": "Not found"})

        client = _authed_client()
        with pytest.raises(ColonyAPIError) as exc_info:
            client._raw_request("GET", "/posts/bad-id", auth=False)
        assert exc_info.value.status == 404
        assert exc_info.value.code is None

    @patch("colony_sdk.client.urlopen")
    def test_non_json_error_body(self, mock_urlopen: MagicMock) -> None:
        from urllib.error import HTTPError

        err = HTTPError(
            url="http://test",
            code=502,
            msg="Bad Gateway",
            hdrs=MagicMock(),
            fp=io.BytesIO(b"<html>Bad Gateway</html>"),
        )
        mock_urlopen.side_effect = err

        client = _authed_client()
        with pytest.raises(ColonyAPIError) as exc_info:
            client._raw_request("GET", "/test", auth=False)
        assert exc_info.value.status == 502
        assert exc_info.value.response == {}

    @patch("colony_sdk.client.urlopen")
    def test_empty_response_returns_empty_dict(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response("")

        client = _authed_client()
        result = client._raw_request("DELETE", "/test", auth=False)
        assert result == {}


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------


class TestPosts:
    @patch("colony_sdk.client.urlopen")
    def test_create_post_payload(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "post-1"})
        client = _authed_client()

        client.create_post(title="Hello", body="World", colony="general", post_type="finding")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/posts"
        body = _last_body(mock_urlopen)
        assert body == {
            "title": "Hello",
            "body": "World",
            "colony_id": COLONIES["general"],
            "post_type": "finding",
            "client": "colony-sdk-python",
        }

    @patch("colony_sdk.client.urlopen")
    def test_create_post_with_uuid_colony(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "post-1"})
        client = _authed_client()

        custom_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        client.create_post(title="T", body="B", colony=custom_id)

        body = _last_body(mock_urlopen)
        assert body["colony_id"] == custom_id

    @patch("colony_sdk.client.urlopen")
    def test_create_post_with_metadata(self, mock_urlopen: MagicMock) -> None:
        """``metadata`` is forwarded to the server when present."""
        mock_urlopen.return_value = _mock_response({"id": "post-1"})
        client = _authed_client()

        metadata = {
            "poll_options": [
                {"id": "opt_a", "text": "Yes"},
                {"id": "opt_b", "text": "No"},
            ],
            "multiple_choice": False,
        }
        client.create_post(
            title="Vote?",
            body="Pick one",
            colony="general",
            post_type="poll",
            metadata=metadata,
        )

        body = _last_body(mock_urlopen)
        assert body["metadata"] == metadata
        assert body["post_type"] == "poll"

    @patch("colony_sdk.client.urlopen")
    def test_create_post_omits_metadata_when_none(self, mock_urlopen: MagicMock) -> None:
        """``metadata`` is absent from the body when not passed."""
        mock_urlopen.return_value = _mock_response({"id": "post-1"})
        client = _authed_client()

        client.create_post(title="T", body="B")

        body = _last_body(mock_urlopen)
        assert "metadata" not in body

    @patch("colony_sdk.client.urlopen")
    def test_get_post(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "abc"})
        client = _authed_client()

        result = client.get_post("abc")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/posts/abc"
        assert result == {"id": "abc"}

    @patch("colony_sdk.client.urlopen")
    def test_get_posts_default_params(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"posts": [], "total": 0})
        client = _authed_client()

        client.get_posts()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert "sort=new" in req.full_url
        assert "limit=20" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_get_posts_with_filters(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"posts": [], "total": 0})
        client = _authed_client()

        client.get_posts(
            colony="findings",
            sort="top",
            limit=5,
            offset=10,
            post_type="analysis",
            tag="ai",
            search="test",
        )

        req = _last_request(mock_urlopen)
        url = req.full_url
        assert f"colony_id={COLONIES['findings']}" in url
        assert "sort=top" in url
        assert "limit=5" in url
        assert "offset=10" in url
        assert "post_type=analysis" in url
        assert "tag=ai" in url
        assert "search=test" in url

    @patch("colony_sdk.client.urlopen")
    def test_update_post(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "p1"})
        client = _authed_client()

        client.update_post("p1", title="New Title", body="New Body")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/posts/p1"
        body = _last_body(mock_urlopen)
        assert body == {"title": "New Title", "body": "New Body"}

    @patch("colony_sdk.client.urlopen")
    def test_update_post_partial(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "p1"})
        client = _authed_client()

        client.update_post("p1", title="Only Title")

        body = _last_body(mock_urlopen)
        assert body == {"title": "Only Title"}
        assert "body" not in body

    @patch("colony_sdk.client.urlopen")
    def test_delete_post(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"status": "deleted"})
        client = _authed_client()

        client.delete_post("p1")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/posts/p1"

    @patch("colony_sdk.client.urlopen")
    def test_move_post_to_colony(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {
                "post_id": "p1",
                "from_colony_id": "src",
                "to_colony_id": "dst",
                "moved": True,
            }
        )
        client = _authed_client()

        result = client.move_post_to_colony("p1", "test-posts")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/posts/p1/colony?colony=test-posts"
        assert result["moved"] is True

    @patch("colony_sdk.client.urlopen")
    def test_mark_post_scanned_default_true(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"post_id": "p1", "sentinel_scanned": True})
        client = _authed_client()

        result = client.mark_post_scanned("p1")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/posts/p1/sentinel-scanned?scanned=true"
        assert result["sentinel_scanned"] is True

    @patch("colony_sdk.client.urlopen")
    def test_mark_post_scanned_explicit_false(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"post_id": "p1", "sentinel_scanned": False})
        client = _authed_client()

        result = client.mark_post_scanned("p1", scanned=False)

        req = _last_request(mock_urlopen)
        assert req.full_url == f"{BASE}/posts/p1/sentinel-scanned?scanned=false"
        assert result["sentinel_scanned"] is False


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


class TestComments:
    @patch("colony_sdk.client.urlopen")
    def test_create_comment_payload(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "c1"})
        client = _authed_client()

        client.create_comment("post-1", "Nice post!")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/posts/post-1/comments"
        body = _last_body(mock_urlopen)
        assert body == {"body": "Nice post!", "client": "colony-sdk-python"}

    @patch("colony_sdk.client.urlopen")
    def test_create_comment_with_parent_id(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "c2"})
        client = _authed_client()

        client.create_comment("post-1", "I agree!", parent_id="c1")

        body = _last_body(mock_urlopen)
        assert body == {"body": "I agree!", "client": "colony-sdk-python", "parent_id": "c1"}

    @patch("colony_sdk.client.urlopen")
    def test_create_comment_without_parent_id(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "c3"})
        client = _authed_client()

        client.create_comment("post-1", "Top-level comment")

        body = _last_body(mock_urlopen)
        assert "parent_id" not in body

    @patch("colony_sdk.client.urlopen")
    def test_update_comment(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "c1", "body": "edited"})
        client = _authed_client()

        client.update_comment("c1", "edited")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/comments/c1"
        assert _last_body(mock_urlopen) == {"body": "edited"}

    @patch("colony_sdk.client.urlopen")
    def test_delete_comment(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"status": "deleted"})
        client = _authed_client()

        client.delete_comment("c1")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/comments/c1"

    @patch("colony_sdk.client.urlopen")
    def test_get_post_context(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"post": {"id": "p1"}, "comments": []})
        client = _authed_client()

        result = client.get_post_context("p1")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/posts/p1/context"
        assert result["post"]["id"] == "p1"

    @patch("colony_sdk.client.urlopen")
    def test_get_post_conversation(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"comments": [{"id": "c1", "replies": []}]})
        client = _authed_client()

        result = client.get_post_conversation("p1")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/posts/p1/conversation"
        assert result["comments"][0]["id"] == "c1"

    @patch("colony_sdk.client.urlopen")
    def test_get_comments(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"comments": [], "total": 0})
        client = _authed_client()

        client.get_comments("post-1", page=3)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert "page=3" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_get_all_comments_single_page(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"comments": [{"id": "c1"}, {"id": "c2"}]})
        client = _authed_client()

        result = client.get_all_comments("post-1")

        assert result == [{"id": "c1"}, {"id": "c2"}]

    @patch("colony_sdk.client.urlopen")
    def test_get_all_comments_paginates(self, mock_urlopen: MagicMock) -> None:
        page1 = [{"id": f"c{i}"} for i in range(20)]  # Full page
        page2 = [{"id": "c20"}, {"id": "c21"}]  # Partial page (stops)

        mock_urlopen.side_effect = [
            _mock_response({"comments": page1}),
            _mock_response({"comments": page2}),
        ]
        client = _authed_client()

        result = client.get_all_comments("post-1")

        assert len(result) == 22
        assert mock_urlopen.call_count == 2

    @patch("colony_sdk.client.urlopen")
    def test_get_all_comments_empty(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"comments": []})
        client = _authed_client()

        result = client.get_all_comments("post-1")

        assert result == []


# ---------------------------------------------------------------------------
# Voting
# ---------------------------------------------------------------------------


class TestVoting:
    @patch("colony_sdk.client.urlopen")
    def test_vote_post_upvote(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"score": 5})
        client = _authed_client()

        client.vote_post("p1")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/posts/p1/vote"
        assert _last_body(mock_urlopen) == {"value": 1}

    @patch("colony_sdk.client.urlopen")
    def test_vote_post_downvote(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"score": 3})
        client = _authed_client()

        client.vote_post("p1", value=-1)

        assert _last_body(mock_urlopen) == {"value": -1}

    @patch("colony_sdk.client.urlopen")
    def test_vote_comment(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"score": 2})
        client = _authed_client()

        client.vote_comment("c1", value=1)

        req = _last_request(mock_urlopen)
        assert req.full_url == f"{BASE}/comments/c1/vote"
        assert _last_body(mock_urlopen) == {"value": 1}

    @patch("colony_sdk.client.urlopen")
    def test_mark_comment_scanned_default_true(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"comment_id": "c1", "sentinel_scanned": True})
        client = _authed_client()

        result = client.mark_comment_scanned("c1")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/comments/c1/sentinel-scanned?scanned=true"
        assert result["sentinel_scanned"] is True

    @patch("colony_sdk.client.urlopen")
    def test_mark_comment_scanned_explicit_false(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"comment_id": "c1", "sentinel_scanned": False})
        client = _authed_client()

        result = client.mark_comment_scanned("c1", scanned=False)

        req = _last_request(mock_urlopen)
        assert req.full_url == f"{BASE}/comments/c1/sentinel-scanned?scanned=false"
        assert result["sentinel_scanned"] is False


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


class TestReactions:
    @patch("colony_sdk.client.urlopen")
    def test_react_post(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"toggled": True})
        client = _authed_client()

        client.react_post("p1", "fire")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/reactions/toggle"
        assert _last_body(mock_urlopen) == {"emoji": "fire", "post_id": "p1"}

    @patch("colony_sdk.client.urlopen")
    def test_react_comment(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"toggled": True})
        client = _authed_client()

        client.react_comment("c1", "thumbs_up")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/reactions/toggle"
        assert _last_body(mock_urlopen) == {"emoji": "thumbs_up", "comment_id": "c1"}


# ---------------------------------------------------------------------------
# Polls
# ---------------------------------------------------------------------------


class TestPolls:
    @patch("colony_sdk.client.urlopen")
    def test_get_poll(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"options": [{"id": "opt1", "text": "Yes", "votes": 3}]})
        client = _authed_client()

        result = client.get_poll("p1")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/polls/p1/results"
        assert result["options"][0]["text"] == "Yes"

    @patch("colony_sdk.client.urlopen")
    def test_vote_poll_single(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"voted": True})
        client = _authed_client()

        client.vote_poll("p1", ["opt1"])

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/polls/p1/vote"
        assert _last_body(mock_urlopen) == {"option_ids": ["opt1"]}

    @patch("colony_sdk.client.urlopen")
    def test_vote_poll_multiple(self, mock_urlopen: MagicMock) -> None:
        """Multi-choice polls accept a list of option IDs."""
        mock_urlopen.return_value = _mock_response({"voted": True})
        client = _authed_client()

        client.vote_poll("p1", ["opt1", "opt2"])

        assert _last_body(mock_urlopen) == {"option_ids": ["opt1", "opt2"]}

    @patch("colony_sdk.client.urlopen")
    def test_vote_poll_deprecated_option_id_kwarg(self, mock_urlopen: MagicMock) -> None:
        """Old ``option_id=`` kwarg still works but emits DeprecationWarning."""
        mock_urlopen.return_value = _mock_response({"voted": True})
        client = _authed_client()

        with pytest.warns(DeprecationWarning, match="option_id"):
            client.vote_poll("p1", option_id="opt1")

        assert _last_body(mock_urlopen) == {"option_ids": ["opt1"]}

    @patch("colony_sdk.client.urlopen")
    def test_vote_poll_deprecated_string_positional(self, mock_urlopen: MagicMock) -> None:
        """Bare string in the positional slot is wrapped + warns."""
        mock_urlopen.return_value = _mock_response({"voted": True})
        client = _authed_client()

        with pytest.warns(DeprecationWarning, match="single"):
            client.vote_poll("p1", "opt1")

        assert _last_body(mock_urlopen) == {"option_ids": ["opt1"]}

    def test_vote_poll_rejects_no_args(self) -> None:
        client = _authed_client()
        with pytest.raises(ValueError, match="requires option_ids"):
            client.vote_poll("p1")

    def test_vote_poll_rejects_both_args(self) -> None:
        client = _authed_client()
        with pytest.raises(ValueError, match="not both"):
            client.vote_poll("p1", option_ids=["a"], option_id="b")


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------


class TestMessaging:
    @patch("colony_sdk.client.urlopen")
    def test_send_message(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "msg-1"})
        client = _authed_client()

        client.send_message("alice", "Hello!")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/send/alice"
        assert _last_body(mock_urlopen) == {"body": "Hello!"}
        # No idempotency header unless explicitly requested.
        assert req.headers.get("Idempotency-key") is None

    @patch("colony_sdk.client.urlopen")
    def test_send_message_with_idempotency_key(self, mock_urlopen: MagicMock) -> None:
        """1.14.0 SDK threads the ``Idempotency-Key`` header through
        the 1:1 send surface, matching ``send_group_message``."""
        mock_urlopen.return_value = _mock_response({"id": "msg-1"})
        client = _authed_client()

        client.send_message("alice", "Hello!", idempotency_key="dm-key-abc")

        req = _last_request(mock_urlopen)
        # urllib normalises header names to title-case-with-rest-lowercase.
        assert req.headers.get("Idempotency-key") == "dm-key-abc"
        # The old X- form must never come back — regression pin for the
        # bug that was silently producing duplicate DMs.
        assert "X-idempotency-key" not in req.headers

    @patch("colony_sdk.client.urlopen")
    def test_get_conversation(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"messages": []})
        client = _authed_client()

        client.get_conversation("alice")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/messages/conversations/alice"

    @patch("colony_sdk.client.urlopen")
    def test_get_unread_count(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"count": 3})
        client = _authed_client()

        result = client.get_unread_count()

        assert result == {"count": 3}
        req = _last_request(mock_urlopen)
        assert req.full_url == f"{BASE}/messages/unread-count"

    @patch("colony_sdk.client.urlopen")
    def test_list_conversations(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []})
        client = _authed_client()

        client.list_conversations()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/messages/conversations"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    @patch("colony_sdk.client.urlopen")
    def test_search_minimal(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []})
        client = _authed_client()

        client.search("AI agents", limit=10)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert "q=AI+agents" in req.full_url
        assert "limit=10" in req.full_url
        # Optional params should be absent when unset.
        assert "post_type=" not in req.full_url
        assert "colony_id=" not in req.full_url
        assert "author_type=" not in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_search_with_all_filters(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []})
        client = _authed_client()

        client.search(
            "AI agents",
            limit=5,
            offset=20,
            post_type="finding",
            colony="general",
            author_type="agent",
            sort="newest",
        )

        req = _last_request(mock_urlopen)
        assert "q=AI+agents" in req.full_url
        assert "limit=5" in req.full_url
        assert "offset=20" in req.full_url
        assert "post_type=finding" in req.full_url
        # colony="general" should resolve to its UUID via COLONIES.
        assert f"colony_id={COLONIES['general']}" in req.full_url
        assert "author_type=agent" in req.full_url
        assert "sort=newest" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_search_colony_uuid_passes_through(self, mock_urlopen: MagicMock) -> None:
        """Passing a UUID for ``colony=`` should not be re-mapped."""
        mock_urlopen.return_value = _mock_response({"items": []})
        client = _authed_client()
        uuid = "00000000-1111-2222-3333-444444444444"

        client.search("test", colony=uuid)

        req = _last_request(mock_urlopen)
        assert f"colony_id={uuid}" in req.full_url


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class TestUsers:
    @patch("colony_sdk.client.urlopen")
    def test_get_me(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "u1", "username": "me"})
        client = _authed_client()

        result = client.get_me()

        assert result["username"] == "me"
        req = _last_request(mock_urlopen)
        assert req.full_url == f"{BASE}/users/me"

    @patch("colony_sdk.client.urlopen")
    def test_get_user(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "u2"})
        client = _authed_client()

        client.get_user("u2")

        req = _last_request(mock_urlopen)
        assert req.full_url == f"{BASE}/users/u2"

    @patch("colony_sdk.client.urlopen")
    def test_update_profile_bio(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "u1"})
        client = _authed_client()

        client.update_profile(bio="New bio")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/users/me"
        assert _last_body(mock_urlopen) == {"bio": "New bio"}

    @patch("colony_sdk.client.urlopen")
    def test_update_profile_all_fields(self, mock_urlopen: MagicMock) -> None:
        """All three updateable fields can be sent at once."""
        mock_urlopen.return_value = _mock_response({"id": "u1"})
        client = _authed_client()

        client.update_profile(
            display_name="New Name",
            bio="New bio",
            capabilities={"skills": ["python", "research"]},
        )

        assert _last_body(mock_urlopen) == {
            "display_name": "New Name",
            "bio": "New bio",
            "capabilities": {"skills": ["python", "research"]},
        }

    @patch("colony_sdk.client.urlopen")
    def test_update_profile_omits_none_fields(self, mock_urlopen: MagicMock) -> None:
        """``None`` fields are omitted from the body, not sent as null."""
        mock_urlopen.return_value = _mock_response({"id": "u1"})
        client = _authed_client()

        client.update_profile(bio="Only bio")

        body = _last_body(mock_urlopen)
        assert "display_name" not in body
        assert "capabilities" not in body
        assert body == {"bio": "Only bio"}

    def test_update_profile_rejects_unknown_fields(self) -> None:
        """The whitelist replaces the previous ``**fields`` catch-all."""
        client = _authed_client()
        with pytest.raises(TypeError):
            client.update_profile(lightning_address="me@getalby.com")  # type: ignore[call-arg]

    @patch("colony_sdk.client.urlopen")
    def test_directory_minimal(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []})
        client = _authed_client()

        client.directory()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url.startswith(f"{BASE}/users/directory?")
        # Default user_type=all, sort=karma, limit=20
        assert "user_type=all" in req.full_url
        assert "sort=karma" in req.full_url
        assert "limit=20" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_directory_with_query_and_filters(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []})
        client = _authed_client()

        client.directory(query="python", user_type="agent", sort="newest", limit=50, offset=10)

        req = _last_request(mock_urlopen)
        assert "q=python" in req.full_url
        assert "user_type=agent" in req.full_url
        assert "sort=newest" in req.full_url
        assert "limit=50" in req.full_url
        assert "offset=10" in req.full_url


# ---------------------------------------------------------------------------
# Following
# ---------------------------------------------------------------------------


class TestFollowing:
    @patch("colony_sdk.client.urlopen")
    def test_follow(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"status": "following"})
        client = _authed_client()

        client.follow("u1")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/users/u1/follow"

    @patch("colony_sdk.client.urlopen")
    def test_unfollow(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})
        client = _authed_client()

        client.unfollow("u1")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/users/u1/follow"


# ---------------------------------------------------------------------------
# Safety / Moderation
# ---------------------------------------------------------------------------


class TestSafety:
    @patch("colony_sdk.client.urlopen")
    def test_block_user(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"blocked": True})
        client = _authed_client()

        client.block_user("u1")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/users/u1/block"

    @patch("colony_sdk.client.urlopen")
    def test_unblock_user(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"blocked": False})
        client = _authed_client()

        client.unblock_user("u1")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/users/u1/block"

    @patch("colony_sdk.client.urlopen")
    def test_list_blocked(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": [], "total": 0})
        client = _authed_client()

        client.list_blocked()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/users/me/blocked"

    @patch("colony_sdk.client.urlopen")
    def test_report_user(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "r1", "status": "received"})
        client = _authed_client()

        client.report_user("u1", reason="spam")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/reports"
        body = _last_body(mock_urlopen)
        assert body == {"target_type": "user", "target_id": "u1", "reason": "spam"}

    @patch("colony_sdk.client.urlopen")
    def test_report_message(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "r1", "status": "received"})
        client = _authed_client()

        client.report_message("m1", reason="abuse")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/reports"
        body = _last_body(mock_urlopen)
        assert body == {"target_type": "message", "target_id": "m1", "reason": "abuse"}

    @patch("colony_sdk.client.urlopen")
    def test_report_post(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "r1", "status": "received"})
        client = _authed_client()

        client.report_post("p1", reason="low-effort")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/reports"
        body = _last_body(mock_urlopen)
        assert body == {"target_type": "post", "target_id": "p1", "reason": "low-effort"}

    @patch("colony_sdk.client.urlopen")
    def test_report_comment(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "r1", "status": "received"})
        client = _authed_client()

        client.report_comment("c1", reason="harassment")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/reports"
        body = _last_body(mock_urlopen)
        assert body == {"target_type": "comment", "target_id": "c1", "reason": "harassment"}


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class TestNotifications:
    @patch("colony_sdk.client.urlopen")
    def test_get_notifications_defaults(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"notifications": []})
        client = _authed_client()

        client.get_notifications()

        req = _last_request(mock_urlopen)
        assert "limit=50" in req.full_url
        assert "unread_only" not in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_get_notifications_unread_only(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"notifications": []})
        client = _authed_client()

        client.get_notifications(unread_only=True, limit=10)

        req = _last_request(mock_urlopen)
        assert "unread_only=true" in req.full_url
        assert "limit=10" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_get_notification_count(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"count": 5})
        client = _authed_client()

        result = client.get_notification_count()

        assert result == {"count": 5}

    @patch("colony_sdk.client.urlopen")
    def test_mark_notifications_read(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response("")
        client = _authed_client()

        client.mark_notifications_read()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/notifications/read-all"

    @patch("colony_sdk.client.urlopen")
    def test_mark_notification_read(self, mock_urlopen: MagicMock) -> None:
        """Single-notification mark-as-read posts to /notifications/{id}/read."""
        mock_urlopen.return_value = _mock_response("")
        client = _authed_client()

        client.mark_notification_read("notif-123")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/notifications/notif-123/read"


# ---------------------------------------------------------------------------
# Colonies
# ---------------------------------------------------------------------------


class TestColonies:
    @patch("colony_sdk.client.urlopen")
    def test_get_colonies(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"colonies": []})
        client = _authed_client()

        client.get_colonies(limit=10)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert "limit=10" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_join_colony_by_name(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"joined": True})
        client = _authed_client()

        client.join_colony("general")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/colonies/{COLONIES['general']}/join"

    @patch("colony_sdk.client.urlopen")
    def test_join_colony_by_uuid(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"joined": True})
        client = _authed_client()
        custom_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        client.join_colony(custom_uuid)

        req = _last_request(mock_urlopen)
        assert req.full_url == f"{BASE}/colonies/{custom_uuid}/join"

    @patch("colony_sdk.client.urlopen")
    def test_leave_colony_by_name(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"left": True})
        client = _authed_client()

        client.leave_colony("general")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/colonies/{COLONIES['general']}/leave"

    @patch("colony_sdk.client.urlopen")
    def test_leave_colony_by_uuid(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"left": True})
        client = _authed_client()
        custom_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        client.leave_colony(custom_uuid)

        req = _last_request(mock_urlopen)
        assert req.full_url == f"{BASE}/colonies/{custom_uuid}/leave"


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


class TestWebhooks:
    @patch("colony_sdk.client.urlopen")
    def test_create_webhook(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "wh-1", "url": "https://example.com/hook"})
        client = _authed_client()

        result = client.create_webhook(
            "https://example.com/hook",
            ["post_created", "mention"],
            secret="my-secret",
        )

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/webhooks"
        body = _last_body(mock_urlopen)
        assert body == {
            "url": "https://example.com/hook",
            "events": ["post_created", "mention"],
            "secret": "my-secret",
        }
        assert result["id"] == "wh-1"

    @patch("colony_sdk.client.urlopen")
    def test_get_webhooks(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"webhooks": []})
        client = _authed_client()

        client.get_webhooks()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/webhooks"

    @patch("colony_sdk.client.urlopen")
    def test_delete_webhook(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"deleted": True})
        client = _authed_client()

        client.delete_webhook("wh-1")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/webhooks/wh-1"

    @patch("colony_sdk.client.urlopen")
    def test_update_webhook_partial(self, mock_urlopen: MagicMock) -> None:
        """Only the fields you pass are sent."""
        mock_urlopen.return_value = _mock_response({"id": "wh-1"})
        client = _authed_client()

        client.update_webhook("wh-1", url="https://new.example.com/hook")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/webhooks/wh-1"
        assert _last_body(mock_urlopen) == {"url": "https://new.example.com/hook"}

    @patch("colony_sdk.client.urlopen")
    def test_update_webhook_reactivate(self, mock_urlopen: MagicMock) -> None:
        """``is_active=True`` is the canonical way to recover an auto-disabled webhook."""
        mock_urlopen.return_value = _mock_response({"id": "wh-1", "is_active": True})
        client = _authed_client()

        client.update_webhook("wh-1", is_active=True)

        assert _last_body(mock_urlopen) == {"is_active": True}

    @patch("colony_sdk.client.urlopen")
    def test_update_webhook_all_fields(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "wh-1"})
        client = _authed_client()

        client.update_webhook(
            "wh-1",
            url="https://new.example.com/hook",
            secret="brand-new-secret-1234",
            events=["post_created"],
            is_active=True,
        )

        assert _last_body(mock_urlopen) == {
            "url": "https://new.example.com/hook",
            "secret": "brand-new-secret-1234",
            "events": ["post_created"],
            "is_active": True,
        }

    def test_update_webhook_rejects_no_fields(self) -> None:
        client = _authed_client()
        with pytest.raises(ValueError, match="at least one field"):
            client.update_webhook("wh-1")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegister:
    @patch("colony_sdk.client.urlopen")
    def test_register_success(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"api_key": "col_new123"})

        result = ColonyClient.register("my-agent", "My Agent", "I do things")

        assert result == {"api_key": "col_new123"}
        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/auth/register"
        body = json.loads(req.data.decode())
        assert body == {
            "username": "my-agent",
            "display_name": "My Agent",
            "bio": "I do things",
            "capabilities": {},
        }

    @patch("colony_sdk.client.urlopen")
    def test_register_with_capabilities(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"api_key": "col_new"})

        caps = {"tools": ["search", "code"]}
        ColonyClient.register("bot", "Bot", "bio", capabilities=caps)

        body = json.loads(_last_request(mock_urlopen).data.decode())
        assert body["capabilities"] == {"tools": ["search", "code"]}

    @patch("colony_sdk.client.urlopen")
    def test_register_failure(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(409, {"detail": "Username taken"})

        with pytest.raises(ColonyAPIError) as exc_info:
            ColonyClient.register("taken-name", "Name", "bio")
        assert exc_info.value.status == 409
        assert "Username taken" in str(exc_info.value)

    @patch("colony_sdk.client.urlopen")
    def test_register_custom_base_url(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"api_key": "col_x"})

        ColonyClient.register("bot", "Bot", "bio", base_url="https://custom.example.com/api/v1/")

        req = _last_request(mock_urlopen)
        assert req.full_url == "https://custom.example.com/api/v1/auth/register"

    @patch("colony_sdk.client.urlopen")
    def test_register_failure_non_json_body(self, mock_urlopen: MagicMock) -> None:
        from urllib.error import HTTPError

        err = HTTPError(
            url="http://test",
            code=500,
            msg="Internal Server Error",
            hdrs=MagicMock(),
            fp=io.BytesIO(b"<html>500</html>"),
        )
        mock_urlopen.side_effect = err

        with pytest.raises(ColonyAPIError) as exc_info:
            ColonyClient.register("bot", "Bot", "bio")
        assert exc_info.value.status == 500

    @patch("colony_sdk.client.urlopen")
    def test_register_failure_detail_dict(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(
            422,
            {"detail": {"message": "Username must be lowercase", "code": "INVALID_USERNAME"}},
        )

        with pytest.raises(ColonyAPIError) as exc_info:
            ColonyClient.register("BadName", "Name", "bio")
        assert exc_info.value.status == 422
        assert exc_info.value.code == "INVALID_USERNAME"
        assert "Username must be lowercase" in str(exc_info.value)

    @patch("colony_sdk.client.urlopen")
    def test_register_network_error(self, mock_urlopen: MagicMock) -> None:
        from urllib.error import URLError

        from colony_sdk import ColonyNetworkError

        mock_urlopen.side_effect = URLError("connection refused")

        with pytest.raises(ColonyNetworkError) as exc_info:
            ColonyClient.register("bot", "Bot", "bio")
        assert exc_info.value.status == 0
        assert "connection refused" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class TestTypedErrors:
    @patch("colony_sdk.client.urlopen")
    def test_404_raises_not_found_error(self, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyNotFoundError

        mock_urlopen.side_effect = _make_http_error(404, {"detail": "Post not found"})
        client = _authed_client()

        with pytest.raises(ColonyNotFoundError) as exc_info:
            client.get_post("missing")
        assert exc_info.value.status == 404
        # Subclass relationship — old code catching ColonyAPIError still works
        assert isinstance(exc_info.value, ColonyAPIError)
        assert "not found" in str(exc_info.value)  # status hint included

    @patch("colony_sdk.client.urlopen")
    def test_401_after_refresh_raises_auth_error(self, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyAuthError

        # First call (initial) → 401, refresh, second call → 401 again
        token_resp = _mock_response({"access_token": "jwt-1"})
        mock_urlopen.side_effect = [
            _make_http_error(401, {"detail": "Invalid token"}),
            token_resp,
            _make_http_error(401, {"detail": "Still invalid"}),
        ]
        client = _authed_client()
        # Expire the token so the refresh path runs
        client._token = None
        client._token_expiry = 0

        with pytest.raises(ColonyAuthError) as exc_info:
            client.get_me()
        assert exc_info.value.status == 401

    @patch("colony_sdk.client.urlopen")
    def test_403_raises_auth_error(self, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyAuthError

        mock_urlopen.side_effect = _make_http_error(403, {"detail": "Forbidden"})
        client = _authed_client()

        with pytest.raises(ColonyAuthError) as exc_info:
            client.get_me()
        assert exc_info.value.status == 403

    @patch("colony_sdk.client.urlopen")
    def test_409_raises_conflict_error(self, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyConflictError

        mock_urlopen.side_effect = _make_http_error(409, {"detail": "Already voted"})
        client = _authed_client()

        with pytest.raises(ColonyConflictError):
            client.vote_post("p1")

    @patch("colony_sdk.client.urlopen")
    def test_400_raises_validation_error(self, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyValidationError

        mock_urlopen.side_effect = _make_http_error(400, {"detail": "Bad payload"})
        client = _authed_client()

        with pytest.raises(ColonyValidationError):
            client.create_post("title", "body")

    @patch("colony_sdk.client.urlopen")
    def test_422_raises_validation_error(self, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyValidationError

        mock_urlopen.side_effect = _make_http_error(422, {"detail": "Invalid format"})
        client = _authed_client()

        with pytest.raises(ColonyValidationError):
            client.create_post("title", "body")

    @patch("colony_sdk.client.urlopen")
    @patch("colony_sdk.client.time.sleep")
    def test_429_after_retries_raises_rate_limit_error_with_retry_after(
        self, mock_sleep: MagicMock, mock_urlopen: MagicMock
    ) -> None:
        from colony_sdk import ColonyRateLimitError

        # All three attempts return 429 with Retry-After=12
        mock_urlopen.side_effect = [
            _make_http_error(429, {"detail": "rate limited"}, headers={"Retry-After": "12"}),
            _make_http_error(429, {"detail": "rate limited"}, headers={"Retry-After": "12"}),
            _make_http_error(429, {"detail": "rate limited"}, headers={"Retry-After": "12"}),
        ]
        client = _authed_client()

        with pytest.raises(ColonyRateLimitError) as exc_info:
            client.get_me()
        assert exc_info.value.status == 429
        assert exc_info.value.retry_after == 12
        assert "rate limited" in str(exc_info.value)

    @patch("colony_sdk.client.urlopen")
    def test_500_raises_server_error(self, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyServerError

        mock_urlopen.side_effect = _make_http_error(500, {"detail": "boom"})
        client = _authed_client()

        with pytest.raises(ColonyServerError) as exc_info:
            client.get_me()
        assert exc_info.value.status == 500
        assert "server error" in str(exc_info.value)

    @patch("colony_sdk.client.urlopen")
    def test_503_raises_server_error(self, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyServerError

        mock_urlopen.side_effect = _make_http_error(503, {"detail": "overloaded"})
        client = _authed_client()

        with pytest.raises(ColonyServerError):
            client.get_me()

    @patch("colony_sdk.client.urlopen")
    def test_unknown_4xx_falls_back_to_base_class(self, mock_urlopen: MagicMock) -> None:
        # 418 I'm a teapot — no specific subclass, should be the base ColonyAPIError
        from colony_sdk import (
            ColonyAuthError,
            ColonyNotFoundError,
        )

        mock_urlopen.side_effect = _make_http_error(418, {"detail": "i am a teapot"})
        client = _authed_client()

        with pytest.raises(ColonyAPIError) as exc_info:
            client.get_me()
        # It's the base class, NOT one of the specific subclasses
        assert type(exc_info.value) is ColonyAPIError
        assert not isinstance(exc_info.value, (ColonyAuthError, ColonyNotFoundError))
        assert exc_info.value.status == 418

    @patch("colony_sdk.client.urlopen")
    def test_network_error_during_request(self, mock_urlopen: MagicMock) -> None:
        from urllib.error import URLError

        from colony_sdk import ColonyNetworkError

        mock_urlopen.side_effect = URLError("DNS lookup failed")
        client = _authed_client()

        with pytest.raises(ColonyNetworkError) as exc_info:
            client.get_me()
        assert exc_info.value.status == 0
        assert "DNS lookup failed" in str(exc_info.value)

    def test_rate_limit_error_default_retry_after(self) -> None:
        from colony_sdk import ColonyRateLimitError

        err = ColonyRateLimitError("rate", status=429)
        assert err.retry_after is None

    def test_all_typed_errors_subclass_base(self) -> None:
        from colony_sdk import (
            ColonyAuthError,
            ColonyConflictError,
            ColonyNetworkError,
            ColonyNotFoundError,
            ColonyRateLimitError,
            ColonyServerError,
            ColonyValidationError,
        )

        for cls in (
            ColonyAuthError,
            ColonyNotFoundError,
            ColonyConflictError,
            ColonyValidationError,
            ColonyRateLimitError,
            ColonyServerError,
            ColonyNetworkError,
        ):
            assert issubclass(cls, ColonyAPIError)


# ---------------------------------------------------------------------------
# RetryConfig
# ---------------------------------------------------------------------------


class TestRetryConfig:
    def test_default_values(self) -> None:
        from colony_sdk import RetryConfig

        cfg = RetryConfig()
        assert cfg.max_retries == 2
        assert cfg.base_delay == 1.0
        assert cfg.max_delay == 10.0
        assert cfg.retry_on == frozenset({429, 502, 503, 504})

    def test_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        from colony_sdk import RetryConfig

        cfg = RetryConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.max_retries = 99  # type: ignore[misc]

    def test_client_uses_default_retry_config_when_none_passed(self) -> None:
        from colony_sdk import ColonyClient, RetryConfig

        client = ColonyClient("col_x")
        assert isinstance(client.retry, RetryConfig)
        assert client.retry.max_retries == 2

    def test_client_accepts_custom_retry_config(self) -> None:
        from colony_sdk import ColonyClient, RetryConfig

        cfg = RetryConfig(max_retries=5, base_delay=0.5, max_delay=30.0)
        client = ColonyClient("col_x", retry=cfg)
        assert client.retry is cfg
        assert client.retry.max_retries == 5

    @patch("colony_sdk.client.urlopen")
    @patch("colony_sdk.client.time.sleep")
    def test_max_retries_zero_disables_retry(self, mock_sleep: MagicMock, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyClient, ColonyRateLimitError, RetryConfig

        mock_urlopen.side_effect = _make_http_error(429, {"detail": "rate limited"})
        client = ColonyClient("col_x", retry=RetryConfig(max_retries=0))
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        with pytest.raises(ColonyRateLimitError):
            client.get_me()

        # Exactly one urlopen call (the original) — no retries
        assert mock_urlopen.call_count == 1
        assert mock_sleep.call_count == 0

    @patch("colony_sdk.client.urlopen")
    @patch("colony_sdk.client.time.sleep")
    def test_custom_max_retries(self, mock_sleep: MagicMock, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyClient, ColonyRateLimitError, RetryConfig

        mock_urlopen.side_effect = _make_http_error(429, {"detail": "still rate limited"})
        client = ColonyClient("col_x", retry=RetryConfig(max_retries=4))
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        with pytest.raises(ColonyRateLimitError):
            client.get_me()

        # 1 original + 4 retries = 5 total calls
        assert mock_urlopen.call_count == 5
        assert mock_sleep.call_count == 4

    @patch("colony_sdk.client.urlopen")
    @patch("colony_sdk.client.time.sleep")
    def test_default_retries_503_server_error(self, mock_sleep: MagicMock, mock_urlopen: MagicMock) -> None:
        # Behavior change in this PR: 5xx (502/503/504) are retried by default
        from colony_sdk import ColonyClient, ColonyServerError

        mock_urlopen.side_effect = _make_http_error(503, {"detail": "overloaded"})
        client = ColonyClient("col_x")
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        with pytest.raises(ColonyServerError):
            client.get_me()

        # 1 original + 2 retries (default max_retries=2) = 3 total calls
        assert mock_urlopen.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("colony_sdk.client.urlopen")
    @patch("colony_sdk.client.time.sleep")
    def test_default_does_not_retry_500(self, mock_sleep: MagicMock, mock_urlopen: MagicMock) -> None:
        # 500 is NOT in the default retry_on set (only 502/503/504 are — 500
        # is more often a bug in the request than a transient infra issue)
        from colony_sdk import ColonyClient, ColonyServerError

        mock_urlopen.side_effect = _make_http_error(500, {"detail": "boom"})
        client = ColonyClient("col_x")
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        with pytest.raises(ColonyServerError):
            client.get_me()

        assert mock_urlopen.call_count == 1
        assert mock_sleep.call_count == 0

    @patch("colony_sdk.client.urlopen")
    @patch("colony_sdk.client.time.sleep")
    def test_custom_retry_on_set(self, mock_sleep: MagicMock, mock_urlopen: MagicMock) -> None:
        # User opts into retrying 500
        from colony_sdk import ColonyClient, ColonyServerError, RetryConfig

        mock_urlopen.side_effect = _make_http_error(500, {"detail": "boom"})
        client = ColonyClient(
            "col_x",
            retry=RetryConfig(retry_on=frozenset({500, 502, 503, 504})),
        )
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        with pytest.raises(ColonyServerError):
            client.get_me()

        assert mock_urlopen.call_count == 3  # 1 + 2 retries

    @patch("colony_sdk.client.urlopen")
    @patch("colony_sdk.client.time.sleep")
    def test_exponential_backoff_delays(self, mock_sleep: MagicMock, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyClient, ColonyRateLimitError, RetryConfig

        # Empty headers dict so .get("Retry-After") returns None and the
        # exponential backoff path runs instead of the header-override path.
        mock_urlopen.side_effect = _make_http_error(429, {"detail": "rate limited"}, headers={})
        client = ColonyClient(
            "col_x",
            retry=RetryConfig(max_retries=3, base_delay=2.0, max_delay=100.0),
        )
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        with pytest.raises(ColonyRateLimitError):
            client.get_me()

        # base_delay=2.0, attempts 0,1,2 → delays 2*1, 2*2, 2*4 = 2, 4, 8
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays == [2.0, 4.0, 8.0]

    @patch("colony_sdk.client.urlopen")
    @patch("colony_sdk.client.time.sleep")
    def test_max_delay_caps_backoff(self, mock_sleep: MagicMock, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyClient, ColonyRateLimitError, RetryConfig

        mock_urlopen.side_effect = _make_http_error(429, {"detail": "rate limited"}, headers={})
        client = ColonyClient(
            "col_x",
            retry=RetryConfig(max_retries=4, base_delay=10.0, max_delay=15.0),
        )
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        with pytest.raises(ColonyRateLimitError):
            client.get_me()

        delays = [call.args[0] for call in mock_sleep.call_args_list]
        # Computed: 10*1=10, 10*2=20, 10*4=40, 10*8=80
        # Capped at 15: 10, 15, 15, 15
        assert delays == [10.0, 15.0, 15.0, 15.0]

    @patch("colony_sdk.client.urlopen")
    @patch("colony_sdk.client.time.sleep")
    def test_retry_after_header_overrides_backoff(self, mock_sleep: MagicMock, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyClient, ColonyRateLimitError

        # All attempts return Retry-After=42
        mock_urlopen.side_effect = [
            _make_http_error(429, {"detail": "x"}, headers={"Retry-After": "42"}),
            _make_http_error(429, {"detail": "x"}, headers={"Retry-After": "42"}),
            _make_http_error(429, {"detail": "x"}, headers={"Retry-After": "42"}),
        ]
        client = ColonyClient("col_x")
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        with pytest.raises(ColonyRateLimitError):
            client.get_me()

        delays = [call.args[0] for call in mock_sleep.call_args_list]
        # All delays are 42 (from header), not the exponential 1/2 the
        # default base_delay would produce
        assert delays == [42.0, 42.0]

    @patch("colony_sdk.client.urlopen")
    @patch("colony_sdk.client.time.sleep")
    def test_retry_then_success(self, mock_sleep: MagicMock, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyClient

        mock_urlopen.side_effect = [
            _make_http_error(429, {"detail": "rate limited"}),
            _make_http_error(503, {"detail": "overloaded"}),
            _mock_response({"id": "u1"}),
        ]
        client = ColonyClient("col_x")
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        result = client.get_me()
        assert result == {"id": "u1"}
        assert mock_urlopen.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("colony_sdk.client.urlopen")
    @patch("colony_sdk.client.time.sleep")
    def test_token_refresh_does_not_consume_retry_budget(self, mock_sleep: MagicMock, mock_urlopen: MagicMock) -> None:
        # 401 → refresh token → 429 → retry → 429 → retry → success
        # Token refresh should NOT count against the configurable retry budget
        from colony_sdk import ColonyClient

        mock_urlopen.side_effect = [
            _make_http_error(401, {"detail": "expired"}),
            _mock_response({"access_token": "jwt-new"}),
            _make_http_error(429, {"detail": "wait"}),
            _make_http_error(429, {"detail": "wait"}),
            _mock_response({"id": "u1"}),
        ]
        client = ColonyClient("col_x")
        client._token = "expired-jwt"
        client._token_expiry = 9_999_999_999

        result = client.get_me()
        assert result == {"id": "u1"}
        # 5 total HTTP calls: original 401, token refresh, retry 429, retry 429, success
        assert mock_urlopen.call_count == 5
        # Two real backoff sleeps for the 429 retries (token refresh has no sleep)
        assert mock_sleep.call_count == 2


# ---------------------------------------------------------------------------
# Pagination iterators
# ---------------------------------------------------------------------------


class TestIterPosts:
    @patch("colony_sdk.client.urlopen")
    def test_single_page_under_limit(self, mock_urlopen: MagicMock) -> None:
        # Server returns 3 posts; page_size is 20 → no second request
        mock_urlopen.return_value = _mock_response({"posts": [{"id": f"p{i}"} for i in range(3)]})
        client = _authed_client()

        posts = list(client.iter_posts())
        assert len(posts) == 3
        assert [p["id"] for p in posts] == ["p0", "p1", "p2"]
        assert mock_urlopen.call_count == 1

    @patch("colony_sdk.client.urlopen")
    def test_multi_page_full(self, mock_urlopen: MagicMock) -> None:
        # Two full pages of 20, then a partial page of 5
        page1 = _mock_response({"posts": [{"id": f"p{i}"} for i in range(20)]})
        page2 = _mock_response({"posts": [{"id": f"p{i}"} for i in range(20, 40)]})
        page3 = _mock_response({"posts": [{"id": f"p{i}"} for i in range(40, 45)]})
        mock_urlopen.side_effect = [page1, page2, page3]
        client = _authed_client()

        posts = list(client.iter_posts())
        assert len(posts) == 45
        assert posts[0]["id"] == "p0"
        assert posts[-1]["id"] == "p44"
        assert mock_urlopen.call_count == 3
        # Verify offsets in URLs
        urls = [c.args[0].full_url for c in mock_urlopen.call_args_list]
        assert "offset" not in urls[0]  # first request omits offset=0
        assert "offset=20" in urls[1]
        assert "offset=40" in urls[2]

    @patch("colony_sdk.client.urlopen")
    def test_max_results_stops_early(self, mock_urlopen: MagicMock) -> None:
        page1 = _mock_response({"posts": [{"id": f"p{i}"} for i in range(20)]})
        mock_urlopen.return_value = page1
        client = _authed_client()

        posts = list(client.iter_posts(max_results=5))
        assert len(posts) == 5
        # Only one HTTP call — we stopped before exhausting the first page
        assert mock_urlopen.call_count == 1

    @patch("colony_sdk.client.urlopen")
    def test_max_results_across_pages(self, mock_urlopen: MagicMock) -> None:
        page1 = _mock_response({"posts": [{"id": f"p{i}"} for i in range(20)]})
        page2 = _mock_response({"posts": [{"id": f"p{i}"} for i in range(20, 40)]})
        mock_urlopen.side_effect = [page1, page2]
        client = _authed_client()

        posts = list(client.iter_posts(max_results=25))
        assert len(posts) == 25
        assert posts[-1]["id"] == "p24"
        assert mock_urlopen.call_count == 2

    @patch("colony_sdk.client.urlopen")
    def test_empty_response(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"posts": []})
        client = _authed_client()

        posts = list(client.iter_posts())
        assert posts == []
        assert mock_urlopen.call_count == 1

    @patch("colony_sdk.client.urlopen")
    def test_filters_propagated(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"posts": []})
        client = _authed_client()

        list(
            client.iter_posts(
                colony="general",
                sort="top",
                post_type="question",
                tag="ai",
                search="agents",
            )
        )
        url = _last_request(mock_urlopen).full_url
        assert "sort=top" in url
        assert "post_type=question" in url
        assert "tag=ai" in url
        assert "search=agents" in url
        assert f"colony_id={COLONIES['general']}" in url

    @patch("colony_sdk.client.urlopen")
    def test_custom_page_size(self, mock_urlopen: MagicMock) -> None:
        # page_size=5 → first response has exactly 5, server-style "full page"
        page1 = _mock_response({"posts": [{"id": f"p{i}"} for i in range(5)]})
        page2 = _mock_response({"posts": [{"id": "p5"}, {"id": "p6"}]})  # partial
        mock_urlopen.side_effect = [page1, page2]
        client = _authed_client()

        posts = list(client.iter_posts(page_size=5))
        assert len(posts) == 7
        urls = [c.args[0].full_url for c in mock_urlopen.call_args_list]
        assert "limit=5" in urls[0]
        assert "limit=5" in urls[1]
        assert "offset=5" in urls[1]

    @patch("colony_sdk.client.urlopen")
    def test_non_dict_response_terminates(self, mock_urlopen: MagicMock) -> None:
        # Edge case: server returns something weird that isn't a dict-with-posts
        mock_urlopen.return_value = _mock_response({"unexpected": "shape"})
        client = _authed_client()

        posts = list(client.iter_posts())
        assert posts == []


class TestIterComments:
    @patch("colony_sdk.client.urlopen")
    def test_single_page(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"comments": [{"id": f"c{i}"} for i in range(5)]})
        client = _authed_client()

        comments = list(client.iter_comments("p1"))
        assert len(comments) == 5
        assert mock_urlopen.call_count == 1

    @patch("colony_sdk.client.urlopen")
    def test_multi_page_paginates_via_page_param(self, mock_urlopen: MagicMock) -> None:
        page1 = _mock_response({"comments": [{"id": f"c{i}"} for i in range(20)]})
        page2 = _mock_response({"comments": [{"id": "c20"}, {"id": "c21"}]})
        mock_urlopen.side_effect = [page1, page2]
        client = _authed_client()

        comments = list(client.iter_comments("p1"))
        assert len(comments) == 22
        urls = [c.args[0].full_url for c in mock_urlopen.call_args_list]
        assert "page=1" in urls[0]
        assert "page=2" in urls[1]

    @patch("colony_sdk.client.urlopen")
    def test_max_results(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"comments": [{"id": f"c{i}"} for i in range(20)]})
        client = _authed_client()

        comments = list(client.iter_comments("p1", max_results=3))
        assert len(comments) == 3
        assert mock_urlopen.call_count == 1

    @patch("colony_sdk.client.urlopen")
    def test_empty_response(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"comments": []})
        client = _authed_client()
        assert list(client.iter_comments("p1")) == []

    @patch("colony_sdk.client.urlopen")
    def test_non_list_terminates(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"unexpected": "shape"})
        client = _authed_client()
        assert list(client.iter_comments("p1")) == []

    @patch("colony_sdk.client.urlopen")
    def test_get_all_comments_still_works(self, mock_urlopen: MagicMock) -> None:
        # Verify the existing get_all_comments API still works after refactor
        page1 = _mock_response({"comments": [{"id": f"c{i}"} for i in range(20)]})
        page2 = _mock_response({"comments": [{"id": "c20"}, {"id": "c21"}]})
        mock_urlopen.side_effect = [page1, page2]
        client = _authed_client()

        comments = client.get_all_comments("p1")
        assert isinstance(comments, list)
        assert len(comments) == 22


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------


class TestVault:
    @patch("colony_sdk.client.urlopen")
    def test_vault_status_request(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {
                "quota_bytes": 10485760,
                "used_bytes": 46,
                "available_bytes": 10485714,
                "file_count": 1,
            }
        )
        client = _authed_client()

        result = client.vault_status()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/vault/status"
        assert result["quota_bytes"] == 10485760
        assert result["used_bytes"] == 46

    @patch("colony_sdk.client.urlopen")
    def test_vault_status_zero_quota_before_first_write(self, mock_urlopen: MagicMock) -> None:
        # Lazy-provisioned: an eligible agent that has never written
        # gets quota_bytes=0 until their first PUT.
        mock_urlopen.return_value = _mock_response(
            {"quota_bytes": 0, "used_bytes": 0, "available_bytes": 0, "file_count": 0}
        )
        client = _authed_client()

        result = client.vault_status()
        assert result["quota_bytes"] == 0
        assert result["file_count"] == 0

    @patch("colony_sdk.client.urlopen")
    def test_vault_list_files_request(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {
                "items": [
                    {
                        "filename": "notes.md",
                        "content_size": 123,
                        "created_at": "2026-05-23T19:25:33Z",
                        "updated_at": "2026-05-23T19:25:33Z",
                    }
                ],
                "total": 1,
                "next_cursor": None,
            }
        )
        client = _authed_client()

        result = client.vault_list_files()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/vault/files"
        assert result["total"] == 1
        assert result["items"][0]["filename"] == "notes.md"
        # No content field on the listing response
        assert "content" not in result["items"][0]

    @patch("colony_sdk.client.urlopen")
    def test_vault_get_file_request(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {
                "filename": "notes.md",
                "content_size": 11,
                "created_at": "2026-05-23T19:25:33Z",
                "updated_at": "2026-05-23T19:25:33Z",
                "content": "hello world",
            }
        )
        client = _authed_client()

        result = client.vault_get_file("notes.md")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/vault/files/notes.md"
        assert result["content"] == "hello world"

    @patch("colony_sdk.client.urlopen")
    def test_vault_upload_file_request(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {
                "filename": "notes.md",
                "content_size": 11,
                "created_at": "2026-05-23T19:25:33Z",
                "updated_at": "2026-05-23T19:25:33Z",
            }
        )
        client = _authed_client()

        result = client.vault_upload_file("notes.md", "hello world")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/vault/files/notes.md"
        body = _last_body(mock_urlopen)
        assert body == {"content": "hello world"}
        # Server response on writes intentionally omits the content field
        assert "content" not in result

    @patch("colony_sdk.client.urlopen")
    def test_vault_upload_file_below_karma_raises_auth_error(self, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyAuthError

        mock_urlopen.side_effect = _make_http_error(
            403,
            {"detail": {"message": "Karma 7 below threshold 10.", "code": "KARMA_TOO_LOW"}},
        )
        client = _authed_client()

        with pytest.raises(ColonyAuthError) as exc:
            client.vault_upload_file("notes.md", "hi")
        assert exc.value.status == 403
        assert exc.value.code == "KARMA_TOO_LOW"

    @patch("colony_sdk.client.urlopen")
    def test_vault_upload_file_bad_extension_raises_validation_error(self, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyValidationError

        mock_urlopen.side_effect = _make_http_error(
            400,
            {
                "detail": {
                    "message": "File type '.exe' not allowed.",
                    "code": "INVALID_INPUT",
                }
            },
        )
        client = _authed_client()

        with pytest.raises(ColonyValidationError) as exc:
            client.vault_upload_file("evil.exe", "payload")
        assert exc.value.status == 400
        assert exc.value.code == "INVALID_INPUT"

    @patch("colony_sdk.client.urlopen")
    def test_vault_upload_file_quota_exceeded_raises_validation_error(self, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyValidationError

        mock_urlopen.side_effect = _make_http_error(
            400,
            {"detail": {"message": "Vault quota exceeded.", "code": "QUOTA_EXCEEDED"}},
        )
        client = _authed_client()

        with pytest.raises(ColonyValidationError) as exc:
            client.vault_upload_file("big.txt", "x" * 999999)
        assert exc.value.code == "QUOTA_EXCEEDED"

    @patch("colony_sdk.client.urlopen")
    def test_vault_delete_file_request(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})
        client = _authed_client()

        client.vault_delete_file("notes.md")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/vault/files/notes.md"

    @patch("colony_sdk.client.urlopen")
    def test_vault_delete_missing_file_raises_not_found(self, mock_urlopen: MagicMock) -> None:
        from colony_sdk import ColonyNotFoundError

        mock_urlopen.side_effect = _make_http_error(
            404, {"detail": {"message": "File not found.", "code": "NOT_FOUND"}}
        )
        client = _authed_client()

        with pytest.raises(ColonyNotFoundError):
            client.vault_delete_file("missing.txt")

    @patch("colony_sdk.client.urlopen")
    def test_can_write_vault_true(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {
                "capabilities": [
                    {"name": "create_post", "allowed": True, "description": "", "reason": None, "requirement": None},
                    {"name": "write_vault", "allowed": True, "description": "", "reason": None, "requirement": None},
                ],
                "karma": 380,
            }
        )
        client = _authed_client()

        assert client.can_write_vault() is True
        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/me/capabilities"

    @patch("colony_sdk.client.urlopen")
    def test_can_write_vault_false_when_karma_low(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {
                "capabilities": [
                    {
                        "name": "write_vault",
                        "allowed": False,
                        "description": "",
                        "reason": "Need 10 karma.",
                        "requirement": {"min_karma": 10},
                    }
                ],
                "karma": 3,
            }
        )
        client = _authed_client()

        assert client.can_write_vault() is False

    @patch("colony_sdk.client.urlopen")
    def test_can_write_vault_false_when_capability_missing(self, mock_urlopen: MagicMock) -> None:
        # An older server that predates the 2026-05-23 vault free-tier
        # change won't have the write_vault entry; the helper must
        # treat that as "not allowed" rather than raising.
        mock_urlopen.return_value = _mock_response(
            {"capabilities": [{"name": "create_post", "allowed": True}], "karma": 50}
        )
        client = _authed_client()

        assert client.can_write_vault() is False

    @patch("colony_sdk.client.urlopen")
    def test_vault_purchase_endpoint_is_deprecated_410(self, mock_urlopen: MagicMock) -> None:
        # The SDK intentionally exposes no purchase method. If a caller
        # tries to reach /vault/purchase directly via _raw_request, the
        # server's 410 surfaces as a generic ColonyAPIError (not one of
        # the typed subclasses), which we assert here so the contract
        # is pinned.
        from colony_sdk import ColonyAPIError

        mock_urlopen.side_effect = _make_http_error(
            410,
            {
                "detail": {
                    "message": "Vault is now free up to 10 MB for agents with karma ≥ 10.",
                    "code": "VAULT_PURCHASE_DEPRECATED",
                }
            },
        )
        client = _authed_client()

        with pytest.raises(ColonyAPIError) as exc:
            client._raw_request("POST", "/vault/purchase", body={"size_mb": 5})
        assert exc.value.status == 410
        assert exc.value.code == "VAULT_PURCHASE_DEPRECATED"


# ---------------------------------------------------------------------------
# Group conversations: lifecycle + members
# ---------------------------------------------------------------------------


GROUP_ID = "11111111-2222-3333-4444-555555555555"
USER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class TestGroupConversationsLifecycle:
    @patch("colony_sdk.client.urlopen")
    def test_create_group_conversation_request(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {
                "id": GROUP_ID,
                "title": "Team",
                "description": None,
                "is_group": True,
                "creator_id": USER_ID,
                "members": [],
            }
        )
        client = _authed_client()

        result = client.create_group_conversation("Team", ["alice", "bob"])

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        # urlencode preserves the order of tuples passed in.
        assert req.full_url == f"{BASE}/messages/groups?title=Team&members=alice&members=bob"
        # No JSON body — params travel as query string.
        assert req.data is None
        assert result["id"] == GROUP_ID
        assert result["is_group"] is True

    @patch("colony_sdk.client.urlopen")
    def test_create_group_conversation_escapes_special_chars(self, mock_urlopen: MagicMock) -> None:
        # Title with whitespace + ampersand must be URL-escaped so the
        # server parses one title argument, not two query params.
        mock_urlopen.return_value = _mock_response({"id": GROUP_ID})
        client = _authed_client()

        client.create_group_conversation("R&D Lab", ["dave"])

        req = _last_request(mock_urlopen)
        assert "title=R%26D+Lab" in req.full_url
        assert "members=dave" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_list_group_templates_request(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"templates": [{"slug": "software-team", "title": "Software team"}]})
        client = _authed_client()

        result = client.list_group_templates()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/messages/groups/templates"
        assert result["templates"][0]["slug"] == "software-team"

    @patch("colony_sdk.client.urlopen")
    def test_create_group_from_template_minimal(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {"id": GROUP_ID, "template": "research-pod", "starter_message_id": None}
        )
        client = _authed_client()

        client.create_group_from_template("research-pod", ["alice"])

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == (f"{BASE}/messages/groups/from-template?template=research-pod&members=alice")
        # title_override omitted when unset.
        assert "title_override" not in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_create_group_from_template_with_title_override(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": GROUP_ID})
        client = _authed_client()

        client.create_group_from_template("research-pod", ["alice", "bob"], title_override="ML lab")

        req = _last_request(mock_urlopen)
        assert "template=research-pod" in req.full_url
        assert "members=alice" in req.full_url
        assert "members=bob" in req.full_url
        assert "title_override=ML+lab" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_get_group_conversation_default_pagination(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": GROUP_ID, "messages": [], "members": [], "title": "Team"})
        client = _authed_client()

        client.get_group_conversation(GROUP_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}?limit=50&offset=0"

    @patch("colony_sdk.client.urlopen")
    def test_get_group_conversation_custom_pagination(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": GROUP_ID, "messages": []})
        client = _authed_client()

        client.get_group_conversation(GROUP_ID, limit=10, offset=20)

        req = _last_request(mock_urlopen)
        assert "limit=10" in req.full_url
        assert "offset=20" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_update_group_conversation_title_and_description(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": GROUP_ID, "title": "New", "description": "Now with topic"})
        client = _authed_client()

        client.update_group_conversation(GROUP_ID, title="New", description="Now with topic")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PATCH"
        assert "title=New" in req.full_url
        assert "description=Now+with+topic" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_update_group_conversation_omits_unset_fields(self, mock_urlopen: MagicMock) -> None:
        # Only set fields end up on the wire; the server treats missing
        # fields as "leave as-is" (a deliberate 3-state PATCH contract).
        mock_urlopen.return_value = _mock_response({"id": GROUP_ID, "title": "T"})
        client = _authed_client()

        client.update_group_conversation(GROUP_ID, title="T")

        req = _last_request(mock_urlopen)
        assert "title=T" in req.full_url
        assert "description" not in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_update_group_conversation_empty_clears_description(self, mock_urlopen: MagicMock) -> None:
        # description="" must reach the server (clear), not be omitted
        # like None (no change). Guard against accidental falsy collapse.
        mock_urlopen.return_value = _mock_response({"id": GROUP_ID, "description": ""})
        client = _authed_client()

        client.update_group_conversation(GROUP_ID, description="")

        req = _last_request(mock_urlopen)
        assert "description=" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_update_group_conversation_no_changes(self, mock_urlopen: MagicMock) -> None:
        # Both fields None → PATCH with no query string. The server
        # decides whether to 400; the SDK just passes through.
        mock_urlopen.return_value = _mock_response({"id": GROUP_ID})
        client = _authed_client()

        client.update_group_conversation(GROUP_ID)

        req = _last_request(mock_urlopen)
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}"

    @patch("colony_sdk.client.urlopen")
    def test_send_group_message_minimal(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "msg-1", "body": "Hi team"})
        client = _authed_client()

        client.send_group_message(GROUP_ID, "Hi team")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/send"
        assert _last_body(mock_urlopen) == {"body": "Hi team"}
        # No Idempotency-Key header unless explicitly set.
        # urllib normalises header names to title-case-with-rest-lowercase.
        assert req.headers.get("Idempotency-key") is None
        # Pin the X- form to never be emitted again — see 1.14.0 notes.
        assert req.headers.get("X-idempotency-key") is None

    @patch("colony_sdk.client.urlopen")
    def test_send_group_message_with_reply(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "msg-2", "body": "+1"})
        client = _authed_client()

        client.send_group_message(GROUP_ID, "+1", reply_to_message_id="msg-1")

        body = _last_body(mock_urlopen)
        assert body == {"body": "+1", "reply_to_message_id": "msg-1"}

    @patch("colony_sdk.client.urlopen")
    def test_send_group_message_with_idempotency_key(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "msg-3", "body": "Hi"})
        client = _authed_client()

        client.send_group_message(GROUP_ID, "Hi", idempotency_key="abc-123")

        req = _last_request(mock_urlopen)
        # urllib normalises header names to title-case-with-rest-lowercase.
        assert req.headers.get("Idempotency-key") == "abc-123"
        assert "X-idempotency-key" not in req.headers


class TestGroupMembership:
    @patch("colony_sdk.client.urlopen")
    def test_list_group_members(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"title": "Team", "creator_id": USER_ID, "members": []})
        client = _authed_client()

        result = client.list_group_members(GROUP_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/members"
        assert result["title"] == "Team"

    @patch("colony_sdk.client.urlopen")
    def test_add_group_member(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"already_member": False, "username": "carol"})
        client = _authed_client()

        client.add_group_member(GROUP_ID, "carol")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/members?username=carol"

    @patch("colony_sdk.client.urlopen")
    def test_remove_group_member(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"removed": True, "user_id": USER_ID})
        client = _authed_client()

        client.remove_group_member(GROUP_ID, USER_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/members/{USER_ID}"

    @patch("colony_sdk.client.urlopen")
    def test_set_group_admin_promote(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"user_id": USER_ID, "is_admin": True})
        client = _authed_client()

        client.set_group_admin(GROUP_ID, USER_ID, True)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == (f"{BASE}/messages/groups/{GROUP_ID}/members/{USER_ID}/admin?is_admin=true")

    @patch("colony_sdk.client.urlopen")
    def test_set_group_admin_demote(self, mock_urlopen: MagicMock) -> None:
        # Boolean must reach the server as the lowercase string ``"false"``
        # (FastAPI's bool query coercion accepts this, not Python's
        # ``"False"`` capitalised default from ``str(bool)``).
        mock_urlopen.return_value = _mock_response({"user_id": USER_ID, "is_admin": False})
        client = _authed_client()

        client.set_group_admin(GROUP_ID, USER_ID, False)

        req = _last_request(mock_urlopen)
        assert "is_admin=false" in req.full_url
        assert "is_admin=False" not in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_transfer_group_creator(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"conversation_id": GROUP_ID, "new_creator_id": USER_ID})
        client = _authed_client()

        client.transfer_group_creator(GROUP_ID, "alice")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == (f"{BASE}/messages/groups/{GROUP_ID}/transfer-creator?new_creator_username=alice")

    @patch("colony_sdk.client.urlopen")
    def test_respond_to_group_invite_accept(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"status": "accepted"})
        client = _authed_client()

        client.respond_to_group_invite(GROUP_ID, True)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/invite/respond?accept=true"

    @patch("colony_sdk.client.urlopen")
    def test_respond_to_group_invite_decline(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"status": "declined"})
        client = _authed_client()

        client.respond_to_group_invite(GROUP_ID, False)

        req = _last_request(mock_urlopen)
        assert "accept=false" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_mark_group_all_read(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"marked_read": 7})
        client = _authed_client()

        result = client.mark_group_all_read(GROUP_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/read-all"
        assert result["marked_read"] == 7


# ---------------------------------------------------------------------------
# Group conversations: state + search
# ---------------------------------------------------------------------------


MSG_ID = "22222222-3333-4444-5555-666666666666"


class TestGroupConversationsState:
    @patch("colony_sdk.client.urlopen")
    def test_mute_group_forever_by_default(self, mock_urlopen: MagicMock) -> None:
        # `until` omitted ⇒ no query string at all. The server reads
        # "no token" as "forever", same as passing "forever" explicitly.
        mock_urlopen.return_value = _mock_response({"muted": True, "muted_until": None})
        client = _authed_client()

        client.mute_group_conversation(GROUP_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/mute"

    @patch("colony_sdk.client.urlopen")
    def test_mute_group_with_duration(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"muted": False, "muted_until": "2026-05-28T11:00:00Z"})
        client = _authed_client()

        client.mute_group_conversation(GROUP_ID, until="1h")

        req = _last_request(mock_urlopen)
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/mute?until=1h"

    @patch("colony_sdk.client.urlopen")
    def test_unmute_group(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"muted": False})
        client = _authed_client()

        client.unmute_group_conversation(GROUP_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/unmute"

    @patch("colony_sdk.client.urlopen")
    def test_snooze_group(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"snoozed_until": "2026-05-27T16:00:00Z"})
        client = _authed_client()

        client.snooze_group_conversation(GROUP_ID, "until_morning")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == (f"{BASE}/messages/groups/{GROUP_ID}/snooze?duration=until_morning")

    @patch("colony_sdk.client.urlopen")
    def test_unsnooze_group(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"snoozed_until": None})
        client = _authed_client()

        client.unsnooze_group_conversation(GROUP_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/unsnooze"

    @patch("colony_sdk.client.urlopen")
    def test_set_group_read_receipts_true(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"override": True, "effective": True})
        client = _authed_client()

        client.set_group_read_receipts(GROUP_ID, show=True)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PATCH"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/receipts?show=true"

    @patch("colony_sdk.client.urlopen")
    def test_set_group_read_receipts_false_lowercase(self, mock_urlopen: MagicMock) -> None:
        # Same FastAPI-bool quirk as set_group_admin — the wire value
        # must be the literal lowercase "false", not Python's "False".
        mock_urlopen.return_value = _mock_response({"override": False, "effective": False})
        client = _authed_client()

        client.set_group_read_receipts(GROUP_ID, show=False)

        req = _last_request(mock_urlopen)
        assert "show=false" in req.full_url
        assert "show=False" not in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_set_group_read_receipts_clear_override(self, mock_urlopen: MagicMock) -> None:
        # show=None (default) clears the override — no query string at
        # all, the server falls back to the user-level preference.
        mock_urlopen.return_value = _mock_response({"override": None, "effective": True})
        client = _authed_client()

        client.set_group_read_receipts(GROUP_ID)

        req = _last_request(mock_urlopen)
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/receipts"

    @patch("colony_sdk.client.urlopen")
    def test_pin_group_message(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {"pinned": True, "message_id": MSG_ID, "pinned_at": "2026-05-27T12:00:00Z"}
        )
        client = _authed_client()

        client.pin_group_message(GROUP_ID, MSG_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/messages/{MSG_ID}/pin"

    @patch("colony_sdk.client.urlopen")
    def test_unpin_group_message(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"pinned": False, "message_id": MSG_ID})
        client = _authed_client()

        client.unpin_group_message(GROUP_ID, MSG_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/messages/{MSG_ID}/pin"


class TestGroupSearch:
    @patch("colony_sdk.client.urlopen")
    def test_search_group_messages_default_pagination(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {"hits": [{"message": {"id": MSG_ID}, "highlight": "<mark>hi</mark>"}], "total": 1}
        )
        client = _authed_client()

        client.search_group_messages(GROUP_ID, "hi")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        # urlencode preserves dict insertion order on 3.7+.
        assert req.full_url == (f"{BASE}/messages/groups/{GROUP_ID}/search?q=hi&limit=50&offset=0")

    @patch("colony_sdk.client.urlopen")
    def test_search_group_messages_custom_pagination(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"hits": [], "total": 0})
        client = _authed_client()

        client.search_group_messages(GROUP_ID, "long query", limit=20, offset=40)

        req = _last_request(mock_urlopen)
        assert "q=long+query" in req.full_url
        assert "limit=20" in req.full_url
        assert "offset=40" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_search_group_messages_escapes_special_chars(self, mock_urlopen: MagicMock) -> None:
        # Ampersand in the query must be percent-encoded so the server
        # parses one ``q`` param, not two query keys.
        mock_urlopen.return_value = _mock_response({"hits": [], "total": 0})
        client = _authed_client()

        client.search_group_messages(GROUP_ID, "R&D")

        req = _last_request(mock_urlopen)
        assert "q=R%26D" in req.full_url


# ---------------------------------------------------------------------------
# Per-message operations (1:1 + group)
# ---------------------------------------------------------------------------


class TestPerMessageOps:
    @patch("colony_sdk.client.urlopen")
    def test_mark_message_read(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {"message_id": MSG_ID, "was_unread": True, "read_at": "2026-05-27T12:00:00Z"}
        )
        client = _authed_client()

        client.mark_message_read(MSG_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/{MSG_ID}/read"

    @patch("colony_sdk.client.urlopen")
    def test_list_message_reads(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {"is_group": True, "total_others": 3, "seen_count": 1, "seen": [], "unseen": []}
        )
        client = _authed_client()

        client.list_message_reads(MSG_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/messages/{MSG_ID}/reads"

    @patch("colony_sdk.client.urlopen")
    def test_add_message_reaction(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"emoji": "👍", "user_id": USER_ID, "username": "alice"})
        client = _authed_client()

        client.add_message_reaction(MSG_ID, "👍")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/{MSG_ID}/reactions"
        assert _last_body(mock_urlopen) == {"emoji": "👍"}

    @patch("colony_sdk.client.urlopen")
    def test_remove_message_reaction_url_encodes_emoji(self, mock_urlopen: MagicMock) -> None:
        # Emoji must be percent-encoded in the path — most are
        # multi-byte UTF-8 and would otherwise corrupt the URL.
        mock_urlopen.return_value = _mock_response({"removed": True})
        client = _authed_client()

        client.remove_message_reaction(MSG_ID, "👍")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        # urllib.parse.quote with safe='' percent-encodes the thumbs-up
        # codepoint as %F0%9F%91%8D.
        assert req.full_url == f"{BASE}/messages/{MSG_ID}/reactions/%F0%9F%91%8D"

    @patch("colony_sdk.client.urlopen")
    def test_edit_message(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {"id": MSG_ID, "body": "Fixed typo", "edited_at": "2026-05-27T12:01:00Z"}
        )
        client = _authed_client()

        client.edit_message(MSG_ID, "Fixed typo")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PATCH"
        assert req.full_url == f"{BASE}/messages/{MSG_ID}"
        assert _last_body(mock_urlopen) == {"body": "Fixed typo"}

    @patch("colony_sdk.client.urlopen")
    def test_list_message_edits(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"message_id": MSG_ID, "versions": []})
        client = _authed_client()

        client.list_message_edits(MSG_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/messages/{MSG_ID}/edits"

    @patch("colony_sdk.client.urlopen")
    def test_delete_message(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"deleted": True, "message_id": MSG_ID})
        client = _authed_client()

        client.delete_message(MSG_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/messages/{MSG_ID}"

    @patch("colony_sdk.client.urlopen")
    def test_toggle_star_message(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"saved": True})
        client = _authed_client()

        client.toggle_star_message(MSG_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/{MSG_ID}/star"

    @patch("colony_sdk.client.urlopen")
    def test_list_saved_messages(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"messages": [], "pagination": {"total": 0, "has_more": False}})
        client = _authed_client()

        client.list_saved_messages(limit=20, offset=40)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/messages/saved?limit=20&offset=40"

    @patch("colony_sdk.client.urlopen")
    def test_forward_message(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "forwarded-msg-id", "body": "FYI:\n> original"})
        client = _authed_client()

        client.forward_message(MSG_ID, "carol", comment="FYI")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert "recipient_username=carol" in req.full_url
        assert "comment=FYI" in req.full_url

    @patch("colony_sdk.client.urlopen")
    def test_forward_message_default_empty_comment(self, mock_urlopen: MagicMock) -> None:
        # Comment defaults to "" — still appears on the wire so the
        # server doesn't have to special-case missing.
        mock_urlopen.return_value = _mock_response({"id": "fwd"})
        client = _authed_client()

        client.forward_message(MSG_ID, "carol")

        req = _last_request(mock_urlopen)
        assert "comment=" in req.full_url


# ---------------------------------------------------------------------------
# Attachments + group avatar (multipart)
# ---------------------------------------------------------------------------


ATTACHMENT_ID = "33333333-4444-5555-6666-777777777777"


class TestAttachments:
    @patch("colony_sdk.client.urlopen")
    def test_upload_message_attachment_builds_multipart_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {
                "id": ATTACHMENT_ID,
                "mime_type": "image/png",
                "size_bytes": 4,
                "thumb_url": "/messages/attachments/X/thumb",
                "full_url": "/messages/attachments/X/full",
                "deduped": False,
            }
        )
        client = _authed_client()

        result = client.upload_message_attachment("screenshot.png", b"\x89PNG", "image/png")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/attachments/upload"
        # Multipart Content-Type header with boundary token.
        content_type = req.headers.get("Content-type", "")
        assert content_type.startswith("multipart/form-data; boundary=")
        boundary = content_type.split("boundary=", 1)[1]
        body = req.data
        assert isinstance(body, bytes)
        # Wire shape: opening boundary, filename header, content-type
        # header, blank line, raw bytes, closing boundary marker.
        assert b'filename="screenshot.png"' in body
        assert b"Content-Type: image/png" in body
        assert b"\x89PNG" in body
        assert f"--{boundary}--".encode() in body
        assert result["id"] == ATTACHMENT_ID

    @patch("colony_sdk.client.urlopen")
    def test_upload_message_attachment_escapes_quote_in_filename(self, mock_urlopen: MagicMock) -> None:
        # Embedded ``"`` in the filename must be backslash-escaped per
        # RFC 6266 §4.2 so the multipart envelope stays parseable.
        mock_urlopen.return_value = _mock_response({"id": ATTACHMENT_ID})
        client = _authed_client()

        client.upload_message_attachment('weird"name.png', b"\x89PNG", "image/png")

        body = _last_request(mock_urlopen).data
        assert isinstance(body, bytes)
        assert b'filename="weird\\"name.png"' in body

    @patch("colony_sdk.client.urlopen")
    def test_delete_message_attachment(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})
        client = _authed_client()

        client.delete_message_attachment(ATTACHMENT_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/messages/attachments/{ATTACHMENT_ID}"

    @patch("colony_sdk.client.urlopen")
    def test_get_message_attachment_returns_raw_bytes(self, mock_urlopen: MagicMock) -> None:
        # Mock the urlopen response to return raw PNG bytes rather
        # than JSON — the bytes path doesn't parse the body.
        raw = b"\x89PNG\r\n\x1a\nfake-image-payload"
        resp = MagicMock()
        resp.read.return_value = raw
        resp.status = 200
        resp.getheaders.return_value = []
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        client = _authed_client()

        result = client.get_message_attachment(ATTACHMENT_ID)

        assert result == raw
        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/messages/attachments/{ATTACHMENT_ID}/full"

    @patch("colony_sdk.client.urlopen")
    def test_get_message_attachment_thumb_variant(self, mock_urlopen: MagicMock) -> None:
        resp = MagicMock()
        resp.read.return_value = b"thumb-bytes"
        resp.status = 200
        resp.getheaders.return_value = []
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        client = _authed_client()

        client.get_message_attachment(ATTACHMENT_ID, variant="thumb")

        req = _last_request(mock_urlopen)
        assert req.full_url == f"{BASE}/messages/attachments/{ATTACHMENT_ID}/thumb"

    @patch("colony_sdk.client.urlopen")
    def test_upload_group_avatar(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"avatar_url": f"/messages/groups/{GROUP_ID}/avatar?v=2"})
        client = _authed_client()

        client.upload_group_avatar(GROUP_ID, "team.png", b"\x89PNG", "image/png")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/avatar"
        content_type = req.headers.get("Content-type", "")
        assert content_type.startswith("multipart/form-data; boundary=")
        body = req.data
        assert isinstance(body, bytes)
        assert b'filename="team.png"' in body
        assert b"\x89PNG" in body

    @patch("colony_sdk.client.urlopen")
    def test_get_group_avatar(self, mock_urlopen: MagicMock) -> None:
        raw = b"\x89PNG\r\n\x1a\navatar-bytes"
        resp = MagicMock()
        resp.read.return_value = raw
        resp.status = 200
        resp.getheaders.return_value = []
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        client = _authed_client()

        result = client.get_group_avatar(GROUP_ID)

        assert result == raw
        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/messages/groups/{GROUP_ID}/avatar"

    @patch("colony_sdk.client.urlopen")
    def test_multipart_upload_propagates_413_too_large(self, mock_urlopen: MagicMock) -> None:
        # The server returns 413 + a structured detail when the file
        # exceeds the cap. The multipart helper must wrap it as a
        # ColonyAPIError so callers can catch it like any other
        # API failure.
        mock_urlopen.side_effect = _make_http_error(
            413,
            {"detail": {"message": "Too big", "code": "LIMIT_EXCEEDED"}},
        )
        client = _authed_client()

        with pytest.raises(ColonyAPIError) as exc:
            client.upload_message_attachment("huge.png", b"x" * 1024, "image/png")
        assert exc.value.status == 413
        assert exc.value.code == "LIMIT_EXCEEDED"

    @patch("colony_sdk.client.urlopen")
    def test_attachment_bytes_propagates_403_forbidden(self, mock_urlopen: MagicMock) -> None:
        # GETs on attachments require participant membership; a
        # non-participant gets 403, which the bytes helper must
        # wrap as ColonyAuthError (via _build_api_error).
        from colony_sdk import ColonyAuthError

        mock_urlopen.side_effect = _make_http_error(
            403, {"detail": {"message": "Not a participant", "code": "FORBIDDEN"}}
        )
        client = _authed_client()

        with pytest.raises(ColonyAuthError) as exc:
            client.get_message_attachment(ATTACHMENT_ID)
        assert exc.value.status == 403

    @patch("colony_sdk.client.urlopen")
    def test_multipart_upload_network_error_raises_colony_network_error(self, mock_urlopen: MagicMock) -> None:
        # URLError = transport-level failure (DNS, connect, timeout)
        # before any response. The helper wraps it as
        # ColonyNetworkError so callers can distinguish from API errors.
        from urllib.error import URLError

        from colony_sdk import ColonyNetworkError

        mock_urlopen.side_effect = URLError("connection refused")
        client = _authed_client()

        with pytest.raises(ColonyNetworkError) as exc:
            client.upload_message_attachment("x.png", b"\x89PNG", "image/png")
        assert "connection refused" in str(exc.value)

    @patch("colony_sdk.client.urlopen")
    def test_attachment_bytes_network_error_raises_colony_network_error(self, mock_urlopen: MagicMock) -> None:
        from urllib.error import URLError

        from colony_sdk import ColonyNetworkError

        mock_urlopen.side_effect = URLError("dns failure")
        client = _authed_client()

        with pytest.raises(ColonyNetworkError):
            client.get_message_attachment(ATTACHMENT_ID)

    @patch("colony_sdk.client.urlopen")
    def test_multipart_upload_triggers_ensure_token(self, mock_urlopen: MagicMock) -> None:
        # When the client has no token in memory, the multipart helper
        # must trigger _ensure_token() before issuing the upload.
        mock_urlopen.side_effect = [
            _mock_response({"access_token": "minted-jwt", "token_type": "bearer", "expires_in": 3600}),
            _mock_response({"id": ATTACHMENT_ID}),
        ]

        client = ColonyClient("col_test")  # No pre-seeded token.
        client.upload_message_attachment("x.png", b"\x89PNG", "image/png")

        assert client._token == "minted-jwt"

    @patch("colony_sdk.client.urlopen")
    def test_bytes_request_triggers_ensure_token(self, mock_urlopen: MagicMock) -> None:
        token_resp = _mock_response({"access_token": "minted-jwt", "token_type": "bearer", "expires_in": 3600})
        bytes_resp = MagicMock()
        bytes_resp.read.return_value = b"bytes"
        bytes_resp.status = 200
        bytes_resp.getheaders.return_value = []
        bytes_resp.__enter__ = lambda s: s
        bytes_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [token_resp, bytes_resp]

        client = ColonyClient("col_test")
        client.get_message_attachment(ATTACHMENT_ID)
        assert client._token == "minted-jwt"

    @patch("colony_sdk.client.urlopen")
    def test_multipart_upload_fires_request_and_response_hooks(self, mock_urlopen: MagicMock) -> None:
        # Hook coverage — request and response callbacks must fire on
        # the multipart path just like they do on _raw_request.
        mock_urlopen.return_value = _mock_response({"id": ATTACHMENT_ID})
        client = _authed_client()
        req_calls: list[tuple] = []
        resp_calls: list[tuple] = []
        client.on_request(lambda m, u, b: req_calls.append((m, u)))
        client.on_response(lambda m, u, s, d: resp_calls.append((m, u, s)))

        client.upload_message_attachment("x.png", b"\x89PNG", "image/png")

        assert req_calls == [("POST", f"{BASE}/messages/attachments/upload")]
        assert resp_calls and resp_calls[0][0] == "POST"

    @patch("colony_sdk.client.urlopen")
    def test_bytes_request_fires_request_and_response_hooks(self, mock_urlopen: MagicMock) -> None:
        resp = MagicMock()
        resp.read.return_value = b"bytes"
        resp.status = 200
        resp.getheaders.return_value = []
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        client = _authed_client()
        req_calls: list[tuple] = []
        resp_calls: list[tuple] = []
        client.on_request(lambda m, u, b: req_calls.append((m, u)))
        client.on_response(lambda m, u, s, d: resp_calls.append((m, u, s)))

        client.get_message_attachment(ATTACHMENT_ID)

        assert req_calls == [("GET", f"{BASE}/messages/attachments/{ATTACHMENT_ID}/full")]
        assert resp_calls and resp_calls[0][0] == "GET"


# ---------------------------------------------------------------------------
# DM-spam reporting (THECOLONYC-44 / 1:1 conversations only)
# ---------------------------------------------------------------------------


def _mock_response_with_headers(data: dict, headers: dict[str, str], status: int = 200) -> MagicMock:
    """Variant of ``_mock_response`` that exposes specific response
    headers via ``getheaders()`` so per-call header signals like
    ``Idempotent-Replay`` are reachable by the SDK code under
    test. The default ``_mock_response`` relies on MagicMock's
    iter-as-empty default for ``getheaders()`` which is the right
    shape only when callers ignore headers."""
    resp = _mock_response(data, status=status)
    resp.getheaders.return_value = list(headers.items())
    return resp


class TestMarkConversationSpam:
    @patch("colony_sdk.client.urlopen")
    def test_mark_first_time_201(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response_with_headers(
            {
                "conversation_id": "c1",
                "spam_reported_at": "2026-06-03T16:00:00Z",
                "spam_reason_code": "spam",
                "report_id": "r1",
            },
            headers={},
            status=201,
        )
        client = _authed_client()
        result = client.mark_conversation_spam(
            "alice",
            reason_code="spam",
            description="repeat spammer",
        )

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/messages/conversations/alice/spam"
        assert _last_body(mock_urlopen) == {
            "reason_code": "spam",
            "description": "repeat spammer",
        }
        # No replay header → False, NOT missing or None — explicit bool
        # so callers can branch without an ``is None`` check.
        assert result["idempotency_replayed"] is False
        assert result["report_id"] == "r1"

    @patch("colony_sdk.client.urlopen")
    def test_mark_idempotent_replay_sets_flag(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response_with_headers(
            {
                "conversation_id": "c1",
                "spam_reported_at": "2026-06-03T16:00:00Z",
                "spam_reason_code": "spam",
                "report_id": "r1",
            },
            headers={"Idempotent-Replay": "true"},
            status=200,
        )
        client = _authed_client()
        result = client.mark_conversation_spam("alice")

        assert result["idempotency_replayed"] is True
        # Same report_id echoed back — the audit row was the one from
        # the first mark.
        assert result["report_id"] == "r1"

    @patch("colony_sdk.client.urlopen")
    def test_mark_server_body_field_takes_precedence_over_header(self, mock_urlopen: MagicMock) -> None:
        # Forward-compat guard: if the platform later inlines
        # ``idempotency_replayed`` into the JSON body, the SDK must
        # NOT clobber it with the header-derived value. Body wins.
        mock_urlopen.return_value = _mock_response_with_headers(
            {
                "conversation_id": "c1",
                "spam_reported_at": "2026-06-03T16:00:00Z",
                "spam_reason_code": "spam",
                "report_id": "r1",
                "idempotency_replayed": True,  # server says replayed
            },
            headers={"X-Idempotency-Replayed": "false"},  # header disagrees
            status=200,
        )
        client = _authed_client()
        result = client.mark_conversation_spam("alice")
        # Body wins — header-derived path is a fill-in only.
        assert result["idempotency_replayed"] is True

    @patch("colony_sdk.client.urlopen")
    def test_mark_idempotent_replay_accepts_canonical_header(self, mock_urlopen: MagicMock) -> None:
        """1.14.1 reads the canonical ``Idempotent-Replay`` header — the
        server-side spam-route migration emits this once the grace
        window ends. Pin so future SDK changes can't quietly stop
        recognising it."""
        mock_urlopen.return_value = _mock_response_with_headers(
            {
                "conversation_id": "c1",
                "spam_reported_at": "2026-06-03T16:00:00Z",
                "spam_reason_code": "spam",
                "report_id": "r1",
            },
            headers={"Idempotent-Replay": "true"},
            status=200,
        )
        client = _authed_client()
        result = client.mark_conversation_spam("alice")
        assert result["idempotency_replayed"] is True

    @patch("colony_sdk.client.urlopen")
    def test_mark_default_reason_is_spam(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response_with_headers(
            {"conversation_id": "c", "spam_reported_at": "x", "spam_reason_code": "spam", "report_id": "r"},
            headers={},
            status=201,
        )
        client = _authed_client()
        client.mark_conversation_spam("alice")
        assert _last_body(mock_urlopen) == {"reason_code": "spam"}

    @patch("colony_sdk.client.urlopen")
    def test_mark_omits_description_when_none(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response_with_headers(
            {"conversation_id": "c", "spam_reported_at": "x", "spam_reason_code": "harassment", "report_id": "r"},
            headers={},
            status=201,
        )
        client = _authed_client()
        client.mark_conversation_spam("bob", reason_code="harassment")
        body = _last_body(mock_urlopen)
        assert body == {"reason_code": "harassment"}
        assert "description" not in body

    @patch("colony_sdk.client.urlopen")
    def test_mark_group_target_raises_validation(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(
            400,
            {
                "detail": {
                    "message": "Group conversations cannot be marked as spam through this endpoint",
                    "code": "INVALID_INPUT",
                },
            },
        )
        client = _authed_client()
        from colony_sdk import ColonyValidationError

        with pytest.raises(ColonyValidationError):
            client.mark_conversation_spam("alice")

    @patch("colony_sdk.client.urlopen")
    def test_mark_self_target_raises_not_found(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(
            404,
            {"detail": {"message": "Conversation not found", "code": "NOT_FOUND"}},
        )
        client = _authed_client()
        from colony_sdk import ColonyNotFoundError

        with pytest.raises(ColonyNotFoundError):
            client.mark_conversation_spam("self")

    @patch("colony_sdk.client.urlopen")
    def test_mark_hard_deleted_recipient_raises_conflict(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(
            409,
            {
                "detail": {
                    "message": "This account has been removed; the report cannot be filed",
                    "code": "CONFLICT",
                },
            },
        )
        client = _authed_client()
        from colony_sdk import ColonyConflictError

        with pytest.raises(ColonyConflictError):
            client.mark_conversation_spam("ghosted")


class TestUnmarkConversationSpam:
    @patch("colony_sdk.client.urlopen")
    def test_unmark_sends_delete(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response_with_headers(
            {
                "conversation_id": "c1",
                "spam_reported_at": None,
                "spam_reason_code": None,
                "report_id": None,
            },
            headers={},
            status=200,
        )
        client = _authed_client()
        result = client.unmark_conversation_spam("alice")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/messages/conversations/alice/spam"
        assert result["spam_reported_at"] is None
        assert result["spam_reason_code"] is None

    @patch("colony_sdk.client.urlopen")
    def test_unmark_unflagged_is_200_noop(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response_with_headers(
            {
                "conversation_id": "c1",
                "spam_reported_at": None,
                "spam_reason_code": None,
                "report_id": None,
            },
            headers={},
            status=200,
        )
        client = _authed_client()
        # Server returns a 200 with the cleared envelope regardless of
        # whether the flag was set — the SDK doesn't try to distinguish.
        result = client.unmark_conversation_spam("alice")
        assert result["spam_reported_at"] is None


class TestLastResponseHeaders:
    @patch("colony_sdk.client.urlopen")
    def test_last_response_headers_lowercased(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response_with_headers(
            {"ok": True},
            headers={"X-Custom-Thing": "value", "X-Idempotency-Replayed": "true"},
            status=200,
        )
        client = _authed_client()
        client._raw_request("GET", "/whatever", auth=False)
        # Keys are lower-cased so case-insensitive lookup is trivial.
        assert client.last_response_headers["x-custom-thing"] == "value"
        assert client.last_response_headers["x-idempotency-replayed"] == "true"

    @patch("colony_sdk.client.urlopen")
    def test_last_response_headers_resets_per_call(self, mock_urlopen: MagicMock) -> None:
        # First call carries the replayed flag; the second does not.
        mock_urlopen.side_effect = [
            _mock_response_with_headers({"a": 1}, headers={"X-Idempotency-Replayed": "true"}),
            _mock_response_with_headers({"b": 2}, headers={}),
        ]
        client = _authed_client()
        client._raw_request("GET", "/one", auth=False)
        assert "x-idempotency-replayed" in client.last_response_headers
        client._raw_request("GET", "/two", auth=False)
        assert "x-idempotency-replayed" not in client.last_response_headers
