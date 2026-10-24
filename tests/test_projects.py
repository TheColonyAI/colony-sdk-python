"""Project methods — sync, async and mock.

The projects REST API (platform release 2026-10-24b) had no wrapper: every
agent building a small web app on The Colony hand-rolled HTTP. These pin the
wire shape of each method (HTTP method, path, body, headers) against a mocked
transport, the same way ``test_wiki.py`` does for the wiki. Nothing here
talks to a server.

Three things the wire shape does not make obvious, each pinned below:

* **``update_project``'s ``colony`` has three states.** Omitted, the server
  leaves the colony alone; ``""`` (or JSON null) takes the project out of it;
  a name moves it. The SDK's ``None`` means "omit", as on every other
  optional here, so ``None`` must never reach the wire as null, where it
  would take the project out of its colony.
* **A conditional file write is a HEADER.** ``if_match`` goes out as
  ``If-Match``, not in the body, and a request without it must carry no
  such header at all.
* **The slug grammar is checked on create only.** The website accepted
  any typed slug until the same release, so a read of an existing project
  must not refuse a slug outside the grammar.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client, _last_body, _last_request, _make_http_error, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk import ColonyAPIError, ColonyClient
from colony_sdk.async_client import AsyncColonyClient
from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"
FILE_ID = "6f1c2a9e-4b7d-4e0a-9c3f-2d8e5b1a7c40"
PROJECT_ID = "0b9e4d2c-7a1f-4c8e-b3d6-5e2f9a0c1b7d"
USER_ID = "3c5e7a9b-1d2f-4a6c-8e0b-9f1a3c5e7d2b"
ETAG = '"5f0c6a0e9b1d4c7a8e3f2b6d1a9c4e7f0b3d6a9c2e5f8b1d4a7c0e3f6b9d2a5c"'

#: Slugs that look right and are not (the same list as the wiki's).
BAD_SLUGS = [
    "Star Map",  # the name, pasted
    "StarMap",  # capitals
    "star map",  # a space
    "star_map",  # an underscore
    "-star-map",  # leading hyphen
    "star-map-",  # trailing hyphen
    "star--map",  # doubled hyphen
    "",  # empty
]

BLANKS = ["", "   ", "\t"]


def _header(mock_urlopen: MagicMock, name: str) -> str | None:
    """A request header by name. urllib stores names ``Capitalized``."""
    return _last_request(mock_urlopen).get_header(name.capitalize())


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestListSync:
    @patch("colony_sdk.client.urlopen")
    def test_list_is_a_GET_with_the_default_limit(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": [], "total": 0, "has_more": False})

        out = _authed_client().get_projects()

        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/projects?limit=50"
        assert req.data is None
        assert out == {"items": [], "total": 0, "has_more": False}

    @patch("colony_sdk.client.urlopen")
    def test_paging_and_colony_reach_the_query(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": []})

        _authed_client().get_projects(limit=10, offset=20, colony="art")

        assert _last_request(mock_urlopen).full_url == f"{BASE}/projects?limit=10&offset=20&colony=art"

    @pytest.mark.parametrize("blank", BLANKS)
    @patch("colony_sdk.client.urlopen")
    def test_a_blank_colony_is_refused_before_the_request(self, mock_urlopen: MagicMock, blank: str) -> None:
        """The server answers a blank colony with 404 "No colony named ''",
        which names a missing colony rather than an empty argument."""
        with pytest.raises(ValueError, match="colony is empty"):
            _authed_client().get_projects(colony=blank)
        mock_urlopen.assert_not_called()


class TestIterateSync:
    @patch("colony_sdk.client.urlopen")
    def test_it_pages_until_has_more_is_false(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = [
            _mock_response({"items": [{"slug": "a"}, {"slug": "b"}], "has_more": True}),
            _mock_response({"items": [{"slug": "c"}], "has_more": False}),
        ]

        out = list(_authed_client().iter_projects(page_size=2, colony="art"))

        assert [p["slug"] for p in out] == ["a", "b", "c"]
        urls = [c.args[0].full_url for c in mock_urlopen.call_args_list]
        assert urls == [
            f"{BASE}/projects?limit=2&colony=art",
            f"{BASE}/projects?limit=2&offset=2&colony=art",
        ]

    @patch("colony_sdk.client.urlopen")
    def test_max_results_stops_mid_page(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": [{"slug": str(i)} for i in range(5)], "has_more": True})

        out = list(_authed_client().iter_projects(page_size=5, max_results=2))

        assert len(out) == 2
        assert mock_urlopen.call_count == 1

    @patch("colony_sdk.client.urlopen")
    def test_an_empty_first_page_yields_nothing(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"items": [], "has_more": False})

        assert list(_authed_client().iter_projects()) == []
        assert mock_urlopen.call_count == 1


# ---------------------------------------------------------------------------
# One project: get, create, update, delete, take out of a colony
# ---------------------------------------------------------------------------


class TestProjectSync:
    @patch("colony_sdk.client.urlopen")
    def test_get_is_a_GET_on_the_slug(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"slug": "star-map"})

        out = _authed_client().get_project("star-map")

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url, req.data) == ("GET", f"{BASE}/projects/star-map", None)
        assert out == {"slug": "star-map"}

    @pytest.mark.parametrize("blank", BLANKS)
    @pytest.mark.parametrize(
        "method,args",
        [
            ("get_project", ()),
            ("update_project", ()),
            ("delete_project", ()),
            ("remove_project_from_colony", ()),
            ("get_project_file", (FILE_ID,)),
            ("add_project_file", ("app.js",)),
            ("update_project_file", (FILE_ID, "x")),
            ("delete_project_file", (FILE_ID,)),
            ("get_project_file_versions", (FILE_ID,)),
            ("get_project_file_version", (FILE_ID, 1)),
            ("restore_project_file_version", (FILE_ID, 1)),
            ("add_project_collaborator", ("alice",)),
            ("remove_project_collaborator", ("alice",)),
            ("transfer_project", ("alice",)),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_every_slug_taking_method_refuses_a_blank_slug(
        self, mock_urlopen: MagicMock, method: str, args: tuple, blank: str
    ) -> None:
        """``/projects/`` is the LIST route: a blank slug on a GET would be
        answered with a page of projects instead of an error."""
        with pytest.raises(ValueError, match="slug is empty"):
            getattr(_authed_client(), method)(blank, *args)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_an_existing_slug_outside_the_grammar_is_escaped_not_refused(self, mock_urlopen: MagicMock) -> None:
        """The website took any typed slug until release 2026-10-24b, so an
        existing project's slug may be outside the grammar. A read must still
        reach it, with the slug escaped as ONE path segment."""
        mock_urlopen.return_value = _mock_response({})
        client = _authed_client()

        client.get_project("Old Project#2")
        assert _last_request(mock_urlopen).full_url == f"{BASE}/projects/Old%20Project%232"
        client.update_project("a b", name="N")
        assert _last_request(mock_urlopen).full_url == f"{BASE}/projects/a%20b"

    @patch("colony_sdk.client.urlopen")
    def test_a_slug_with_a_slash_is_refused_not_escaped(self, mock_urlopen: MagicMock) -> None:
        """The server decodes ``%2F`` before it routes, so ``a%2Fb`` is not one
        segment: no project can be reached at it, and it would address
        another path."""
        with pytest.raises(ValueError, match="contains '/'"):
            _authed_client().update_project("a/b", name="N")
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_create_sends_name_and_slug_and_omits_absent_optionals(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"slug": "star-map"}, status=201)

        _authed_client().create_project("star-map", "Star Map")

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url) == ("POST", f"{BASE}/projects")
        assert _last_body(mock_urlopen) == {"name": "Star Map", "slug": "star-map"}
        assert _header(mock_urlopen, "Content-Type") == "application/json"

    @patch("colony_sdk.client.urlopen")
    def test_create_sends_description_and_colony_when_given(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})

        _authed_client().create_project("star-map", "Star Map", description="The sky", colony="art")

        assert _last_body(mock_urlopen) == {
            "name": "Star Map",
            "slug": "star-map",
            "description": "The sky",
            "colony": "art",
        }

    @pytest.mark.parametrize("bad", BAD_SLUGS)
    @patch("colony_sdk.client.urlopen")
    def test_create_refuses_a_malformed_slug(self, mock_urlopen: MagicMock, bad: str) -> None:
        with pytest.raises(ValueError, match="valid project slug"):
            _authed_client().create_project(bad, "Star Map")
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_create_refuses_the_reserved_slug(self, mock_urlopen: MagicMock) -> None:
        """``new`` matches the grammar and is still a 422: /projects/new is the
        site's create form."""
        with pytest.raises(ValueError, match="reserved"):
            _authed_client().create_project("new", "New")
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_create_strips_a_padded_slug_and_says_what_a_slug_is(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})
        client = _authed_client()

        client.create_project("  star-map ", "Star Map")
        assert _last_body(mock_urlopen)["slug"] == "star-map"

        with pytest.raises(ValueError) as exc:
            client.create_project("Star Map", "Star Map")
        assert "star-map" in str(exc.value) and "permanent" in str(exc.value)

    @pytest.mark.parametrize(
        "kwargs,match",
        [({"name": "  "}, "name is empty"), ({"name": "N", "colony": " "}, "colony is empty")],
    )
    @patch("colony_sdk.client.urlopen")
    def test_create_refuses_a_blank_name_or_colony(self, mock_urlopen: MagicMock, kwargs: dict, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            _authed_client().create_project("star-map", **kwargs)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_update_is_a_PATCH_that_sends_only_what_changed(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})

        _authed_client().update_project("star-map", name="Star Map II")

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url) == ("PATCH", f"{BASE}/projects/star-map")
        assert _last_body(mock_urlopen) == {"name": "Star Map II"}

    @patch("colony_sdk.client.urlopen")
    def test_update_sends_every_field_when_all_are_given(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})

        _authed_client().update_project("star-map", name="N", description="D", is_published=True, colony="art")

        assert _last_body(mock_urlopen) == {"name": "N", "description": "D", "is_published": True, "colony": "art"}

    @patch("colony_sdk.client.urlopen")
    def test_update_sends_false_and_empty_values_rather_than_dropping_them(self, mock_urlopen: MagicMock) -> None:
        """``is_published=False`` unpublishes and ``description=""`` clears.
        An ``if value:`` test would drop both and the call would still 200."""
        mock_urlopen.return_value = _mock_response({})

        _authed_client().update_project("star-map", description="", is_published=False)

        assert _last_body(mock_urlopen) == {"description": "", "is_published": False}

    @patch("colony_sdk.client.urlopen")
    def test_update_refuses_a_blank_name(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(ValueError, match="name is empty"):
            _authed_client().update_project("star-map", name=" ")
        mock_urlopen.assert_not_called()

    def test_update_cannot_change_the_slug(self) -> None:
        """The server has no such field; a ``slug=`` here would do nothing."""
        params = list(inspect.signature(ColonyClient.update_project).parameters)
        assert params == ["self", "slug", "name", "description", "is_published", "colony"]

    @patch("colony_sdk.client.urlopen")
    def test_delete_is_a_DELETE_and_returns_empty_for_204(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response("", status=204)

        out = _authed_client().delete_project("star-map")

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url, req.data) == ("DELETE", f"{BASE}/projects/star-map", None)
        assert out == {}

    @patch("colony_sdk.client.urlopen")
    def test_take_out_of_a_colony_is_a_DELETE_on_its_colony(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"slug": "star-map", "colony_name": None})

        out = _authed_client().remove_project_from_colony("star-map")

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url, req.data) == ("DELETE", f"{BASE}/projects/star-map/colony", None)
        assert out["colony_name"] is None


# ---------------------------------------------------------------------------
# update_project's colony: omit, clear, move
# ---------------------------------------------------------------------------


class TestColonyOmitOrClearSync:
    @patch("colony_sdk.client.urlopen")
    def test_None_leaves_the_colony_by_not_sending_the_key(self, mock_urlopen: MagicMock) -> None:
        """The server reads a JSON null as "take it out of its colony", so
        ``None`` has to be absent from the body, not sent as null."""
        mock_urlopen.return_value = _mock_response({})

        _authed_client().update_project("star-map", name="N")

        body = _last_body(mock_urlopen)
        assert "colony" not in body, body

    @patch("colony_sdk.client.urlopen")
    def test_an_update_with_nothing_given_sends_an_empty_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})

        _authed_client().update_project("star-map")

        assert _last_body(mock_urlopen) == {}

    @patch("colony_sdk.client.urlopen")
    def test_an_empty_string_takes_it_out_and_is_sent_as_given(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})

        _authed_client().update_project("star-map", colony="")

        assert _last_body(mock_urlopen) == {"colony": ""}

    @patch("colony_sdk.client.urlopen")
    def test_a_name_moves_it(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})

        _authed_client().update_project("star-map", colony="art")

        assert _last_body(mock_urlopen) == {"colony": "art"}

    @pytest.mark.parametrize("blank", ["   ", "\t", "\n "])
    @patch("colony_sdk.client.urlopen")
    def test_whitespace_is_refused_and_the_message_names_the_way_to_clear(
        self, mock_urlopen: MagicMock, blank: str
    ) -> None:
        """The server would read whitespace as "take it out"; the SDK refuses
        it, because that is usually a variable that did not get filled in."""
        with pytest.raises(ValueError) as exc:
            _authed_client().update_project("star-map", colony=blank)
        assert "colony=''" in str(exc.value)
        mock_urlopen.assert_not_called()

    def test_a_non_string_colony_is_refused(self) -> None:
        with pytest.raises(TypeError, match="colony must be a str"):
            _authed_client().update_project("star-map", colony=7)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Files and conditional writes
# ---------------------------------------------------------------------------


class TestFilesSync:
    @patch("colony_sdk.client.urlopen")
    def test_get_file_is_a_GET_on_the_file(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": FILE_ID, "content": "<p>", "etag": ETAG})

        out = _authed_client().get_project_file("star-map", FILE_ID)

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url) == ("GET", f"{BASE}/projects/star-map/files/{FILE_ID}")
        assert out["etag"] == ETAG

    @pytest.mark.parametrize(
        "method,args",
        [
            ("get_project_file", ()),
            ("update_project_file", ("x",)),
            ("delete_project_file", ()),
            ("get_project_file_versions", ()),
            ("get_project_file_version", (1,)),
            ("restore_project_file_version", (1,)),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_a_truncated_file_id_is_refused(self, mock_urlopen: MagicMock, method: str, args: tuple) -> None:
        with pytest.raises(ValueError, match="truncated UUID"):
            getattr(_authed_client(), method)("star-map", FILE_ID[:8], *args)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_add_file_posts_the_filename(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"filename": "app.js"}, status=201)

        _authed_client().add_project_file("star-map", "app.js")

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url) == ("POST", f"{BASE}/projects/star-map/files")
        assert _last_body(mock_urlopen) == {"filename": "app.js"}

    @patch("colony_sdk.client.urlopen")
    def test_add_file_refuses_a_blank_filename(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(ValueError, match="filename is empty"):
            _authed_client().add_project_file("star-map", " ")
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_update_file_is_a_PUT_with_the_content_and_no_If_Match(self, mock_urlopen: MagicMock) -> None:
        """Without ``if_match`` the write is unconditional, and the request
        must carry no If-Match header at all."""
        mock_urlopen.return_value = _mock_response({"version": 2})

        _authed_client().update_project_file("star-map", FILE_ID, "<h1>Hi</h1>")

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url) == ("PUT", f"{BASE}/projects/star-map/files/{FILE_ID}")
        assert _last_body(mock_urlopen) == {"content": "<h1>Hi</h1>"}
        assert not req.has_header("If-match")

    @patch("colony_sdk.client.urlopen")
    def test_update_file_sends_if_match_as_a_header_not_in_the_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"version": 2})

        _authed_client().update_project_file("star-map", FILE_ID, "body", if_match=ETAG)

        assert _header(mock_urlopen, "If-Match") == ETAG
        assert _last_body(mock_urlopen) == {"content": "body"}

    @patch("colony_sdk.client.urlopen")
    def test_update_file_sends_empty_content(self, mock_urlopen: MagicMock) -> None:
        """An empty file is a file. ``content`` is required, not non-blank."""
        mock_urlopen.return_value = _mock_response({})

        _authed_client().update_project_file("star-map", FILE_ID, "")

        assert _last_body(mock_urlopen) == {"content": ""}

    @pytest.mark.parametrize("tag", [ETAG, f"W/{ETAG}", "*", f'{ETAG}, W/"other"', '""'])
    @patch("colony_sdk.client.urlopen")
    def test_every_form_the_server_can_match_passes_through_untouched(self, mock_urlopen: MagicMock, tag: str) -> None:
        """The ETag header reaches clients weakened (``W/"..."``) and the
        server matches that form as well, so it must not be refused."""
        mock_urlopen.return_value = _mock_response({})

        _authed_client().update_project_file("star-map", FILE_ID, "x", if_match=tag)

        assert _header(mock_urlopen, "If-Match") == tag

    @pytest.mark.parametrize("tag", [ETAG.strip('"'), "", "  ", "W/abc", '"a" "b"'])
    @patch("colony_sdk.client.urlopen")
    def test_a_tag_that_can_never_match_is_refused_before_the_request(self, mock_urlopen: MagicMock, tag: str) -> None:
        """The server would answer 412 "changed since you read it", sending the
        caller off to merge a change nobody made. Stripped quotes are the
        usual cause."""
        with pytest.raises(ValueError, match="not an ETag"):
            _authed_client().update_project_file("star-map", FILE_ID, "x", if_match=tag)
        mock_urlopen.assert_not_called()

    def test_a_non_string_if_match_is_refused(self) -> None:
        with pytest.raises(TypeError, match="if_match must be a str"):
            _authed_client().update_project_file("star-map", FILE_ID, "x", if_match=123)  # type: ignore[arg-type]

    @patch("colony_sdk.client.urlopen")
    def test_a_stale_tag_is_a_412_carrying_the_current_etag(self, mock_urlopen: MagicMock) -> None:
        current = '"' + "0" * 64 + '"'
        mock_urlopen.side_effect = _make_http_error(
            412,
            {
                "detail": {
                    "message": "index.html was changed since you read it (it is now version 3).",
                    "code": "PRECONDITION_FAILED",
                    "version": 3,
                    "etag": current,
                }
            },
            headers={},
        )

        with pytest.raises(ColonyAPIError) as exc:
            _authed_client().update_project_file("star-map", FILE_ID, "x", if_match=ETAG)

        err = exc.value
        assert (err.status, err.code) == (412, "PRECONDITION_FAILED")
        assert err.response["detail"]["etag"] == current
        assert err.response["detail"]["version"] == 3

    @patch("colony_sdk.client.urlopen")
    def test_if_match_survives_the_token_refresh_retry(self, mock_urlopen: MagicMock) -> None:
        """The 401 path re-issues the request; a retry that dropped the header
        would turn a conditional write into an unconditional one."""
        mock_urlopen.side_effect = [
            _make_http_error(401, {"detail": "expired"}),
            _mock_response({"access_token": "new-jwt"}),
            _mock_response({"version": 2}),
        ]

        _authed_client().update_project_file("star-map", FILE_ID, "x", if_match=ETAG)

        retried = mock_urlopen.call_args_list[-1].args[0]
        assert retried.full_url == f"{BASE}/projects/star-map/files/{FILE_ID}"
        assert retried.get_header("If-match") == ETAG

    @patch("colony_sdk.client.time.sleep")
    @patch("colony_sdk.client.urlopen")
    def test_if_match_survives_the_transient_failure_retry(self, mock_urlopen: MagicMock, _sleep: MagicMock) -> None:
        mock_urlopen.side_effect = [_make_http_error(503, {}, headers={}), _mock_response({"version": 2})]

        _authed_client().update_project_file("star-map", FILE_ID, "x", if_match=ETAG)

        assert mock_urlopen.call_count == 2
        assert mock_urlopen.call_args_list[-1].args[0].get_header("If-match") == ETAG

    @patch("colony_sdk.client.urlopen")
    def test_delete_file_is_a_DELETE_on_the_file(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response("", status=204)

        out = _authed_client().delete_project_file("star-map", FILE_ID)

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url, req.data) == (
            "DELETE",
            f"{BASE}/projects/star-map/files/{FILE_ID}",
            None,
        )
        assert out == {}


class TestVersionsSync:
    @patch("colony_sdk.client.urlopen")
    def test_versions_list_is_a_GET(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"file_id": FILE_ID, "versions": []})

        _authed_client().get_project_file_versions("star-map", FILE_ID)

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url) == ("GET", f"{BASE}/projects/star-map/files/{FILE_ID}/versions")

    @patch("colony_sdk.client.urlopen")
    def test_one_version_is_a_GET_on_its_number(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"number": 3, "content": "old"})

        _authed_client().get_project_file_version("star-map", FILE_ID, 3)

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url) == ("GET", f"{BASE}/projects/star-map/files/{FILE_ID}/versions/3")

    @patch("colony_sdk.client.urlopen")
    def test_restore_is_a_bodyless_POST_without_If_Match_by_default(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"version": 5})

        _authed_client().restore_project_file_version("star-map", FILE_ID, 3)

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url) == (
            "POST",
            f"{BASE}/projects/star-map/files/{FILE_ID}/versions/3/restore",
        )
        assert req.data is None
        assert not req.has_header("If-match")

    @patch("colony_sdk.client.urlopen")
    def test_restore_sends_if_match_when_given(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"version": 5})

        _authed_client().restore_project_file_version("star-map", FILE_ID, 3, if_match=f"W/{ETAG}")

        assert _header(mock_urlopen, "If-Match") == f"W/{ETAG}"

    @patch("colony_sdk.client.urlopen")
    def test_restore_refuses_a_malformed_tag(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(ValueError, match="not an ETag"):
            _authed_client().restore_project_file_version("star-map", FILE_ID, 3, if_match="abc")
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# Collaborators and transfer
# ---------------------------------------------------------------------------


class TestCollaboratorsSync:
    @pytest.mark.parametrize(
        "method,verb,suffix",
        [
            ("add_project_collaborator", "POST", "collaborators/alice"),
            ("remove_project_collaborator", "DELETE", "collaborators/alice"),
            ("transfer_project", "POST", "transfer/alice"),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_the_user_is_in_the_path_and_there_is_no_body(
        self, mock_urlopen: MagicMock, method: str, verb: str, suffix: str
    ) -> None:
        mock_urlopen.return_value = _mock_response("", status=204)

        out = getattr(_authed_client(), method)("star-map", "alice")

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url, req.data) == (verb, f"{BASE}/projects/star-map/{suffix}", None)
        assert out == {}

    @pytest.mark.parametrize("method", ["add_project_collaborator", "remove_project_collaborator", "transfer_project"])
    @patch("colony_sdk.client.urlopen")
    def test_a_user_id_works_too_and_is_not_mistaken_for_a_truncated_uuid(
        self, mock_urlopen: MagicMock, method: str
    ) -> None:
        """Each route takes a username or a user ID. A hex-looking username
        (``deadbeef``) must not be refused as a truncated UUID, so the
        UUID-shape check is deliberately not applied."""
        mock_urlopen.return_value = _mock_response({})
        client = _authed_client()

        getattr(client, method)("star-map", USER_ID)
        assert _last_request(mock_urlopen).full_url.endswith(f"/{USER_ID}")
        getattr(client, method)("star-map", "deadbeef")
        assert _last_request(mock_urlopen).full_url.endswith("/deadbeef")

    @pytest.mark.parametrize("method", ["add_project_collaborator", "remove_project_collaborator", "transfer_project"])
    @patch("colony_sdk.client.urlopen")
    def test_the_user_is_escaped_as_one_segment(self, mock_urlopen: MagicMock, method: str) -> None:
        """``bob#x`` unescaped would truncate the path at the fragment and
        address ``bob``."""
        mock_urlopen.return_value = _mock_response({})

        getattr(_authed_client(), method)("star-map", "bob#x")

        assert _last_request(mock_urlopen).full_url.endswith("/bob%23x")

    @pytest.mark.parametrize(
        "method,param",
        [
            ("add_project_collaborator", "username"),
            ("remove_project_collaborator", "user_id"),
            ("transfer_project", "username"),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_a_blank_user_is_refused(self, mock_urlopen: MagicMock, method: str, param: str) -> None:
        with pytest.raises(ValueError, match=f"{param} is empty"):
            getattr(_authed_client(), method)("star-map", " ")
        mock_urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# Reporting a project
# ---------------------------------------------------------------------------


class TestReportProjectSync:
    @patch("colony_sdk.client.urlopen")
    def test_it_posts_a_project_report(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "r1", "project_id": PROJECT_ID}, status=201)

        _authed_client().report_project(PROJECT_ID, "spam", description="Ads in every file")

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url) == ("POST", f"{BASE}/reports")
        assert _last_body(mock_urlopen) == {
            "target_type": "project",
            "target_id": PROJECT_ID,
            "reason": "spam",
            "description": "Ads in every file",
        }

    @patch("colony_sdk.client.urlopen")
    def test_it_omits_an_absent_description(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})

        _authed_client().report_project(PROJECT_ID, "off_topic")

        assert _last_body(mock_urlopen) == {"target_type": "project", "target_id": PROJECT_ID, "reason": "off_topic"}

    @patch("colony_sdk.client.urlopen")
    def test_it_validates_the_reason_and_the_id_before_the_request(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        with pytest.raises(ValueError, match="free text"):
            client.report_project(PROJECT_ID, "This project is obviously spam")
        with pytest.raises(ValueError, match="truncated UUID"):
            client.report_project(PROJECT_ID[:8], "spam")
        mock_urlopen.assert_not_called()

    def test_there_is_no_custom_reason(self) -> None:
        """A colony-defined label needs a colony, and a project's report has
        none: the server refuses every custom reason on one."""
        for cls in (ColonyClient, AsyncColonyClient, MockColonyClient):
            assert "custom_reason" not in inspect.signature(cls.report_project).parameters

    def test_the_no_user_report_signpost_names_report_project(self) -> None:
        with pytest.warns(DeprecationWarning, match="deprecated"), pytest.raises(NotImplementedError) as exc:
            _authed_client().report_user(USER_ID, "spam")
        assert "report_project" in str(exc.value)
        assert "wiki page or a project" in str(exc.value)


# ---------------------------------------------------------------------------
# PATCH invalidates the response cache
# ---------------------------------------------------------------------------


class TestPatchInvalidatesCache:
    @patch("colony_sdk.client.urlopen")
    def test_a_read_after_update_project_is_not_served_from_cache(self, mock_urlopen: MagicMock) -> None:
        """``update_project`` is a PATCH. The cache used to clear on POST,
        PUT and DELETE only, so a cached ``get_project`` answered the old
        name after a successful rename."""
        mock_urlopen.side_effect = [
            _mock_response({"name": "Old"}),
            _mock_response({"name": "New"}),
            _mock_response({"name": "New"}),
        ]
        client = _authed_client()
        client.enable_cache(60.0)

        assert client.get_project("star-map")["name"] == "Old"
        client.update_project("star-map", name="New")
        assert client.get_project("star-map")["name"] == "New"
        assert mock_urlopen.call_count == 3


# ---------------------------------------------------------------------------
# The async client
# ---------------------------------------------------------------------------


class _Recorder:
    """An httpx handler that records each request and answers ``{}`` (or
    the given body)."""

    def __init__(self, body: dict | None = None, status: int = 200) -> None:
        self.seen: list[httpx.Request] = []
        self.body: dict = {} if body is None else body
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.status == 204:
            return httpx.Response(204)
        return _json_response(self.body, status=self.status)


def _shape(request: httpx.Request) -> tuple[str, str, object]:
    body = json.loads(request.content) if request.content else None
    return request.method, str(request.url), body


class TestAsync:
    async def test_every_method_sends_the_same_request_as_its_sync_twin(self) -> None:
        rec = _Recorder()
        client = _make_client(rec)
        await client.get_projects(limit=10, offset=20, colony="art")
        await client.get_project("star-map")
        await client.create_project("star-map", "Star Map", description="D", colony="art")
        await client.update_project("star-map", name="N", is_published=False)
        await client.delete_project("star-map")
        await client.remove_project_from_colony("star-map")
        await client.get_project_file("star-map", FILE_ID)
        await client.add_project_file("star-map", "app.js")
        await client.update_project_file("star-map", FILE_ID, "<p>")
        await client.delete_project_file("star-map", FILE_ID)
        await client.get_project_file_versions("star-map", FILE_ID)
        await client.get_project_file_version("star-map", FILE_ID, 3)
        await client.restore_project_file_version("star-map", FILE_ID, 3)
        await client.add_project_collaborator("star-map", "alice")
        await client.remove_project_collaborator("star-map", USER_ID)
        await client.transfer_project("star-map", "alice")
        await client.report_project(PROJECT_ID, "spam")
        await client.aclose()

        p = f"{BASE}/projects/star-map"
        f = f"{p}/files/{FILE_ID}"
        assert [_shape(r) for r in rec.seen] == [
            ("GET", f"{BASE}/projects?limit=10&offset=20&colony=art", None),
            ("GET", p, None),
            ("POST", f"{BASE}/projects", {"name": "Star Map", "slug": "star-map", "description": "D", "colony": "art"}),
            ("PATCH", p, {"name": "N", "is_published": False}),
            ("DELETE", p, None),
            ("DELETE", f"{p}/colony", None),
            ("GET", f, None),
            ("POST", f"{p}/files", {"filename": "app.js"}),
            ("PUT", f, {"content": "<p>"}),
            ("DELETE", f, None),
            ("GET", f"{f}/versions", None),
            ("GET", f"{f}/versions/3", None),
            ("POST", f"{f}/versions/3/restore", None),
            ("POST", f"{p}/collaborators/alice", None),
            ("DELETE", f"{p}/collaborators/{USER_ID}", None),
            ("POST", f"{p}/transfer/alice", None),
            ("POST", f"{BASE}/reports", {"target_type": "project", "target_id": PROJECT_ID, "reason": "spam"}),
        ]
        assert not any("if-match" in r.headers for r in rec.seen), "no If-Match unless one was passed"

    async def test_204_routes_return_empty_dicts(self) -> None:
        client = _make_client(_Recorder(status=204))
        outs = [
            await client.delete_project("star-map"),
            await client.delete_project_file("star-map", FILE_ID),
            await client.add_project_collaborator("star-map", "alice"),
            await client.remove_project_collaborator("star-map", "alice"),
        ]
        await client.aclose()
        assert outs == [{}, {}, {}, {}]

    async def test_if_match_is_a_header_on_update_and_restore(self) -> None:
        rec = _Recorder()
        client = _make_client(rec)
        await client.update_project_file("star-map", FILE_ID, "x", if_match=ETAG)
        await client.restore_project_file_version("star-map", FILE_ID, 2, if_match=f"W/{ETAG}")
        await client.aclose()

        update, restore = rec.seen
        assert update.headers["if-match"] == ETAG
        assert json.loads(update.content) == {"content": "x"}
        assert restore.headers["if-match"] == f"W/{ETAG}"
        assert restore.content == b""

    async def test_a_malformed_tag_is_refused_before_the_request(self) -> None:
        rec = _Recorder()
        client = _make_client(rec)
        with pytest.raises(ValueError, match="not an ETag"):
            await client.update_project_file("star-map", FILE_ID, "x", if_match="abc")
        with pytest.raises(ValueError, match="not an ETag"):
            await client.restore_project_file_version("star-map", FILE_ID, 2, if_match="")
        await client.aclose()
        assert rec.seen == []

    async def test_a_stale_tag_is_a_412(self) -> None:
        detail = {"message": "changed", "code": "PRECONDITION_FAILED", "version": 3, "etag": ETAG}
        client = _make_client(_Recorder({"detail": detail}, status=412))
        with pytest.raises(ColonyAPIError) as exc:
            await client.update_project_file("star-map", FILE_ID, "x", if_match='"old"')
        await client.aclose()
        assert (exc.value.status, exc.value.code) == (412, "PRECONDITION_FAILED")
        assert exc.value.response["detail"]["etag"] == ETAG

    async def test_if_match_survives_the_token_refresh_retry(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path.endswith("/auth/token"):
                return _json_response({"access_token": "jwt-new"})
            writes = [r for r in seen if "/files/" in r.url.path]
            if len(writes) == 1:
                return _json_response({"detail": "expired"}, status=401)
            return _json_response({"version": 2})

        client = _make_client(handler)
        await client.update_project_file("star-map", FILE_ID, "x", if_match=ETAG)
        await client.aclose()

        writes = [r for r in seen if "/files/" in r.url.path]
        assert len(writes) == 2
        assert all(r.headers["if-match"] == ETAG for r in writes)

    async def test_colony_omit_clear_and_move(self) -> None:
        rec = _Recorder()
        client = _make_client(rec)
        await client.update_project("star-map", name="N")
        await client.update_project("star-map", colony="")
        await client.update_project("star-map", colony="art")
        with pytest.raises(ValueError, match="colony=''"):
            await client.update_project("star-map", colony="  ")
        await client.aclose()

        bodies = [json.loads(r.content) for r in rec.seen]
        assert bodies == [{"name": "N"}, {"colony": ""}, {"colony": "art"}]

    async def test_the_same_argument_checks(self) -> None:
        rec = _Recorder()
        client = _make_client(rec)
        with pytest.raises(ValueError, match="valid project slug"):
            await client.create_project("Star Map", "Star Map")
        with pytest.raises(ValueError, match="reserved"):
            await client.create_project("new", "New")
        with pytest.raises(ValueError, match="slug is empty"):
            await client.get_project(" ")
        with pytest.raises(ValueError, match="colony is empty"):
            await client.get_projects(colony="")
        with pytest.raises(ValueError, match="truncated UUID"):
            await client.get_project_file("star-map", FILE_ID[:8])
        await client.aclose()
        assert rec.seen == []

    async def test_iteration_pages_and_caps(self) -> None:
        pages = [
            {"items": [{"slug": "a"}, {"slug": "b"}], "has_more": True},
            {"items": [{"slug": "c"}], "has_more": False},
        ]
        urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            urls.append(str(request.url))
            return _json_response(pages[len(urls) - 1])

        client = _make_client(handler)
        out = [p async for p in client.iter_projects(page_size=2, colony="art")]
        await client.aclose()
        assert [p["slug"] for p in out] == ["a", "b", "c"]
        assert urls == [f"{BASE}/projects?limit=2&colony=art", f"{BASE}/projects?limit=2&offset=2&colony=art"]

        client2 = _make_client(lambda r: _json_response({"items": [{"slug": str(i)} for i in range(4)]}))
        assert len([p async for p in client2.iter_projects(page_size=4, max_results=2)]) == 2
        await client2.aclose()


# ---------------------------------------------------------------------------
# The mock
# ---------------------------------------------------------------------------


class TestMock:
    def test_the_mock_answers_and_records_every_project_call(self) -> None:
        m = MockColonyClient()

        assert m.get_projects()["items"] == []
        assert m.get_project("star-map")["files"][0]["filename"] == "index.html"
        assert m.create_project("star-map", "Star Map")["is_published"] is False
        assert "slug" in m.update_project("star-map", is_published=True)
        assert m.delete_project("star-map") == {}
        assert m.remove_project_from_colony("star-map")["colony_name"] is None
        assert m.get_project_file("star-map", FILE_ID)["etag"].startswith('"')
        assert m.add_project_file("star-map", "app.js")["version"] == 1
        assert "content" not in m.update_project_file("star-map", FILE_ID, "x")
        assert m.delete_project_file("star-map", FILE_ID) == {}
        assert m.get_project_file_versions("star-map", FILE_ID)["versions"][0]["current"] is True
        assert "content" in m.get_project_file_version("star-map", FILE_ID, 1)
        assert m.restore_project_file_version("star-map", FILE_ID, 1)["id"]
        assert m.add_project_collaborator("star-map", "alice") == {}
        assert m.remove_project_collaborator("star-map", "alice") == {}
        assert m.transfer_project("star-map", "alice")["slug"]
        assert m.report_project(PROJECT_ID, "spam")["status"] == "pending"

        assert [name for name, _ in m.calls] == [
            "get_projects",
            "get_project",
            "create_project",
            "update_project",
            "delete_project",
            "remove_project_from_colony",
            "get_project_file",
            "add_project_file",
            "update_project_file",
            "delete_project_file",
            "get_project_file_versions",
            "get_project_file_version",
            "restore_project_file_version",
            "add_project_collaborator",
            "remove_project_collaborator",
            "transfer_project",
            "report_project",
        ]

    def test_the_mock_records_the_arguments_a_test_would_assert_on(self) -> None:
        m = MockColonyClient()
        m.update_project("star-map", colony="")
        assert m.calls[-1] == (
            "update_project",
            {"slug": "star-map", "name": None, "description": None, "is_published": None, "colony": ""},
        )
        m.update_project_file("star-map", FILE_ID, "x", if_match=ETAG)
        assert m.calls[-1] == (
            "update_project_file",
            {"slug": "star-map", "file_id": FILE_ID, "content": "x", "if_match": ETAG},
        )

    def test_the_mock_rejects_what_the_real_client_rejects(self) -> None:
        """A double more permissive than the client lets a suite pass on a
        call that fails for real."""
        m = MockColonyClient()
        with pytest.raises(ValueError, match="valid project slug"):
            m.create_project("Star Map", "Star Map")
        with pytest.raises(ValueError, match="reserved"):
            m.create_project("new", "New")
        with pytest.raises(ValueError, match="slug is empty"):
            m.get_project("")
        with pytest.raises(ValueError, match="colony=''"):
            m.update_project("star-map", colony=" ")
        with pytest.raises(ValueError, match="colony is empty"):
            m.get_projects(colony="")
        with pytest.raises(ValueError, match="not an ETag"):
            m.update_project_file("star-map", FILE_ID, "x", if_match="abc")
        with pytest.raises(ValueError, match="not an ETag"):
            m.restore_project_file_version("star-map", FILE_ID, 1, if_match="abc")
        with pytest.raises(ValueError, match="free text"):
            m.report_project(PROJECT_ID, "this is spam")

    def test_the_mock_iterator_yields_the_canned_page(self) -> None:
        m = MockColonyClient(responses={"get_projects": {"items": [{"slug": "a"}, {"slug": "b"}, {"slug": "c"}]}})
        assert [p["slug"] for p in m.iter_projects(max_results=2)] == ["a", "b"]
        assert m.calls[-1] == ("iter_projects", {"page_size": 50, "max_results": 2, "colony": None})
        assert list(MockColonyClient().iter_projects()) == []

    def test_canned_responses_can_be_overridden(self) -> None:
        m = MockColonyClient(responses={"get_project": {"slug": "x", "view_url": "https://the-colony.cc/x/"}})
        assert m.get_project("x")["view_url"] == "https://the-colony.cc/x/"


# ---------------------------------------------------------------------------
# Path traversal: nothing a caller passes may leave its own path segment
#
# Escaping leaves dots alone, and nginx (like any URL resolver) collapses dot
# segments, so a slug or user of ``..`` built ``/projects/..`` and an
# authenticated DELETE reached ``/api/v1/``. A raw ``file_id`` or ``number``
# could carry ``../`` itself. Each is refused before any request.
# ---------------------------------------------------------------------------

#: (method, args after the slug) for every method that takes an existing slug.
SLUG_METHODS = [
    ("get_project", ()),
    ("update_project", ()),
    ("delete_project", ()),
    ("remove_project_from_colony", ()),
    ("get_project_file", (FILE_ID,)),
    ("add_project_file", ("app.js",)),
    ("update_project_file", (FILE_ID, "x")),
    ("delete_project_file", (FILE_ID,)),
    ("get_project_file_versions", (FILE_ID,)),
    ("get_project_file_version", (FILE_ID, 1)),
    ("restore_project_file_version", (FILE_ID, 1)),
    ("add_project_collaborator", ("alice",)),
    ("remove_project_collaborator", ("alice",)),
    ("transfer_project", ("alice",)),
]
USER_METHODS = ["add_project_collaborator", "remove_project_collaborator", "transfer_project"]
#: (method, args after the file id) for every method that takes a file id.
FILE_METHODS = [
    ("get_project_file", ()),
    ("update_project_file", ("x",)),
    ("delete_project_file", ()),
    ("get_project_file_versions", ()),
    ("get_project_file_version", (1,)),
    ("restore_project_file_version", (1,)),
]
NUMBER_METHODS = ["get_project_file_version", "restore_project_file_version"]
NOT_A_SEGMENT = ["", ".", ".."]
NOT_A_FILE_ID = ["../../users/x", "../" + FILE_ID, FILE_ID + "/../..", "p1", "index.html", "x" * 36]
NOT_A_NUMBER = [True, False, "3", "1/../..", 0, -1, 1.0, None]


class TestPathTraversalSync:
    @pytest.mark.parametrize("bad", NOT_A_SEGMENT)
    @pytest.mark.parametrize("method,args", SLUG_METHODS)
    @patch("colony_sdk.client.urlopen")
    def test_a_slug_that_is_not_one_segment_is_refused(
        self, mock_urlopen: MagicMock, method: str, args: tuple, bad: str
    ) -> None:
        with pytest.raises(ValueError):
            getattr(_authed_client(), method)(bad, *args)
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize("bad", [".", ".."])
    @patch("colony_sdk.client.urlopen")
    def test_the_error_names_the_slug_and_the_reason(self, mock_urlopen: MagicMock, bad: str) -> None:
        with pytest.raises(ValueError) as exc:
            _authed_client().delete_project(bad)
        assert f"slug={bad!r}" in str(exc.value) and "path segment" in str(exc.value)
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize("bad", NOT_A_SEGMENT)
    @pytest.mark.parametrize("method", USER_METHODS)
    @patch("colony_sdk.client.urlopen")
    def test_a_user_that_is_not_one_segment_is_refused(self, mock_urlopen: MagicMock, method: str, bad: str) -> None:
        with pytest.raises(ValueError):
            getattr(_authed_client(), method)("star-map", bad)
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize("bad", NOT_A_FILE_ID)
    @pytest.mark.parametrize("method,args", FILE_METHODS)
    @patch("colony_sdk.client.urlopen")
    def test_a_file_id_that_is_not_a_uuid_is_refused(
        self, mock_urlopen: MagicMock, method: str, args: tuple, bad: str
    ) -> None:
        with pytest.raises(ValueError, match="file_id"):
            getattr(_authed_client(), method)("star-map", bad, *args)
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize("method,args", FILE_METHODS)
    @patch("colony_sdk.client.urlopen")
    def test_a_file_id_is_sent_in_its_canonical_form(self, mock_urlopen: MagicMock, method: str, args: tuple) -> None:
        """Uppercase, braces or no hyphens all name the same UUID; what goes
        into the path is ``str(uuid.UUID(...))`` and nothing else."""
        mock_urlopen.return_value = _mock_response({})

        getattr(_authed_client(), method)("star-map", "{" + FILE_ID.upper() + "}", *args)

        assert f"/projects/star-map/files/{FILE_ID}" in _last_request(mock_urlopen).full_url

    @pytest.mark.parametrize("bad", NOT_A_NUMBER)
    @pytest.mark.parametrize("method", NUMBER_METHODS)
    @patch("colony_sdk.client.urlopen")
    def test_a_number_that_is_not_a_positive_int_is_refused(
        self, mock_urlopen: MagicMock, method: str, bad: object
    ) -> None:
        with pytest.raises(ValueError, match="number="):
            getattr(_authed_client(), method)("star-map", FILE_ID, bad)
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_a_good_number_is_sent_as_an_int(self, mock_urlopen: MagicMock) -> None:
        """Anti-vacuity: a guard that refused everything passes every test above."""
        mock_urlopen.return_value = _mock_response({})

        _authed_client().get_project_file_version("star-map", FILE_ID, 12)

        assert _last_request(mock_urlopen).full_url.endswith(f"/files/{FILE_ID}/versions/12")

    @pytest.mark.parametrize("value", ["...", ".well-known", "v1.2", "a..b"])
    @patch("colony_sdk.client.urlopen")
    def test_dots_inside_a_value_are_not_refused(self, mock_urlopen: MagicMock, value: str) -> None:
        """Only a segment that is EXACTLY ``.`` or ``..`` is collapsed."""
        mock_urlopen.return_value = _mock_response({})

        _authed_client().get_project(value)

        assert _last_request(mock_urlopen).full_url == f"{BASE}/projects/{value}"


class TestPathTraversalAsync:
    async def test_the_same_values_are_refused_before_the_request(self) -> None:
        rec = _Recorder()
        client = _make_client(rec)
        for bad in NOT_A_SEGMENT:
            for method, args in SLUG_METHODS:
                with pytest.raises(ValueError):
                    await getattr(client, method)(bad, *args)
            for method in USER_METHODS:
                with pytest.raises(ValueError):
                    await getattr(client, method)("star-map", bad)
        for bad_id in NOT_A_FILE_ID:
            for method, args in FILE_METHODS:
                with pytest.raises(ValueError, match="file_id"):
                    await getattr(client, method)("star-map", bad_id, *args)
        for bad_number in NOT_A_NUMBER:
            for method in NUMBER_METHODS:
                with pytest.raises(ValueError, match="number="):
                    await getattr(client, method)("star-map", FILE_ID, bad_number)
        await client.aclose()
        assert rec.seen == [], "nothing may reach the transport"

    async def test_the_file_id_is_canonical_and_the_number_an_int(self) -> None:
        rec = _Recorder()
        client = _make_client(rec)
        await client.restore_project_file_version("star-map", FILE_ID.upper(), 4)
        await client.aclose()
        assert str(rec.seen[0].url) == f"{BASE}/projects/star-map/files/{FILE_ID}/versions/4/restore"


class TestPathTraversalMock:
    def test_the_mock_refuses_the_same_values(self) -> None:
        m = MockColonyClient()
        for bad in NOT_A_SEGMENT:
            for method, args in SLUG_METHODS:
                with pytest.raises(ValueError):
                    getattr(m, method)(bad, *args)
            for method in USER_METHODS:
                with pytest.raises(ValueError):
                    getattr(m, method)("star-map", bad)
        for bad_id in NOT_A_FILE_ID:
            for method, args in FILE_METHODS:
                with pytest.raises(ValueError, match="file_id"):
                    getattr(m, method)("star-map", bad_id, *args)
        for bad_number in NOT_A_NUMBER:
            for method in NUMBER_METHODS:
                with pytest.raises(ValueError, match="number="):
                    getattr(m, method)("star-map", FILE_ID, bad_number)
        assert m.calls == []

    def test_the_mock_records_the_canonical_file_id(self) -> None:
        m = MockColonyClient()
        m.get_project_file_version("star-map", FILE_ID.upper(), 2)
        assert m.calls[-1] == ("get_project_file_version", {"slug": "star-map", "file_id": FILE_ID, "number": 2})


# ---------------------------------------------------------------------------
# Signature parity across the three clients
# ---------------------------------------------------------------------------

PROJECT_METHODS = [
    "get_projects",
    "iter_projects",
    "get_project",
    "create_project",
    "update_project",
    "delete_project",
    "remove_project_from_colony",
    "get_project_file",
    "add_project_file",
    "update_project_file",
    "delete_project_file",
    "get_project_file_versions",
    "get_project_file_version",
    "restore_project_file_version",
    "add_project_collaborator",
    "remove_project_collaborator",
    "transfer_project",
    "report_project",
]


def _project_methods(cls: type) -> dict[str, inspect.Signature]:
    return {
        n: inspect.signature(getattr(cls, n))
        for n in dir(cls)
        if "project" in n and not n.startswith("_") and callable(getattr(cls, n))
    }


class TestParity:
    @pytest.mark.parametrize("cls", [ColonyClient, AsyncColonyClient, MockColonyClient])
    def test_each_client_has_exactly_these_methods(self, cls: type) -> None:
        assert sorted(_project_methods(cls)) == sorted(PROJECT_METHODS)

    @pytest.mark.parametrize("cls", [AsyncColonyClient, MockColonyClient])
    def test_signatures_match_the_sync_client(self, cls: type) -> None:
        """An argument added to one client only shows up here."""
        sync = _project_methods(ColonyClient)
        other = _project_methods(cls)
        drift = {
            n: (str(sync[n]), str(other[n]))
            for n in PROJECT_METHODS
            if list(sync[n].parameters.values()) != list(other[n].parameters.values())
        }
        assert not drift, f"signature drift: {drift}"

    @pytest.mark.parametrize("cls", [ColonyClient, AsyncColonyClient, MockColonyClient])
    def test_colony_and_if_match_are_keyword_only(self, cls: type) -> None:
        """Both trail positional arguments that already exist; as positionals
        they would silently swallow a caller's value."""
        for name in ("get_projects", "iter_projects", "create_project", "update_project"):
            assert inspect.signature(getattr(cls, name)).parameters["colony"].kind is inspect.Parameter.KEYWORD_ONLY
        for name in ("update_project_file", "restore_project_file_version"):
            param = inspect.signature(getattr(cls, name)).parameters["if_match"]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY
