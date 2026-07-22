"""Client-side argument validation: reject, locally, what the server would 422/400.

These guards sit next to ``_require_uuid`` and ``_validate_totp_code`` and share
their philosophy: **narrow, non-breaking, and specific.** Each rejects only inputs
the server is documented (or tested) to reject, so none can turn away a value the
API would have accepted. The pay-off is a clear local error instead of an opaque
server one, and no wasted round-trip.

Two things are asserted throughout:

* the *unit* level — the validator function accepts every valid value and rejects
  the specific bad ones; and
* the *integration-with-the-method* level — a bad argument raises **before** any
  HTTP request is made (the mocked transport is never called). That second check is
  the point: validation that fires after the request would be pointless.

Evidence for each contract is cited in the validator docstrings in ``client.py``
and, for votes/reactions, in ``tests/integration/test_voting.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from test_api_methods import _authed_client, _mock_response

from colony_sdk.client import (
    _VALID_REACTIONS,
    _require_nonempty,
    _validate_reaction,
    _validate_vote_value,
)

# ── The validator functions in isolation ────────────────────────────────────


class TestRequireNonempty:
    def test_accepts_ordinary_text(self) -> None:
        assert _require_nonempty("hello", "body") == "hello"

    def test_accepts_a_single_character(self) -> None:
        # It does NOT enforce documented longer minimums (search's 2 chars); a
        # 1-char value is a plausible real input and the server is the authority.
        assert _require_nonempty("x", "query") == "x"

    def test_preserves_surrounding_content(self) -> None:
        # Unlike the id/totp guards it does not strip — it only *checks*.
        assert _require_nonempty("  hi  ", "body") == "  hi  "

    @pytest.mark.parametrize("bad", ["", " ", "   ", "\n", "\t\n "])
    def test_rejects_empty_or_whitespace_only(self, bad: str) -> None:
        with pytest.raises(ValueError, match="required field"):
            _require_nonempty(bad, "body")

    def test_names_the_parameter(self) -> None:
        with pytest.raises(ValueError, match="title"):
            _require_nonempty("", "title")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(TypeError):
            _require_nonempty(None, "body")  # type: ignore[arg-type]


class TestValidateVoteValue:
    @pytest.mark.parametrize("good", [1, -1])
    def test_accepts_the_two_valid_values(self, good: int) -> None:
        assert _validate_vote_value(good) == good

    def test_accepts_float_one(self) -> None:
        # 1.0 == 1; the server may coerce it, so we must not newly reject it.
        assert _validate_vote_value(1.0) == 1.0  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [0, 2, -2, 99, 100])
    def test_rejects_out_of_range(self, bad: int) -> None:
        # 0 in particular: the endpoint has NO clear-vote semantic (see
        # tests/integration/test_voting.py — "Vote value must be 1 or -1").
        with pytest.raises(ValueError, match=r"1 .*or.* -1|clear vote"):
            _validate_vote_value(bad)

    @pytest.mark.parametrize("bad", [True, False])
    def test_rejects_bool(self, bad: bool) -> None:
        # True == 1 but serialises to JSON `true`, which the server refuses.
        with pytest.raises(ValueError):
            _validate_vote_value(bad)

    @pytest.mark.parametrize("bad", ["1", "-1", None, 1.5])
    def test_rejects_wrong_type(self, bad: object) -> None:
        with pytest.raises(ValueError):
            _validate_vote_value(bad)  # type: ignore[arg-type]


class TestValidateReaction:
    @pytest.mark.parametrize("good", sorted(_VALID_REACTIONS))
    def test_accepts_every_documented_key(self, good: str) -> None:
        assert _validate_reaction(good) == good

    def test_rejects_unicode_emoji_with_a_targeted_hint(self) -> None:
        # The exact mistake the docstrings warn about: the emoji, not the key.
        with pytest.raises(ValueError, match=r"KEY|key"):
            _validate_reaction("\U0001f44d")  # 👍

    @pytest.mark.parametrize("bad", ["thumbsup", "THUMBS_UP", "like", "", "+1"])
    def test_rejects_off_list_keys(self, bad: str) -> None:
        with pytest.raises(ValueError):
            _validate_reaction(bad)

    def test_error_lists_the_valid_set(self) -> None:
        with pytest.raises(ValueError, match="thumbs_up"):
            _validate_reaction("nope")


# ── The guards fire BEFORE any request is made ───────────────────────────────
#
# This is the part that matters operationally: a bad argument must cost zero
# network round-trips. We patch the transport and assert it is never touched.


class TestNoRequestIsMadeOnBadInput:
    def _client_and_transport(self):
        client = _authed_client()
        return client

    @patch("colony_sdk.client.urlopen")
    def test_create_post_empty_title(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "x"})
        with pytest.raises(ValueError):
            self._client_and_transport().create_post(title="", body="ok")
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_create_post_empty_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "x"})
        with pytest.raises(ValueError):
            self._client_and_transport().create_post(title="ok", body="   ")
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_create_comment_empty_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "x"})
        real = "2a2579a2-c0db-486a-ba05-3ef7ea05fc3d"
        with pytest.raises(ValueError):
            self._client_and_transport().create_comment(real, body="")
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_send_message_empty_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "x"})
        with pytest.raises(ValueError):
            self._client_and_transport().send_message("someone", body="")
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_search_empty_query(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"results": []})
        with pytest.raises(ValueError):
            self._client_and_transport().search(query="  ")
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_vote_post_bad_value(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"ok": True})
        real = "2a2579a2-c0db-486a-ba05-3ef7ea05fc3d"
        with pytest.raises(ValueError):
            self._client_and_transport().vote_post(real, value=0)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_vote_comment_bad_value(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"ok": True})
        real = "2a2579a2-c0db-486a-ba05-3ef7ea05fc3d"
        with pytest.raises(ValueError):
            self._client_and_transport().vote_comment(real, value=5)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_react_post_unicode_emoji(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"ok": True})
        real = "2a2579a2-c0db-486a-ba05-3ef7ea05fc3d"
        with pytest.raises(ValueError):
            self._client_and_transport().react_post(real, "\U0001f525")  # 🔥
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_vote_poll_empty_list(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"ok": True})
        real = "2a2579a2-c0db-486a-ba05-3ef7ea05fc3d"
        with pytest.raises(ValueError, match="at least one"):
            self._client_and_transport().vote_poll(real, option_ids=[])
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_subscribe_premium_bad_period(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"ok": True})
        with pytest.raises(ValueError, match=r"monthly.*annual|annual.*monthly"):
            self._client_and_transport().subscribe_premium(period="weekly")
        mock_urlopen.assert_not_called()


class TestValidInputStillReachesTheServer:
    """The guards must not block the happy path — a valid call still requests."""

    @patch("colony_sdk.client.urlopen")
    def test_valid_vote_reaches_transport(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"ok": True})
        real = "2a2579a2-c0db-486a-ba05-3ef7ea05fc3d"
        _authed_client().vote_post(real, value=-1)
        mock_urlopen.assert_called_once()

    @patch("colony_sdk.client.urlopen")
    def test_valid_reaction_reaches_transport(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"ok": True})
        real = "2a2579a2-c0db-486a-ba05-3ef7ea05fc3d"
        _authed_client().react_post(real, "fire")
        mock_urlopen.assert_called_once()

    @patch("colony_sdk.client.urlopen")
    def test_valid_post_reaches_transport(self, mock_urlopen: MagicMock) -> None:
        # colony resolves from the local COLONIES map, so no extra request.
        mock_urlopen.return_value = _mock_response({"id": "x"})
        _authed_client().create_post(title="A title", body="A body", colony="general")
        mock_urlopen.assert_called_once()
