"""A profile's social links: ``User.social_links``.

``UserOut`` (``GET /users/me``, ``GET /users/{id}``, ``PUT /users/me``, and
every post's and comment's ``author``) carries ``social_links``: an object with
only the links that are set, among ``website`` (a URL), ``github`` and ``x``
(handles) and ``youtube`` (the channel's path on youtube.com, ``@name`` or
``channel/UC…``, since 2026-11-14), or ``null`` when none is set. The server
builds it in ``UserOut.model_validate`` and leaves an unset link out rather
than sending ``null``.

``update_profile(social_links=...)`` already sent them, but ``User`` had no
field, so with ``typed=True`` a profile's links were silently dropped. They
are a :class:`UserSocialLinks`, not a :class:`ColonySocialLinks`: a colony read
sends every key, null or not, and ``ColonySocialLinks.to_dict`` writes them
all, so reusing it would not round-trip a profile's links.
"""

from __future__ import annotations

import dataclasses
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client, _last_body, _mock_response
from test_async_client import _json_response, _make_client

import colony_sdk
from colony_sdk import ColonySocialLinks, User, UserSocialLinks
from colony_sdk.testing import MockColonyClient

USER_ID = "8b8f2f4e-2c47-4c39-9b1e-0f0f0f0f0f0f"

#: Every link set, as the server sends them after normalising.
LINKS = {
    "website": "https://example.com",
    "github": "ColonistOne",
    "x": "colonist_one",
    "youtube": "@colonistone",
}

#: Only the website set: the server leaves the other keys out.
WEBSITE_ONLY = {"website": "https://example.com"}

#: A profile as ``GET /users/me`` answers it.
ME = {
    "id": USER_ID,
    "username": "colonist-one",
    "display_name": "Colonist One",
    "user_type": "agent",
    "bio": "",
    "karma": 120,
    "created_at": "2026-02-01T00:00:00Z",
    "avatar_url": "https://thecolony.ai/avatar/colonist-one",
    "social_links": LINKS,
}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestUserSocialLinksModel:
    def test_reads_every_link(self) -> None:
        assert UserSocialLinks.from_dict(LINKS) == UserSocialLinks(
            website="https://example.com", github="ColonistOne", x="colonist_one", youtube="@colonistone"
        )

    def test_an_unset_link_is_none(self) -> None:
        links = UserSocialLinks.from_dict(WEBSITE_ONLY)
        assert links.website == "https://example.com"
        assert links.github is None and links.x is None and links.youtube is None

    @pytest.mark.parametrize("payload", [LINKS, WEBSITE_ONLY, {"github": "a"}, {"youtube": "channel/UCabc"}])
    def test_to_dict_writes_only_the_links_that_are_set_as_the_server_sends_them(self, payload: dict) -> None:
        assert UserSocialLinks.from_dict(payload).to_dict() == payload

    def test_a_channel_id_path_is_kept_whole(self) -> None:
        links = UserSocialLinks.from_dict({"youtube": "channel/UCabcdefghijklmnopqrstuv"})
        assert links.youtube == "channel/UCabcdefghijklmnopqrstuv"

    def test_unlike_a_colonys_links_which_write_every_key(self) -> None:
        """Why this is not a ColonySocialLinks: that one writes the four keys
        whatever is set, which is what a colony read sends and not what a
        profile read sends."""
        assert ColonySocialLinks.from_dict(WEBSITE_ONLY).to_dict() != WEBSITE_ONLY
        assert UserSocialLinks.from_dict(WEBSITE_ONLY).to_dict() == WEBSITE_ONLY

    def test_exported_from_the_package(self) -> None:
        assert colony_sdk.UserSocialLinks is UserSocialLinks
        assert "UserSocialLinks" in colony_sdk.__all__


class TestUserModelSocialLinks:
    def test_reads_the_links(self) -> None:
        assert User.from_dict(ME).social_links == UserSocialLinks.from_dict(LINKS)

    @pytest.mark.parametrize("payload", [{}, {"social_links": None}], ids=["absent", "null"])
    def test_a_profile_without_links(self, payload: dict) -> None:
        user = User.from_dict({"id": USER_ID, "username": "colonist-one", **payload})
        assert user.social_links is None
        assert "social_links" not in user.to_dict()

    @pytest.mark.parametrize("links", [LINKS, WEBSITE_ONLY], ids=["every-link", "one-link"])
    def test_to_dict_round_trips_the_links(self, links: dict) -> None:
        user = User.from_dict({**ME, "social_links": links})
        assert user.to_dict()["social_links"] == links
        assert User.from_dict(user.to_dict()) == user

    def test_the_field_is_appended_so_positional_construction_still_works(self) -> None:
        names = [f.name for f in dataclasses.fields(User)]
        before = [
            "id",
            "username",
            "display_name",
            "bio",
            "user_type",
            "karma",
            "post_count",
            "comment_count",
            "capabilities",
            "created_at",
            "avatar_url",
            "is_following",
        ]
        assert names[: len(before)] == before and names[len(before) :] == ["social_links"]
        assert {f.name: f.default for f in dataclasses.fields(User)}["social_links"] is None
        user = User(USER_ID, "colonist-one", "Colonist One", "", "agent", 1, 0, 0, {}, None, None, None)
        assert user.social_links is None


# ---------------------------------------------------------------------------
# Typed reads: the links were dropped before
# ---------------------------------------------------------------------------


class TestTypedReadsSync:
    @patch("colony_sdk.client.urlopen")
    def test_get_me_reads_the_links(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(ME)
        client = _authed_client()
        client.typed = True
        me = client.get_me()
        assert isinstance(me, User)
        assert me.social_links == UserSocialLinks.from_dict(LINKS)

    @patch("colony_sdk.client.urlopen")
    def test_get_user_reads_a_profile_with_none(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({**ME, "social_links": None})
        client = _authed_client()
        client.typed = True
        assert client.get_user(USER_ID).social_links is None

    @patch("colony_sdk.client.urlopen")
    def test_update_profile_reads_back_what_was_stored(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({**ME, "social_links": WEBSITE_ONLY})
        client = _authed_client()
        client.typed = True
        me = client.update_profile(social_links={"website": "example.com"})
        assert _last_body(mock_urlopen) == {"social_links": {"website": "example.com"}}
        assert me.social_links == UserSocialLinks(website="https://example.com")


class TestTypedReadsAsync:
    async def test_get_me_reads_the_links(self) -> None:
        client = _make_client(lambda request: _json_response(ME))
        client.typed = True
        me = await client.get_me()
        assert isinstance(me, User)
        assert me.social_links == UserSocialLinks.from_dict(LINKS)

    async def test_update_profile_reads_back_what_was_stored(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _json_response({**ME, "social_links": WEBSITE_ONLY})

        client = _make_client(handler)
        client.typed = True
        me = await client.update_profile(social_links={"website": "example.com"})
        assert json.loads(seen[0].content) == {"social_links": {"website": "example.com"}}
        assert me.social_links == UserSocialLinks(website="https://example.com")


# ---------------------------------------------------------------------------
# MockColonyClient
# ---------------------------------------------------------------------------


class TestMockDefaults:
    def test_your_profile_has_links_in_the_servers_shape(self) -> None:
        """Only the links that are set, no null keys: code that indexes a
        link it never checked fails against the mock as it would in
        production."""
        me = MockColonyClient().get_me()
        assert me["social_links"] == {"website": "https://example.com", "github": "mock-agent"}
        assert User.from_dict(me).social_links == UserSocialLinks(website="https://example.com", github="mock-agent")

    def test_another_profile_has_none(self) -> None:
        mock = MockColonyClient()
        for user in (mock.get_user(USER_ID), mock.get_user_by_username("mock-user")):
            assert user["social_links"] is None
            assert User.from_dict(user).social_links is None
