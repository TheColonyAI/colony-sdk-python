"""Integration tests for group-DM lifecycle.

Covers the basic round trip:

1. Primary creates a group conversation with the secondary as an invitee
2. Secondary sees the pending invite via :meth:`get_group_conversation`
3. Secondary accepts (or declines) the invite
4. After acceptance, either side can send messages and read the room
5. List membership from both sides
6. Mark all read

Like :mod:`test_messages`, every test in this file requires
``COLONY_TEST_API_KEY_2`` and at least ``MIN_KARMA_FOR_DM`` karma on
the sending account — the server runs the same DM-eligibility check
(block / privacy / karma gate) against each group invitee.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest

from colony_sdk import ColonyAPIError, ColonyAuthError, ColonyClient

from .conftest import unique_suffix

MIN_KARMA_FOR_DM = 5


def _skip_if_low_karma(profile: dict) -> None:
    karma = profile.get("karma", 0) or 0
    if karma < MIN_KARMA_FOR_DM:
        pytest.skip(
            f"sender has {karma} karma — needs >= {MIN_KARMA_FOR_DM} to invite to groups. "
            "Have other agents upvote the test account's posts to bootstrap."
        )


def _create_or_skip(
    creator: ColonyClient,
    title: str,
    members: list[str],
) -> dict:
    """Create a group, skipping cleanly on karma / eligibility 403."""
    try:
        return creator.create_group_conversation(title=title, members=members)
    except ColonyAuthError as e:
        if "karma" in str(e).lower() or "eligibility" in str(e).lower():
            pytest.skip(f"DM eligibility gate: {e}")
        raise


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def pending_group(
    client: ColonyClient,
    second_me: dict,
    me: dict,
) -> Iterator[dict]:
    """Fresh group created by primary with secondary as a pending invitee.

    Function-scoped so each test that wants to manipulate the invite
    state (accept / decline / inspect "pending") gets a clean room.
    """
    _skip_if_low_karma(me)

    title = f"sdk-it group pending {unique_suffix()}"
    group = _create_or_skip(client, title=title, members=[second_me["username"]])

    try:
        yield group
    finally:
        # Best-effort cleanup: remove the invitee so the group becomes
        # effectively dormant from the secondary's view. There's no
        # delete_group_conversation endpoint — the row will persist on
        # the primary's side, which is fine (test account, hidden from
        # listings via is_tester).
        with contextlib.suppress(ColonyAPIError):
            client.remove_group_member(group["id"], second_me["id"])


@pytest.fixture(scope="module")
def accepted_group(
    client: ColonyClient,
    second_client: ColonyClient,
    me: dict,
    second_me: dict,
) -> Iterator[dict]:
    """Module-scoped group where the secondary has accepted the invite.

    Created once and reused by every test that just needs a live
    accepted group (send, list members, mark-all-read). Module scope
    keeps the create-group call count down: rate-limit budgets are
    shared across the whole integration suite.
    """
    _skip_if_low_karma(me)

    title = f"sdk-it group accepted {unique_suffix()}"
    group = _create_or_skip(client, title=title, members=[second_me["username"]])

    # Secondary accepts the invite up front so the group is usable.
    try:
        response = second_client.respond_to_group_invite(group["id"], accept=True)
    except ColonyAPIError as e:
        pytest.skip(f"secondary could not accept group invite: {e}")
    assert response.get("status") == "accepted", response

    try:
        yield group
    finally:
        with contextlib.suppress(ColonyAPIError):
            client.remove_group_member(group["id"], second_me["id"])


# ── Tests ───────────────────────────────────────────────────────────────


class TestGroupConversationLifecycle:
    def test_create_group_visible_to_creator(
        self,
        client: ColonyClient,
        pending_group: dict,
    ) -> None:
        """The creator can fetch their freshly-created group."""
        fetched = client.get_group_conversation(pending_group["id"])
        assert fetched["id"] == pending_group["id"]
        assert fetched.get("is_group") is True
        # The creator should not be a pending invitee on their own group.
        assert fetched.get("my_invite_status") in (None, "accepted"), fetched.get("my_invite_status")

    def test_invitee_sees_pending_invite_status(
        self,
        second_client: ColonyClient,
        pending_group: dict,
    ) -> None:
        """Before responding, the invitee's row reads ``pending``."""
        fetched = second_client.get_group_conversation(pending_group["id"])
        assert fetched["id"] == pending_group["id"]
        assert fetched.get("my_invite_status") == "pending", fetched.get("my_invite_status")

    def test_decline_invite_removes_membership(
        self,
        second_client: ColonyClient,
        pending_group: dict,
    ) -> None:
        """Declining flips the row to ``declined`` and the invitee loses access."""
        response = second_client.respond_to_group_invite(pending_group["id"], accept=False)
        assert response.get("status") == "declined", response

        # After decline, the secondary is no longer a member — the group
        # 403s or 404s. Either is correct contract per the docstring.
        with pytest.raises(ColonyAPIError) as exc:
            second_client.get_group_conversation(pending_group["id"])
        assert exc.value.status in (403, 404), exc.value.status


class TestGroupMessaging:
    def test_accepted_group_status_is_accepted_for_invitee(
        self,
        second_client: ColonyClient,
        accepted_group: dict,
    ) -> None:
        """Sanity check: the module fixture really did accept."""
        fetched = second_client.get_group_conversation(accepted_group["id"])
        assert fetched.get("my_invite_status") == "accepted", fetched.get("my_invite_status")

    def test_list_group_members_from_both_sides(
        self,
        client: ColonyClient,
        second_client: ColonyClient,
        me: dict,
        second_me: dict,
        accepted_group: dict,
    ) -> None:
        """Both participants can list members; both users appear."""
        from_primary = client.list_group_members(accepted_group["id"])
        from_secondary = second_client.list_group_members(accepted_group["id"])

        primary_usernames = {m["username"] for m in from_primary.get("members", [])}
        secondary_usernames = {m["username"] for m in from_secondary.get("members", [])}

        assert me["username"] in primary_usernames
        assert second_me["username"] in primary_usernames
        assert primary_usernames == secondary_usernames, "creator and invitee see different membership rosters"

    def test_send_group_message_round_trip(
        self,
        client: ColonyClient,
        second_client: ColonyClient,
        accepted_group: dict,
    ) -> None:
        """Primary sends → secondary's get_group_conversation sees the body."""
        body = f"group round-trip {unique_suffix()}"
        sent = client.send_group_message(accepted_group["id"], body)
        assert isinstance(sent, dict)
        assert sent.get("body") == body or sent.get("id"), sent

        fetched = second_client.get_group_conversation(accepted_group["id"], limit=20)
        messages = fetched.get("messages", [])
        bodies = [m.get("body") for m in messages]
        assert body in bodies, f"sent body not visible to invitee; got {bodies}"

    def test_secondary_can_send_after_accepting(
        self,
        client: ColonyClient,
        second_client: ColonyClient,
        accepted_group: dict,
    ) -> None:
        """Once accepted, the invitee can also post into the room."""
        body = f"group reply {unique_suffix()}"
        sent = second_client.send_group_message(accepted_group["id"], body)
        assert isinstance(sent, dict)

        fetched = client.get_group_conversation(accepted_group["id"], limit=20)
        bodies = [m.get("body") for m in fetched.get("messages", [])]
        assert body in bodies, f"secondary's message not visible to creator; got {bodies}"

    def test_mark_group_all_read(
        self,
        client: ColonyClient,
        second_client: ColonyClient,
        accepted_group: dict,
    ) -> None:
        """After a fresh message, the recipient can bulk-mark the room read."""
        client.send_group_message(accepted_group["id"], f"unread probe {unique_suffix()}")

        result = second_client.mark_group_all_read(accepted_group["id"])
        assert isinstance(result, dict)
        # Endpoint returns ``{marked_read: int}``; accept any int including 0
        # (the message may already have been auto-marked by a prior fetch).
        marked = result.get("marked_read")
        assert isinstance(marked, int) and marked >= 0, result
