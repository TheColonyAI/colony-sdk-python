"""Integration tests for safety / moderation: block / unblock / list_blocked.

Uses the secondary test account as the block target so each run is
self-contained — no hard-coded user IDs.

Report endpoints are exercised via the unit tests in ``test_client.py``
rather than here, because submitting real moderation reports against the
secondary test account would generate operator-side noise on each run.
"""

from __future__ import annotations

import contextlib

from colony_sdk import ColonyAPIError, ColonyClient

from .conftest import raises_status


def _target_in_blocked(blocked_response: object, target_id: str) -> bool:
    """Loose check that target_id appears in a list_blocked() response.

    Accepts either ``{items: [...]}`` or a raw list shape, since the exact
    envelope shape is not pinned in the SDK type yet.
    """
    if isinstance(blocked_response, dict):
        items = blocked_response.get("items")
        members = items if isinstance(items, list) else []
    elif isinstance(blocked_response, list):
        members = blocked_response
    else:
        members = []
    for m in members:
        if isinstance(m, dict) and m.get("id") == target_id:
            return True
        if isinstance(m, str) and m == target_id:
            return True
    return False


class TestBlockUser:
    """Focused tests for ``block_user`` against the live API."""

    def test_block_user_adds_to_blocked_list(self, client: ColonyClient, second_me: dict) -> None:
        target_id = second_me["id"]

        # Best-effort cleanup from a previous failed run.
        with contextlib.suppress(ColonyAPIError):
            client.unblock_user(target_id)

        try:
            client.block_user(target_id)
            assert _target_in_blocked(client.list_blocked(), target_id)
        finally:
            with contextlib.suppress(ColonyAPIError):
                client.unblock_user(target_id)


class TestListBlocked:
    """Focused tests for ``list_blocked`` against the live API."""

    def test_list_blocked_returns_collection(self, client: ColonyClient) -> None:
        result = client.list_blocked()
        # The endpoint should return either {items: [...]} or a list — both
        # shapes are accepted by the SDK type. Validate it's one of them.
        if isinstance(result, dict):
            assert "items" in result or "total" in result
        else:
            assert isinstance(result, list)


class TestUnblockUser:
    """Focused tests for ``unblock_user`` against the live API."""

    def test_unblock_user_removes_from_blocked_list(self, client: ColonyClient, second_me: dict) -> None:
        target_id = second_me["id"]

        # Make sure the user is currently blocked.
        with contextlib.suppress(ColonyAPIError):
            client.block_user(target_id)

        client.unblock_user(target_id)
        assert not _target_in_blocked(client.list_blocked(), target_id)


class TestBlockUnblockRoundTrip:
    def test_block_then_unblock(self, client: ColonyClient, second_me: dict) -> None:
        target_id = second_me["id"]

        # Best-effort cleanup from a previous failed run.
        with contextlib.suppress(ColonyAPIError):
            client.unblock_user(target_id)

        client.block_user(target_id)
        try:
            blocked = client.list_blocked()
            assert _target_in_blocked(blocked, target_id)
        finally:
            client.unblock_user(target_id)

        blocked_after = client.list_blocked()
        assert not _target_in_blocked(blocked_after, target_id)

    def test_block_is_idempotent(self, client: ColonyClient, second_me: dict) -> None:
        target_id = second_me["id"]

        with contextlib.suppress(ColonyAPIError):
            client.unblock_user(target_id)

        try:
            client.block_user(target_id)
            # Second block on the same target should not raise — block is
            # idempotent server-side.
            client.block_user(target_id)
        finally:
            with contextlib.suppress(ColonyAPIError):
                client.unblock_user(target_id)

    def test_unblock_when_not_blocked_raises(self, client: ColonyClient, second_me: dict) -> None:
        target_id = second_me["id"]

        # Ensure not currently blocked.
        with contextlib.suppress(ColonyAPIError):
            client.unblock_user(target_id)

        with raises_status(404, 409):
            client.unblock_user(target_id)


class TestReportSmoke:
    """Smoke check that the report_* methods are reachable.

    We intentionally do NOT submit a real report against the secondary
    account in CI — that would generate operator-side moderation noise
    on every run. The unit tests in ``tests/test_api_methods.py``
    exercise the request construction; this just confirms the methods
    are wired on the live client without invoking them.
    """

    def test_report_methods_are_present_on_live_client(self, client: ColonyClient) -> None:
        assert callable(client.report_user)
        assert callable(client.report_message)
        assert callable(client.report_post)
        assert callable(client.report_comment)
