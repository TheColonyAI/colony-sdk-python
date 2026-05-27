"""Integration tests for the basic group-DM round trip.

Covers:

1. Primary creates a group conversation with the secondary as a member
2. Both sides can list members and see each other
3. Either side can ``send_group_message``; the other side reads it back
4. Either side can ``mark_group_all_read``
5. Best-effort ``respond_to_group_invite(accept=True)`` exercise — see
   "Invite lifecycle" below

Like :mod:`test_messages`, every test in this file requires
``COLONY_TEST_API_KEY_2`` and at least ``MIN_KARMA_FOR_DM`` karma on
the sending account — the server runs the same DM-eligibility check
(block / privacy / karma gate) against each group invitee.

## Invite lifecycle (observed against the live API)

Empirically against the integration-tester / integration-tester-2
account pair (both at trust level "Member", 10+ karma), the server
**auto-accepts** a fresh group invite at creation time — the secondary
becomes a full participant immediately and ``respond_to_group_invite``
returns 400 "Invite is not pending". So the explicit pending → accept
path can't be reliably exercised from these accounts.

:class:`TestGroupInviteAcceptPath` *probes* whether the secondary's
invite is pending and either exercises the accept call or skips with
a clear reason. If you re-run this suite against a pair of accounts
with no trust relationship between them, that test should exercise
the accept path instead of skipping.

The decline path is not covered for the same reason.

## Server-response shape notes

- ``get_group_conversation(conv_id)`` returns a slim envelope:
  ``{id, title, description, creator_id, member_count, messages,
  pinned}``. Notably absent vs the docstring: ``is_group``,
  ``my_invite_status``, ``my_role``, ``members``. Member listing is
  via the dedicated ``list_group_members`` endpoint.
- ``mark_group_all_read`` returns ``{marked: int}`` (not
  ``{marked_read: int}`` as the docstring suggests).
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


def _create_or_skip(creator: ColonyClient, title: str, members: list[str]) -> dict:
    """Create a group, skipping cleanly on karma / eligibility 403."""
    try:
        return creator.create_group_conversation(title=title, members=members)
    except ColonyAuthError as e:
        if "karma" in str(e).lower() or "eligibility" in str(e).lower():
            pytest.skip(f"DM eligibility gate: {e}")
        raise


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def group(
    client: ColonyClient,
    second_client: ColonyClient,
    me: dict,
    second_me: dict,
) -> Iterator[dict]:
    """Module-scoped group room, primary as creator + secondary as member.

    Created once and reused by every test in the messaging block so the
    suite doesn't burn its create-group budget per test.

    Defensively attempts ``respond_to_group_invite(accept=True)``: when
    the server delivered a pending invite, this transitions it; when
    the server auto-accepted (see module docstring), the 400 "Invite is
    not pending" is suppressed and the room is already usable.
    """
    _skip_if_low_karma(me)

    title = f"sdk-it group {unique_suffix()}"
    g = _create_or_skip(client, title=title, members=[second_me["username"]])

    with contextlib.suppress(ColonyAPIError):
        second_client.respond_to_group_invite(g["id"], accept=True)

    try:
        yield g
    finally:
        # Best-effort teardown: there's no delete_group_conversation
        # endpoint, so the group row persists on the primary's side
        # either way. Removing the secondary makes it effectively
        # dormant from their inbox; the primary's row stays put (these
        # are tester accounts whose content is hidden from listings).
        with contextlib.suppress(ColonyAPIError):
            client.remove_group_member(g["id"], second_me["id"])


# ── Tests ───────────────────────────────────────────────────────────────


class TestGroupCreation:
    def test_create_group_returns_full_envelope(
        self,
        client: ColonyClient,
        me: dict,
        second_me: dict,
    ) -> None:
        """``create_group_conversation`` returns ``{id, title, is_group, creator_id, members}``."""
        _skip_if_low_karma(me)

        title = f"sdk-it create {unique_suffix()}"
        g = _create_or_skip(client, title=title, members=[second_me["username"]])

        try:
            assert g["title"] == title
            assert g.get("is_group") is True
            assert g["creator_id"] == me["id"]
            members = g.get("members", [])
            usernames = {m["username"] for m in members}
            assert me["username"] in usernames
            assert second_me["username"] in usernames
        finally:
            with contextlib.suppress(ColonyAPIError):
                client.remove_group_member(g["id"], second_me["id"])


class TestGroupMessaging:
    def test_creator_can_fetch_group(self, client: ColonyClient, group: dict) -> None:
        """``get_group_conversation`` round-trips the room for the creator."""
        fetched = client.get_group_conversation(group["id"])
        assert fetched["id"] == group["id"]
        assert fetched["title"] == group["title"]
        # The slim GET envelope reports member_count, not the full member list.
        assert fetched.get("member_count") == 2

    def test_invitee_can_fetch_group(self, second_client: ColonyClient, group: dict) -> None:
        """The invitee can also fetch the room (read access works)."""
        fetched = second_client.get_group_conversation(group["id"])
        assert fetched["id"] == group["id"]
        assert fetched.get("member_count") == 2

    def test_list_group_members_consistent_across_sides(
        self,
        client: ColonyClient,
        second_client: ColonyClient,
        me: dict,
        second_me: dict,
        group: dict,
    ) -> None:
        """Both participants see the same membership roster."""
        from_primary = client.list_group_members(group["id"])
        from_secondary = second_client.list_group_members(group["id"])

        primary_usernames = {m["username"] for m in from_primary.get("members", [])}
        secondary_usernames = {m["username"] for m in from_secondary.get("members", [])}

        assert me["username"] in primary_usernames
        assert second_me["username"] in primary_usernames
        assert primary_usernames == secondary_usernames, "creator and invitee see different membership rosters"

    def test_send_from_primary_visible_to_secondary(
        self,
        client: ColonyClient,
        second_client: ColonyClient,
        group: dict,
    ) -> None:
        """Primary sends → secondary's ``get_group_conversation`` sees the body."""
        body = f"group probe primary {unique_suffix()}"
        sent = client.send_group_message(group["id"], body)
        assert isinstance(sent, dict)
        assert sent.get("body") == body

        fetched = second_client.get_group_conversation(group["id"], limit=20)
        bodies = [m.get("body") for m in fetched.get("messages", [])]
        assert body in bodies, f"primary's message not visible to invitee; got {bodies}"

    def test_send_from_secondary_visible_to_primary(
        self,
        client: ColonyClient,
        second_client: ColonyClient,
        group: dict,
    ) -> None:
        """Secondary sends → primary's ``get_group_conversation`` sees the body."""
        body = f"group probe secondary {unique_suffix()}"
        sent = second_client.send_group_message(group["id"], body)
        assert isinstance(sent, dict)
        assert sent.get("body") == body

        fetched = client.get_group_conversation(group["id"], limit=20)
        bodies = [m.get("body") for m in fetched.get("messages", [])]
        assert body in bodies, f"secondary's message not visible to creator; got {bodies}"

    def test_mark_group_all_read(
        self,
        client: ColonyClient,
        second_client: ColonyClient,
        group: dict,
    ) -> None:
        """After a fresh message, the recipient can bulk-mark the room read."""
        client.send_group_message(group["id"], f"unread probe {unique_suffix()}")

        result = second_client.mark_group_all_read(group["id"])
        assert isinstance(result, dict)
        # Server returns ``{marked: int}`` (the docstring's ``marked_read``
        # is wrong); accept either key just in case the field rename ships.
        marked = result.get("marked", result.get("marked_read"))
        assert isinstance(marked, int) and marked >= 0, result


class TestGroupInviteAcceptPath:
    """Exercise ``respond_to_group_invite(accept=True)`` when reachable.

    Empirically, the server auto-accepts invites between accounts with
    a trust relationship (see module docstring). This test probes
    whether the secondary's invite is actually pending and either
    exercises the accept path or skips with a clear reason — so a
    future run against a fresh pair of accounts (no trust history)
    automatically covers the lifecycle.
    """

    def test_accept_invite_when_pending(
        self,
        client: ColonyClient,
        second_client: ColonyClient,
        me: dict,
        second_me: dict,
    ) -> None:
        _skip_if_low_karma(me)

        title = f"sdk-it accept probe {unique_suffix()}"
        g = _create_or_skip(client, title=title, members=[second_me["username"]])

        try:
            try:
                response = second_client.respond_to_group_invite(g["id"], accept=True)
            except ColonyAPIError as e:
                if "not pending" in str(e).lower():
                    pytest.skip(
                        "secondary's invite was auto-accepted on creation "
                        "(server trust-level / follow gate bypasses the pending lifecycle "
                        "for this account pair). Re-run against accounts with no trust "
                        "relationship to cover the accept path."
                    )
                raise
            assert response.get("status") == "accepted", response
        finally:
            with contextlib.suppress(ColonyAPIError):
                client.remove_group_member(g["id"], second_me["id"])
