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
"""

from __future__ import annotations

import email.parser
import email.policy
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client, _last_body, _last_request, _make_http_error, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk import AudioUpload, ColonyAPIError, ColonyClient, ColonyRateLimitError, Post, PostAudio
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
