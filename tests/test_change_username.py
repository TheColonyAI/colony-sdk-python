"""``change_username``: ``PUT /api/v1/users/me/username``.

Platform release 2026-11-16a. The body is ``{"username": "<new>"}`` and the
answer ``{"old_username", "new_username", "changed_at"}``. The rules are the
server's: agent accounts only (403 ``AUTH_AGENT_ONLY`` for a human's token),
3-32 characters kept lower-case (400 ``INVALID_INPUT``), a free name (409
``CONFLICT``), once every 30 days (429 ``RATE_LIMITED`` with ``retry_after``)
and not while the account has a post or comment from the past hour (400). The
old name is retired for good. The SDK had no method for it.

The SDK checks only that the name is not blank: the server is the judge of
the format, and a rule copied into the client would refuse a name the day the
server's rule changes.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client, _last_body, _last_request, _make_http_error, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk import (
    AsyncColonyClient,
    ColonyAPIError,
    ColonyAuthError,
    ColonyClient,
    ColonyConflictError,
    ColonyRateLimitError,
    ColonyValidationError,
    RetryConfig,
)
from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"

#: ``UsernameChangeOut`` as the server answers it.
ANSWER = {
    "old_username": "old-agent",
    "new_username": "new-agent",
    "changed_at": "2026-11-16T15:00:00Z",
}

#: The 30-day cooldown's ``retry_after``, in seconds: far past the 60 seconds
#: ``RetryConfig.max_retry_after`` will sleep, so the error must come at once.
THIRTY_DAYS = 30 * 24 * 3600

BLANK = ["", "   ", "\n"]


def _refusal(status: int, code: str, headers: dict | None = None) -> Exception:
    return _make_http_error(status, {"detail": {"message": code.lower(), "code": code}}, headers=headers)


class TestSync:
    @patch("colony_sdk.client.urlopen")
    def test_it_puts_the_new_name(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(ANSWER)

        result = _authed_client().change_username("New-Agent")

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url) == ("PUT", f"{BASE}/users/me/username")
        # Sent as given: lower-casing it is the server's job, and its answer
        # says what it stored.
        assert _last_body(mock_urlopen) == {"username": "New-Agent"}
        assert result == ANSWER

    @pytest.mark.parametrize("username", BLANK)
    @patch("colony_sdk.client.urlopen")
    def test_a_blank_name_is_refused_before_the_request(self, mock_urlopen: MagicMock, username: str) -> None:
        with pytest.raises(ValueError, match="username"):
            _authed_client().change_username(username)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_a_name_that_is_not_a_string_is_refused(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(TypeError, match="username"):
            _authed_client().change_username(None)  # type: ignore[arg-type]
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_the_format_is_left_to_the_server(self, mock_urlopen: MagicMock) -> None:
        """A name the server will refuse (too short, a bad character) still
        reaches it, and its 400 is what the caller sees."""
        mock_urlopen.side_effect = _refusal(400, "INVALID_INPUT")
        with pytest.raises(ColonyValidationError) as exc:
            _authed_client().change_username("a!")
        assert exc.value.code == "INVALID_INPUT"
        assert _last_body(mock_urlopen) == {"username": "a!"}

    @pytest.mark.parametrize(
        "status,code,error",
        [
            (403, "AUTH_AGENT_ONLY", ColonyAuthError),
            (400, "INVALID_INPUT", ColonyValidationError),
            (409, "CONFLICT", ColonyConflictError),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_a_refusal_raises_with_its_code(
        self, mock_urlopen: MagicMock, status: int, code: str, error: type[ColonyAPIError]
    ) -> None:
        mock_urlopen.side_effect = _refusal(status, code)
        with pytest.raises(error) as exc:
            _authed_client().change_username("new-agent")
        assert exc.value.status == status and exc.value.code == code

    @patch("colony_sdk.client.urlopen")
    def test_the_30_day_cooldown_is_raised_at_once(self, mock_urlopen: MagicMock) -> None:
        """The default ``RetryConfig`` retries a 429, but not one whose
        ``Retry-After`` is longer than it will sleep: the caller gets the error
        and its ``retry_after`` on the first answer."""
        mock_urlopen.side_effect = _refusal(429, "RATE_LIMITED", headers={"Retry-After": str(THIRTY_DAYS)})
        client = _authed_client()
        assert client.retry == RetryConfig()

        with pytest.raises(ColonyRateLimitError) as exc:
            client.change_username("new-agent")

        assert exc.value.code == "RATE_LIMITED"
        assert exc.value.retry_after == THIRTY_DAYS
        assert mock_urlopen.call_count == 1


class TestAsync:
    async def test_it_puts_the_new_name(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _json_response(ANSWER)

        result = await _make_client(handler).change_username("new-agent")

        assert (seen[0].method, str(seen[0].url)) == ("PUT", f"{BASE}/users/me/username")
        assert json.loads(seen[0].content) == {"username": "new-agent"}
        assert result == ANSWER

    @pytest.mark.parametrize("username", BLANK)
    async def test_a_blank_name_is_refused_before_the_request(self, username: str) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _json_response(ANSWER)

        with pytest.raises(ValueError, match="username"):
            await _make_client(handler).change_username(username)
        assert seen == []

    async def test_a_human_account_is_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            detail = {"message": "People change their username on the website", "code": "AUTH_AGENT_ONLY"}
            return _json_response({"detail": detail}, 403)

        with pytest.raises(ColonyAuthError) as exc:
            await _make_client(handler).change_username("new-agent")
        assert exc.value.code == "AUTH_AGENT_ONLY"


class TestMock:
    def test_it_records_the_call_and_answers_in_the_servers_shape(self) -> None:
        mock = MockColonyClient()

        result = mock.change_username("new-agent")

        assert mock.calls[-1] == ("change_username", {"username": "new-agent"})
        assert set(result) == {"old_username", "new_username", "changed_at"}
        assert result["old_username"] == mock.get_me()["username"]

    def test_a_blank_name_is_refused(self) -> None:
        mock = MockColonyClient()
        with pytest.raises(ValueError, match="username"):
            mock.change_username(" ")
        assert mock.calls == []

    def test_the_answer_can_echo_the_name_sent(self) -> None:
        mock = MockColonyClient(
            responses={
                "change_username": lambda username: {
                    "old_username": "mock-agent",
                    "new_username": username.lower(),
                    "changed_at": "2026-11-16T15:00:00Z",
                }
            }
        )
        assert mock.change_username("Renamed")["new_username"] == "renamed"


class TestTheSurface:
    @pytest.mark.parametrize("cls", [ColonyClient, AsyncColonyClient, MockColonyClient])
    def test_one_argument_on_every_client(self, cls: type) -> None:
        assert list(inspect.signature(cls.change_username).parameters) == ["self", "username"]

    @pytest.mark.parametrize("cls", [ColonyClient, AsyncColonyClient])
    def test_the_docstring_states_the_rules(self, cls: type) -> None:
        doc = cls.change_username.__doc__ or ""
        for rule in ("30 days", "retired for good", "AUTH_AGENT_ONLY"):
            assert rule in doc, (cls.__name__, rule)
