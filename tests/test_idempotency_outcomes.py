"""What the server did with a request's ``Idempotency-Key``.

Platform release 2026-11-15a. For an authenticated POST/PUT/PATCH/DELETE under
``/api/v1`` sent with an ``Idempotency-Key`` (``app/middleware/idempotency.py``):

- a replay of a stored response carries ``Idempotent-Replay: true``;
- a response that was not stored, so that a retry with the same key runs the
  endpoint again, carries ``X-Colony-Idempotency-Skipped: <reason>``, one of
  ``error_response``, ``response_too_large``, ``request_too_large``,
  ``request_length_unknown``, ``no_bearer_token`` and ``unavailable``;
- neither header: the response is stored for 24 hours;
- reusing a key with a different request is 409 with ``detail: {"code":
  "idempotency_payload_mismatch", "message": ..., "first_request": "completed"
  | "in_progress"}``. An older platform leaves ``first_request`` out.

The clients read the first two into ``last_response_idempotent_replay`` and
``last_response_idempotency_skipped``, reset on every request as
``last_response_not_modified`` is, and :class:`ColonyConflictError` exposes
``first_request``. ``error_response`` only ever arrives on an error, so the
sync client now keeps an error's headers, as the async client always has. The
JS and Go SDKs use the same names (``lastResponseIdempotentReplay``,
``LastResponseIdempotencySkipped()``, ``firstRequest`` ...).
"""

from __future__ import annotations

import io
import json
from http.client import HTTPMessage
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import httpx
import pytest
from test_api_methods import _authed_client, _last_request, _mock_response, _mock_response_with_headers
from test_async_client import _make_client

from colony_sdk import (
    ColonyClient,
    ColonyConflictError,
    ColonyNetworkError,
    ColonyNotFoundError,
    ColonyValidationError,
)
from colony_sdk.async_client import AsyncColonyClient
from colony_sdk.client import _build_api_error, _header_snapshot, _idempotency_outcome
from colony_sdk.testing import MockColonyClient

KEY = "3d7f1c2e-4b5a-4c6d-8e9f-0a1b2c3d4e5f"
POST = {"id": "9cb7ff7d-51a0-46c6-acd2-d731164c792a", "title": "T", "body": "B"}

#: Every reason the platform names (``SKIP_REASONS``).
REASONS = [
    "error_response",
    "response_too_large",
    "request_too_large",
    "request_length_unknown",
    "no_bearer_token",
    "unavailable",
]
#: The reasons that can come on a 2xx: every one but ``error_response``.
SUCCESS_REASONS = [r for r in REASONS if r != "error_response"]


def _mismatch(first_request: str | None) -> dict:
    detail = {
        "message": "Idempotency-Key reused with a different request payload.",
        "code": "idempotency_payload_mismatch",
    }
    if first_request is not None:
        detail["first_request"] = first_request
    return {"detail": detail}


def _http_error(code: int, data: dict, headers: dict[str, str] | None = None) -> HTTPError:
    """A urllib HTTPError whose headers are real, as urllib builds them."""
    msg = HTTPMessage()
    for name, value in (headers or {}).items():
        msg[name] = value
    return HTTPError("http://test", code, "error", msg, io.BytesIO(json.dumps(data).encode()))


# ---------------------------------------------------------------------------
# Reading the headers
# ---------------------------------------------------------------------------


class TestReadingTheHeaders:
    def test_no_headers_is_a_stored_response_or_no_key(self) -> None:
        assert _idempotency_outcome({}) == (False, None)

    @pytest.mark.parametrize("value", ["true", "True", " true "])
    def test_a_replay(self, value: str) -> None:
        assert _idempotency_outcome({"idempotent-replay": value}) == (True, None)

    @pytest.mark.parametrize("value", ["false", "", "1", "yes"])
    def test_anything_but_true_is_not_a_replay(self, value: str) -> None:
        assert _idempotency_outcome({"idempotent-replay": value}) == (False, None)

    @pytest.mark.parametrize("reason", REASONS)
    def test_each_reason_is_passed_on(self, reason: str) -> None:
        assert _idempotency_outcome({"x-colony-idempotency-skipped": reason}) == (False, reason)

    def test_a_reason_newer_than_this_sdk_is_passed_on_too(self) -> None:
        assert _idempotency_outcome({"x-colony-idempotency-skipped": "some_new_reason"}) == (False, "some_new_reason")

    def test_an_empty_reason_is_none(self) -> None:
        assert _idempotency_outcome({"x-colony-idempotency-skipped": " "}) == (False, None)

    def test_header_names_are_lower_cased(self) -> None:
        msg = HTTPMessage()
        msg["Idempotent-Replay"] = "true"
        assert _header_snapshot(msg) == {"idempotent-replay": "true"}
        assert _header_snapshot({"X-Colony-Idempotency-Skipped": "unavailable"}) == {
            "x-colony-idempotency-skipped": "unavailable"
        }

    @pytest.mark.parametrize("headers", [None, object(), 42], ids=["none", "no-items", "not-a-mapping"])
    def test_an_error_with_no_readable_headers_reads_as_none(self, headers: object) -> None:
        assert _header_snapshot(headers) == {}


# ---------------------------------------------------------------------------
# ColonyConflictError.first_request
# ---------------------------------------------------------------------------


class TestFirstRequest:
    @pytest.mark.parametrize("first_request", ["completed", "in_progress"])
    def test_a_mismatch_says_what_became_of_the_first_request(self, first_request: str) -> None:
        err = _build_api_error(409, json.dumps(_mismatch(first_request)), fallback="x", message_prefix="p")
        assert isinstance(err, ColonyConflictError)
        assert err.code == "idempotency_payload_mismatch"
        assert err.first_request == first_request

    def test_an_older_platform_does_not_say(self) -> None:
        err = _build_api_error(409, json.dumps(_mismatch(None)), fallback="x", message_prefix="p")
        assert isinstance(err, ColonyConflictError)
        assert err.first_request is None

    @pytest.mark.parametrize(
        "body",
        [
            {"detail": "Already voted"},
            {"detail": {"code": "idempotency_in_progress", "message": "still running"}},
            {"detail": {"code": "idempotency_payload_mismatch", "first_request": 1}},
            {},
        ],
        ids=["string-detail", "in-progress-code", "not-a-string", "no-body"],
    )
    def test_any_other_409_is_none(self, body: dict) -> None:
        err = _build_api_error(409, json.dumps(body), fallback="x", message_prefix="p")
        assert isinstance(err, ColonyConflictError)
        assert err.first_request is None

    def test_built_by_hand(self) -> None:
        assert ColonyConflictError("m", 409, _mismatch("completed")).first_request == "completed"
        assert ColonyConflictError("m", 409).first_request is None

    def test_the_docstring_says_what_each_value_means_for_a_retry(self) -> None:
        doc = " ".join((ColonyConflictError.first_request.__doc__ or "").split())
        assert '``"completed"``: it succeeded (a 2xx)' in doc
        assert "Do not send that write again under a new key" in doc
        assert '``"in_progress"``: it is still running' in doc
        assert "a platform older than 2026-11-15a" in doc


# ---------------------------------------------------------------------------
# ColonyClient
# ---------------------------------------------------------------------------


class TestSync:
    @patch("colony_sdk.client.urlopen")
    def test_a_replay(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response_with_headers(POST, {"Idempotent-Replay": "true"}, status=201)
        client = _authed_client()
        client.create_post("T", "B", idempotency_key=KEY)
        assert _last_request(mock_urlopen).get_header("Idempotency-key") == KEY
        assert client.last_response_idempotent_replay is True
        assert client.last_response_idempotency_skipped is None

    @patch("colony_sdk.client.urlopen")
    def test_a_stored_response_has_neither(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response_with_headers(POST, {"Content-Type": "application/json"})
        client = _authed_client()
        client.create_post("T", "B", idempotency_key=KEY)
        assert client.last_response_idempotent_replay is False
        assert client.last_response_idempotency_skipped is None

    @pytest.mark.parametrize("reason", SUCCESS_REASONS)
    @patch("colony_sdk.client.urlopen")
    def test_a_2xx_that_was_not_stored(self, mock_urlopen: MagicMock, reason: str) -> None:
        mock_urlopen.return_value = _mock_response_with_headers(POST, {"X-Colony-Idempotency-Skipped": reason})
        client = _authed_client()
        client.create_post("T", "B", idempotency_key=KEY)
        assert client.last_response_idempotency_skipped == reason
        assert client.last_response_idempotent_replay is False

    @patch("colony_sdk.client.urlopen")
    def test_an_error_was_not_stored_and_says_so(self, mock_urlopen: MagicMock) -> None:
        """``error_response`` comes only on an error, so it is read from the
        error's headers; the sync client now keeps them in
        ``last_response_headers`` too, as the async client does."""
        mock_urlopen.side_effect = _http_error(
            422, {"detail": "title is too long"}, {"X-Colony-Idempotency-Skipped": "error_response"}
        )
        client = _authed_client()
        with pytest.raises(ColonyValidationError):
            client.create_post("T", "B", idempotency_key=KEY)
        assert client.last_response_idempotency_skipped == "error_response"
        assert client.last_response_idempotent_replay is False
        assert client.last_response_headers["x-colony-idempotency-skipped"] == "error_response"

    @pytest.mark.parametrize("first_request", ["completed", "in_progress"])
    @patch("colony_sdk.client.urlopen")
    def test_a_key_reused_with_another_request(self, mock_urlopen: MagicMock, first_request: str) -> None:
        mock_urlopen.side_effect = _http_error(409, _mismatch(first_request))
        client = _authed_client()
        with pytest.raises(ColonyConflictError) as exc:
            client.create_post("T", "other body", idempotency_key=KEY)
        assert exc.value.first_request == first_request
        # The 409 is answered before the endpoint, and carries neither header.
        assert (client.last_response_idempotent_replay, client.last_response_idempotency_skipped) == (False, None)

    @patch("colony_sdk.client.urlopen")
    def test_reset_on_every_request(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        mock_urlopen.return_value = _mock_response_with_headers(POST, {"Idempotent-Replay": "true"})
        client.create_post("T", "B", idempotency_key=KEY)
        assert client.last_response_idempotent_replay is True
        mock_urlopen.return_value = _mock_response(POST)
        client.get_post(POST["id"])
        assert client.last_response_idempotent_replay is False

        mock_urlopen.return_value = _mock_response_with_headers(POST, {"X-Colony-Idempotency-Skipped": "unavailable"})
        client.create_post("T", "B", idempotency_key=KEY)
        assert client.last_response_idempotency_skipped == "unavailable"
        mock_urlopen.side_effect = _http_error(404, {"detail": "Post not found"})
        with pytest.raises(ColonyNotFoundError):
            client.get_post(POST["id"])
        assert client.last_response_idempotency_skipped is None

    @patch("colony_sdk.client.urlopen")
    def test_a_request_that_never_got_an_answer_says_nothing(self, mock_urlopen: MagicMock) -> None:
        """A timeout after a replay must not read as a second replay: there
        was no response, so nothing is known about the key."""
        client = _authed_client()
        mock_urlopen.return_value = _mock_response_with_headers(POST, {"Idempotent-Replay": "true"})
        client.create_post("T", "B", idempotency_key=KEY)
        mock_urlopen.side_effect = URLError("timed out")
        with pytest.raises(ColonyNetworkError):
            client.create_post("T", "B", idempotency_key=KEY)
        assert (client.last_response_idempotent_replay, client.last_response_idempotency_skipped) == (False, None)

    @patch("colony_sdk.client.urlopen")
    def test_reset_on_a_cached_read_too(self, mock_urlopen: MagicMock) -> None:
        """Reset before the cache is consulted, exactly as
        ``last_response_not_modified`` is: a cached read is not a replay."""
        client = _authed_client()
        client.enable_cache(ttl=60)
        mock_urlopen.return_value = _mock_response(POST)
        client.get_post(POST["id"])
        # A failed write leaves the cache as it was (a successful one clears it).
        mock_urlopen.side_effect = _http_error(
            422, {"detail": "bad"}, {"X-Colony-Idempotency-Skipped": "error_response"}
        )
        with pytest.raises(ColonyValidationError):
            client.create_post("T", "B", idempotency_key=KEY)
        assert client.last_response_idempotency_skipped == "error_response"
        calls = mock_urlopen.call_count
        client.get_post(POST["id"])  # answered from the cache
        assert mock_urlopen.call_count == calls
        assert client.last_response_idempotency_skipped is None

    def test_a_new_client_starts_clear(self) -> None:
        client = ColonyClient("col_test")
        assert (client.last_response_idempotent_replay, client.last_response_idempotency_skipped) == (False, None)


# ---------------------------------------------------------------------------
# AsyncColonyClient
# ---------------------------------------------------------------------------


def _answer(status: int, body: dict, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers=headers or {})


class TestAsync:
    async def test_a_replay(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _answer(201, POST, {"Idempotent-Replay": "true"})

        client = _make_client(handler)
        await client.create_post("T", "B", idempotency_key=KEY)
        assert seen[0].headers["idempotency-key"] == KEY
        assert client.last_response_idempotent_replay is True
        assert client.last_response_idempotency_skipped is None

    @pytest.mark.parametrize("reason", SUCCESS_REASONS)
    async def test_a_2xx_that_was_not_stored(self, reason: str) -> None:
        client = _make_client(lambda request: _answer(201, POST, {"X-Colony-Idempotency-Skipped": reason}))
        await client.create_post("T", "B", idempotency_key=KEY)
        assert client.last_response_idempotency_skipped == reason
        assert client.last_response_idempotent_replay is False

    async def test_an_error_was_not_stored_and_says_so(self) -> None:
        client = _make_client(
            lambda request: _answer(422, {"detail": "bad"}, {"X-Colony-Idempotency-Skipped": "error_response"})
        )
        with pytest.raises(ColonyValidationError):
            await client.create_post("T", "B", idempotency_key=KEY)
        assert client.last_response_idempotency_skipped == "error_response"

    @pytest.mark.parametrize("first_request", ["completed", "in_progress", None])
    async def test_a_key_reused_with_another_request(self, first_request: str | None) -> None:
        client = _make_client(lambda request: _answer(409, _mismatch(first_request)))
        with pytest.raises(ColonyConflictError) as exc:
            await client.create_post("T", "other body", idempotency_key=KEY)
        assert exc.value.first_request == first_request

    async def test_reset_on_every_request(self) -> None:
        answers = [
            _answer(201, POST, {"Idempotent-Replay": "true", "X-Colony-Idempotency-Skipped": "unavailable"}),
            _answer(200, POST),
        ]
        client = _make_client(lambda request: answers.pop(0))
        await client.create_post("T", "B", idempotency_key=KEY)
        assert (client.last_response_idempotent_replay, client.last_response_idempotency_skipped) == (
            True,
            "unavailable",
        )
        await client.get_post(POST["id"])
        assert (client.last_response_idempotent_replay, client.last_response_idempotency_skipped) == (False, None)

    async def test_a_request_that_never_got_an_answer_says_nothing(self) -> None:
        answers: list[httpx.Response | Exception] = [
            _answer(201, POST, {"Idempotent-Replay": "true"}),
            httpx.ConnectTimeout("timed out"),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        client = _make_client(handler)
        await client.create_post("T", "B", idempotency_key=KEY)
        assert client.last_response_idempotent_replay is True
        with pytest.raises(ColonyNetworkError):
            await client.create_post("T", "B", idempotency_key=KEY)
        assert (client.last_response_idempotent_replay, client.last_response_idempotency_skipped) == (False, None)

    def test_a_new_client_starts_clear(self) -> None:
        client = AsyncColonyClient("col_test")
        assert (client.last_response_idempotent_replay, client.last_response_idempotency_skipped) == (False, None)


# ---------------------------------------------------------------------------
# MockColonyClient
# ---------------------------------------------------------------------------


class TestMock:
    def test_present_and_clear(self) -> None:
        mock = MockColonyClient()
        mock.create_post("T", "B", idempotency_key=KEY)
        assert (mock.last_response_idempotent_replay, mock.last_response_idempotency_skipped) == (False, None)

    def test_a_test_can_set_them_to_drive_its_code_down_a_path(self) -> None:
        mock = MockColonyClient()
        mock.last_response_idempotent_replay = True
        mock.last_response_idempotency_skipped = "response_too_large"
        mock.create_post("T", "B", idempotency_key=KEY)
        assert mock.last_response_idempotent_replay is True
        assert mock.last_response_idempotency_skipped == "response_too_large"


@pytest.mark.parametrize("cls", [ColonyClient, AsyncColonyClient, MockColonyClient])
def test_the_names_match_on_every_client(cls: type) -> None:
    """The JS and Go SDKs ship the same names; these must not drift."""
    client = cls("col_test")
    assert client.last_response_idempotent_replay is False
    assert client.last_response_idempotency_skipped is None


# ---------------------------------------------------------------------------
# The key is sent on every write, never on a read
# ---------------------------------------------------------------------------

#: The server keys POST, PUT, PATCH and DELETE (a write with no body included
#: from platform release 2026-11-15a). The transport sent the key on POST and
#: PUT only, so one given for a PATCH or a DELETE was dropped.
WRITES = [("POST", {"a": 1}), ("PUT", {"a": 1}), ("PATCH", {"a": 1}), ("DELETE", None)]
READS = ["GET", "HEAD"]


class TestKeySentOnEveryWrite:
    @pytest.mark.parametrize("method,body", WRITES, ids=[m for m, _ in WRITES])
    @patch("colony_sdk.client.urlopen")
    def test_sync_sends_it(self, mock_urlopen: MagicMock, method: str, body: dict | None) -> None:
        mock_urlopen.return_value = _mock_response({})
        _authed_client()._raw_request(method, "/things/1", body=body, idempotency_key=KEY)
        req = _last_request(mock_urlopen)
        assert req.get_method() == method
        assert req.get_header("Idempotency-key") == KEY

    @pytest.mark.parametrize("method", READS)
    @patch("colony_sdk.client.urlopen")
    def test_sync_never_sends_it_on_a_read(self, mock_urlopen: MagicMock, method: str) -> None:
        mock_urlopen.return_value = _mock_response({})
        _authed_client()._raw_request(method, "/things/1", idempotency_key=KEY)
        assert _last_request(mock_urlopen).get_header("Idempotency-key") is None

    @pytest.mark.parametrize("method,body", WRITES, ids=[m for m, _ in WRITES])
    async def test_async_sends_it(self, method: str, body: dict | None) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _answer(200, {})

        await _make_client(handler)._raw_request(method, "/things/1", body=body, idempotency_key=KEY)
        assert seen[0].method == method
        assert seen[0].headers["idempotency-key"] == KEY

    @pytest.mark.parametrize("method", READS)
    async def test_async_never_sends_it_on_a_read(self, method: str) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _answer(200, {})

        await _make_client(handler)._raw_request(method, "/things/1", idempotency_key=KEY)
        assert "idempotency-key" not in seen[0].headers
