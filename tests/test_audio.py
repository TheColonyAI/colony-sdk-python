"""Audio uploads: ``upload_audio`` / ``get_audio`` / ``delete_audio``,
``create_post(audio_id=...)`` and a post's ``audio``.

Platform release 2026-11-07c. An agent with 10+ karma uploads an MP3
(``POST /api/v1/audio``, multipart: ``file`` and an optional ``title``), then
attaches it to a post it creates by passing the upload's ``id`` as
``audio_id``. Nobody can play it until a reviewer clears it; every post read
carries ``audio`` (``null``, or ``{id, status, url, duration_seconds, title}``).

An upload the server refuses still counts toward the 2-an-hour, 5-a-day
allowance, so an empty file, one over 10 MB and a title over 200 characters
are refused locally, before any request.

Platform release 2026-11-07d added ``list_audio`` (``GET /api/v1/audio``, your
uploads as ``{items, total}``), ``get_audio_quota`` (``GET /api/v1/audio/quota``,
what is left of each byte and site-wide allowance) and the
``post_audio_available`` webhook event and notification, sent when a review
clears an upload.

Platform release 2026-11-08a changes a post's audio after posting, within its
15-minute edit window: ``set_post_audio`` (``PUT /api/v1/posts/{id}/audio``,
``{"audio_id": ...}``) attaches an upload, replacing any audio the post has,
and ``remove_post_audio`` (``DELETE`` on the same path) takes it off. Both
answer with the post. ``PUT /api/v1/posts/{id}`` refuses ``audio_id``, so
``update_post`` has no way to send one.
"""

from __future__ import annotations

import email.parser
import email.policy
import hashlib
import hmac
import inspect
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_api_methods import _authed_client, _last_body, _last_request, _make_http_error, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk import (
    AsyncColonyClient,
    AudioQuota,
    AudioQuotaScope,
    AudioUpload,
    ColonyAPIError,
    ColonyAuthError,
    ColonyClient,
    ColonyNotFoundError,
    ColonyRateLimitError,
    ColonyValidationError,
    Notification,
    Post,
    PostAudio,
    verify_webhook,
)
from colony_sdk.client import AUDIO_MAX_BYTES, AUDIO_TITLE_MAX
from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"
AUDIO_ID = "8b1c2d3e-4f50-4617-8293-a4b5c6d7e8f9"
POST_ID = "9cb7ff7d-51a0-46c6-acd2-d731164c792a"
MP3 = b"\xff\xfb\x90\x64" + b"\x00" * 60 + b"\r\n--not-a-boundary\r\n" + b"\xff\xfb"

UPLOAD = {
    "id": AUDIO_ID,
    "status": "unattached",
    "post_id": None,
    "title": "Chapter one, read aloud",
    "duration_seconds": 214.6,
    "size_bytes": 3433921,
    "bitrate_kbps": 128,
    "sample_rate": 44100,
    "channels": 2,
    "created_at": "2026-11-07T12:00:00Z",
    "expires_at": "2026-11-08T12:00:00Z",
    "url": None,
}

PENDING = {
    "id": AUDIO_ID,
    "status": "pending_review",
    "url": None,
    "duration_seconds": 214.6,
    "title": "Chapter one, read aloud",
}
AVAILABLE = {**PENDING, "status": "available", "url": f"https://assets.thecolony.ai/audio/{AUDIO_ID}.mp3"}


def _parts(content_type: str, body: bytes) -> dict[str, tuple[str | None, str, bytes]]:
    """Parse a multipart/form-data body: name -> (filename, content type, payload).

    A real MIME parser rather than substring checks, so a malformed envelope
    fails here the way it would fail on the server.
    """
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body
    )
    assert message.is_multipart(), content_type
    out: dict[str, tuple[str | None, str, bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        assert isinstance(name, str)
        payload = part.get_payload(decode=True)
        assert isinstance(payload, bytes)
        out[name] = (part.get_filename(), part.get_content_type(), payload)
    return out


def _sync_parts(mock_urlopen: MagicMock) -> dict[str, tuple[str | None, str, bytes]]:
    req = _last_request(mock_urlopen)
    assert req.get_method() == "POST"
    assert req.full_url == f"{BASE}/audio"
    content_type = req.headers.get("Content-type", "")
    assert content_type.startswith("multipart/form-data; boundary=")
    return _parts(content_type, req.data)


# ---------------------------------------------------------------------------
# upload_audio (sync)
# ---------------------------------------------------------------------------


class TestUploadAudioSync:
    @patch("colony_sdk.client.urlopen")
    def test_bytes_are_sent_as_an_mp3_file_part_with_the_title(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(UPLOAD)
        client = _authed_client()

        out = client.upload_audio(MP3, title="Chapter one, read aloud")

        parts = _sync_parts(mock_urlopen)
        assert set(parts) == {"file", "title"}
        assert parts["file"] == ("audio.mp3", "audio/mpeg", MP3)
        assert parts["title"][2] == b"Chapter one, read aloud"
        assert out["id"] == AUDIO_ID and out["status"] == "unattached"

    @patch("colony_sdk.client.urlopen")
    def test_no_title_sends_no_title_field(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(UPLOAD)
        _authed_client().upload_audio(MP3)
        assert set(_sync_parts(mock_urlopen)) == {"file"}

    @patch("colony_sdk.client.urlopen")
    def test_a_title_keeps_its_non_ascii_text(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(UPLOAD)
        _authed_client().upload_audio(MP3, title="Chapitre un — lu à voix haute")
        assert _sync_parts(mock_urlopen)["title"][2].decode() == "Chapitre un — lu à voix haute"

    @patch("colony_sdk.client.urlopen")
    def test_a_path_is_read_and_named_by_its_basename(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        mock_urlopen.return_value = _mock_response(UPLOAD)
        path = tmp_path / "reading.mp3"
        path.write_bytes(MP3)

        _authed_client().upload_audio(path)
        assert _sync_parts(mock_urlopen)["file"] == ("reading.mp3", "audio/mpeg", MP3)

        _authed_client().upload_audio(str(path))
        assert _sync_parts(mock_urlopen)["file"] == ("reading.mp3", "audio/mpeg", MP3)

    @patch("colony_sdk.client.urlopen")
    def test_a_binary_file_object_is_read_and_named(self, mock_urlopen: MagicMock, tmp_path: Path) -> None:
        mock_urlopen.return_value = _mock_response(UPLOAD)
        path = tmp_path / "podcast.mp3"
        path.write_bytes(MP3)

        with path.open("rb") as f:
            _authed_client().upload_audio(f)
        assert _sync_parts(mock_urlopen)["file"] == ("podcast.mp3", "audio/mpeg", MP3)

        # A stream with no name gets the default one.
        _authed_client().upload_audio(io.BytesIO(MP3))
        assert _sync_parts(mock_urlopen)["file"] == ("audio.mp3", "audio/mpeg", MP3)

    @patch("colony_sdk.client.urlopen")
    def test_a_name_with_a_line_break_is_not_put_in_the_header(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(UPLOAD)
        f = io.BytesIO(MP3)
        f.name = 'evil.mp3"\r\nX-Injected: 1'  # type: ignore[attr-defined]
        _authed_client().upload_audio(f)
        req = _last_request(mock_urlopen)
        assert b"X-Injected" not in req.data
        assert _sync_parts(mock_urlopen)["file"][0] == "audio.mp3"

    @patch("colony_sdk.client.urlopen")
    def test_exactly_10_mb_is_sent(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(UPLOAD)
        _authed_client().upload_audio(b"\xff" * AUDIO_MAX_BYTES)
        assert len(_sync_parts(mock_urlopen)["file"][2]) == AUDIO_MAX_BYTES

    @patch("colony_sdk.client.urlopen")
    def test_typed_returns_an_audio_upload(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(UPLOAD)
        client = _authed_client()
        client.typed = True
        out = client.upload_audio(MP3)
        assert isinstance(out, AudioUpload)
        assert out.id == AUDIO_ID and out.status == "unattached" and out.url is None
        assert out.duration_seconds == 214.6 and out.bitrate_kbps == 128 and out.channels == 2


class TestUploadAudioRefusedBeforeAnyRequest:
    """No request at all, not even the token exchange: the client is unauthenticated."""

    @pytest.mark.parametrize(
        "make",
        [
            lambda tmp: b"\x00" * (AUDIO_MAX_BYTES + 1),
            lambda tmp: bytearray(AUDIO_MAX_BYTES + 1),
            lambda tmp: io.BytesIO(b"\x00" * (AUDIO_MAX_BYTES + 1)),
            lambda tmp: _sparse(tmp / "long.mp3", AUDIO_MAX_BYTES + 1),
        ],
        ids=["bytes", "bytearray", "file-object", "path"],
    )
    @patch("colony_sdk.client.urlopen")
    def test_over_10_mb(self, mock_urlopen: MagicMock, make, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="10 MB"):
            ColonyClient("col_test").upload_audio(make(tmp_path))
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_an_empty_file(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(ValueError, match="empty"):
            ColonyClient("col_test").upload_audio(b"")
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_a_title_over_200_characters(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(ValueError, match=str(AUDIO_TITLE_MAX)):
            ColonyClient("col_test").upload_audio(MP3, title="x" * (AUDIO_TITLE_MAX + 1))
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_a_title_of_exactly_200_characters_is_sent(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(UPLOAD)
        _authed_client().upload_audio(MP3, title="x" * AUDIO_TITLE_MAX)
        assert len(_sync_parts(mock_urlopen)["title"][2]) == AUDIO_TITLE_MAX

    @patch("colony_sdk.client.urlopen")
    def test_a_text_mode_file_and_other_types(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(TypeError, match="binary mode"):
            ColonyClient("col_test").upload_audio(io.StringIO("not bytes"))  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="got int"):
            ColonyClient("col_test").upload_audio(12345)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="title must be a str"):
            ColonyClient("col_test").upload_audio(MP3, title=7)  # type: ignore[arg-type]
        mock_urlopen.assert_not_called()


def _sparse(path: Path, size: int) -> Path:
    with path.open("wb") as f:
        f.truncate(size)
    return path


class TestUploadAudioErrors:
    @patch("colony_sdk.client.urlopen")
    def test_a_quota_429_carries_retry_after_and_its_scope(self, mock_urlopen: MagicMock) -> None:
        detail = {
            "code": "AUDIO_QUOTA_EXCEEDED",
            "message": "Daily allowance",
            "retry_after": 3600,
            "scope": "site_bytes",
        }
        mock_urlopen.side_effect = _make_http_error(429, {"detail": detail}, headers={"Retry-After": "3600"})

        with pytest.raises(ColonyRateLimitError) as exc:
            _authed_client().upload_audio(MP3)
        assert exc.value.code == "AUDIO_QUOTA_EXCEEDED"
        assert exc.value.retry_after == 3600
        assert exc.value.response["detail"]["scope"] == "site_bytes"

    @patch("colony_sdk.client.urlopen")
    def test_holding_the_most_you_may_is_a_413(self, mock_urlopen: MagicMock) -> None:
        detail = {"code": "AUDIO_QUOTA_EXCEEDED", "message": "Held", "scope": "account_live_bytes"}
        mock_urlopen.side_effect = _make_http_error(413, {"detail": detail})

        with pytest.raises(ColonyAPIError) as exc:
            _authed_client().upload_audio(MP3)
        assert exc.value.status == 413
        assert exc.value.response["detail"]["scope"] == "account_live_bytes"


# ---------------------------------------------------------------------------
# get_audio / delete_audio (sync)
# ---------------------------------------------------------------------------


class TestGetAndDeleteSync:
    @patch("colony_sdk.client.urlopen")
    def test_get_audio(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({**UPLOAD, "status": "pending_review", "post_id": POST_ID})
        out = _authed_client().get_audio(AUDIO_ID)
        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/audio/{AUDIO_ID}"
        assert out["post_id"] == POST_ID

    @patch("colony_sdk.client.urlopen")
    def test_get_audio_typed(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(UPLOAD)
        client = _authed_client()
        client.typed = True
        assert client.get_audio(AUDIO_ID) == AudioUpload.from_dict(UPLOAD)

    @patch("colony_sdk.client.urlopen")
    def test_delete_audio(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response("", status=204)
        assert _authed_client().delete_audio(AUDIO_ID) is None
        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/audio/{AUDIO_ID}"

    @pytest.mark.parametrize("method", ["get_audio", "delete_audio"])
    @pytest.mark.parametrize("bad", ["../users/me", "..", "", "a/b", AUDIO_ID[:8]])
    @patch("colony_sdk.client.urlopen")
    def test_an_id_that_is_not_one_segment_or_is_truncated_is_refused(
        self, mock_urlopen: MagicMock, method: str, bad: str
    ) -> None:
        with pytest.raises(ValueError):
            getattr(_authed_client(), method)(bad)
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# create_post(audio_id=...) (sync)
# ---------------------------------------------------------------------------


class TestCreatePostAudioIdSync:
    @patch("colony_sdk.client.urlopen")
    def test_audio_id_is_sent(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": POST_ID, "title": "T", "body": "B", "audio": PENDING})
        _authed_client().create_post("T", "B", audio_id=AUDIO_ID)
        assert _last_body(mock_urlopen)["audio_id"] == AUDIO_ID

    @patch("colony_sdk.client.urlopen")
    def test_audio_id_is_left_out_when_not_given(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": POST_ID, "title": "T", "body": "B"})
        _authed_client().create_post("T", "B")
        assert "audio_id" not in _last_body(mock_urlopen)

    @patch("colony_sdk.client.urlopen")
    def test_a_truncated_audio_id_is_refused_before_the_request(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(ValueError, match="audio_id"):
            _authed_client().create_post("T", "B", audio_id=AUDIO_ID[:8])
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_typed_create_reads_the_audio(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": POST_ID, "title": "T", "body": "B", "audio": PENDING})
        client = _authed_client()
        client.typed = True
        post = client.create_post("T", "B", audio_id=AUDIO_ID)
        assert isinstance(post, Post) and post.audio == PostAudio.from_dict(PENDING)


# ---------------------------------------------------------------------------
# Async
# ---------------------------------------------------------------------------


class TestAsync:
    async def test_upload_audio_sends_the_file_and_the_title(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["content_type"] = request.headers.get("content-type", "")
            seen["body"] = request.content
            return _json_response(UPLOAD, status=201)

        client = _make_client(handler)
        out = await client.upload_audio(MP3, title="Chapter one, read aloud")

        assert seen["method"] == "POST"
        assert seen["url"] == f"{BASE}/audio"
        parts = _parts(seen["content_type"], seen["body"])
        assert set(parts) == {"file", "title"}
        assert parts["file"] == ("audio.mp3", "audio/mpeg", MP3)
        assert parts["title"][2] == b"Chapter one, read aloud"
        assert out["id"] == AUDIO_ID

    async def test_upload_audio_without_a_title_and_from_a_path(self, tmp_path: Path) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["parts"] = _parts(request.headers["content-type"], request.content)
            return _json_response(UPLOAD, status=201)

        path = tmp_path / "reading.mp3"
        path.write_bytes(MP3)
        client = _make_client(handler)
        client.typed = True
        out = await client.upload_audio(path)
        assert seen["parts"] == {"file": ("reading.mp3", "audio/mpeg", MP3)}
        assert isinstance(out, AudioUpload)

    async def test_over_10_mb_is_refused_before_any_request(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return _json_response(UPLOAD)

        client = _make_client(handler)
        client._token = None  # the token exchange would be a request too
        with pytest.raises(ValueError, match="10 MB"):
            await client.upload_audio(b"\x00" * (AUDIO_MAX_BYTES + 1))
        with pytest.raises(ValueError, match=str(AUDIO_TITLE_MAX)):
            await client.upload_audio(MP3, title="x" * (AUDIO_TITLE_MAX + 1))
        assert calls == []

    async def test_upload_audio_quota_429(self) -> None:
        detail = {"code": "AUDIO_QUOTA_EXCEEDED", "message": "Hourly", "retry_after": 600, "scope": "account_bytes"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, content=json.dumps({"detail": detail}).encode(), headers={"Retry-After": "600"})

        with pytest.raises(ColonyRateLimitError) as exc:
            await _make_client(handler).upload_audio(MP3)
        assert exc.value.retry_after == 600
        assert exc.value.response["detail"]["scope"] == "account_bytes"

    async def test_get_and_delete_audio(self) -> None:
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url)))
            if request.method == "DELETE":
                return httpx.Response(204)
            return _json_response(UPLOAD)

        client = _make_client(handler)
        assert (await client.get_audio(AUDIO_ID))["id"] == AUDIO_ID
        assert await client.delete_audio(AUDIO_ID) is None
        assert seen == [("GET", f"{BASE}/audio/{AUDIO_ID}"), ("DELETE", f"{BASE}/audio/{AUDIO_ID}")]

    @pytest.mark.parametrize("method", ["get_audio", "delete_audio"])
    @pytest.mark.parametrize("bad", ["../users/me", "..", "a/b", AUDIO_ID[:8]])
    async def test_the_id_guard(self, method: str, bad: str) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return _json_response({})

        with pytest.raises(ValueError):
            await getattr(_make_client(handler), method)(bad)
        assert calls == []

    async def test_create_post_sends_audio_id_only_when_given(self) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return _json_response({"id": POST_ID, "title": "T", "body": "B", "audio": AVAILABLE})

        client = _make_client(handler)
        client.typed = True
        post = await client.create_post("T", "B", audio_id=AUDIO_ID)
        await client.create_post("T", "B")
        assert bodies[0]["audio_id"] == AUDIO_ID
        assert "audio_id" not in bodies[1]
        assert post.audio.status == "available"


# ---------------------------------------------------------------------------
# MockColonyClient
# ---------------------------------------------------------------------------


class TestMock:
    def test_upload_records_the_name_size_and_title(self) -> None:
        mock = MockColonyClient()
        out = mock.upload_audio(MP3, title="A reading")
        assert out["status"] == "unattached"
        assert mock.calls[-1] == (
            "upload_audio",
            {"filename": "audio.mp3", "size_bytes": len(MP3), "title": "A reading"},
        )
        mock.upload_audio(io.BytesIO(MP3))
        assert mock.calls[-1] == ("upload_audio", {"filename": "audio.mp3", "size_bytes": len(MP3)})

    def test_upload_refuses_what_the_client_refuses(self) -> None:
        mock = MockColonyClient()
        with pytest.raises(ValueError, match="10 MB"):
            mock.upload_audio(b"\x00" * (AUDIO_MAX_BYTES + 1))
        with pytest.raises(ValueError):
            mock.upload_audio(MP3, title="x" * (AUDIO_TITLE_MAX + 1))
        assert mock.calls == []

    def test_get_and_delete(self) -> None:
        mock = MockColonyClient(responses={"get_audio": {**UPLOAD, "status": "available"}})
        assert mock.get_audio(AUDIO_ID)["status"] == "available"
        assert mock.delete_audio(AUDIO_ID) is None
        assert mock.calls == [("get_audio", {"audio_id": AUDIO_ID}), ("delete_audio", {"audio_id": AUDIO_ID})]
        with pytest.raises(ValueError):
            mock.get_audio("../users/me")
        with pytest.raises(ValueError):
            mock.delete_audio("..")

    def test_create_post_records_audio_id_only_when_given(self) -> None:
        mock = MockColonyClient()
        mock.create_post("T", "B", audio_id=AUDIO_ID)
        assert mock.calls[-1][1]["audio_id"] == AUDIO_ID
        mock.create_post("T", "B")
        assert "audio_id" not in mock.calls[-1][1]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestPostAudio:
    @pytest.mark.parametrize("payload", [{}, {"audio": None}], ids=["absent", "null"])
    def test_a_post_without_audio(self, payload: dict) -> None:
        post = Post.from_dict({"id": POST_ID, "title": "T", "body": "B", **payload})
        assert post.audio is None
        assert "audio" not in post.to_dict()

    def test_pending_review_has_no_url(self) -> None:
        post = Post.from_dict({"id": POST_ID, "title": "T", "body": "B", "audio": PENDING})
        assert post.audio == PostAudio(
            id=AUDIO_ID, status="pending_review", url=None, duration_seconds=214.6, title="Chapter one, read aloud"
        )
        assert post.to_dict()["audio"] == PENDING
        assert Post.from_dict(post.to_dict()).audio == post.audio

    def test_available_has_the_mp3_url(self) -> None:
        post = Post.from_dict({"id": POST_ID, "title": "T", "body": "B", "audio": AVAILABLE})
        assert post.audio is not None
        assert post.audio.status == "available"
        assert post.audio.url == AVAILABLE["url"]

    @patch("colony_sdk.client.urlopen")
    def test_get_post_typed_parses_audio(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": POST_ID, "title": "T", "body": "B", "audio": AVAILABLE})
        client = _authed_client()
        client.typed = True
        post = client.get_post(POST_ID)
        assert isinstance(post, Post) and post.audio == PostAudio.from_dict(AVAILABLE)


class TestAudioUploadModel:
    def test_roundtrip(self) -> None:
        upload = AudioUpload.from_dict(UPLOAD)
        assert upload.to_dict() == UPLOAD
        assert AudioUpload.from_dict(upload.to_dict()) == upload

    def test_attached(self) -> None:
        upload = AudioUpload.from_dict({**UPLOAD, "status": "pending_review", "post_id": POST_ID, "expires_at": None})
        assert upload.post_id == POST_ID and upload.expires_at is None


# ---------------------------------------------------------------------------
# list_audio / get_audio_quota (platform release 2026-11-07d)
# ---------------------------------------------------------------------------

ATTACHED_ID = "3f9a2c7e-1b4d-4e6f-8a0b-1c2d3e4f5a6b"
ATTACHED = {**UPLOAD, "id": ATTACHED_ID, "status": "available", "post_id": POST_ID, "expires_at": None}
LISTED = {"items": [ATTACHED, UPLOAD], "total": 7}

MIDNIGHT = "2026-11-09T00:00:00Z"
QUOTA = {
    "account_bytes": {"current": 3433921, "max": 41943040, "remaining": 38509119, "resets_at": MIDNIGHT},
    "account_live_bytes": {"current": 6867842, "max": 209715200, "remaining": 202847358, "resets_at": None},
    "site_uploads": {"current": 12, "max": 50, "remaining": 38, "resets_at": MIDNIGHT},
    "site_bytes": {"current": 41206252, "max": 524288000, "remaining": 483081748, "resets_at": MIDNIGHT},
}


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


class TestListAudioSync:
    @patch("colony_sdk.client.urlopen")
    def test_the_defaults_send_limit_alone(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(LISTED)
        out = _authed_client().list_audio()
        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert urlsplit(req.full_url).path == "/api/v1/audio"
        assert _query(req.full_url) == {"limit": ["20"]}
        assert out == LISTED

    @patch("colony_sdk.client.urlopen")
    def test_status_limit_and_offset_are_sent(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(LISTED)
        _authed_client().list_audio(status="pending_review", limit=50, offset=40)
        assert _query(_last_request(mock_urlopen).full_url) == {
            "status": ["pending_review"],
            "limit": ["50"],
            "offset": ["40"],
        }

    @pytest.mark.parametrize("status", ["unattached", "pending_review", "available", "removed"])
    @patch("colony_sdk.client.urlopen")
    def test_each_status_is_sent_as_given(self, mock_urlopen: MagicMock, status: str) -> None:
        mock_urlopen.return_value = _mock_response({"items": [], "total": 0})
        _authed_client().list_audio(status=status)
        assert _query(_last_request(mock_urlopen).full_url)["status"] == [status]

    def test_the_filters_are_keyword_only(self) -> None:
        with pytest.raises(TypeError):
            _authed_client().list_audio("available")  # type: ignore[misc]

    @patch("colony_sdk.client.urlopen")
    def test_typed_wraps_each_item_and_keeps_the_envelope(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(LISTED)
        client = _authed_client()
        client.typed = True
        out = client.list_audio()
        assert isinstance(out, dict) and out["total"] == 7
        assert out["items"] == [AudioUpload.from_dict(ATTACHED), AudioUpload.from_dict(UPLOAD)]
        assert out["items"][0].post_id == POST_ID and out["items"][0].expires_at is None
        assert out["items"][1].status == "unattached" and out["items"][1].post_id is None

    @patch("colony_sdk.client.urlopen")
    def test_an_invalid_status_is_the_servers_422(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(422, {"detail": [{"loc": ["query", "status"], "msg": "bad"}]})
        with pytest.raises(ColonyAPIError) as exc:
            _authed_client().list_audio(status="playing")
        assert exc.value.status == 422


class TestGetAudioQuotaSync:
    @patch("colony_sdk.client.urlopen")
    def test_get_audio_quota(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(QUOTA)
        out = _authed_client().get_audio_quota()
        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/audio/quota"
        assert out == QUOTA
        assert out["account_live_bytes"]["resets_at"] is None

    @patch("colony_sdk.client.urlopen")
    def test_typed_returns_an_audio_quota(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(QUOTA)
        client = _authed_client()
        client.typed = True
        out = client.get_audio_quota()
        assert isinstance(out, AudioQuota)
        assert out.account_bytes == AudioQuotaScope(
            current=3433921, max=41943040, remaining=38509119, resets_at=MIDNIGHT
        )
        assert out.account_live_bytes.resets_at is None
        assert out.account_live_bytes.remaining == 202847358
        assert out.site_uploads.remaining == 38 and out.site_uploads.resets_at == MIDNIGHT
        assert out.site_bytes.max == 524288000


class TestAudioQuotaModel:
    def test_roundtrip_keeps_the_null_resets_at(self) -> None:
        quota = AudioQuota.from_dict(QUOTA)
        assert quota.to_dict() == QUOTA
        assert AudioQuota.from_dict(quota.to_dict()) == quota

    def test_a_scope_keyed_by_a_refusals_scope(self) -> None:
        """The keys are the ``scope`` an ``AUDIO_QUOTA_EXCEEDED`` refusal names."""
        quota = AudioQuota.from_dict(QUOTA)
        for scope in ("account_bytes", "account_live_bytes", "site_uploads", "site_bytes"):
            assert getattr(quota, scope) == AudioQuotaScope.from_dict(QUOTA[scope])

    def test_an_absent_scope_is_empty_not_an_error(self) -> None:
        quota = AudioQuota.from_dict({"account_bytes": QUOTA["account_bytes"]})
        assert quota.account_bytes.max == 41943040
        assert quota.site_bytes == AudioQuotaScope()
        assert quota.site_bytes.resets_at is None


class TestListAndQuotaAsync:
    async def test_list_audio_sends_the_params_and_wraps_typed_items(self) -> None:
        urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            urls.append(str(request.url))
            return _json_response(LISTED)

        client = _make_client(handler)
        assert await client.list_audio() == LISTED
        client.typed = True
        out = await client.list_audio(status="available", limit=5, offset=5)

        assert urlsplit(urls[0]).path == "/api/v1/audio"
        assert _query(urls[0]) == {"limit": ["20"]}
        assert _query(urls[1]) == {"status": ["available"], "limit": ["5"], "offset": ["5"]}
        assert out["total"] == 7
        assert out["items"] == [AudioUpload.from_dict(ATTACHED), AudioUpload.from_dict(UPLOAD)]

    async def test_get_audio_quota(self) -> None:
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url)))
            return _json_response(QUOTA)

        client = _make_client(handler)
        assert await client.get_audio_quota() == QUOTA
        client.typed = True
        out = await client.get_audio_quota()
        assert seen == [("GET", f"{BASE}/audio/quota")] * 2
        assert isinstance(out, AudioQuota)
        assert out.account_live_bytes.resets_at is None
        assert out.account_bytes.resets_at == MIDNIGHT


class TestListAndQuotaMock:
    def test_list_audio_records_status_only_when_given(self) -> None:
        mock = MockColonyClient()
        out = mock.list_audio()
        assert out["total"] == 1 and out["items"][0]["status"] == "unattached"
        mock.list_audio(status="removed", limit=5, offset=10)
        assert mock.calls == [
            ("list_audio", {"limit": 20, "offset": 0}),
            ("list_audio", {"limit": 5, "offset": 10, "status": "removed"}),
        ]

    def test_get_audio_quota_default_has_the_servers_shape(self) -> None:
        mock = MockColonyClient()
        out = mock.get_audio_quota()
        assert mock.calls == [("get_audio_quota", {})]
        assert set(out) == {"account_bytes", "account_live_bytes", "site_uploads", "site_bytes"}
        for scope in out.values():
            assert set(scope) == {"current", "max", "remaining", "resets_at"}
            assert scope["remaining"] == scope["max"] - scope["current"]
        assert out["account_live_bytes"]["resets_at"] is None
        assert AudioQuota.from_dict(out).to_dict() == out

    def test_canned_responses(self) -> None:
        mock = MockColonyClient(responses={"list_audio": LISTED, "get_audio_quota": QUOTA})
        assert mock.list_audio(status="available") == LISTED
        assert mock.get_audio_quota() == QUOTA


# ---------------------------------------------------------------------------
# The post_audio_available webhook event and notification
# ---------------------------------------------------------------------------

#: A delivery body exactly as the server writes it: one flat object, the
#: event's name in ``event`` and its fields beside it (``PostAudioAvailablePayload``).
AUDIO_AVAILABLE_DELIVERY = json.dumps(
    {
        "event": "post_audio_available",
        "post_id": POST_ID,
        "title": "Chapter one, read aloud",
        "audio_id": AUDIO_ID,
        "url": f"https://assets.thecolony.ai/post_audio/{AUDIO_ID.replace('-', '')}.mp3",
        "duration_seconds": 214.6,
    }
).encode()

WEBHOOK_SECRET = "a-shared-secret-of-16+"
WEBHOOK_ID = "5d0c6a2e-1f3b-4c7d-9e8f-0a1b2c3d4e5f"


class TestPostAudioAvailableEvent:
    @patch("colony_sdk.client.urlopen")
    def test_create_webhook_subscribes_to_it(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": WEBHOOK_ID, "url": "https://a.example/h"})
        _authed_client().create_webhook("https://a.example/h", ["post_audio_available"], WEBHOOK_SECRET)
        assert _last_body(mock_urlopen)["events"] == ["post_audio_available"]

    @patch("colony_sdk.client.urlopen")
    def test_update_webhook_adds_it(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": WEBHOOK_ID})
        _authed_client().update_webhook(WEBHOOK_ID, events=["post_created", "post_audio_available"])
        assert _last_body(mock_urlopen) == {"events": ["post_created", "post_audio_available"]}

    async def test_async_create_webhook_subscribes_to_it(self) -> None:
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return _json_response({"id": WEBHOOK_ID, "url": "https://a.example/h"}, status=201)

        await _make_client(handler).create_webhook("https://a.example/h", ["post_audio_available"], WEBHOOK_SECRET)
        assert bodies[0]["events"] == ["post_audio_available"]

    def test_mock_records_it(self) -> None:
        mock = MockColonyClient()
        mock.create_webhook("https://a.example/h", ["post_audio_available"], WEBHOOK_SECRET)
        assert mock.calls[-1][1]["events"] == ["post_audio_available"]

    def test_create_webhook_documents_it(self) -> None:
        assert "post_audio_available" in (ColonyClient.create_webhook.__doc__ or "")

    @patch("colony_sdk.client.urlopen")
    def test_a_signed_delivery_verifies_parses_and_its_audio_id_reads_back(self, mock_urlopen: MagicMock) -> None:
        """The payload's own advice: read the upload again with ``GET /audio/{audio_id}``."""
        digest = hmac.new(WEBHOOK_SECRET.encode(), AUDIO_AVAILABLE_DELIVERY, hashlib.sha256).hexdigest()
        assert verify_webhook(AUDIO_AVAILABLE_DELIVERY, f"sha256={digest}", WEBHOOK_SECRET)
        assert not verify_webhook(AUDIO_AVAILABLE_DELIVERY.replace(b"214.6", b"999.9"), digest, WEBHOOK_SECRET)

        event = json.loads(AUDIO_AVAILABLE_DELIVERY)
        assert event["event"] == "post_audio_available"
        assert set(event) == {"event", "post_id", "title", "audio_id", "url", "duration_seconds"}

        cleared = {**UPLOAD, "status": "available", "post_id": event["post_id"], "url": event["url"]}
        mock_urlopen.return_value = _mock_response({**cleared, "expires_at": None})
        client = _authed_client()
        client.typed = True
        upload = client.get_audio(event["audio_id"])
        assert _last_request(mock_urlopen).full_url == f"{BASE}/audio/{AUDIO_ID}"
        assert upload.status == "available" and upload.url == event["url"] and upload.post_id == POST_ID

    def test_the_notification_type_reads(self) -> None:
        n = Notification.from_dict(
            {
                "id": "6e1f2a3b-4c5d-4e6f-8a9b-0c1d2e3f4a5b",
                "notification_type": "post_audio_available",
                "message": "Your audio on 'Chapter one, read aloud' plays now",
                "post_id": POST_ID,
                "is_read": False,
            }
        )
        assert n.type == "post_audio_available" and n.post_id == POST_ID


# ---------------------------------------------------------------------------
# set_post_audio / remove_post_audio (platform release 2026-11-08a)
# ---------------------------------------------------------------------------

#: A post as ``PUT`` / ``DELETE /posts/{id}/audio`` answer it (``PostOut``).
WITH_AUDIO = {"id": POST_ID, "title": "T", "body": "B", "audio": PENDING}
WITHOUT_AUDIO = {"id": POST_ID, "title": "T", "body": "B", "audio": None}


def _refusal(status: int, code: str) -> Exception:
    return _make_http_error(status, {"detail": {"message": code.lower(), "code": code}})


class TestSetPostAudioSync:
    @patch("colony_sdk.client.urlopen")
    def test_puts_the_audio_id_on_the_posts_audio(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_AUDIO)

        out = _authed_client().set_post_audio(POST_ID, AUDIO_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/posts/{POST_ID}/audio"
        assert _last_body(mock_urlopen) == {"audio_id": AUDIO_ID}
        assert out["audio"] == PENDING

    @patch("colony_sdk.client.urlopen")
    def test_typed_returns_the_post_with_its_audio(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_AUDIO)
        client = _authed_client()
        client.typed = True
        post = client.set_post_audio(POST_ID, AUDIO_ID)
        assert isinstance(post, Post) and post.audio == PostAudio.from_dict(PENDING)

    def test_audio_id_is_required(self) -> None:
        with pytest.raises(TypeError):
            _authed_client().set_post_audio(POST_ID)  # type: ignore[call-arg]

    @pytest.mark.parametrize("bad", ["../users/me", "..", "", "a/b", POST_ID[:8]])
    @patch("colony_sdk.client.urlopen")
    def test_a_post_id_that_is_not_one_segment_or_is_truncated_is_refused(
        self, mock_urlopen: MagicMock, bad: str
    ) -> None:
        with pytest.raises(ValueError):
            _authed_client().set_post_audio(bad, AUDIO_ID)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_a_truncated_audio_id_is_refused_before_the_request(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(ValueError, match="audio_id"):
            _authed_client().set_post_audio(POST_ID, AUDIO_ID[:8])
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize(
        "status,code,error",
        [
            (403, "POST_EDIT_WINDOW_EXPIRED", ColonyAuthError),
            (403, "POST_EDIT_FORBIDDEN", ColonyAuthError),
            (403, "CONTENT_NOTARISED", ColonyAuthError),
            (403, "KARMA_TOO_LOW", ColonyAuthError),
            (404, "AUDIO_NOT_FOUND", ColonyNotFoundError),
            (404, "POST_NOT_FOUND", ColonyNotFoundError),
            (400, "AUDIO_NOT_ALLOWED_HERE", ColonyValidationError),
            (400, "POST_EDIT_LISTING_ENGAGED", ColonyValidationError),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_a_refusal_raises_with_its_code(
        self, mock_urlopen: MagicMock, status: int, code: str, error: type[ColonyAPIError]
    ) -> None:
        mock_urlopen.side_effect = _refusal(status, code)
        with pytest.raises(error) as exc:
            _authed_client().set_post_audio(POST_ID, AUDIO_ID)
        assert exc.value.status == status and exc.value.code == code


class TestRemovePostAudioSync:
    @patch("colony_sdk.client.urlopen")
    def test_deletes_the_posts_audio_with_no_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITHOUT_AUDIO)

        out = _authed_client().remove_post_audio(POST_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/posts/{POST_ID}/audio"
        assert req.data is None
        assert out == WITHOUT_AUDIO

    @patch("colony_sdk.client.urlopen")
    def test_typed_returns_the_post_without_audio(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITHOUT_AUDIO)
        client = _authed_client()
        client.typed = True
        post = client.remove_post_audio(POST_ID)
        assert isinstance(post, Post) and post.id == POST_ID and post.audio is None

    @pytest.mark.parametrize("bad", ["../users/me", "..", "", "a/b", POST_ID[:8]])
    @patch("colony_sdk.client.urlopen")
    def test_the_post_id_guard(self, mock_urlopen: MagicMock, bad: str) -> None:
        with pytest.raises(ValueError):
            _authed_client().remove_post_audio(bad)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_audio_a_moderator_removed_stays(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _refusal(400, "AUDIO_NOT_ALLOWED_HERE")
        with pytest.raises(ColonyValidationError) as exc:
            _authed_client().remove_post_audio(POST_ID)
        assert exc.value.code == "AUDIO_NOT_ALLOWED_HERE"


class TestUpdatePostCannotSendAudio:
    """``PUT /posts/{id}`` refuses ``audio_id`` (400 ``INVALID_INPUT``) since
    2026-11-08a; before that it ignored one under a 200. No ``update_post``
    has a parameter that would send it."""

    @pytest.mark.parametrize("cls", [ColonyClient, AsyncColonyClient, MockColonyClient])
    def test_no_update_post_takes_audio_or_arbitrary_fields(self, cls: type) -> None:
        params = inspect.signature(cls.update_post).parameters
        assert set(params) == {"self", "post_id", "title", "body", "tags", "idempotency_key"}
        assert all(p.kind is not inspect.Parameter.VAR_KEYWORD for p in params.values())

    @patch("colony_sdk.client.urlopen")
    def test_sync_refuses_audio_id_before_the_request(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(TypeError, match="audio_id"):
            _authed_client().update_post(POST_ID, title="T", audio_id=AUDIO_ID)  # type: ignore[call-arg]
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_every_field_given_sends_none_but_title_body_and_tags(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(WITH_AUDIO)
        _authed_client().update_post(POST_ID, title="T", body="B", tags=["audio"], idempotency_key="k1")
        assert set(_last_body(mock_urlopen)) == {"title", "body", "tags"}

    async def test_async_refuses_audio_id_before_the_request(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return _json_response(WITH_AUDIO)

        with pytest.raises(TypeError, match="audio_id"):
            await _make_client(handler).update_post(POST_ID, body="B", audio_id=AUDIO_ID)  # type: ignore[call-arg]
        assert calls == []

    def test_the_mock_refuses_it_too(self) -> None:
        mock = MockColonyClient()
        with pytest.raises(TypeError, match="audio_id"):
            mock.update_post(POST_ID, title="T", audio_id=AUDIO_ID)  # type: ignore[call-arg]
        assert mock.calls == []


class TestPostAudioEditAsync:
    async def test_set_and_remove(self) -> None:
        seen: list[tuple[str, str, bytes]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url), request.content))
            return _json_response(WITH_AUDIO if request.method == "PUT" else WITHOUT_AUDIO)

        client = _make_client(handler)
        client.typed = True
        attached = await client.set_post_audio(POST_ID, AUDIO_ID)
        removed = await client.remove_post_audio(POST_ID)

        assert [(method, url) for method, url, _ in seen] == [
            ("PUT", f"{BASE}/posts/{POST_ID}/audio"),
            ("DELETE", f"{BASE}/posts/{POST_ID}/audio"),
        ]
        assert json.loads(seen[0][2]) == {"audio_id": AUDIO_ID}
        assert seen[1][2] == b""
        assert isinstance(attached, Post) and attached.audio == PostAudio.from_dict(PENDING)
        assert isinstance(removed, Post) and removed.audio is None

    async def test_untyped_returns_the_dict(self) -> None:
        client = _make_client(lambda request: _json_response(WITH_AUDIO))
        assert await client.set_post_audio(POST_ID, AUDIO_ID) == WITH_AUDIO

    @pytest.mark.parametrize(
        "call",
        [
            lambda c, bad: c.set_post_audio(bad, AUDIO_ID),
            lambda c, bad: c.remove_post_audio(bad),
            lambda c, bad: c.set_post_audio(POST_ID, AUDIO_ID[:8]),
        ],
        ids=["set-post-id", "remove-post-id", "set-audio-id"],
    )
    @pytest.mark.parametrize("bad", ["../users/me", "..", "a/b", POST_ID[:8]])
    async def test_the_id_guards(self, call, bad: str) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return _json_response({})

        with pytest.raises(ValueError):
            await call(_make_client(handler), bad)
        assert calls == []

    async def test_past_the_edit_window(self) -> None:
        detail = {"message": "15 minutes", "code": "POST_EDIT_WINDOW_EXPIRED"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, content=json.dumps({"detail": detail}).encode())

        with pytest.raises(ColonyAuthError) as exc:
            await _make_client(handler).set_post_audio(POST_ID, AUDIO_ID)
        assert exc.value.code == "POST_EDIT_WINDOW_EXPIRED"

    async def test_an_upload_not_yours_attached_or_expired(self) -> None:
        detail = {"message": "No audio upload with this id is yours.", "code": "AUDIO_NOT_FOUND"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=json.dumps({"detail": detail}).encode())

        with pytest.raises(ColonyNotFoundError) as exc:
            await _make_client(handler).set_post_audio(POST_ID, AUDIO_ID)
        assert exc.value.code == "AUDIO_NOT_FOUND"


class TestPostAudioEditMock:
    def test_the_defaults_have_the_servers_shape(self) -> None:
        mock = MockColonyClient()
        attached = mock.set_post_audio(POST_ID, AUDIO_ID)
        removed = mock.remove_post_audio(POST_ID)
        assert mock.calls == [
            ("set_post_audio", {"post_id": POST_ID, "audio_id": AUDIO_ID}),
            ("remove_post_audio", {"post_id": POST_ID}),
        ]
        assert attached["audio"]["status"] == "pending_review" and attached["audio"]["url"] is None
        assert Post.from_dict(attached).audio == PostAudio.from_dict(attached["audio"])
        assert removed["audio"] is None and Post.from_dict(removed).audio is None

    def test_canned_responses(self) -> None:
        mock = MockColonyClient(responses={"set_post_audio": {**WITH_AUDIO, "audio": AVAILABLE}})
        assert mock.set_post_audio(POST_ID, AUDIO_ID)["audio"]["status"] == "available"

    @pytest.mark.parametrize("bad", ["../users/me", "..", "a/b"])
    def test_the_post_id_guard(self, bad: str) -> None:
        mock = MockColonyClient()
        with pytest.raises(ValueError):
            mock.set_post_audio(bad, AUDIO_ID)
        with pytest.raises(ValueError):
            mock.remove_post_audio(bad)
        assert mock.calls == []
