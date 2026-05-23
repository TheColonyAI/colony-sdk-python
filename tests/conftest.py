"""Test-suite-wide fixtures.

We default-isolate the JWT cache directory so individual tests don't
write tokens to (or read tokens from) the developer's real
``~/.cache/colony-sdk/`` location, and so cache files written by one
test cannot leak into another. Tests that need to assert specific
cache-file presence (e.g., :class:`TestTokenCachePersistence`) override
the env var via ``monkeypatch.setenv`` per test, which takes precedence.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_token_cache(tmp_path, monkeypatch):
    """Route the JWT cache to a per-test tmp directory by default.

    Why autouse: many tests construct ``ColonyClient`` and trigger
    ``_ensure_token``, which writes a cache file. Without isolation,
    those writes would land in the developer's real cache dir, where
    they could leak between tests in the same suite run and across
    invocations of the test suite. Per-test tmp dir solves both.

    Tests in :class:`TestTokenCachePersistence` override
    ``COLONY_SDK_TOKEN_CACHE_DIR`` themselves to assert specific paths;
    that monkeypatch.setenv call takes precedence over this fixture.
    """
    monkeypatch.setenv("COLONY_SDK_TOKEN_CACHE_DIR", str(tmp_path / "colony-sdk-cache"))
    # Defensive: also clear the global kill-switch so a stale env var
    # from the developer's shell doesn't silently disable caching for
    # tests that depend on it.
    monkeypatch.delenv("COLONY_SDK_NO_TOKEN_CACHE", raising=False)
    yield
