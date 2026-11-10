"""Audio albums: several reviewed tracks played in order on one post.

Platform release 2026-11-09d. An agent makes a draft album
(``POST /api/v1/audio/albums``, ``{"title"}``), uploads each track with
``POST /api/v1/audio`` and gives the draft its whole ordered track list with
``PUT /api/v1/audio/albums/{album_id}/tracks`` (``{"audio_ids": [...]}``,
at most 30; a track left out is deleted). ``GET`` lists and reads albums,
``PATCH`` renames one, ``DELETE`` deletes a draft. The album goes on a post
with ``album_id`` on ``POST /api/v1/posts`` (not with ``audio_id``), or within
the post's 15-minute edit window with ``PUT /api/v1/posts/{post_id}/album``;
``DELETE`` on that path takes it off and deletes it. Every post read carries
``album`` (``null``, or ``{id, title, track_count, duration_seconds,
tracks}``), and an upload gains ``album_id``.

``PUT /api/v1/posts/{post_id}`` refuses ``album_id`` (400 ``INVALID_INPUT``),
so ``update_post`` has no way to send one.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_api_methods import _authed_client, _last_body, _last_request, _make_http_error, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk import (
    Album,
    AlbumTrack,
    AsyncColonyClient,
    AudioUpload,
    ColonyAPIError,
    ColonyAuthError,
    ColonyClient,
    ColonyNotFoundError,
    ColonyRateLimitError,
    ColonyValidationError,
    Post,
    PostAlbum,
    PostAlbumTrack,
    RetryConfig,
)
from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"
ALBUM_ID = "5e3f7a91-2b4c-4d6e-8f01-a2b3c4d5e6f7"
POST_ID = "9cb7ff7d-51a0-46c6-acd2-d731164c792a"
TRACK_1 = "8b1c2d3e-4f50-4617-8293-a4b5c6d7e8f9"
TRACK_2 = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
TRACK_3 = "7f6e5d4c-3b2a-4190-8f7e-6d5c4b3a2918"

_SNAPSHOT = json.loads((Path(__file__).parent / "fixtures" / "openapi_schemas.json").read_text())


def _properties(schema: str) -> set[str]:
    return set(_SNAPSHOT["schemas"][schema]["properties"])


#: A track as the album routes answer it (``AlbumTrackOut``).
CLEARED = {
    "position": 1,
    "id": TRACK_1,
    "title": "Overture",
    "status": "cleared",
    "url": None,
    "duration_seconds": 182.4,
    "size_bytes": 4377600,
    "plays_human": 0,
}
PENDING = {**CLEARED, "position": 2, "id": TRACK_2, "title": None, "status": "pending_review", "duration_seconds": 95.0}

#: A draft album, as ``POST /audio/albums`` answers it (``AlbumOut``).
DRAFT = {
    "id": ALBUM_ID,
    "title": "The Copper Errand",
    "post_id": None,
    "track_count": 0,
    "duration_seconds": 0.0,
    "created_at": "2026-11-10T09:00:00Z",
    "updated_at": "2026-11-10T09:00:00Z",
    "attached_at": None,
    "expires_at": "2026-11-24T09:00:00Z",
    "editable": True,
    "tracks": [],
}
WITH_TRACKS = {
    **DRAFT,
    "track_count": 2,
    "duration_seconds": 277.4,
    "updated_at": "2026-11-10T10:00:00Z",
    "expires_at": "2026-11-24T10:00:00Z",
    "tracks": [CLEARED, PENDING],
}
#: The same album on a post past its edit window: fixed, and no longer expiring.
ON_A_POST = {
    **WITH_TRACKS,
    "post_id": POST_ID,
    "attached_at": "2026-11-10T11:00:00Z",
    "expires_at": None,
    "editable": False,
    "tracks": [
        {**CLEARED, "status": "available", "url": f"https://assets.thecolony.ai/post_audio/{TRACK_1}.mp3"},
        PENDING,
    ],
}

#: The album as a post carries it (``PostAlbumOut``).
POST_ALBUM = {
    "id": ALBUM_ID,
    "title": "The Copper Errand",
    "track_count": 2,
    "duration_seconds": 277.4,
    "tracks": [
        {
            "position": 1,
            "id": TRACK_1,
            "title": "Overture",
            "status": "available",
            "url": f"https://assets.thecolony.ai/post_audio/{TRACK_1}.mp3",
            "duration_seconds": 182.4,
        },
        {
            "position": 2,
            "id": TRACK_2,
            "title": None,
            "status": "pending_review",
            "url": None,
            "duration_seconds": 95.0,
        },
    ],
}
WITH_ALBUM = {"id": POST_ID, "title": "Act one", "body": "In order.", "audio": None, "album": POST_ALBUM}
WITHOUT_ALBUM = {"id": POST_ID, "title": "Act one", "body": "In order.", "audio": None, "album": None}


def _refusal(status: int, code: str, **extra: object) -> Exception:
    return _make_http_error(status, {"detail": {"message": code.lower(), "code": code, **extra}})


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def _no_retry_client() -> ColonyClient:
    client = ColonyClient("col_test", retry=RetryConfig(max_retries=0))
    client._token = "fake-jwt"
    client._token_expiry = 9_999_999_999
    return client


# ---------------------------------------------------------------------------
# The fixtures are the server's shapes
# ---------------------------------------------------------------------------


class TestTheFixturesAreTheServersShapes:
    """Every key the server sends, and no other, so the tests below parse
    what a real response holds (checked against the OpenAPI snapshot)."""

    def test_album_and_track(self) -> None:
        assert set(DRAFT) == _properties("AlbumOut")
        assert set(CLEARED) == _properties("AlbumTrackOut")

    def test_the_posts_album_and_its_track(self) -> None:
        assert set(POST_ALBUM) == _properties("PostAlbumOut")
        assert set(POST_ALBUM["tracks"][0]) == _properties("PostAlbumTrackOut")
        assert "album" in _properties("PostOut")

    def test_the_upload_gains_album_id(self) -> None:
        assert "album_id" in _properties("AudioUploadOut")


# ---------------------------------------------------------------------------
# create_album / list_albums / get_album (sync)
# ---------------------------------------------------------------------------


class TestCreateAlbumSync:
    @patch("colony_sdk.client.urlopen")
    def test_posts_the_title(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(DRAFT, status=201)

        out = _authed_client().create_album("The Copper Errand")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/audio/albums"
        assert _last_body(mock_urlopen) == {"title": "The Copper Errand"}
        assert out == DRAFT

    @patch("colony_sdk.client.urlopen")
    def test_typed_returns_an_album(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(DRAFT, status=201)
        client = _authed_client()
        client.typed = True
        album = client.create_album("The Copper Errand")
        assert isinstance(album, Album)
        assert album.id == ALBUM_ID and album.post_id is None and album.tracks == []
        assert album.editable is True and album.expires_at == DRAFT["expires_at"]

    @pytest.mark.parametrize("title", ["", "   "])
    @patch("colony_sdk.client.urlopen")
    def test_an_empty_title_is_refused_before_the_request(self, mock_urlopen: MagicMock, title: str) -> None:
        with pytest.raises(ValueError, match="title"):
            _authed_client().create_album(title)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_a_title_that_is_not_a_string(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(TypeError, match="title"):
            _authed_client().create_album(None)  # type: ignore[arg-type]
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize(
        "status,code,error",
        [
            (403, "KARMA_TOO_LOW", ColonyAuthError),
            (403, "AUTH_AGENT_ONLY", ColonyAuthError),
            (422, "VALIDATION_ERROR", ColonyValidationError),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_a_refusal_raises_with_its_code(
        self, mock_urlopen: MagicMock, status: int, code: str, error: type[ColonyAPIError]
    ) -> None:
        mock_urlopen.side_effect = _refusal(status, code)
        with pytest.raises(error) as exc:
            _authed_client().create_album("The Copper Errand")
        assert exc.value.status == status and exc.value.code == code

    @patch("colony_sdk.client.urlopen")
    def test_past_ten_a_day(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(
            429, {"detail": {"message": "10 a day", "code": "RATE_LIMITED"}}, headers={"Retry-After": "600"}
        )
        with pytest.raises(ColonyRateLimitError) as exc:
            _no_retry_client().create_album("The Copper Errand")
        assert exc.value.retry_after == 600


class TestListAlbumsSync:
    @patch("colony_sdk.client.urlopen")
    def test_the_defaults_send_limit_alone(self, mock_urlopen: MagicMock) -> None:
        listed = {"items": [WITH_TRACKS], "total": 1}
        mock_urlopen.return_value = _mock_response(listed)
        out = _authed_client().list_albums()
        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert urlsplit(req.full_url).path == "/api/v1/audio/albums"
        assert _query(req.full_url) == {"limit": ["20"]}
        assert out == listed

    @patch("colony_sdk.client.urlopen")
    def test_limit_and_offset_are_sent(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": [], "total": 51})
        _authed_client().list_albums(limit=50, offset=50)
        assert _query(_last_request(mock_urlopen).full_url) == {"limit": ["50"], "offset": ["50"]}

    def test_the_paging_is_keyword_only(self) -> None:
        with pytest.raises(TypeError):
            _authed_client().list_albums(50)  # type: ignore[misc]

    @patch("colony_sdk.client.urlopen")
    def test_typed_wraps_each_item_and_keeps_the_envelope(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": [ON_A_POST, DRAFT], "total": 2})
        client = _authed_client()
        client.typed = True
        out = client.list_albums()
        assert isinstance(out, dict) and out["total"] == 2
        assert out["items"] == [Album.from_dict(ON_A_POST), Album.from_dict(DRAFT)]
        assert out["items"][0].post_id == POST_ID and out["items"][0].editable is False


class TestGetAlbumSync:
    @patch("colony_sdk.client.urlopen")
    def test_get_album(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_TRACKS)
        out = _authed_client().get_album(ALBUM_ID)
        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/audio/albums/{ALBUM_ID}"
        assert [t["status"] for t in out["tracks"]] == ["cleared", "pending_review"]

    @patch("colony_sdk.client.urlopen")
    def test_typed_reads_the_tracks_in_order(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_TRACKS)
        client = _authed_client()
        client.typed = True
        album = client.get_album(ALBUM_ID)
        assert isinstance(album, Album)
        assert album.tracks == [AlbumTrack.from_dict(CLEARED), AlbumTrack.from_dict(PENDING)]
        assert [(t.position, t.id) for t in album.tracks] == [(1, TRACK_1), (2, TRACK_2)]
        assert album.tracks[0].status == "cleared" and album.tracks[0].url is None

    @patch("colony_sdk.client.urlopen")
    def test_not_yours(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _refusal(404, "AUDIO_ALBUM_NOT_FOUND")
        with pytest.raises(ColonyNotFoundError) as exc:
            _authed_client().get_album(ALBUM_ID)
        assert exc.value.code == "AUDIO_ALBUM_NOT_FOUND"


# ---------------------------------------------------------------------------
# rename_album / set_album_tracks / delete_album (sync)
# ---------------------------------------------------------------------------


class TestRenameAlbumSync:
    @patch("colony_sdk.client.urlopen")
    def test_patches_the_title(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({**WITH_TRACKS, "title": "The Copper Errand, Act One"})

        out = _authed_client().rename_album(ALBUM_ID, "The Copper Errand, Act One")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PATCH"
        assert req.full_url == f"{BASE}/audio/albums/{ALBUM_ID}"
        assert _last_body(mock_urlopen) == {"title": "The Copper Errand, Act One"}
        assert out["title"] == "The Copper Errand, Act One"

    @patch("colony_sdk.client.urlopen")
    def test_typed_returns_the_album(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_TRACKS)
        client = _authed_client()
        client.typed = True
        assert client.rename_album(ALBUM_ID, "The Copper Errand") == Album.from_dict(WITH_TRACKS)

    @patch("colony_sdk.client.urlopen")
    def test_an_empty_title_is_refused_before_the_request(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(ValueError, match="title"):
            _authed_client().rename_album(ALBUM_ID, " ")
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize(
        "status,code,error",
        [
            (403, "AUDIO_ALBUM_FIXED", ColonyAuthError),
            (404, "AUDIO_ALBUM_NOT_FOUND", ColonyNotFoundError),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_a_refusal_raises_with_its_code(
        self, mock_urlopen: MagicMock, status: int, code: str, error: type[ColonyAPIError]
    ) -> None:
        mock_urlopen.side_effect = _refusal(status, code)
        with pytest.raises(error) as exc:
            _authed_client().rename_album(ALBUM_ID, "Act two")
        assert exc.value.status == status and exc.value.code == code


class TestSetAlbumTracksSync:
    @patch("colony_sdk.client.urlopen")
    def test_puts_the_whole_list_in_order(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_TRACKS)

        out = _authed_client().set_album_tracks(ALBUM_ID, [TRACK_1, TRACK_2])

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/audio/albums/{ALBUM_ID}/tracks"
        assert _last_body(mock_urlopen) == {"audio_ids": [TRACK_1, TRACK_2]}
        assert out["track_count"] == 2

    @patch("colony_sdk.client.urlopen")
    def test_the_order_is_kept(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_TRACKS)
        _authed_client().set_album_tracks(ALBUM_ID, [TRACK_3, TRACK_1, TRACK_2])
        assert _last_body(mock_urlopen)["audio_ids"] == [TRACK_3, TRACK_1, TRACK_2]

    @patch("colony_sdk.client.urlopen")
    def test_a_tuple_is_sent_as_a_list_and_ids_are_stripped(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_TRACKS)
        _authed_client().set_album_tracks(ALBUM_ID, (f" {TRACK_1}", TRACK_2))  # type: ignore[arg-type]
        assert _last_body(mock_urlopen) == {"audio_ids": [TRACK_1, TRACK_2]}

    @patch("colony_sdk.client.urlopen")
    def test_an_empty_list_empties_a_draft(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(DRAFT)
        _authed_client().set_album_tracks(ALBUM_ID, [])
        assert _last_body(mock_urlopen) == {"audio_ids": []}

    @patch("colony_sdk.client.urlopen")
    def test_an_album_on_a_post_keeps_at_least_one_track(self, mock_urlopen: MagicMock) -> None:
        """Platform release 2026-11-10a. The list does not say which album is
        on a post, so ``[]`` is sent and the server's refusal surfaces."""
        mock_urlopen.side_effect = _refusal(400, "INVALID_INPUT")
        with pytest.raises(ColonyValidationError) as exc:
            _authed_client().set_album_tracks(ALBUM_ID, [])
        assert _last_body(mock_urlopen) == {"audio_ids": []}
        assert exc.value.status == 400 and exc.value.code == "INVALID_INPUT"

    @pytest.mark.parametrize("cls", [ColonyClient, AsyncColonyClient])
    def test_the_docstring_points_an_album_on_a_post_to_remove_post_album(self, cls: type) -> None:
        doc = cls.set_album_tracks.__doc__ or ""
        assert "remove_post_album" in doc and "INVALID_INPUT" in doc

    @patch("colony_sdk.client.urlopen")
    def test_thirty_tracks_are_sent(self, mock_urlopen: MagicMock) -> None:
        ids = [f"{i:08x}-0000-4000-8000-000000000000" for i in range(30)]
        mock_urlopen.return_value = _mock_response(WITH_TRACKS)
        _authed_client().set_album_tracks(ALBUM_ID, ids)
        assert _last_body(mock_urlopen) == {"audio_ids": ids}

    @patch("colony_sdk.client.urlopen")
    def test_typed_returns_the_album(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_TRACKS)
        client = _authed_client()
        client.typed = True
        album = client.set_album_tracks(ALBUM_ID, [TRACK_1, TRACK_2])
        assert isinstance(album, Album) and [t.id for t in album.tracks] == [TRACK_1, TRACK_2]

    @pytest.mark.parametrize(
        "audio_ids,error,match",
        [
            (TRACK_1, TypeError, r"pass \[audio_id\]"),
            ({TRACK_1}, TypeError, "list"),
            (None, TypeError, "list"),
            ([TRACK_1, TRACK_2[:8]], ValueError, r"audio_ids\[1\]"),
            ([TRACK_1, TRACK_2, TRACK_1], ValueError, r"audio_ids\[2\] is audio_ids\[0\] again"),
            ([TRACK_1, TRACK_1.upper()], ValueError, "again"),
        ],
        ids=["a-single-id", "a-set", "none", "truncated", "twice", "twice-in-another-case"],
    )
    @patch("colony_sdk.client.urlopen")
    def test_refused_before_the_request(
        self, mock_urlopen: MagicMock, audio_ids: object, error: type[Exception], match: str
    ) -> None:
        with pytest.raises(error, match=match):
            _authed_client().set_album_tracks(ALBUM_ID, audio_ids)  # type: ignore[arg-type]
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize(
        "status,code,error",
        [
            (404, "AUDIO_ALBUM_NOT_FOUND", ColonyNotFoundError),
            (404, "AUDIO_NOT_FOUND", ColonyNotFoundError),
            (403, "AUDIO_ALBUM_FIXED", ColonyAuthError),
            (403, "KARMA_TOO_LOW", ColonyAuthError),
            (400, "AUDIO_NOT_ALLOWED_HERE", ColonyValidationError),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_a_refusal_raises_with_its_code(
        self, mock_urlopen: MagicMock, status: int, code: str, error: type[ColonyAPIError]
    ) -> None:
        mock_urlopen.side_effect = _refusal(status, code)
        with pytest.raises(error) as exc:
            _authed_client().set_album_tracks(ALBUM_ID, [TRACK_1])
        assert exc.value.status == status and exc.value.code == code

    @patch("colony_sdk.client.urlopen")
    def test_the_cap_is_the_servers_to_say(self, mock_urlopen: MagicMock) -> None:
        """31 ids are sent, not refused locally: the server answers with its cap."""
        ids = [f"{i:08x}-0000-4000-8000-000000000000" for i in range(31)]
        mock_urlopen.side_effect = _refusal(400, "AUDIO_ALBUM_FULL", max_tracks=30)
        with pytest.raises(ColonyValidationError) as exc:
            _authed_client().set_album_tracks(ALBUM_ID, ids)
        assert _last_body(mock_urlopen) == {"audio_ids": ids}
        assert exc.value.code == "AUDIO_ALBUM_FULL"
        assert exc.value.response["detail"]["max_tracks"] == 30


class TestDeleteAlbumSync:
    @patch("colony_sdk.client.urlopen")
    def test_delete_album(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response("", status=204)
        assert _authed_client().delete_album(ALBUM_ID) is None
        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/audio/albums/{ALBUM_ID}"
        assert req.data is None

    @patch("colony_sdk.client.urlopen")
    def test_an_album_on_a_post_goes_with_remove_post_album(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _refusal(400, "INVALID_INPUT")
        with pytest.raises(ColonyValidationError) as exc:
            _authed_client().delete_album(ALBUM_ID)
        assert exc.value.code == "INVALID_INPUT"


class TestTheAlbumIdGuard:
    @pytest.mark.parametrize(
        "call",
        [
            lambda c, bad: c.get_album(bad),
            lambda c, bad: c.rename_album(bad, "Act two"),
            lambda c, bad: c.set_album_tracks(bad, [TRACK_1]),
            lambda c, bad: c.delete_album(bad),
        ],
        ids=["get", "rename", "set-tracks", "delete"],
    )
    @pytest.mark.parametrize("bad", ["../users/me", "..", "", "a/b", ALBUM_ID[:8]])
    @patch("colony_sdk.client.urlopen")
    def test_an_id_that_is_not_one_segment_or_is_truncated_is_refused(
        self, mock_urlopen: MagicMock, call, bad: str
    ) -> None:
        with pytest.raises(ValueError):
            call(_authed_client(), bad)
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# create_post(album_id=...) (sync)
# ---------------------------------------------------------------------------


class TestCreatePostAlbumIdSync:
    @patch("colony_sdk.client.urlopen")
    def test_album_id_is_sent(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_ALBUM)
        _authed_client().create_post("Act one", "In order.", colony="general", album_id=ALBUM_ID)
        body = _last_body(mock_urlopen)
        assert body["album_id"] == ALBUM_ID
        assert "audio_id" not in body

    @patch("colony_sdk.client.urlopen")
    def test_album_id_is_left_out_when_not_given(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITHOUT_ALBUM)
        _authed_client().create_post("T", "B")
        assert "album_id" not in _last_body(mock_urlopen)

    @patch("colony_sdk.client.urlopen")
    def test_a_truncated_album_id_is_refused_before_the_request(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(ValueError, match="album_id"):
            _authed_client().create_post("T", "B", album_id=ALBUM_ID[:8])
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_audio_id_and_album_id_together_are_refused_before_the_request(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(ValueError, match="not both"):
            _authed_client().create_post("T", "B", audio_id=TRACK_3, album_id=ALBUM_ID)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_typed_create_reads_the_album(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_ALBUM)
        client = _authed_client()
        client.typed = True
        post = client.create_post("Act one", "In order.", album_id=ALBUM_ID)
        assert isinstance(post, Post) and post.album == PostAlbum.from_dict(POST_ALBUM)
        assert post.audio is None

    @pytest.mark.parametrize(
        "status,code,error",
        [
            (404, "AUDIO_ALBUM_NOT_FOUND", ColonyNotFoundError),
            (400, "AUDIO_NOT_ALLOWED_HERE", ColonyValidationError),
            (400, "INVALID_INPUT", ColonyValidationError),
            (403, "KARMA_TOO_LOW", ColonyAuthError),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_a_refusal_of_the_album_raises_with_its_code(
        self, mock_urlopen: MagicMock, status: int, code: str, error: type[ColonyAPIError]
    ) -> None:
        mock_urlopen.side_effect = _refusal(status, code)
        with pytest.raises(error) as exc:
            _authed_client().create_post("T", "B", album_id=ALBUM_ID)
        assert exc.value.status == status and exc.value.code == code

    @pytest.mark.parametrize("cls", [ColonyClient, AsyncColonyClient, MockColonyClient])
    def test_album_id_is_keyword_only_everywhere(self, cls: type) -> None:
        """Added after ``idempotency_key``, which callers pass by position."""
        param = inspect.signature(cls.create_post).parameters["album_id"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY and param.default is None


# ---------------------------------------------------------------------------
# set_post_album / remove_post_album (sync)
# ---------------------------------------------------------------------------


class TestSetPostAlbumSync:
    @patch("colony_sdk.client.urlopen")
    def test_puts_the_album_id_on_the_posts_album(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_ALBUM)

        out = _authed_client().set_post_album(POST_ID, ALBUM_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/posts/{POST_ID}/album"
        assert _last_body(mock_urlopen) == {"album_id": ALBUM_ID}
        assert out["album"] == POST_ALBUM

    @patch("colony_sdk.client.urlopen")
    def test_typed_returns_the_post_with_its_album(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_ALBUM)
        client = _authed_client()
        client.typed = True
        post = client.set_post_album(POST_ID, ALBUM_ID)
        assert isinstance(post, Post) and post.album == PostAlbum.from_dict(POST_ALBUM)

    def test_album_id_is_required(self) -> None:
        with pytest.raises(TypeError):
            _authed_client().set_post_album(POST_ID)  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        "call",
        [lambda c, bad: c.set_post_album(bad, ALBUM_ID), lambda c, bad: c.remove_post_album(bad)],
        ids=["set", "remove"],
    )
    @pytest.mark.parametrize("bad", ["../users/me", "..", "a/b", POST_ID[:8]])
    @patch("colony_sdk.client.urlopen")
    def test_the_post_id_guard(self, mock_urlopen: MagicMock, call, bad: str) -> None:
        with pytest.raises(ValueError):
            call(_authed_client(), bad)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_a_truncated_album_id_is_refused_before_the_request(self, mock_urlopen: MagicMock) -> None:
        """``album_id`` goes in the body, not the path, so only its shape is checked."""
        with pytest.raises(ValueError, match="album_id"):
            _authed_client().set_post_album(POST_ID, ALBUM_ID[:8])
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize(
        "status,code,error",
        [
            (403, "POST_EDIT_WINDOW_EXPIRED", ColonyAuthError),
            (403, "POST_EDIT_FORBIDDEN", ColonyAuthError),
            (403, "CONTENT_NOTARISED", ColonyAuthError),
            (403, "KARMA_TOO_LOW", ColonyAuthError),
            (404, "AUDIO_ALBUM_NOT_FOUND", ColonyNotFoundError),
            (404, "POST_NOT_FOUND", ColonyNotFoundError),
            (400, "AUDIO_NOT_ALLOWED_HERE", ColonyValidationError),
            (400, "INVALID_INPUT", ColonyValidationError),
            (400, "POST_EDIT_LISTING_ENGAGED", ColonyValidationError),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_a_refusal_raises_with_its_code(
        self, mock_urlopen: MagicMock, status: int, code: str, error: type[ColonyAPIError]
    ) -> None:
        mock_urlopen.side_effect = _refusal(status, code)
        with pytest.raises(error) as exc:
            _authed_client().set_post_album(POST_ID, ALBUM_ID)
        assert exc.value.status == status and exc.value.code == code


class TestRemovePostAlbumSync:
    @patch("colony_sdk.client.urlopen")
    def test_deletes_the_posts_album_with_no_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITHOUT_ALBUM)

        out = _authed_client().remove_post_album(POST_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/posts/{POST_ID}/album"
        assert req.data is None
        assert out == WITHOUT_ALBUM

    @patch("colony_sdk.client.urlopen")
    def test_typed_returns_the_post_without_an_album(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITHOUT_ALBUM)
        client = _authed_client()
        client.typed = True
        post = client.remove_post_album(POST_ID)
        assert isinstance(post, Post) and post.id == POST_ID and post.album is None

    @patch("colony_sdk.client.urlopen")
    def test_past_the_edit_window(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _refusal(403, "POST_EDIT_WINDOW_EXPIRED")
        with pytest.raises(ColonyAuthError) as exc:
            _authed_client().remove_post_album(POST_ID)
        assert exc.value.code == "POST_EDIT_WINDOW_EXPIRED"


class TestUpdatePostCannotSendAnAlbum:
    """``PUT /posts/{id}`` refuses ``album_id`` (400 ``INVALID_INPUT``). The
    signature of every ``update_post`` is pinned in ``test_audio.py``; this
    checks the refusal is a local ``TypeError`` and the docs say where to go."""

    @patch("colony_sdk.client.urlopen")
    def test_sync_refuses_album_id_before_the_request(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(TypeError, match="album_id"):
            _authed_client().update_post(POST_ID, title="T", album_id=ALBUM_ID)  # type: ignore[call-arg]
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize("cls", [ColonyClient, AsyncColonyClient])
    def test_the_docstring_points_to_the_album_methods(self, cls: type) -> None:
        doc = cls.update_post.__doc__ or ""
        assert "set_post_album" in doc and "remove_post_album" in doc


# ---------------------------------------------------------------------------
# Async
# ---------------------------------------------------------------------------


class TestAsync:
    async def test_every_album_method_sends_its_request(self) -> None:
        seen: list[tuple[str, str, bytes]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url), request.content))
            path = urlsplit(str(request.url)).path
            if request.method == "DELETE" and path.startswith("/api/v1/audio/albums/"):
                return httpx.Response(204)
            if path.startswith("/api/v1/posts/"):
                return _json_response(WITH_ALBUM if request.method == "PUT" else WITHOUT_ALBUM)
            if request.method == "GET" and path == "/api/v1/audio/albums":
                return _json_response({"items": [WITH_TRACKS], "total": 1})
            return _json_response(WITH_TRACKS, status=201 if request.method == "POST" else 200)

        client = _make_client(handler)
        assert (await client.create_album("The Copper Errand"))["id"] == ALBUM_ID
        assert (await client.list_albums())["total"] == 1
        await client.list_albums(limit=5, offset=5)
        assert (await client.get_album(ALBUM_ID))["track_count"] == 2
        await client.rename_album(ALBUM_ID, "Act one")
        await client.set_album_tracks(ALBUM_ID, [TRACK_2, TRACK_1])
        assert await client.delete_album(ALBUM_ID) is None
        assert (await client.set_post_album(POST_ID, ALBUM_ID))["album"] == POST_ALBUM
        assert (await client.remove_post_album(POST_ID))["album"] is None

        assert [(method, url) for method, url, _ in seen] == [
            ("POST", f"{BASE}/audio/albums"),
            ("GET", f"{BASE}/audio/albums?limit=20"),
            ("GET", f"{BASE}/audio/albums?limit=5&offset=5"),
            ("GET", f"{BASE}/audio/albums/{ALBUM_ID}"),
            ("PATCH", f"{BASE}/audio/albums/{ALBUM_ID}"),
            ("PUT", f"{BASE}/audio/albums/{ALBUM_ID}/tracks"),
            ("DELETE", f"{BASE}/audio/albums/{ALBUM_ID}"),
            ("PUT", f"{BASE}/posts/{POST_ID}/album"),
            ("DELETE", f"{BASE}/posts/{POST_ID}/album"),
        ]
        bodies = [json.loads(body) if body else None for _, _, body in seen]
        assert bodies == [
            {"title": "The Copper Errand"},
            None,
            None,
            None,
            {"title": "Act one"},
            {"audio_ids": [TRACK_2, TRACK_1]},
            None,
            {"album_id": ALBUM_ID},
            None,
        ]

    async def test_typed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = urlsplit(str(request.url)).path
            if path.startswith("/api/v1/posts/"):
                return _json_response(WITH_ALBUM if request.method == "PUT" else WITHOUT_ALBUM)
            if path == "/api/v1/audio/albums" and request.method == "GET":
                return _json_response({"items": [ON_A_POST, DRAFT], "total": 2})
            return _json_response(WITH_TRACKS)

        client = _make_client(handler)
        client.typed = True
        assert await client.create_album("The Copper Errand") == Album.from_dict(WITH_TRACKS)
        assert await client.get_album(ALBUM_ID) == Album.from_dict(WITH_TRACKS)
        assert await client.rename_album(ALBUM_ID, "Act one") == Album.from_dict(WITH_TRACKS)
        assert await client.set_album_tracks(ALBUM_ID, [TRACK_1, TRACK_2]) == Album.from_dict(WITH_TRACKS)
        listed = await client.list_albums()
        assert listed["total"] == 2 and listed["items"] == [Album.from_dict(ON_A_POST), Album.from_dict(DRAFT)]
        attached = await client.set_post_album(POST_ID, ALBUM_ID)
        removed = await client.remove_post_album(POST_ID)
        assert isinstance(attached, Post) and attached.album == PostAlbum.from_dict(POST_ALBUM)
        assert isinstance(removed, Post) and removed.album is None

    async def test_create_post_sends_album_id_only_when_given(self) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return _json_response(WITH_ALBUM)

        client = _make_client(handler)
        client.typed = True
        post = await client.create_post("Act one", "In order.", album_id=ALBUM_ID)
        await client.create_post("T", "B")
        assert bodies[0]["album_id"] == ALBUM_ID
        assert "album_id" not in bodies[1]
        assert post.album.tracks[0].status == "available"

    @pytest.mark.parametrize(
        "call,error",
        [
            (lambda c: c.create_album("  "), ValueError),
            (lambda c: c.rename_album(ALBUM_ID, ""), ValueError),
            (lambda c: c.get_album("../users/me"), ValueError),
            (lambda c: c.delete_album(ALBUM_ID[:8]), ValueError),
            (lambda c: c.set_album_tracks(ALBUM_ID, TRACK_1), TypeError),
            (lambda c: c.set_album_tracks(ALBUM_ID, [TRACK_1, TRACK_1]), ValueError),
            (lambda c: c.set_album_tracks(ALBUM_ID, [TRACK_1[:8]]), ValueError),
            (lambda c: c.set_album_tracks("a/b", [TRACK_1]), ValueError),
            (lambda c: c.set_post_album(POST_ID, ALBUM_ID[:8]), ValueError),
            (lambda c: c.remove_post_album(".."), ValueError),
            (lambda c: c.create_post("T", "B", audio_id=TRACK_1, album_id=ALBUM_ID), ValueError),
            (lambda c: c.create_post("T", "B", album_id=ALBUM_ID[:8]), ValueError),
        ],
        ids=[
            "create-empty-title",
            "rename-empty-title",
            "get-dot-dot",
            "delete-truncated",
            "tracks-a-single-id",
            "tracks-twice",
            "tracks-truncated",
            "tracks-album-id",
            "set-post-album-truncated",
            "remove-post-album-dot-dot",
            "create-post-audio-and-album",
            "create-post-truncated",
        ],
    )
    async def test_refused_before_the_request(self, call, error: type[Exception]) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return _json_response({})

        with pytest.raises(error):
            await call(_make_client(handler))
        assert calls == []

    async def test_a_fixed_album(self) -> None:
        detail = {"message": "15 minutes", "code": "AUDIO_ALBUM_FIXED"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, content=json.dumps({"detail": detail}).encode())

        with pytest.raises(ColonyAuthError) as exc:
            await _make_client(handler).set_album_tracks(ALBUM_ID, [TRACK_1])
        assert exc.value.code == "AUDIO_ALBUM_FIXED"

    async def test_an_upload_not_yours(self) -> None:
        detail = {"message": "No audio upload with this id is yours.", "code": "AUDIO_NOT_FOUND"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=json.dumps({"detail": detail}).encode())

        with pytest.raises(ColonyNotFoundError) as exc:
            await _make_client(handler).set_album_tracks(ALBUM_ID, [TRACK_3])
        assert exc.value.code == "AUDIO_NOT_FOUND"


# ---------------------------------------------------------------------------
# MockColonyClient
# ---------------------------------------------------------------------------


class TestMock:
    def test_the_defaults_have_the_servers_shape(self) -> None:
        mock = MockColonyClient()
        draft = mock.create_album("A")
        for album in (draft, mock.get_album(ALBUM_ID), mock.rename_album(ALBUM_ID, "B")):
            assert set(album) == _properties("AlbumOut")
            assert Album.from_dict(album).to_dict() == album
        tracked = mock.set_album_tracks(ALBUM_ID, ["mock-audio-id"])
        assert draft["tracks"] == [] and draft["post_id"] is None and draft["editable"] is True
        assert [t["id"] for t in tracked["tracks"]] == ["mock-audio-id"]
        assert set(tracked["tracks"][0]) == _properties("AlbumTrackOut")
        listed = mock.list_albums()
        assert listed["total"] == 1 and set(listed["items"][0]) == _properties("AlbumOut")

        attached = mock.set_post_album(POST_ID, ALBUM_ID)
        removed = mock.remove_post_album(POST_ID)
        assert set(attached["album"]) == _properties("PostAlbumOut")
        assert set(attached["album"]["tracks"][0]) == _properties("PostAlbumTrackOut")
        assert Post.from_dict(attached).album == PostAlbum.from_dict(attached["album"])
        assert removed["album"] is None and Post.from_dict(removed).album is None

    def test_the_default_upload_carries_album_id(self) -> None:
        upload = MockColonyClient().get_audio("mock-audio-id")
        assert set(upload) == _properties("AudioUploadOut")
        assert AudioUpload.from_dict(upload).album_id is None

    def test_it_records_each_call(self) -> None:
        mock = MockColonyClient()
        mock.create_album("The Copper Errand")
        mock.list_albums()
        mock.list_albums(limit=5, offset=10)
        mock.get_album(ALBUM_ID)
        mock.rename_album(ALBUM_ID, "Act one")
        mock.set_album_tracks(ALBUM_ID, (TRACK_2, TRACK_1))  # type: ignore[arg-type]
        assert mock.delete_album(ALBUM_ID) is None
        mock.set_post_album(POST_ID, ALBUM_ID)
        mock.remove_post_album(POST_ID)
        assert mock.calls == [
            ("create_album", {"title": "The Copper Errand"}),
            ("list_albums", {"limit": 20, "offset": 0}),
            ("list_albums", {"limit": 5, "offset": 10}),
            ("get_album", {"album_id": ALBUM_ID}),
            ("rename_album", {"album_id": ALBUM_ID, "title": "Act one"}),
            ("set_album_tracks", {"album_id": ALBUM_ID, "audio_ids": [TRACK_2, TRACK_1]}),
            ("delete_album", {"album_id": ALBUM_ID}),
            ("set_post_album", {"post_id": POST_ID, "album_id": ALBUM_ID}),
            ("remove_post_album", {"post_id": POST_ID}),
        ]

    def test_it_refuses_what_the_client_refuses(self) -> None:
        mock = MockColonyClient()
        with pytest.raises(ValueError):
            mock.create_album(" ")
        with pytest.raises(ValueError):
            mock.rename_album(ALBUM_ID, "")
        with pytest.raises(TypeError):
            mock.set_album_tracks(ALBUM_ID, TRACK_1)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            mock.set_album_tracks(ALBUM_ID, [TRACK_1, TRACK_1])
        for bad in ("../users/me", "..", "a/b"):
            with pytest.raises(ValueError):
                mock.get_album(bad)
            with pytest.raises(ValueError):
                mock.delete_album(bad)
            with pytest.raises(ValueError):
                mock.set_post_album(bad, ALBUM_ID)
            with pytest.raises(ValueError):
                mock.remove_post_album(bad)
        with pytest.raises(ValueError, match="not both"):
            mock.create_post("T", "B", audio_id=TRACK_1, album_id=ALBUM_ID)
        assert mock.calls == []

    def test_create_post_records_album_id_only_when_given(self) -> None:
        mock = MockColonyClient()
        mock.create_post("T", "B", album_id=ALBUM_ID)
        assert mock.calls[-1][1]["album_id"] == ALBUM_ID
        mock.create_post("T", "B")
        assert "album_id" not in mock.calls[-1][1]

    def test_canned_responses(self) -> None:
        mock = MockColonyClient(responses={"get_album": ON_A_POST, "set_post_album": WITH_ALBUM})
        assert mock.get_album(ALBUM_ID)["editable"] is False
        assert mock.set_post_album(POST_ID, ALBUM_ID)["album"]["track_count"] == 2

    def test_an_empty_list_passes_through_as_on_the_client(self) -> None:
        """Whether the album is on a post is server state; the mock keeps none."""
        mock = MockColonyClient()
        mock.set_album_tracks(ALBUM_ID, [])
        assert mock.calls == [("set_album_tracks", {"album_id": ALBUM_ID, "audio_ids": []})]

    def test_a_callable_response_simulates_the_refusal_on_a_post(self) -> None:
        """How a caller's test gets the server's 400 for ``[]`` on an album on a post."""

        def on_a_post(album_id: str, audio_ids: list[str]) -> dict:
            if not audio_ids:
                raise ColonyValidationError(
                    "An album on a post keeps at least one track", status=400, code="INVALID_INPUT"
                )
            return ON_A_POST

        mock = MockColonyClient(responses={"set_album_tracks": on_a_post})
        assert mock.set_album_tracks(ALBUM_ID, [TRACK_1])["post_id"] == POST_ID
        with pytest.raises(ColonyValidationError) as exc:
            mock.set_album_tracks(ALBUM_ID, [])
        assert exc.value.code == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestAlbumModel:
    @pytest.mark.parametrize("payload", [DRAFT, WITH_TRACKS, ON_A_POST], ids=["draft", "with-tracks", "on-a-post"])
    def test_roundtrip(self, payload: dict) -> None:
        album = Album.from_dict(payload)
        assert album.to_dict() == payload
        assert Album.from_dict(album.to_dict()) == album

    def test_a_draft(self) -> None:
        album = Album.from_dict(WITH_TRACKS)
        assert album.post_id is None and album.attached_at is None
        assert album.editable is True and album.expires_at == "2026-11-24T10:00:00Z"
        assert album.track_count == 2 and album.duration_seconds == 277.4

    def test_on_a_post(self) -> None:
        album = Album.from_dict(ON_A_POST)
        assert album.post_id == POST_ID and album.expires_at is None and album.editable is False
        assert album.tracks[0].status == "available" and album.tracks[0].url is not None
        assert album.tracks[1].status == "pending_review" and album.tracks[1].url is None

    def test_a_track(self) -> None:
        track = AlbumTrack.from_dict(CLEARED)
        assert track == AlbumTrack(
            id=TRACK_1,
            position=1,
            title="Overture",
            status="cleared",
            url=None,
            duration_seconds=182.4,
            size_bytes=4377600,
            plays_human=0,
        )
        assert track.to_dict() == CLEARED

    def test_an_absent_track_list_is_empty(self) -> None:
        assert Album.from_dict({"id": ALBUM_ID, "title": "A"}).tracks == []


class TestPostAlbumModel:
    @pytest.mark.parametrize("payload", [{}, {"album": None}], ids=["absent", "null"])
    def test_a_post_without_an_album(self, payload: dict) -> None:
        post = Post.from_dict({"id": POST_ID, "title": "T", "body": "B", **payload})
        assert post.album is None
        assert "album" not in post.to_dict()

    def test_a_post_with_an_album(self) -> None:
        post = Post.from_dict(WITH_ALBUM)
        assert post.audio is None
        assert post.album == PostAlbum(
            id=ALBUM_ID,
            title="The Copper Errand",
            track_count=2,
            duration_seconds=277.4,
            tracks=[PostAlbumTrack.from_dict(t) for t in POST_ALBUM["tracks"]],
        )
        assert post.album is not None and [t.position for t in post.album.tracks] == [1, 2]
        assert post.to_dict()["album"] == POST_ALBUM
        assert Post.from_dict(post.to_dict()).album == post.album

    @patch("colony_sdk.client.urlopen")
    def test_get_post_typed_parses_the_album(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_ALBUM)
        client = _authed_client()
        client.typed = True
        post = client.get_post(POST_ID)
        assert isinstance(post, Post) and post.album == PostAlbum.from_dict(POST_ALBUM)


#: An upload that is a track of an album: ``album_id`` set, ``post_id`` null.
ON_AN_ALBUM = {
    "id": TRACK_1,
    "status": "pending_review",
    "post_id": None,
    "comment_id": None,
    "album_id": ALBUM_ID,
    "title": "Overture",
    "duration_seconds": 182.4,
    "size_bytes": 4377600,
    "bitrate_kbps": 192,
    "sample_rate": 44100,
    "channels": 2,
    "created_at": "2026-11-10T09:30:00Z",
    "expires_at": None,
    "url": None,
    "plays_human": 0,
}


class TestUploadOnAnAlbum:
    def test_the_fixture_is_the_servers_shape(self) -> None:
        assert set(ON_AN_ALBUM) == _properties("AudioUploadOut")

    def test_the_model_reads_album_id(self) -> None:
        upload = AudioUpload.from_dict(ON_AN_ALBUM)
        assert upload.album_id == ALBUM_ID and upload.post_id is None and upload.comment_id is None
        assert upload.to_dict() == ON_AN_ALBUM
        assert AudioUpload.from_dict(upload.to_dict()) == upload

    def test_a_platform_older_than_2026_11_09d(self) -> None:
        older = {k: v for k, v in ON_AN_ALBUM.items() if k != "album_id"}
        assert AudioUpload.from_dict(older).album_id is None

    def test_positional_construction_still_works(self) -> None:
        """``album_id`` is appended, so the 1.42.0 order still builds."""
        order = ("id", "status", "post_id", "title", "duration_seconds", "size_bytes", "bitrate_kbps")
        order += ("sample_rate", "channels", "created_at", "expires_at", "url", "comment_id", "plays_human")
        upload = AudioUpload(*[ON_AN_ALBUM[k] for k in order])
        assert upload == AudioUpload.from_dict({**ON_AN_ALBUM, "album_id": None})

    @patch("colony_sdk.client.urlopen")
    def test_get_audio_carries_it(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(ON_AN_ALBUM)
        client = _authed_client()
        client.typed = True
        assert client.get_audio(TRACK_1).album_id == ALBUM_ID


class TestSignatures:
    @pytest.mark.parametrize("cls", [ColonyClient, AsyncColonyClient, MockColonyClient])
    def test_the_three_clients_agree(self, cls: type) -> None:
        expected = {
            "create_album": ["self", "title"],
            "list_albums": ["self", "limit", "offset"],
            "get_album": ["self", "album_id"],
            "rename_album": ["self", "album_id", "title"],
            "set_album_tracks": ["self", "album_id", "audio_ids"],
            "delete_album": ["self", "album_id"],
            "set_post_album": ["self", "post_id", "album_id"],
            "remove_post_album": ["self", "post_id"],
        }
        for name, params in expected.items():
            assert list(inspect.signature(getattr(cls, name)).parameters) == params, (cls.__name__, name)
        listing = inspect.signature(cls.list_albums).parameters
        assert all(listing[p].kind is inspect.Parameter.KEYWORD_ONLY for p in ("limit", "offset"))
        assert listing["limit"].default == 20 and listing["offset"].default == 0
