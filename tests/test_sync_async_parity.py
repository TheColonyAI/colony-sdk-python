"""Every API method on ColonyClient exists on AsyncColonyClient.

WHY THIS EXISTS
===============
On 2026-08-11 ``vault_append_file`` and ``vault_search_files`` were added
to the sync client and to ``MockColonyClient`` — and not to the async
client. Nothing failed. The mock-completeness ratchet
(``test_mock_completeness.py``) compares sync against the MOCK, so it
caught the mock and was blind to the async surface entirely.

The gap was found by accident, while looking at something else. Measured
at that moment the async client was otherwise at FULL parity — 269 sync
methods, 266 async, and every difference accounted for below. So the
convention was real and universally held; there was simply nothing
keeping it that way, and the first violation went in unnoticed.

An async user hitting a method the sync docs promise gets
``AttributeError`` at runtime. That is the same failure mode the mock
ratchet exists to prevent, one surface along.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import ColonyClient
from colony_sdk.async_client import AsyncColonyClient

#: Sync-only, and correctly so.
#:
#: ``clear_cache`` / ``enable_cache`` configure the sync transport's
#: response cache. They are the same two entries ``test_mock_completeness``
#: excludes as client-state rather than API surface — a method that
#: configures a transport is not something the other client owes you.
#:
#: Keep this list short. Anything added here is a promise the sync docs
#: make that async users cannot keep, so it needs a reason that survives
#: someone reading it a year from now.
SYNC_ONLY: frozenset[str] = frozenset(
    {
        "clear_cache",
        "enable_cache",
    }
)

#: Async-only: closing the underlying session has no sync equivalent.
ASYNC_ONLY: frozenset[str] = frozenset(
    {
        "aclose",
    }
)


def _public_callables(cls: type) -> set[str]:
    return {name for name in dir(cls) if not name.startswith("_") and callable(getattr(cls, name, None))}


def test_async_client_implements_every_sync_method() -> None:
    missing = sorted(_public_callables(ColonyClient) - _public_callables(AsyncColonyClient) - SYNC_ONLY)
    assert not missing, (
        "AsyncColonyClient is missing these ColonyClient methods, so async "
        "users calling them get AttributeError at runtime:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd them to async_client.py, or — only if the method genuinely "
        "cannot exist on the async surface — add it to SYNC_ONLY with a "
        "rationale."
    )


def test_sync_client_implements_every_async_method() -> None:
    """The other direction, so parity cannot be restored by deleting.

    Without this, a future 'fix' to the assertion above could be to drop
    the sync method rather than add the async one, and the suite would
    go green on a smaller API.
    """
    missing = sorted(_public_callables(AsyncColonyClient) - _public_callables(ColonyClient) - ASYNC_ONLY)
    assert not missing, "AsyncColonyClient has methods the sync client lacks:\n  " + "\n  ".join(missing)


def test_the_exclusion_lists_are_not_stale() -> None:
    """An exclusion for a method that no longer exists is a lie that
    quietly widens the allowance. Every name listed must be real."""
    sync = _public_callables(ColonyClient)
    asyn = _public_callables(AsyncColonyClient)

    dead_sync = sorted(n for n in SYNC_ONLY if n not in sync)
    assert not dead_sync, f"SYNC_ONLY names methods that no longer exist: {dead_sync}"

    dead_async = sorted(n for n in ASYNC_ONLY if n not in asyn)
    assert not dead_async, f"ASYNC_ONLY names methods that no longer exist: {dead_async}"

    both = sorted(n for n in SYNC_ONLY if n in asyn)
    assert not both, (
        f"SYNC_ONLY lists methods the async client actually has: {both}. Remove them so the parity check covers them."
    )
