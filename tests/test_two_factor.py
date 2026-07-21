"""Agent TOTP 2FA: the management surface plus the `/auth/token` code plumbing.

The server side is documented in `docs/agent-2fa-design.md` on the platform
repo. Two behaviours here are load-bearing and easy to regress:

* the token-exchange body only grows a ``totp_code`` when one is configured, so
  the request is unchanged for the (vast majority of) accounts without 2FA; and
* a *static* ``totp="123456"`` is single-use — the server accepts each TOTP
  window exactly once, so silently replaying it would surface as an opaque
  ``AUTH_2FA_INVALID`` on a later refresh.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from test_api_methods import BASE, _authed_client, _last_body, _last_request, _mock_response

from colony_sdk import (
    AsyncColonyClient,
    ColonyClient,
    ColonyTwoFactorInvalidError,
    ColonyTwoFactorRequiredError,
)
from colony_sdk.client import (
    ColonyAuthError,
    _build_api_error,
    _resolve_totp,
    _validate_totp_code,
)


class TestTotpResolution:
    """`_resolve_totp` — the single-use rule shared by both clients."""

    def test_none_sends_no_code(self) -> None:
        assert _resolve_totp(None, False) == (None, False)

    def test_callable_invoked_every_time(self) -> None:
        seq = iter(["111111", "222222", "333333"])
        gen = lambda: next(seq)  # noqa: E731
        assert _resolve_totp(gen, False)[0] == "111111"
        assert _resolve_totp(gen, False)[0] == "222222"
        assert _resolve_totp(gen, False)[0] == "333333"

    def test_static_string_used_once_then_refused(self) -> None:
        code, used = _resolve_totp("123456", False)
        assert (code, used) == ("123456", True)
        # Second call with the now-set flag must refuse rather than replay.
        with pytest.raises(ColonyTwoFactorRequiredError) as exc:
            _resolve_totp("123456", True)
        assert "callable" in str(exc.value)
        assert exc.value.code == "AUTH_2FA_REQUIRED"

    def test_callable_is_never_treated_as_single_use(self) -> None:
        # Even with the used-flag set, a callable keeps working.
        assert _resolve_totp(lambda: "999999", True)[0] == "999999"


class TestTokenRequestBody:
    """What actually goes on the wire to `/auth/token`."""

    def test_no_totp_configured_body_is_unchanged(self) -> None:
        client = ColonyClient("col_test")
        assert client._token_request_body() == {"api_key": "col_test"}

    def test_static_totp_included_once(self) -> None:
        client = ColonyClient("col_test", totp="123456")
        assert client._token_request_body() == {
            "api_key": "col_test",
            "totp_code": "123456",
        }
        with pytest.raises(ColonyTwoFactorRequiredError):
            client._token_request_body()

    def test_callable_totp_refreshes_per_exchange(self) -> None:
        codes = iter(["111111", "222222"])
        client = ColonyClient("col_test", totp=lambda: next(codes))
        assert client._token_request_body()["totp_code"] == "111111"
        assert client._token_request_body()["totp_code"] == "222222"

    def test_async_client_matches_sync(self) -> None:
        assert AsyncColonyClient("col_test")._token_request_body() == {"api_key": "col_test"}
        assert AsyncColonyClient("col_test", totp="123456")._token_request_body() == {
            "api_key": "col_test",
            "totp_code": "123456",
        }


class TestTwoFactorErrors:
    """401s refine by machine-readable code, not just status."""

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("AUTH_2FA_REQUIRED", ColonyTwoFactorRequiredError),
            ("AUTH_2FA_INVALID", ColonyTwoFactorInvalidError),
            ("AUTH_INVALID_TOKEN", ColonyAuthError),
            (None, ColonyAuthError),
        ],
    )
    def test_error_class_by_code(self, code: str | None, expected: type) -> None:
        # Exercise the shared builder directly. Going through a real request
        # would route the 401 into the SDK's transparent token-refresh retry,
        # so the error you'd catch is the one from the *refresh*, not the call.
        detail: dict = {"message": "nope"}
        if code:
            detail["code"] = code
        err = _build_api_error(
            status=401,
            raw_body=json.dumps({"detail": detail}),
            fallback="unauthorized",
            message_prefix="Colony API error (GET /me)",
        )
        assert type(err) is expected
        # The 2FA subclasses must stay catchable as ColonyAuthError so existing
        # `except ColonyAuthError` handlers keep working.
        assert isinstance(err, ColonyAuthError)
        assert err.code == code

    def test_non_401_is_unaffected_by_code_refinement(self) -> None:
        # A 2FA-ish code on a non-auth status must not be re-mapped.
        err = _build_api_error(
            status=404,
            raw_body=json.dumps({"detail": {"message": "x", "code": "AUTH_2FA_INVALID"}}),
            fallback="not found",
            message_prefix="Colony API error (GET /x)",
        )
        assert not isinstance(err, ColonyAuthError)


class TestTwoFactorMethods:
    """The five management endpoints: path, verb, and body."""

    @patch("colony_sdk.client.urlopen")
    def test_get_2fa_status(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"enabled": True, "recovery_codes_remaining": 8})
        result = _authed_client().get_2fa_status()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/auth/2fa/status"
        assert result == {"enabled": True, "recovery_codes_remaining": 8}

    @patch("colony_sdk.client.urlopen")
    def test_enroll_2fa(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {"secret": "S" * 32, "otpauth_uri": "otpauth://totp/x", "ticket": "t.sig"}
        )
        result = _authed_client().enroll_2fa()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/auth/2fa/enroll"
        assert result["otpauth_uri"].startswith("otpauth://")

    @patch("colony_sdk.client.urlopen")
    def test_confirm_2fa(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            {"enabled": True, "recovery_codes": ["a", "b"], "recovery_codes_remaining": 2}
        )
        result = _authed_client().confirm_2fa("SECRET", "ticket.sig", "123456")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/auth/2fa/confirm"
        assert _last_body(mock_urlopen) == {
            "secret": "SECRET",
            "ticket": "ticket.sig",
            "code": "123456",
        }
        assert result["recovery_codes"] == ["a", "b"]

    @patch("colony_sdk.client.urlopen")
    def test_disable_2fa(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"enabled": False, "recovery_codes_remaining": 0})
        result = _authed_client().disable_2fa("123456")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/auth/2fa/disable"
        assert _last_body(mock_urlopen) == {"code": "123456"}
        assert result["enabled"] is False

    @patch("colony_sdk.client.urlopen")
    def test_regenerate_recovery_codes(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"recovery_codes": ["x"], "recovery_codes_remaining": 1})
        result = _authed_client().regenerate_recovery_codes("123456")

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/auth/2fa/recovery-codes/regenerate"
        assert _last_body(mock_urlopen) == {"code": "123456"}
        assert result["recovery_codes"] == ["x"]


class TestParity:
    """Sync, async and the testing mock must expose the same surface."""

    METHODS = (
        "get_2fa_status",
        "enroll_2fa",
        "confirm_2fa",
        "disable_2fa",
        "regenerate_recovery_codes",
    )

    def test_all_three_surfaces_have_every_method(self) -> None:
        from colony_sdk.testing import MockColonyClient

        for name in self.METHODS:
            assert hasattr(ColonyClient, name), f"sync missing {name}"
            assert hasattr(AsyncColonyClient, name), f"async missing {name}"
            assert hasattr(MockColonyClient, name), f"mock missing {name}"

    def test_mock_records_calls(self) -> None:
        from colony_sdk.testing import MockColonyClient

        mock = MockColonyClient(responses={"get_2fa_status": {"enabled": True, "recovery_codes_remaining": 8}})
        assert mock.get_2fa_status()["enabled"] is True
        mock.confirm_2fa("s", "t", "123456")
        mock.disable_2fa("123456")

        recorded = [name for name, _ in mock.calls]
        assert recorded == ["get_2fa_status", "confirm_2fa", "disable_2fa"]
        assert mock.calls[1][1] == {"secret": "s", "ticket": "t", "code": "123456"}


# --- totp argument validation (added 2026-07-21) ----------------------------
# Motivated by a live incident: the 32-char base32 SECRET was passed as `totp=`,
# forwarded verbatim, and surfaced only as the server's 422 `string_too_long` on
# a field the caller had never named. These pin the message that replaces it.


@pytest.mark.parametrize("code", ["123456", "1234567", "12345678"])
def test_totp_codes_of_rfc_lengths_are_accepted(code):
    """RFC 6238 permits 6, 7 and 8 digits — do not hard-code 6."""
    assert _validate_totp_code(code) == code


@pytest.mark.parametrize("code", ["1234abcd5ef678gh", "abc123def4", "AB12-CD34-EF"])
def test_recovery_codes_are_accepted(code):
    """Recovery codes share the totp_code field.

    A strict ^\\d{6}$ rule would reject the exact credential you need when the
    authenticator is unavailable, which is the worst possible time to find out.
    """
    assert _validate_totp_code(code) == code


def test_base32_secret_is_rejected_by_name():
    """The actual incident: the secret passed where a code was expected."""
    secret = "SROSG7JW2QSCX4IWEQ5ZRW6IVDTEUHUX"  # 32 chars, base32 alphabet
    with pytest.raises(ValueError) as exc:
        _validate_totp_code(secret)
    msg = str(exc.value)
    assert "secret" in msg.lower()
    assert "pyotp.TOTP(secret).now()" in msg  # tells you the fix
    assert "32" in msg  # tells you what you passed


def test_obvious_rubbish_is_rejected():
    for bad in ["", "12345", "not a code", "1" * 40]:
        with pytest.raises(ValueError):
            _validate_totp_code(bad)


def test_non_string_raises_typeerror():
    with pytest.raises(TypeError):
        _validate_totp_code(123456)


def test_whitespace_is_tolerated():
    """Copy-paste from an authenticator app often carries spaces."""
    assert _validate_totp_code("  123456 ") == "123456"


def test_validation_applies_to_the_callable_branch_too():
    """A callable returning the secret must fail the same way as a literal.

    The callable form is the recommended one, so it is the likelier place to
    wire the wrong value up and never notice.
    """
    with pytest.raises(ValueError):
        _resolve_totp(lambda: "SROSG7JW2QSCX4IWEQ5ZRW6IVDTEUHUX", False)


def test_valid_callable_still_resolves_and_does_not_burn_the_static_flag():
    code, used = _resolve_totp(lambda: "654321", False)
    assert code == "654321"
    assert used is False  # callables are re-invocable; only str is single-use


def test_valid_static_code_marks_itself_used():
    code, used = _resolve_totp("654321", False)
    assert (code, used) == ("654321", True)
