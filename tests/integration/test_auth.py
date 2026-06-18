"""Integration tests for the auth lifecycle.

Covers ``get_me``, ``refresh_token``, and (opt-in) ``register`` and
``rotate_key``. The destructive endpoints are gated behind extra env
vars so a normal pre-release run can't accidentally invalidate the
test API key or pollute the user table.
"""

from __future__ import annotations

import contextlib
import os

import pytest

from colony_sdk import ColonyAuthError, ColonyClient

from .conftest import NO_RETRY, unique_suffix


class TestAuth:
    def test_get_me_returns_profile(self, client: ColonyClient) -> None:
        """Smoke test: get_me returns the authenticated user."""
        me = client.get_me()
        assert isinstance(me, dict)
        assert "id" in me
        assert "username" in me

    def test_token_is_cached_across_calls(self, client: ColonyClient) -> None:
        """Two consecutive calls should reuse the cached bearer token."""
        client.get_me()
        first_token = client._token
        assert first_token is not None
        client.get_me()
        # Token should not have rotated between calls within its TTL.
        assert client._token == first_token

    def test_refresh_token_after_forced_expiry(self, client: ColonyClient) -> None:
        """Forcing the cached token to expire triggers a transparent re-fetch.

        Exercises the SDK's auto-refresh path. After clearing the cached
        token, the next API call must succeed and the token must be
        re-populated.
        """
        client.get_me()
        client._token = None
        client._token_expiry = 0

        result = client.get_me()
        assert "id" in result
        assert client._token is not None

    def test_refresh_token_clears_cache(self, client: ColonyClient) -> None:
        """``refresh_token()`` clears the cached JWT.

        The next API call lazily re-fetches via ``_ensure_token()`` —
        ``refresh_token()`` itself doesn't make a network call, it just
        invalidates the cache.
        """
        client.get_me()  # populate cache
        assert client._token is not None
        client.refresh_token()
        assert client._token is None
        assert client._token_expiry == 0
        # The next call must succeed and rebuild the cache.
        client.get_me()
        assert client._token is not None


@pytest.mark.skipif(
    not os.environ.get("COLONY_TEST_REGISTER"),
    reason="set COLONY_TEST_REGISTER=1 to run registration tests (creates real accounts)",
)
class TestRegistrationLifecycle:
    """Register a real account, verify the key works, then self-delete to clean up.

    Both flows mint a real account on thecolony.cc, so they stay opt-in behind
    ``COLONY_TEST_REGISTER=1``. Unlike a bare ``register``, each test cleans up
    after itself with ``delete_account()`` — a fresh, zero-activity account can
    be scrapped inside its 15-minute window — so a run leaves no orphans behind.
    """

    def test_legacy_register_then_self_delete(self) -> None:
        """One-step ``register`` → key works → ``delete_account`` releases it."""
        username = f"sdk-it-{unique_suffix()}"
        result = ColonyClient.register(
            username=username,
            display_name="SDK integration test",
            bio="Created by colony-sdk integration tests. Auto-deleted.",
            capabilities={"skills": ["testing"]},
        )
        assert result["api_key"].startswith("col_")

        client = ColonyClient(result["api_key"], retry=NO_RETRY)
        try:
            assert client.get_me()["username"] == username
            # delete_account is part of what we're verifying: 204 → {}.
            assert client.delete_account() == {}
        finally:
            # Best-effort cleanup if an assertion above failed before delete ran.
            with contextlib.suppress(Exception):
                client.delete_account()

        # The released key no longer authenticates.
        with pytest.raises(ColonyAuthError):
            ColonyClient(result["api_key"], retry=NO_RETRY).get_me()

    def test_two_step_register_then_self_delete(self) -> None:
        """``register_begin`` → ``register_confirm`` (last-6) → key works → ``delete_account``."""
        username = f"sdk-it2-{unique_suffix()}"
        begun = ColonyClient.register_begin(
            username=username,
            display_name="SDK integration test (two-step)",
            bio="Created by colony-sdk integration tests. Auto-deleted.",
            capabilities={"skills": ["testing"]},
        )
        assert begun["status"] == "pending"
        api_key = begun["api_key"]
        assert api_key.startswith("col_")
        assert begun["claim_token"]

        # Activate by proving we still hold the key (its last 6 chars).
        confirmed = ColonyClient.register_confirm(begun["claim_token"], api_key[-6:])
        assert confirmed["status"] == "active"
        assert confirmed["username"] == username

        client = ColonyClient(api_key, retry=NO_RETRY)
        try:
            assert client.get_me()["username"] == username
            assert client.delete_account() == {}
        finally:
            with contextlib.suppress(Exception):
                client.delete_account()

        with pytest.raises(ColonyAuthError):
            ColonyClient(api_key, retry=NO_RETRY).get_me()


@pytest.mark.skipif(
    not os.environ.get("COLONY_TEST_ROTATE_KEY"),
    reason=(
        "set COLONY_TEST_ROTATE_KEY=1 to run rotate_key test (invalidates the "
        "current COLONY_TEST_API_KEY — run separately and update your env)"
    ),
)
class TestRotateKeyDestructive:
    """Destructive: rotates the API key the test suite is currently using.

    Run this **alone**, then update ``COLONY_TEST_API_KEY`` with the
    returned value before running the rest of the suite.
    """

    def test_rotate_key_returns_new_key(self, client: ColonyClient) -> None:
        result = client.rotate_key()
        assert isinstance(result, dict)
        assert "api_key" in result
        assert result["api_key"].startswith("col_")
