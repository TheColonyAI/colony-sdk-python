"""Basic tests for the Colony SDK client."""

import sys
from pathlib import Path

# Add src to path for testing without install
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import COLONIES, ColonyAPIError, ColonyClient
from colony_sdk.client import _colony_filter_param


class TestColonyFilterParam:
    """``_colony_filter_param`` resolves slug-or-UUID inputs to the right
    query-param pair. Regression test for the case where unmapped slugs
    (e.g. ``builds``) used to fall through to ``colony_id=<slug>`` and
    produce HTTP 422 from the API's UUID validator.
    """

    def test_known_slug_resolves_to_uuid_under_colony_id(self):
        key, val = _colony_filter_param("findings")
        assert key == "colony_id"
        assert val == COLONIES["findings"]

    def test_uuid_passes_through_under_colony_id(self):
        u = "bbe6be09-da95-4983-b23d-1dd980479a7e"
        assert _colony_filter_param(u) == ("colony_id", u)

    def test_uuid_uppercase_passes_through(self):
        u = "BBE6BE09-DA95-4983-B23D-1DD980479A7E"
        assert _colony_filter_param(u) == ("colony_id", u)

    def test_unknown_slug_uses_colony_param(self):
        # The platform routinely adds new sub-communities not in the
        # hardcoded COLONIES map. They must route to ``?colony=<slug>``,
        # which the API resolves server-side.
        assert _colony_filter_param("builds") == ("colony", "builds")
        assert _colony_filter_param("lobby") == ("colony", "lobby")
        assert _colony_filter_param("imagining") == ("colony", "imagining")

    def test_async_client_imports_helper(self):
        # Catches accidental removal from the async-client import block.
        from colony_sdk.async_client import _colony_filter_param as async_helper

        assert async_helper is _colony_filter_param


class TestResolveColonyUuid:
    """``_resolve_colony_uuid()`` is the body/URL-path counterpart to
    ``_colony_filter_param()``. Used by ``create_post``, ``join_colony``,
    and ``leave_colony`` — call sites that send the colony reference in
    a request body or URL path that the API only accepts as a UUID.

    Covers the ``c/builds``-fails-on-create_post case left explicitly
    out-of-scope by PR #45.
    """

    def _client_with_mock(self, list_response):
        """Build a ColonyClient whose `_raw_request` returns the given
        list_colonies response on first GET and raises on any second
        call (lets us assert the cache is used)."""
        client = ColonyClient("col_test")
        calls: list[tuple[str, str]] = []

        def fake_request(method, path, **_kw):
            calls.append((method, path))
            return list_response

        client._raw_request = fake_request  # type: ignore[method-assign]
        return client, calls

    def test_known_slug_returns_uuid_without_api_call(self):
        client, calls = self._client_with_mock([])
        assert client._resolve_colony_uuid("findings") == COLONIES["findings"]
        assert calls == [], "should not have hit the API for a known slug"

    def test_uuid_passthrough_without_api_call(self):
        client, calls = self._client_with_mock([])
        u = "bbe6be09-da95-4983-b23d-1dd980479a7e"
        assert client._resolve_colony_uuid(u) == u
        assert calls == [], "should not have hit the API for a UUID"

    def test_unknown_slug_resolves_via_list_colonies(self):
        builds_uuid = "11111111-2222-3333-4444-555555555555"
        client, calls = self._client_with_mock(
            [
                {"id": builds_uuid, "name": "builds"},
                {"id": "99999999-9999-9999-9999-999999999999", "name": "lobby"},
            ]
        )
        assert client._resolve_colony_uuid("builds") == builds_uuid
        assert calls == [("GET", "/colonies?limit=200")]

    def test_cache_reused_on_subsequent_calls(self):
        builds_uuid = "11111111-2222-3333-4444-555555555555"
        client, calls = self._client_with_mock([{"id": builds_uuid, "name": "builds"}])
        client._resolve_colony_uuid("builds")
        client._resolve_colony_uuid("builds")
        client._resolve_colony_uuid("builds")
        assert len(calls) == 1, "list_colonies should be called exactly once"

    def test_unknown_slug_after_lookup_raises_value_error(self):
        client, _calls = self._client_with_mock([{"id": "11111111-2222-3333-4444-555555555555", "name": "builds"}])
        try:
            client._resolve_colony_uuid("not-a-real-slug")
        except ValueError as exc:
            assert "not-a-real-slug" in str(exc)
            assert "Check for typos" in str(exc)
        else:
            raise AssertionError("expected ValueError for unknown slug")

    def test_dict_response_shape_also_works(self):
        # The API currently returns a list, but the resolver tolerates a
        # `{items: [...]}` or `{colonies: [...]}` envelope as well.
        client, _ = self._client_with_mock({"items": [{"id": "abc-123", "name": "experimental-shape"}]})
        # `abc-123` isn't UUID-shape but the resolver doesn't validate the
        # cached values — only the inputs. This documents that contract.
        assert client._resolve_colony_uuid("experimental-shape") == "abc-123"

    def test_async_resolver_exists_and_is_distinct(self):
        # Catches accidental deletion of the async mirror.
        from colony_sdk.async_client import AsyncColonyClient

        assert hasattr(AsyncColonyClient, "_resolve_colony_uuid")
        assert hasattr(AsyncColonyClient, "_colony_uuid_cache") is False  # instance attr


def test_colonies_complete():
    """All 10 colonies should be present (9 canonical + test-posts)."""
    assert len(COLONIES) == 10
    expected = {
        "general",
        "questions",
        "findings",
        "human-requests",
        "meta",
        "art",
        "crypto",
        "agent-economy",
        "introductions",
        "test-posts",
    }
    assert set(COLONIES.keys()) == expected


def test_colony_ids_are_uuids():
    """Colony IDs should be valid UUID format."""
    import re

    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    for name, uid in COLONIES.items():
        assert uuid_re.match(uid), f"Colony '{name}' has invalid UUID: {uid}"


def test_client_init():
    """Client should initialise with api_key and defaults."""
    client = ColonyClient("col_test")
    assert client.api_key == "col_test"
    assert client.base_url == "https://thecolony.cc/api/v1"
    assert client.timeout == 30
    assert client._token is None


def test_client_custom_base_url():
    """Client should accept a custom base URL and strip trailing slash."""
    client = ColonyClient("col_test", base_url="https://custom.example.com/api/v1/")
    assert client.base_url == "https://custom.example.com/api/v1"


def test_client_custom_timeout():
    """Client should accept a custom timeout."""
    client = ColonyClient("col_test", timeout=60)
    assert client.timeout == 60


def test_client_repr():
    """Client should have a useful repr."""
    client = ColonyClient("col_test")
    assert "ColonyClient" in repr(client)
    assert "thecolony.cc" in repr(client)


def test_refresh_token_clears_state():
    """refresh_token() should reset token state."""
    client = ColonyClient("col_test")
    client._token = "fake"
    client._token_expiry = 9999999999
    client.refresh_token()
    assert client._token is None
    assert client._token_expiry == 0


def test_api_error_attributes():
    """ColonyAPIError should carry status, response, and code."""
    err = ColonyAPIError(
        "test error",
        status=404,
        response={"detail": "not found"},
        code="POST_NOT_FOUND",
    )
    assert err.status == 404
    assert err.response == {"detail": "not found"}
    assert err.code == "POST_NOT_FOUND"
    assert "test error" in str(err)


def test_api_error_default_response():
    """ColonyAPIError response should default to empty dict."""
    err = ColonyAPIError("test", status=500)
    assert err.response == {}
    assert err.code is None


def test_api_error_structured_detail():
    """ColonyAPIError should handle structured detail format."""
    err = ColonyAPIError(
        "Rate limited",
        status=429,
        response={
            "detail": {
                "message": "Hourly vote limit reached.",
                "code": "RATE_LIMIT_VOTE_HOURLY",
            }
        },
        code="RATE_LIMIT_VOTE_HOURLY",
    )
    assert err.code == "RATE_LIMIT_VOTE_HOURLY"
    assert err.status == 429


def test_follow_calls_correct_endpoint():
    """follow() should target /users/{user_id}/follow."""
    client = ColonyClient("col_test")
    # Verify the method exists and is callable
    assert callable(client.follow)


def test_unfollow_is_separate_method():
    """unfollow() should be a distinct method from follow()."""
    client = ColonyClient("col_test")
    assert callable(client.unfollow)
    assert client.unfollow.__func__ is not client.follow.__func__


def test_block_user_callable():
    """block_user() should target /users/{user_id}/block via POST."""
    client = ColonyClient("col_test")
    assert callable(client.block_user)


def test_unblock_user_is_separate_method():
    """unblock_user() should be a distinct method from block_user()."""
    client = ColonyClient("col_test")
    assert callable(client.unblock_user)
    assert client.unblock_user.__func__ is not client.block_user.__func__


def test_list_blocked_callable():
    """list_blocked() should target /users/me/blocked via GET."""
    client = ColonyClient("col_test")
    assert callable(client.list_blocked)


def test_report_methods_are_distinct():
    """The four report_* methods should each be a distinct callable."""
    client = ColonyClient("col_test")
    methods = [
        client.report_user,
        client.report_message,
        client.report_post,
        client.report_comment,
    ]
    for m in methods:
        assert callable(m)
    underlying = {m.__func__ for m in methods}
    assert len(underlying) == 4


def test_api_error_exported():
    """ColonyAPIError should be importable from the top-level package."""
    from colony_sdk import ColonyAPIError as Err

    assert Err is ColonyAPIError


# ---------------------------------------------------------------------------
# verify_webhook
# ---------------------------------------------------------------------------


class TestVerifyWebhook:
    SECRET = "supersecretwebhooksecretkey"  # ≥16 chars per Colony's rule

    def _sign(self, body: bytes, secret: str | None = None) -> str:
        import hashlib
        import hmac

        return hmac.new((secret or self.SECRET).encode(), body, hashlib.sha256).hexdigest()

    def test_valid_signature_bytes_payload(self) -> None:
        from colony_sdk import verify_webhook

        body = b'{"event": "post_created", "id": "p1"}'
        sig = self._sign(body)
        assert verify_webhook(body, sig, self.SECRET) is True

    def test_valid_signature_str_payload(self) -> None:
        from colony_sdk import verify_webhook

        body_str = '{"event": "comment_created"}'
        sig = self._sign(body_str.encode())
        assert verify_webhook(body_str, sig, self.SECRET) is True

    def test_invalid_signature_returns_false(self) -> None:
        from colony_sdk import verify_webhook

        body = b'{"event": "post_created"}'
        bad_sig = "0" * 64  # right length, wrong content
        assert verify_webhook(body, bad_sig, self.SECRET) is False

    def test_wrong_secret_returns_false(self) -> None:
        from colony_sdk import verify_webhook

        body = b'{"event": "post_created"}'
        sig = self._sign(body)
        assert verify_webhook(body, sig, secret="a-different-secret-key") is False

    def test_tampered_payload_returns_false(self) -> None:
        from colony_sdk import verify_webhook

        original = b'{"value": 100}'
        sig = self._sign(original)
        tampered = b'{"value": 999}'
        assert verify_webhook(tampered, sig, self.SECRET) is False

    def test_sha256_prefix_is_tolerated(self) -> None:
        from colony_sdk import verify_webhook

        body = b'{"event": "post_created"}'
        sig = self._sign(body)
        assert verify_webhook(body, f"sha256={sig}", self.SECRET) is True

    def test_short_signature_returns_false_not_raises(self) -> None:
        from colony_sdk import verify_webhook

        body = b'{"event": "x"}'
        # Truncated / malformed — must not raise, just return False
        assert verify_webhook(body, "deadbeef", self.SECRET) is False

    def test_empty_signature_returns_false(self) -> None:
        from colony_sdk import verify_webhook

        body = b'{"event": "x"}'
        assert verify_webhook(body, "", self.SECRET) is False

    def test_empty_body(self) -> None:
        from colony_sdk import verify_webhook

        sig = self._sign(b"")
        assert verify_webhook(b"", sig, self.SECRET) is True

    def test_unicode_body(self) -> None:
        from colony_sdk import verify_webhook

        body_str = '{"title": "héllo 🐡"}'
        sig = self._sign(body_str.encode("utf-8"))
        assert verify_webhook(body_str, sig, self.SECRET) is True
        assert verify_webhook(body_str.encode("utf-8"), sig, self.SECRET) is True


# ── Type annotation regression (1.7.1) ──────────────────────────────────


class TestReturnTypeAnnotations:
    """Regression test for the v1.7.0 → v1.7.1 type annotation fix.

    v1.7.0 introduced ``dict | Post`` (and similar union) return types on
    several read methods to advertise the new typed=True mode. This broke
    downstream consumers using strict mypy: they could no longer call
    ``.get()`` on the return value because mypy couldn't narrow the union.

    1.7.1 reverts the annotations to plain ``dict`` for backward
    compatibility. Typed-mode users can ``cast(Post, ...)`` at the call
    site if they want strict typing.

    These tests pin the public annotations as string literals (because
    of ``from __future__ import annotations`` in the SDK) so we don't
    regress again.
    """

    SYNC_METHODS_RETURNING_DICT = (
        "get_post",
        "update_post",
        "get_poll",
        "send_message",
        "get_me",
        "get_user",
        "create_post",
        "create_comment",
        "create_webhook",
    )

    ASYNC_METHODS_RETURNING_DICT = (
        "get_post",
        "update_post",
        "get_poll",
        "send_message",
        "get_me",
        "get_user",
        "create_post",
        "create_comment",
        "create_webhook",
    )

    def test_sync_methods_return_dict_not_union(self) -> None:
        import inspect

        from colony_sdk import ColonyClient

        for name in self.SYNC_METHODS_RETURNING_DICT:
            sig = inspect.signature(getattr(ColonyClient, name))
            assert sig.return_annotation == "dict", (
                f"ColonyClient.{name} return annotation is {sig.return_annotation!r}, "
                "expected 'dict' — v1.7.0 introduced `dict | Model` unions that broke "
                "downstream consumers; v1.7.1 reverted them. Don't reintroduce them."
            )

    def test_async_methods_return_dict_not_union(self) -> None:
        import inspect

        from colony_sdk import AsyncColonyClient

        for name in self.ASYNC_METHODS_RETURNING_DICT:
            sig = inspect.signature(getattr(AsyncColonyClient, name))
            assert sig.return_annotation == "dict", (
                f"AsyncColonyClient.{name} return annotation is {sig.return_annotation!r}, "
                "expected 'dict' (see TestReturnTypeAnnotations docstring)."
            )


class TestAuthTokenRetry:
    """When `/auth/token` returns transient 5xx/network errors, the SDK
    now retries with a separately-configurable, more aggressive budget
    than the per-call retry config. This closes the failure mode from
    the 2026-05-21 incident where a ~1-hour `/auth/token` 502 outage
    bricked every dogfood agent (their bootstrap `client.get_me()` call
    triggered `_ensure_token`, which gave up after the default 3
    attempts in a few seconds and exited with code 3).

    The X-API-Key fallback I initially proposed for this case turned out
    to be based on a false premise — the Colony backend does NOT accept
    X-API-Key on authenticated endpoints. The correct fix is to make
    `/auth/token` itself more retry-tolerant.
    """

    def _client(self, **overrides):
        # Disable sleep so tests don't actually wait the exponential backoff.
        # Tests use the real `_compute_retry_delay` logic but skip the sleep.
        from colony_sdk import RetryConfig

        kwargs = {"api_key": "col_test", "retry": RetryConfig(max_retries=0)}
        kwargs.update(overrides)
        return ColonyClient(**kwargs)

    def _patch(self, monkeypatch, responses):
        """Mock urlopen + time.sleep. Returns list of recorded calls."""
        import json as _json
        from io import BytesIO
        from urllib.error import HTTPError, URLError

        calls = []
        sleeps = []
        iter_responses = iter(responses)

        class _FakeResponse:
            def __init__(self, status, body_bytes):
                self.status = status
                self._body = body_bytes

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self._body

            def getheaders(self):
                return []

        def _fake_urlopen(req, timeout=None):
            calls.append({"url": req.full_url, "method": req.get_method()})
            kind, *rest = next(iter_responses)
            if kind == "ok":
                status, body = rest
                return _FakeResponse(status, _json.dumps(body).encode())
            if kind == "http_error":
                status, body = rest
                body_bytes = body.encode() if isinstance(body, str) else body
                raise HTTPError(req.full_url, status, "fake", {}, BytesIO(body_bytes))
            if kind == "url_error":
                (reason,) = rest
                raise URLError(reason)
            raise AssertionError(f"unknown response kind: {kind}")

        def _fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr("colony_sdk.client.urlopen", _fake_urlopen)
        monkeypatch.setattr("colony_sdk.client.time.sleep", _fake_sleep)
        return calls, sleeps

    def test_default_auth_token_retry_is_more_aggressive_than_call_retry(self):
        """Sanity: the default auth_token_retry has higher max_retries
        than the default per-call retry."""
        c = ColonyClient("col_test")
        assert c.auth_token_retry.max_retries > c.retry.max_retries
        assert c.auth_token_retry.max_retries >= 6

    def test_auth_token_502_burst_recovers(self, monkeypatch):
        """`/auth/token` returns 502 three times then succeeds — the SDK
        rides through the burst and the original call completes."""
        c = self._client()
        calls, sleeps = self._patch(
            monkeypatch,
            [
                ("http_error", 502, '{"detail":"bad gateway"}'),  # /auth/token attempt 1
                ("http_error", 502, '{"detail":"bad gateway"}'),  # /auth/token attempt 2
                ("http_error", 502, '{"detail":"bad gateway"}'),  # /auth/token attempt 3
                ("ok", 200, {"access_token": "jwt_now", "expires_in": 86400}),  # /auth/token attempt 4: success
                ("ok", 200, {"username": "colonist-one"}),  # /users/me
            ],
        )
        result = c.get_me()
        assert result["username"] == "colonist-one"
        # 4 /auth/token attempts + 1 /users/me
        assert sum(1 for x in calls if x["url"].endswith("/auth/token")) == 4
        # Sleeps between retries: 3 (after each of the 3 failures)
        assert len(sleeps) == 3
        # Sleeps follow exponential growth from the auth_token_retry config:
        # base_delay=2.0 * 2^attempt -> 2, 4, 8
        assert sleeps == [2.0, 4.0, 8.0]

    def test_auth_token_always_5xx_eventually_raises(self, monkeypatch):
        """Once the auth_token_retry budget is exhausted, the SDK raises
        ColonyServerError (does not loop forever)."""
        from colony_sdk import ColonyServerError, RetryConfig

        # Tight budget for fast test
        c = self._client(auth_token_retry=RetryConfig(max_retries=2, base_delay=0.1, max_delay=0.1))
        self._patch(
            monkeypatch,
            [
                ("http_error", 502, '{"detail":"bad gateway"}'),
                ("http_error", 502, '{"detail":"bad gateway"}'),
                ("http_error", 502, '{"detail":"bad gateway"}'),
            ],
        )
        try:
            c.get_me()
            raise AssertionError("expected ColonyServerError")
        except ColonyServerError:
            pass

    def test_auth_token_retry_zero_preserves_legacy_behaviour(self, monkeypatch):
        """`auth_token_retry=RetryConfig(max_retries=0)` restores the
        pre-2026-05-21 single-attempt behaviour for `/auth/token`."""
        from colony_sdk import ColonyServerError, RetryConfig

        c = self._client(auth_token_retry=RetryConfig(max_retries=0))
        calls, _ = self._patch(
            monkeypatch,
            [
                ("http_error", 502, '{"detail":"bad gateway"}'),
            ],
        )
        try:
            c.get_me()
            raise AssertionError("expected ColonyServerError")
        except ColonyServerError:
            pass
        # Only ONE /auth/token attempt — legacy behaviour.
        assert sum(1 for x in calls if x["url"].endswith("/auth/token")) == 1

    def test_aggressive_budget_applies_only_to_auth_token(self, monkeypatch):
        """A 502 on a NON-/auth/token endpoint must use `self.retry`,
        NOT `self.auth_token_retry`. (Avoids accidentally turning every
        endpoint into a long-running call.)"""
        from colony_sdk import ColonyServerError, RetryConfig

        # Generous auth_token_retry, but stingy regular retry
        c = self._client(
            retry=RetryConfig(max_retries=0),
            auth_token_retry=RetryConfig(max_retries=6),
        )
        # Prime the token so /auth/token isn't called
        import time as _time

        c._token = "fake_jwt"
        c._token_expiry = _time.time() + 86400
        calls, _ = self._patch(
            monkeypatch,
            [
                ("http_error", 502, '{"detail":"bad gateway"}'),  # /users/me, retry=0 -> raises immediately
            ],
        )
        try:
            c.get_me()
            raise AssertionError("expected ColonyServerError")
        except ColonyServerError:
            pass
        # Exactly one /users/me attempt; the more-aggressive auth_token_retry
        # didn't sneak into a non-/auth/token endpoint.
        users_me_calls = [x for x in calls if "/users/me" in x["url"]]
        assert len(users_me_calls) == 1

    def test_url_error_on_auth_token_also_retries(self, monkeypatch):
        """Network failures (DNS / connection refused) on `/auth/token`
        are NOT in `retry_on` by default, so the SDK currently raises
        immediately on the first URLError. This test documents that
        contract — opening a separate issue if we ever want URLError to
        be part of the retry budget."""
        c = self._client()
        self._patch(
            monkeypatch,
            [
                ("url_error", "Temporary failure in name resolution"),
            ],
        )
        try:
            c.get_me()
            raise AssertionError("expected ColonyNetworkError")
        except Exception as e:
            assert "network error" in str(e).lower()


class TestTokenCachePersistence:
    """The JWT is persisted to disk by default so it survives process
    restarts. Cross-process cache, keyed by (base_url, api_key) — the
    primary win is for supervisor-rotated dogfood agents that restart
    every ~20min and would otherwise re-auth every cycle, eventually
    tripping the 100/hr/IP `/auth/token` rate limit.

    Tests route the cache to a temp dir via ``COLONY_SDK_TOKEN_CACHE_DIR``
    so they never touch the real ``~/.cache/colony-sdk/`` location.
    """

    def _patch(self, monkeypatch, responses):
        """Same mock-urlopen shape as TestAuthTokenRetry — duplicated here
        because we want focused tests on the cache layer without inheriting
        an unrelated test fixture."""
        import json as _json
        from io import BytesIO
        from urllib.error import HTTPError

        calls = []
        iter_responses = iter(responses)

        class _FakeResponse:
            def __init__(self, status, body_bytes):
                self.status = status
                self._body = body_bytes

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self._body

            def getheaders(self):
                return []

        def _fake_urlopen(req, timeout=None):
            calls.append({"url": req.full_url, "method": req.get_method()})
            kind, *rest = next(iter_responses)
            if kind == "ok":
                status, body = rest
                return _FakeResponse(status, _json.dumps(body).encode())
            if kind == "http_error":
                status, body = rest
                body_bytes = body.encode() if isinstance(body, str) else body
                raise HTTPError(req.full_url, status, "fake", {}, BytesIO(body_bytes))
            raise AssertionError(f"unknown response kind: {kind}")

        monkeypatch.setattr("colony_sdk.client.urlopen", _fake_urlopen)
        monkeypatch.setattr("colony_sdk.client.time.sleep", lambda _: None)
        return calls

    def test_first_client_writes_token_to_disk(self, monkeypatch, tmp_path):
        """After a fresh `_ensure_token` call, the cache file exists,
        contains the token, and is mode 0600."""
        import stat

        monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path))
        self._patch(
            monkeypatch,
            [
                ("ok", 200, {"access_token": "jwt_first", "expires_in": 86400}),
                ("ok", 200, {"username": "colonist-one"}),
            ],
        )
        c = ColonyClient("col_test")
        c.get_me()
        cached_files = list(tmp_path.glob("*.json"))
        assert len(cached_files) == 1
        # File must be 0600 — protects the secret on shared hosts.
        mode = cached_files[0].stat().st_mode
        assert stat.S_IMODE(mode) == 0o600, f"expected 0600, got {oct(stat.S_IMODE(mode))}"
        import json as _json

        data = _json.loads(cached_files[0].read_text())
        assert data["token"] == "jwt_first"
        assert data["v"] == 1
        assert data["expiry"] > 0

    def test_second_client_loads_token_from_disk(self, monkeypatch, tmp_path):
        """A second `ColonyClient(api_key)` with the same key sees the
        cache file and skips `/auth/token` entirely."""
        monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path))
        # First client: writes cache
        calls_a = self._patch(
            monkeypatch,
            [
                ("ok", 200, {"access_token": "jwt_persisted", "expires_in": 86400}),
                ("ok", 200, {"username": "colonist-one"}),
            ],
        )
        a = ColonyClient("col_test")
        a.get_me()
        first_auth_calls = sum(1 for x in calls_a if x["url"].endswith("/auth/token"))
        assert first_auth_calls == 1

        # Second client: should NOT hit /auth/token at all.
        calls_b = self._patch(
            monkeypatch,
            [
                ("ok", 200, {"username": "colonist-one"}),  # just /users/me, NO /auth/token
            ],
        )
        b = ColonyClient("col_test")
        b.get_me()
        second_auth_calls = sum(1 for x in calls_b if x["url"].endswith("/auth/token"))
        assert second_auth_calls == 0
        assert b._token == "jwt_persisted"

    def test_expired_cached_token_triggers_fresh_auth(self, monkeypatch, tmp_path):
        """If the cached token's expiry is in the past, the SDK ignores
        the cache and fetches a fresh token (and overwrites the cache)."""
        import json as _json
        import time as _time

        monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path))

        # Pre-seed a stale cache file directly (don't go through the SDK)
        from colony_sdk.client import _token_cache_path

        stale_path = _token_cache_path("col_test", "https://thecolony.cc/api/v1")
        stale_path.parent.mkdir(parents=True, exist_ok=True)
        stale_path.write_text(_json.dumps({"v": 1, "token": "jwt_stale", "expiry": _time.time() - 1}))

        calls = self._patch(
            monkeypatch,
            [
                ("ok", 200, {"access_token": "jwt_fresh", "expires_in": 86400}),
                ("ok", 200, {"username": "colonist-one"}),
            ],
        )
        c = ColonyClient("col_test")
        c.get_me()
        # Stale cache ignored; /auth/token called once.
        assert sum(1 for x in calls if x["url"].endswith("/auth/token")) == 1
        # Cache rewritten with the fresh token.
        assert c._token == "jwt_fresh"

    def test_corrupt_cache_file_falls_through_to_fresh_auth(self, monkeypatch, tmp_path):
        """A garbage cache file is silently ignored and a fresh token is
        fetched. Cache correctness is not load-bearing."""
        monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path))

        from colony_sdk.client import _token_cache_path

        path = _token_cache_path("col_test", "https://thecolony.cc/api/v1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json at all")

        calls = self._patch(
            monkeypatch,
            [
                ("ok", 200, {"access_token": "jwt_recovered", "expires_in": 86400}),
                ("ok", 200, {"username": "colonist-one"}),
            ],
        )
        c = ColonyClient("col_test")
        c.get_me()  # MUST NOT raise
        assert sum(1 for x in calls if x["url"].endswith("/auth/token")) == 1
        assert c._token == "jwt_recovered"

    def test_cache_token_false_per_client_disables_persistence(self, monkeypatch, tmp_path):
        """When the constructor arg is False, no cache file is written —
        even if the env var would otherwise enable caching."""
        monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path))
        self._patch(
            monkeypatch,
            [
                ("ok", 200, {"access_token": "jwt_no_cache", "expires_in": 86400}),
                ("ok", 200, {"username": "colonist-one"}),
            ],
        )
        c = ColonyClient("col_test", cache_token=False)
        c.get_me()
        assert list(tmp_path.glob("*.json")) == []

    def test_env_var_disables_cache_globally(self, monkeypatch, tmp_path):
        """`COLONY_SDK_NO_TOKEN_CACHE=1` disables caching even when the
        per-client setting would enable it. Operator-level kill switch
        without code change."""
        monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path))
        monkeypatch.setenv("COLONY_SDK_NO_TOKEN_CACHE", "1")
        self._patch(
            monkeypatch,
            [
                ("ok", 200, {"access_token": "jwt_global_off", "expires_in": 86400}),
                ("ok", 200, {"username": "colonist-one"}),
            ],
        )
        c = ColonyClient("col_test")  # cache_token defaults to True
        c.get_me()
        assert list(tmp_path.glob("*.json")) == []

    def test_different_api_keys_get_different_cache_files(self, monkeypatch, tmp_path):
        """The cache filename is keyed by (base_url, api_key) — two clients
        with different keys must not collide. Otherwise rotating an api_key
        would silently re-load the old key's token until expiry."""
        monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path))
        self._patch(
            monkeypatch,
            [
                ("ok", 200, {"access_token": "jwt_key_a", "expires_in": 86400}),
                ("ok", 200, {"username": "alice"}),
                ("ok", 200, {"access_token": "jwt_key_b", "expires_in": 86400}),
                ("ok", 200, {"username": "bob"}),
            ],
        )
        ColonyClient("col_key_alice").get_me()
        ColonyClient("col_key_bob").get_me()
        assert len(list(tmp_path.glob("*.json"))) == 2

    def test_different_base_urls_get_different_cache_files(self, monkeypatch, tmp_path):
        """Same api_key against prod vs staging must get independent cache
        files — same key may be valid on both bases with different tokens."""
        monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path))
        self._patch(
            monkeypatch,
            [
                ("ok", 200, {"access_token": "jwt_prod", "expires_in": 86400}),
                ("ok", 200, {"username": "u"}),
                ("ok", 200, {"access_token": "jwt_staging", "expires_in": 86400}),
                ("ok", 200, {"username": "u"}),
            ],
        )
        ColonyClient("col_same", base_url="https://thecolony.cc/api/v1").get_me()
        ColonyClient("col_same", base_url="https://staging.example/api/v1").get_me()
        assert len(list(tmp_path.glob("*.json"))) == 2

    def test_refresh_token_removes_cache_file(self, monkeypatch, tmp_path):
        """`refresh_token()` clears both in-memory and on-disk state so
        the next request hits `/auth/token` even on a fresh process."""
        monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path))
        self._patch(
            monkeypatch,
            [
                ("ok", 200, {"access_token": "jwt_initial", "expires_in": 86400}),
                ("ok", 200, {"username": "colonist-one"}),
            ],
        )
        c = ColonyClient("col_test")
        c.get_me()
        assert len(list(tmp_path.glob("*.json"))) == 1
        c.refresh_token()
        assert list(tmp_path.glob("*.json")) == []
        assert c._token is None

    def test_401_response_invalidates_disk_cache(self, monkeypatch, tmp_path):
        """A 401 from the server means the (possibly cached) token is stale.
        The disk cache must be cleared so the next process doesn't re-load
        the same stale token and immediately 401 again."""
        monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path))
        # Pre-seed a "valid-looking but server-rejected" token.
        import json as _json
        import time as _time

        from colony_sdk.client import _token_cache_path

        stale_path = _token_cache_path("col_test", "https://thecolony.cc/api/v1")
        stale_path.parent.mkdir(parents=True, exist_ok=True)
        stale_path.write_text(_json.dumps({"v": 1, "token": "jwt_revoked", "expiry": _time.time() + 86400}))

        self._patch(
            monkeypatch,
            [
                ("http_error", 401, '{"detail":"invalid token"}'),  # /users/me with cached jwt_revoked
                ("ok", 200, {"access_token": "jwt_new", "expires_in": 86400}),  # /auth/token refresh
                ("ok", 200, {"username": "colonist-one"}),  # /users/me retry
            ],
        )
        c = ColonyClient("col_test")
        result = c.get_me()
        assert result["username"] == "colonist-one"
        # Cache file rewritten with the new token (not zero — _ensure_token
        # wrote the new one after fetching).
        assert len(list(tmp_path.glob("*.json"))) == 1
        cached = _json.loads(next(iter(tmp_path.glob("*.json"))).read_text())
        assert cached["token"] == "jwt_new"

    def test_cache_dir_explicit_override_wins_on_every_platform(self, monkeypatch, tmp_path):
        """`COLONY_SDK_TOKEN_CACHE_DIR` short-circuits all platform
        detection — it's the escape hatch for tests, multi-user hosts,
        and anyone who wants the cache somewhere specific."""
        from colony_sdk.client import _token_cache_dir

        monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path / "explicit"))
        # Force a non-Linux platform — override must still win.
        monkeypatch.setattr("colony_sdk.client.sys.platform", "win32")
        assert _token_cache_dir() == tmp_path / "explicit"

    def test_cache_dir_linux_honors_xdg_cache_home(self, monkeypatch, tmp_path):
        """Linux: `$XDG_CACHE_HOME/colony-sdk` per the XDG Base Directory Spec."""
        from colony_sdk.client import _token_cache_dir

        monkeypatch.delenv("COLONY_SDK_TOKEN_CACHE_DIR", raising=False)
        monkeypatch.setattr("colony_sdk.client.sys.platform", "linux")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        assert _token_cache_dir() == tmp_path / "colony-sdk"

    def test_cache_dir_linux_falls_back_to_home_dot_cache(self, monkeypatch, tmp_path):
        """Linux without XDG_CACHE_HOME falls back to `~/.cache/colony-sdk`."""
        from pathlib import Path

        from colony_sdk.client import _token_cache_dir

        monkeypatch.delenv("COLONY_SDK_TOKEN_CACHE_DIR", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setattr("colony_sdk.client.sys.platform", "linux")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert _token_cache_dir() == tmp_path / ".cache" / "colony-sdk"

    def test_cache_dir_macos_uses_library_caches(self, monkeypatch, tmp_path):
        """macOS: `~/Library/Caches/colony-sdk` per Apple's File System
        Programming Guide."""
        from pathlib import Path

        from colony_sdk.client import _token_cache_dir

        monkeypatch.delenv("COLONY_SDK_TOKEN_CACHE_DIR", raising=False)
        monkeypatch.setattr("colony_sdk.client.sys.platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        # Even with XDG_CACHE_HOME set, macOS should ignore it.
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "wrong"))
        assert _token_cache_dir() == tmp_path / "Library" / "Caches" / "colony-sdk"

    def test_cache_dir_windows_prefers_localappdata(self, monkeypatch, tmp_path):
        """Windows: `%LOCALAPPDATA%\\colony-sdk\\Cache` — machine-local
        rather than roamed. (Local cache shouldn't sync to other
        machines via the roaming profile.)"""
        from colony_sdk.client import _token_cache_dir

        monkeypatch.delenv("COLONY_SDK_TOKEN_CACHE_DIR", raising=False)
        monkeypatch.setattr("colony_sdk.client.sys.platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
        result = _token_cache_dir()
        assert result == tmp_path / "Local" / "colony-sdk" / "Cache"

    def test_cache_dir_windows_falls_back_to_appdata(self, monkeypatch, tmp_path):
        """Windows without LOCALAPPDATA falls back to APPDATA (still
        better than dumping under home root)."""
        from colony_sdk.client import _token_cache_dir

        monkeypatch.delenv("COLONY_SDK_TOKEN_CACHE_DIR", raising=False)
        monkeypatch.setattr("colony_sdk.client.sys.platform", "win32")
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
        result = _token_cache_dir()
        assert result == tmp_path / "Roaming" / "colony-sdk" / "Cache"

    def test_cache_dir_windows_falls_back_to_home_appdata_local(self, monkeypatch, tmp_path):
        """Windows with neither env var falls back to the conventional
        `~/AppData/Local/colony-sdk/Cache` path."""
        from pathlib import Path

        from colony_sdk.client import _token_cache_dir

        monkeypatch.delenv("COLONY_SDK_TOKEN_CACHE_DIR", raising=False)
        monkeypatch.setattr("colony_sdk.client.sys.platform", "win32")
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv("APPDATA", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert _token_cache_dir() == tmp_path / "AppData" / "Local" / "colony-sdk" / "Cache"

    def test_save_swallows_mid_write_oserror(self, monkeypatch, tmp_path):
        """If `json.dump` raises an OSError mid-write (disk full, broken
        pipe, etc.), the tmp file is cleaned up and the outer save call
        returns silently — caching never blocks a request from completing.

        Exercises the inner-except partial-write cleanup branch + outer
        OSError swallow."""
        monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path))

        import colony_sdk.client as _client_mod

        def _exploding_dump(obj, fp, **kwargs):
            # OSError-shape — same as what a real disk-full scenario raises
            # mid json.dump call when the underlying fd hits ENOSPC.
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(_client_mod.json, "dump", _exploding_dump)

        c = ColonyClient("col_test")
        c._token = "jwt_will_fail_to_save"
        c._token_expiry = 9999999999
        # MUST NOT raise — _save_cached_token is best-effort under OSError.
        c._save_cached_token()
        # No stale tmp file left behind, no final cache file either.
        assert list(tmp_path.glob("*")) == []

    def test_save_swallows_outer_oserror(self, monkeypatch, tmp_path):
        """If mkdir/open at the very top raises OSError, save returns
        silently — never propagates to the caller."""
        monkeypatch.setenv(
            "COLONY_SDK_TOKEN_CACHE_DIR",
            "/proc/1/root/cache-cannot-write-here",  # path that can't be created
        )
        c = ColonyClient("col_test")
        c._token = "jwt_unwritable"
        c._token_expiry = 9999999999
        # MUST NOT raise.
        c._save_cached_token()

    def test_clear_cached_token_no_op_when_cache_disabled(self, monkeypatch, tmp_path):
        """`_clear_cached_token` early-returns without touching the
        filesystem when caching is globally disabled — protects against
        accidentally nuking a file someone else's process owns."""
        monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path))
        monkeypatch.setenv("COLONY_SDK_NO_TOKEN_CACHE", "1")
        # Pre-seed a file at the path that WOULD be the cache target.
        from colony_sdk.client import _token_cache_path

        # Compute path before the env-disable check would skip the unlink.
        # We need to bypass the disable to compute the path, so check the
        # path without invoking _clear_cached_token.
        # Set up: cache disabled by env, but a file exists at the path
        # (could be left over from a previous run).
        monkeypatch.delenv("COLONY_SDK_NO_TOKEN_CACHE", raising=False)
        path = _token_cache_path("col_test", "https://thecolony.cc/api/v1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"v":1,"token":"untouched","expiry":9999999999}')
        # Now disable caching and call clear — file must remain.
        monkeypatch.setenv("COLONY_SDK_NO_TOKEN_CACHE", "1")
        c = ColonyClient("col_test")
        c._clear_cached_token()
        assert path.exists(), "cache disabled → clear must not touch the filesystem"

    def test_safety_margin_treats_near_expiry_as_miss(self, monkeypatch, tmp_path):
        """A token whose expiry is within the 60s safety margin is treated
        as a cache miss — otherwise a long request could outlive the token
        and 401 mid-flight."""
        import json as _json
        import time as _time

        monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path))
        from colony_sdk.client import _token_cache_path

        path = _token_cache_path("col_test", "https://thecolony.cc/api/v1")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Token "expires" in 30s — within the 60s safety margin.
        path.write_text(_json.dumps({"v": 1, "token": "jwt_near_expiry", "expiry": _time.time() + 30}))

        calls = self._patch(
            monkeypatch,
            [
                ("ok", 200, {"access_token": "jwt_refreshed", "expires_in": 86400}),
                ("ok", 200, {"username": "colonist-one"}),
            ],
        )
        c = ColonyClient("col_test")
        c.get_me()
        assert sum(1 for x in calls if x["url"].endswith("/auth/token")) == 1
        assert c._token == "jwt_refreshed"
