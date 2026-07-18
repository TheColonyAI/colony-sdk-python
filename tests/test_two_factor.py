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
from colony_sdk.client import ColonyAuthError, _build_api_error, _resolve_totp


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
