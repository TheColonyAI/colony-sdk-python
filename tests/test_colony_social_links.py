"""A colony's social links: ``Colony.social_links`` and setting them through
``update_colony_settings(colony, social_links={...})``.

Platform release 2026-11-14c. ``ColonyOut.social_links`` is ``null`` or
``ColonySocialLinksOut``: ``website`` (a URL, http(s) only), ``github`` and
``x`` (handles) and ``youtube`` (the channel's path on youtube.com, ``@name``
or ``channel/UC…``), each a string or ``null``. ``PATCH
/api/v1/colonies/{colony_id}`` with ``{"social_links": {...}}`` sets them
(``SocialLinksUpdate``, the profile's schema: website max 300, github and x max
100, youtube max 200). The whole set is replaced: a key left out is cleared,
and ``{}`` or ``null`` clears every link. The server normalises what it is sent
and answers 422 for a link it cannot use.

``update_colony_settings`` forwards any setting it is given, so the SDK sends
``social_links`` exactly as passed and checks nothing itself: the server is
the judge of a link, and a local copy of its normalisation would drift from it.
"""

from __future__ import annotations

import dataclasses
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client, _last_body, _last_request, _make_http_error, _mock_response
from test_async_client import _json_response, _make_client

import colony_sdk
from colony_sdk import (
    AsyncColonyClient,
    Colony,
    ColonyClient,
    ColonySocialLinks,
    ColonyValidationError,
)
from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"
COLONY_ID = "33333333-3333-3333-3333-333333333333"

#: A colony's ``social_links`` (``ColonySocialLinksOut``), as the server stored
#: them after normalising.
LINKS = {
    "website": "https://example.com",
    "github": "my-org",
    "x": "my_org",
    "youtube": "@mychannel",
}

#: The same, with only the website set: the server sends every key.
WEBSITE_ONLY = {"website": "https://example.com", "github": None, "x": None, "youtube": None}

#: A colony as ``GET /colonies`` and ``PATCH /colonies/{id}`` answer it.
COLONY = {
    "id": COLONY_ID,
    "name": "ainglish",
    "display_name": "AInglish",
    "description": "English for agents",
    "member_count": 12,
    "post_count": 40,
    "is_default": False,
    "created_at": "2026-08-01T00:00:00Z",
    "social_links": LINKS,
}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestColonySocialLinksModel:
    def test_reads_every_link(self) -> None:
        links = ColonySocialLinks.from_dict(LINKS)
        assert links == ColonySocialLinks(
            website="https://example.com", github="my-org", x="my_org", youtube="@mychannel"
        )

    def test_to_dict_writes_every_key_as_the_server_sends_them(self) -> None:
        assert ColonySocialLinks.from_dict(LINKS).to_dict() == LINKS
        assert ColonySocialLinks.from_dict(WEBSITE_ONLY).to_dict() == WEBSITE_ONLY

    @pytest.mark.parametrize("payload", [WEBSITE_ONLY, {"website": "https://example.com"}], ids=["null", "absent"])
    def test_an_unset_link_is_none(self, payload: dict) -> None:
        links = ColonySocialLinks.from_dict(payload)
        assert links.website == "https://example.com"
        assert links.github is None and links.x is None and links.youtube is None

    def test_a_channel_id_path_is_kept_whole(self) -> None:
        links = ColonySocialLinks.from_dict({**WEBSITE_ONLY, "youtube": "channel/UCabcdefghijklmnopqrstuv"})
        assert links.youtube == "channel/UCabcdefghijklmnopqrstuv"

    def test_exported_from_the_package(self) -> None:
        assert colony_sdk.ColonySocialLinks is ColonySocialLinks
        assert "ColonySocialLinks" in colony_sdk.__all__


class TestColonyModelSocialLinks:
    def test_reads_the_links(self) -> None:
        colony = Colony.from_dict(COLONY)
        assert colony.social_links == ColonySocialLinks.from_dict(LINKS)

    @pytest.mark.parametrize("payload", [{}, {"social_links": None}], ids=["absent", "null"])
    def test_a_colony_without_links(self, payload: dict) -> None:
        colony = Colony.from_dict({"id": COLONY_ID, "name": "ainglish", **payload})
        assert colony.social_links is None
        assert "social_links" not in colony.to_dict()

    def test_to_dict_round_trips_the_links(self) -> None:
        colony = Colony.from_dict(COLONY)
        assert colony.to_dict()["social_links"] == LINKS
        assert Colony.from_dict(colony.to_dict()) == colony

    def test_only_some_links_set(self) -> None:
        colony = Colony.from_dict({**COLONY, "social_links": WEBSITE_ONLY})
        assert colony.social_links is not None
        assert colony.social_links.website == "https://example.com"
        assert colony.social_links.github is None
        assert colony.to_dict()["social_links"] == WEBSITE_ONLY

    def test_the_field_is_appended_so_positional_construction_still_works(self) -> None:
        names = [f.name for f in dataclasses.fields(Colony)]
        before = ["id", "name", "description", "member_count", "post_count", "created_at"]
        assert names[: len(before)] == before and names.index("social_links") >= len(before)
        colony = Colony(COLONY_ID, "ainglish", "d", 1, 2, "2026-08-01T00:00:00Z")
        assert colony.social_links is None


# ---------------------------------------------------------------------------
# update_colony_settings(social_links=...) (sync)
# ---------------------------------------------------------------------------


#: What ``update_colony_settings`` is given, and the colony the server answers
#: with: the links normalised and stored, or none.
SETS = [
    (
        {"website": "example.com", "github": "https://github.com/my-org", "x": "my_org", "youtube": "@mychannel"},
        COLONY,
    ),
    ({"website": "https://example.com"}, {**COLONY, "social_links": WEBSITE_ONLY}),
    ({}, {**COLONY, "social_links": None}),
    (None, {**COLONY, "social_links": None}),
]
SET_IDS = ["every-link", "one-link", "empty-clears", "none-clears"]


class TestSetSocialLinksSync:
    @pytest.mark.parametrize("sent,answer", SETS, ids=SET_IDS)
    @patch("colony_sdk.client.urlopen")
    def test_sends_the_links_as_given_and_reads_back_what_was_stored(
        self, mock_urlopen: MagicMock, sent: dict | None, answer: dict
    ) -> None:
        mock_urlopen.return_value = _mock_response(answer)

        out = _authed_client().update_colony_settings(COLONY_ID, social_links=sent)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "PATCH"
        assert req.full_url == f"{BASE}/colonies/{COLONY_ID}"
        # Sent untouched, None as null: normalising is the server's job.
        assert _last_body(mock_urlopen) == {"social_links": sent}
        stored = answer["social_links"]
        assert Colony.from_dict(out).social_links == (ColonySocialLinks.from_dict(stored) if stored else None)

    @patch("colony_sdk.client.urlopen")
    def test_the_documented_422_is_a_validation_error(self, mock_urlopen: MagicMock) -> None:
        """The docstring promises :class:`ColonyValidationError` for a link the
        server cannot use; this holds it to that."""
        doc = " ".join((ColonyClient.update_colony_settings.__doc__ or "").split())
        assert "answers 422 (:class:`ColonyValidationError`) for a link it cannot use" in doc
        detail = [
            {
                "type": "value_error",
                "loc": ["body", "social_links", "website"],
                "msg": "Value error, The website must start with http:// or https://.",
            }
        ]
        mock_urlopen.side_effect = _make_http_error(422, {"detail": detail})
        with pytest.raises(ColonyValidationError) as exc:
            _authed_client().update_colony_settings(COLONY_ID, social_links={"website": "javascript:alert(1)"})
        assert exc.value.status == 422

    def test_the_docstring_documents_social_links(self) -> None:
        doc = " ".join((ColonyClient.update_colony_settings.__doc__ or "").split())
        assert "``social_links`` (platform release 2026-11-14c)" in doc
        assert "**The whole set is replaced**" in doc
        assert "``{}`` or ``None`` clears them all" in doc
        for key in ("``website``", "``github``", "``x``", "``youtube``"):
            assert key in doc

    def test_the_async_docstring_points_at_it(self) -> None:
        doc = " ".join((AsyncColonyClient.update_colony_settings.__doc__ or "").split())
        assert "``social_links``" in doc and "whole set is replaced" in doc


# ---------------------------------------------------------------------------
# Async
# ---------------------------------------------------------------------------


class TestSetSocialLinksAsync:
    @pytest.mark.parametrize("sent,answer", SETS, ids=SET_IDS)
    async def test_sends_the_links_as_given_and_reads_back_what_was_stored(
        self, sent: dict | None, answer: dict
    ) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _json_response(answer)

        out = await _make_client(handler).update_colony_settings(COLONY_ID, social_links=sent)

        assert [r.method for r in seen] == ["PATCH"]
        assert str(seen[0].url) == f"{BASE}/colonies/{COLONY_ID}"
        assert json.loads(seen[0].content) == {"social_links": sent}
        stored = answer["social_links"]
        assert Colony.from_dict(out).social_links == (ColonySocialLinks.from_dict(stored) if stored else None)


# ---------------------------------------------------------------------------
# MockColonyClient
# ---------------------------------------------------------------------------


class TestSetSocialLinksMock:
    @pytest.mark.parametrize("sent,answer", SETS, ids=SET_IDS)
    def test_records_the_links_as_given_and_answers_a_canned_colony(self, sent: dict | None, answer: dict) -> None:
        mock = MockColonyClient(responses={"update_colony_settings": answer})
        out = mock.update_colony_settings(COLONY_ID, social_links=sent)
        assert mock.calls == [("update_colony_settings", {"colony": COLONY_ID, "social_links": sent})]
        stored = answer["social_links"]
        assert Colony.from_dict(out).social_links == (ColonySocialLinks.from_dict(stored) if stored else None)
