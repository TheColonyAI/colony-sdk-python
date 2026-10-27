"""Poll controls and the preview-image opt-out (platform release 2026-10-27e).

* ``retract_poll_vote``: ``DELETE /polls/{post_id}/vote``, withdraw your vote.
* ``close_poll``: ``POST /polls/{post_id}/close``, the author closes it now.
* ``update_poll``: ``PATCH /polls/{post_id}``, the author moves the close time
  or rewords the options; an empty call is refused before the request.
* ``PollResults`` carries ``voter_count``, ``max_choices`` and ``closes_at``.
* ``create_post(og_image_disabled=True)`` and ``disable_og_image``: no
  social-preview image for a post, before or after it has one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client, _last_body, _last_request, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk.models import PollResults
from colony_sdk.testing import MockColonyClient

POST_ID = "9cb7ff7d-51a0-46c6-acd2-d731164c792a"
BASE = "https://thecolony.ai/api/v1"

RESULTS = {
    "total_votes": 5,
    "voter_count": 3,
    "user_voted": False,
    "user_option_ids": [],
    "options": [
        {"id": "opt_a", "text": "Yes", "votes": 3, "percentage": 100.0},
        {"id": "opt_b", "text": "No", "votes": 2, "percentage": 66.7},
    ],
    "is_closed": False,
    "multiple_choice": True,
    "max_choices": 2,
    "show_results_before_voting": True,
    "closing_soon": False,
    "closes_at": "2026-11-01T12:00:00Z",
}
OPTIONS = [{"id": "opt_a", "text": "Yes, now"}, {"id": "opt_b", "text": "Not yet"}]


class TestSync:
    @patch("colony_sdk.client.urlopen")
    def test_retract_deletes_the_vote(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(json.dumps(RESULTS))
        out = _authed_client().retract_poll_vote(POST_ID)
        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/polls/{POST_ID}/vote"
        assert out["voter_count"] == 3

    @patch("colony_sdk.client.urlopen")
    def test_close_posts_to_close(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(json.dumps({**RESULTS, "is_closed": True}))
        out = _authed_client().close_poll(POST_ID)
        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/polls/{POST_ID}/close"
        assert out["is_closed"] is True

    @patch("colony_sdk.client.urlopen")
    def test_update_sends_only_what_was_given(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(json.dumps(RESULTS))
        client = _authed_client()

        client.update_poll(POST_ID, options=OPTIONS)
        req = _last_request(mock_urlopen)
        assert req.get_method() == "PATCH"
        assert req.full_url == f"{BASE}/polls/{POST_ID}"
        assert _last_body(mock_urlopen) == {"options": OPTIONS}

        client.update_poll(POST_ID, closes_at="2026-11-02T09:00:00Z")
        assert _last_body(mock_urlopen) == {"closes_at": "2026-11-02T09:00:00Z"}

    @patch("colony_sdk.client.urlopen")
    def test_update_writes_a_datetime_as_iso_8601(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(json.dumps(RESULTS))
        when = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
        _authed_client().update_poll(POST_ID, closes_at=when, options=OPTIONS)
        assert _last_body(mock_urlopen) == {"closes_at": "2026-11-02T09:00:00+00:00", "options": OPTIONS}

    @patch("colony_sdk.client.urlopen")
    def test_update_with_nothing_is_refused_before_the_request(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(ValueError):
            _authed_client().update_poll(POST_ID)
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize("method", ["retract_poll_vote", "close_poll", "disable_og_image"])
    @patch("colony_sdk.client.urlopen")
    def test_a_truncated_id_is_refused_before_the_request(self, mock_urlopen: MagicMock, method: str) -> None:
        with pytest.raises(ValueError):
            getattr(_authed_client(), method)(POST_ID[:8])
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_create_post_sends_the_opt_out_only_when_set(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(json.dumps({"id": POST_ID, "title": "t", "body": "b"}))
        client = _authed_client()

        client.create_post("A title", "A body", og_image_disabled=True)
        assert _last_body(mock_urlopen)["og_image_disabled"] is True

        client.create_post("A title", "A body")
        assert "og_image_disabled" not in _last_body(mock_urlopen)

    @patch("colony_sdk.client.urlopen")
    def test_disable_og_image(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            json.dumps({"post_id": POST_ID, "og_image_disabled": True, "detached": True})
        )
        out = _authed_client().disable_og_image(POST_ID)
        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/posts/{POST_ID}/og-image/disable"
        assert out["detached"] is True


class TestAsync:
    async def test_each_method_reaches_its_route(self) -> None:
        seen: list[tuple[str, str, bytes]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url), request.content))
            if request.url.path.endswith("/og-image/disable"):
                return _json_response({"post_id": POST_ID, "og_image_disabled": True, "detached": False})
            if request.url.path.endswith("/posts"):
                return _json_response({"id": POST_ID, "title": "t", "body": "b"})
            return _json_response(RESULTS)

        client = _make_client(handler)
        await client.retract_poll_vote(POST_ID)
        await client.close_poll(POST_ID)
        await client.update_poll(POST_ID, closes_at="2026-11-02T09:00:00Z")
        await client.disable_og_image(POST_ID)
        await client.create_post("A title", "A body", og_image_disabled=True)

        assert [(m, u.removeprefix(BASE)) for m, u, _ in seen] == [
            ("DELETE", f"/polls/{POST_ID}/vote"),
            ("POST", f"/polls/{POST_ID}/close"),
            ("PATCH", f"/polls/{POST_ID}"),
            ("POST", f"/posts/{POST_ID}/og-image/disable"),
            ("POST", "/posts"),
        ]
        assert json.loads(seen[2][2]) == {"closes_at": "2026-11-02T09:00:00Z"}
        assert json.loads(seen[4][2])["og_image_disabled"] is True

    async def test_update_with_nothing_is_refused_before_the_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no request should be sent")

        with pytest.raises(ValueError):
            await _make_client(handler).update_poll(POST_ID)


class TestMock:
    def test_records_each_call(self) -> None:
        mock = MockColonyClient()
        mock.retract_poll_vote(POST_ID)
        mock.close_poll(POST_ID)
        mock.update_poll(POST_ID, options=OPTIONS)
        mock.disable_og_image(POST_ID)
        mock.create_post("A title", "A body", og_image_disabled=True)
        assert [name for name, _ in mock.calls] == [
            "retract_poll_vote",
            "close_poll",
            "update_poll",
            "disable_og_image",
            "create_post",
        ]
        assert mock.calls[2][1] == {"post_id": POST_ID, "options": OPTIONS}
        assert mock.calls[4][1]["og_image_disabled"] is True

    def test_refuses_an_empty_update_as_the_clients_do(self) -> None:
        with pytest.raises(ValueError):
            MockColonyClient().update_poll(POST_ID)


class TestPollResultsFields:
    def test_the_new_fields_are_read_and_written(self) -> None:
        p = PollResults.from_dict(RESULTS)
        assert (p.voter_count, p.max_choices, p.closes_at) == (3, 2, "2026-11-01T12:00:00Z")
        d = p.to_dict()
        assert (d["voter_count"], d["max_choices"], d["closes_at"]) == (3, 2, "2026-11-01T12:00:00Z")

    def test_an_older_platform_reads_as_no_voters_no_limit_no_close(self) -> None:
        p = PollResults.from_dict({"total_votes": 4, "is_closed": False, "options": []})
        assert (p.voter_count, p.max_choices, p.closes_at) == (0, None, None)
