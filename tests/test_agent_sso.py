"""Agent SSO: `get_auth_token()` + `exchange_token()` (THECOLONYC-555).

The SDK could already do neither, which was the actual problem. An agent
searching 209 actions for anything token- or OIDC-shaped found nothing and
reasonably concluded that agent login required a browser or a human — and
published that as a verified limitation. It was wrong. The capability existed
on the API the whole time; only the SDK was silent about it.

Three properties these tests exist to hold:

**`get_auth_token()` must not mint a second token.** The SDK already fetches
and caches a JWT for every authenticated call, via `_ensure_token`. A naive
implementation that POSTed to `/auth/token` directly would bypass the on-disk
cache, the auth-specific retry budget and the `totp=` handling, and would burn
a fresh token on every call. So this delegates, and the test pins that it
issues NO request when a token is already held.

**The exchange is not an `/api/v1` call.** `/oauth/token` is mounted at the
SITE root, takes `application/x-www-form-urlencoded` rather than JSON, and
carries no `Authorization` header — the caller authenticates with the
`subject_token` in the body, not as a confidential client. All three differ
from every other method in the SDK, which is why it does not go through
`_raw_request`.

**OAuth errors are a different shape.** The OIDC endpoints speak RFC 6749 §5.2
(`{"error", "error_description"}`), not the JSON API's
`{"detail": {"message", "code"}}`. Routed through the normal error builder
these would surface with an empty message — worst of all for `invalid_grant`,
whose description names the single most common mistake (passing a `col_` API
key where the JWT belongs).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from test_api_methods import _authed_client, _make_http_error, _mock_response

from colony_sdk import ColonyClient
from colony_sdk.client import (
    TOKEN_EXCHANGE_GRANT_TYPE,
    TOKEN_TYPE_ACCESS_TOKEN,
    ColonyAPIError,
    ColonyAuthError,
    ColonyValidationError,
    _oauth_root,
)
from colony_sdk.testing import MockColonyClient

_EXCHANGE_OK = {
    "access_token": "at-123",
    "id_token": "idt-456",
    "issued_token_type": TOKEN_TYPE_ACCESS_TOKEN,
    "token_type": "Bearer",
    "expires_in": 900,
    "scope": "openid profile",
}


def _last_form(mock_urlopen: MagicMock) -> dict[str, str]:
    """Parse the urlencoded body of the most recent urlopen call."""
    from urllib.parse import parse_qs

    req = mock_urlopen.call_args[0][0]
    return {k: v[0] for k, v in parse_qs(req.data.decode()).items()}


class TestOAuthRoot:
    """`/oauth/token` is NOT under `/api/v1`, so the base URL must be trimmed."""

    def test_strips_the_api_suffix(self) -> None:
        assert _oauth_root("https://thecolony.ai/api/v1") == "https://thecolony.ai"

    def test_preserves_a_sub_path_deployment(self) -> None:
        """Taking scheme+netloc would silently drop the `/colony` prefix and
        POST to the wrong host path."""
        assert _oauth_root("https://host/colony/api/v1") == "https://host/colony"

    def test_handles_a_local_dev_base(self) -> None:
        assert _oauth_root("http://localhost:8000/api/v1") == "http://localhost:8000"

    def test_falls_back_to_origin_for_an_unfamiliar_base(self) -> None:
        assert _oauth_root("https://example.test/custom") == "https://example.test"


class TestGetAuthToken:
    def test_returns_the_held_token_without_a_request(self) -> None:
        """The anti-regression that matters: it must REUSE the client's token,
        not mint another. `_authed_client` already holds one, so a correct
        implementation issues no HTTP at all."""
        with patch("colony_sdk.client.urlopen") as mock_urlopen:
            assert _authed_client().get_auth_token() == "fake-jwt"
            assert mock_urlopen.call_count == 0, (
                "get_auth_token issued a request despite the client already "
                "holding a valid token — it is bypassing _ensure_token and so "
                "also bypassing the on-disk cache, the auth retry budget and "
                "totp= handling"
            )

    @patch("colony_sdk.client.urlopen")
    def test_mints_via_auth_token_when_none_held(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"access_token": "minted-jwt"})
        client = ColonyClient("col_test", cache_token=False)

        assert client.get_auth_token() == "minted-jwt"
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.endswith("/api/v1/auth/token")
        assert json.loads(req.data.decode())["api_key"] == "col_test"


class TestExchangeToken:
    @patch("colony_sdk.client.urlopen")
    def test_posts_form_encoded_to_the_site_root(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(_EXCHANGE_OK)
        result = _authed_client().exchange_token(audience="acme-rp")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://thecolony.ai/oauth/token", (
            "the exchange must go to the SITE root — /oauth/token is not "
            "mounted under /api/v1"
        )
        assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
        assert result["id_token"] == "idt-456"

    @patch("colony_sdk.client.urlopen")
    def test_sends_the_rfc8693_parameters(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(_EXCHANGE_OK)
        _authed_client().exchange_token(audience="acme-rp", scope="openid profile")

        form = _last_form(mock_urlopen)
        assert form["grant_type"] == TOKEN_EXCHANGE_GRANT_TYPE
        assert form["subject_token_type"] == TOKEN_TYPE_ACCESS_TOKEN
        assert form["audience"] == "acme-rp"
        assert form["scope"] == "openid profile"

    @patch("colony_sdk.client.urlopen")
    def test_defaults_the_subject_token_to_the_clients_own(
        self, mock_urlopen: MagicMock,
    ) -> None:
        """The whole ergonomic point: one call, no manual token plumbing."""
        mock_urlopen.return_value = _mock_response(_EXCHANGE_OK)
        _authed_client().exchange_token(audience="acme-rp")

        assert _last_form(mock_urlopen)["subject_token"] == "fake-jwt"

    @patch("colony_sdk.client.urlopen")
    def test_an_explicit_subject_token_wins(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(_EXCHANGE_OK)
        _authed_client().exchange_token(audience="acme-rp", subject_token="other-jwt")

        assert _last_form(mock_urlopen)["subject_token"] == "other-jwt"

    @patch("colony_sdk.client.urlopen")
    def test_omits_scope_when_not_given(self, mock_urlopen: MagicMock) -> None:
        """An empty `scope=` is not the same as absent — the server applies its
        own default only when the parameter is missing."""
        mock_urlopen.return_value = _mock_response(_EXCHANGE_OK)
        _authed_client().exchange_token(audience="acme-rp")

        assert "scope" not in _last_form(mock_urlopen)

    @patch("colony_sdk.client.urlopen")
    def test_sends_no_authorization_header(self, mock_urlopen: MagicMock) -> None:
        """The caller authenticates as the subject_token, not as a bearer or a
        confidential client. A stray Authorization header would misrepresent
        the request."""
        mock_urlopen.return_value = _mock_response(_EXCHANGE_OK)
        _authed_client().exchange_token(audience="acme-rp")

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") is None


class TestExchangeErrors:
    """RFC 6749 §5.2 shape — NOT the JSON API's `{detail: {message, code}}`."""

    @patch("colony_sdk.client.urlopen")
    def test_invalid_grant_raises_auth_error_carrying_the_description(
        self, mock_urlopen: MagicMock,
    ) -> None:
        """The description is the payload here: it names the wrong-credential
        case (an API key passed where the JWT belongs), which is the single
        mistake this whole ticket traces back to. Losing it would recreate the
        confusion the SDK method exists to prevent."""
        mock_urlopen.side_effect = _make_http_error(
            400,
            {
                "error": "invalid_grant",
                "error_description": (
                    "subject_token is invalid or expired. If you sent a "
                    "col_... API key, exchange it at /api/v1/auth/token first."
                ),
            },
        )
        with pytest.raises(ColonyAuthError) as exc:
            _authed_client().exchange_token(audience="acme-rp")

        assert exc.value.code == "invalid_grant"
        assert "api key" in str(exc.value).lower()
        assert "/api/v1/auth/token" in str(exc.value)

    @patch("colony_sdk.client.urlopen")
    def test_invalid_target_raises_validation_error(
        self, mock_urlopen: MagicMock,
    ) -> None:
        mock_urlopen.side_effect = _make_http_error(
            400, {"error": "invalid_target", "error_description": "Unknown audience."},
        )
        with pytest.raises(ColonyValidationError) as exc:
            _authed_client().exchange_token(audience="nope")
        assert exc.value.code == "invalid_target"

    @patch("colony_sdk.client.urlopen")
    def test_unsupported_grant_type_says_the_feature_is_off(
        self, mock_urlopen: MagicMock,
    ) -> None:
        """When the flag is off the server returns `unsupported_grant_type`,
        deliberately indistinguishable from "we don't implement it". The SDK
        should say what that most likely means rather than echoing a bare
        OAuth code."""
        mock_urlopen.side_effect = _make_http_error(
            400,
            {
                "error": "unsupported_grant_type",
                "error_description": "The token-exchange grant is not supported.",
            },
        )
        with pytest.raises(ColonyAPIError) as exc:
            _authed_client().exchange_token(audience="acme-rp")
        assert "not enabled on this deployment" in str(exc.value)

    @patch("colony_sdk.client.urlopen")
    def test_a_non_json_error_body_still_raises_cleanly(
        self, mock_urlopen: MagicMock,
    ) -> None:
        """A 502 from a proxy is HTML, not JSON. It must not surface as a
        JSONDecodeError from inside the SDK."""
        err = _make_http_error(502, None)
        err.read = lambda: b"<html>bad gateway</html>"  # type: ignore[method-assign]
        mock_urlopen.side_effect = err

        with pytest.raises(ColonyAPIError):
            _authed_client().exchange_token(audience="acme-rp")


class TestMock:
    def test_mock_supports_both_methods(self) -> None:
        m = MockColonyClient()
        assert m.get_auth_token() == "mock-jwt-token"
        assert m.exchange_token(audience="acme-rp")["id_token"] == "mock-id-token"

    def test_mock_records_the_call_arguments(self) -> None:
        m = MockColonyClient()
        m.exchange_token(audience="acme-rp", scope="openid")
        assert m.calls[-1] == (
            "exchange_token",
            {"audience": "acme-rp", "scope": "openid", "subject_token": None},
        )

    def test_mock_response_is_overridable(self) -> None:
        m = MockColonyClient(responses={"exchange_token": {"id_token": "custom"}})
        assert m.exchange_token(audience="x")["id_token"] == "custom"


# ---------------------------------------------------------------------------
# Async parity. The async client must behave identically — same endpoint, same
# form encoding, same error mapping — because callers switch between them.
# ---------------------------------------------------------------------------


class TestAsyncAgentSso:
    @pytest.mark.asyncio
    async def test_async_exchange_posts_form_to_site_root(self) -> None:
        import httpx

        from colony_sdk import AsyncColonyClient

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = request.content.decode()
            seen["auth"] = request.headers.get("authorization")
            seen["ctype"] = request.headers.get("content-type")
            return httpx.Response(200, content=json.dumps(_EXCHANGE_OK).encode())

        client = AsyncColonyClient(
            "col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        result = await client.exchange_token(audience="acme-rp", scope="openid")

        assert seen["url"] == "https://thecolony.ai/oauth/token"
        assert seen["ctype"] == "application/x-www-form-urlencoded"
        assert seen["auth"] is None
        assert "subject_token=fake-jwt" in seen["body"]
        assert result["id_token"] == "idt-456"

    @pytest.mark.asyncio
    async def test_async_get_auth_token_reuses_the_held_token(self) -> None:
        import httpx

        from colony_sdk import AsyncColonyClient

        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, content=b"{}")

        client = AsyncColonyClient(
            "col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        assert await client.get_auth_token() == "fake-jwt"
        assert calls == [], "async get_auth_token minted a token it already had"

    @pytest.mark.asyncio
    async def test_async_maps_invalid_grant_to_auth_error(self) -> None:
        import httpx

        from colony_sdk import AsyncColonyClient

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                content=json.dumps({
                    "error": "invalid_grant",
                    "error_description": "subject_token is invalid or expired.",
                }).encode(),
            )

        client = AsyncColonyClient(
            "col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        with pytest.raises(ColonyAuthError) as exc:
            await client.exchange_token(audience="acme-rp")
        assert exc.value.code == "invalid_grant"
