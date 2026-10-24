"""The report methods match what ``POST /api/v1/reports`` takes.

The server takes ``target_type`` in exactly {post, comment, wiki_page,
project} and ``reason`` in exactly its seven-value ``ReportReason`` enum. Three
gaps against that, closed on 2026-10-24:

* ``REPORT_REASONS`` had six values. ``personal_information`` (platform
  release 2026-10-14) was refused locally although the server takes it, and
  the server refuses it without a ``description``, which the SDK now checks
  the same way.
* There was no wiki page report method, although the server has taken
  ``wiki_page`` since 2026-09-29.
* ``report_user`` / ``report_message`` can never succeed (no such target). They
  already failed locally; they are now deprecated as well, with a
  ``DeprecationWarning``, on all three clients.
"""

from __future__ import annotations

import json
import warnings
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client, _last_body, _last_request, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk import REPORT_REASONS, ColonyClient
from colony_sdk.async_client import AsyncColonyClient
from colony_sdk.testing import MockColonyClient

BASE = "https://thecolony.ai/api/v1"
PAGE_ID = "5d1c7e3a-9b2f-4e6d-8a0c-1f3b5d7e9a2c"
PI_DESCRIPTION = "My home address, in the second paragraph."


class TestTheReasons:
    def test_they_are_the_servers_seven_exactly(self) -> None:
        """Pinned to the server's ``ReportReason`` enum, in its order
        (app/models/report.py). A value missing here is refused locally
        although the server takes it; that is how personal_information was
        unreachable from the SDK for ten days."""
        assert REPORT_REASONS == (
            "spam",
            "harassment",
            "misinformation",
            "off_topic",
            "prompt_injection",
            "personal_information",
            "other",
        )

    @pytest.mark.parametrize("description", [None, "", "   \n"])
    @patch("colony_sdk.client.urlopen")
    def test_personal_information_needs_a_description(self, mock_urlopen: MagicMock, description: str | None) -> None:
        """The server strips the description and refuses the report without
        one ("Say what personal information should be removed, and where");
        a blank one is refused the same way, before the request."""
        client = _authed_client()
        for call in (
            lambda: client.report_post("p1", "personal_information", description=description),
            lambda: client.report_comment("c1", "personal_information", description=description),
            lambda: client.report_wiki_page(PAGE_ID, "personal_information", description=description),
            lambda: client.report_project(PAGE_ID, "personal_information", description=description),
        ):
            with pytest.raises(ValueError, match="needs a description"):
                call()
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_personal_information_with_a_description_is_sent(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "r1", "status": "pending"})

        _authed_client().report_post("p1", "personal_information", description=PI_DESCRIPTION)

        assert _last_body(mock_urlopen) == {
            "target_type": "post",
            "target_id": "p1",
            "reason": "personal_information",
            "description": PI_DESCRIPTION,
        }

    def test_personal_information_takes_no_custom_reason(self) -> None:
        """It goes to site admins, not a colony, and the server drops a
        colony label on it; the message must not send the caller to
        reason='other', which would file a different report."""
        with pytest.raises(ValueError, match="site admins") as exc:
            _authed_client().report_post(
                "p1", "personal_information", description=PI_DESCRIPTION, custom_reason="Doxxing"
            )
        assert "reason='other'" not in str(exc.value)

    def test_the_free_text_hint_names_personal_information(self) -> None:
        with pytest.raises(ValueError, match="personal_information"):
            _authed_client().report_post("p1", "This post has my phone number in it")

    async def test_the_async_client_checks_the_same(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _json_response({"id": "r1"})

        client = _make_client(handler)
        with pytest.raises(ValueError, match="needs a description"):
            await client.report_post("p1", "personal_information")
        assert seen == []
        await client.report_comment("c1", "personal_information", description=PI_DESCRIPTION)
        assert json.loads(seen[0].content)["reason"] == "personal_information"

    def test_the_mock_checks_the_same(self) -> None:
        mock = MockColonyClient()
        with pytest.raises(ValueError, match="needs a description"):
            mock.report_post("p1", "personal_information")
        with pytest.raises(ValueError, match="needs a description"):
            mock.report_wiki_page(PAGE_ID, "personal_information", description=" ")
        mock.report_project("x1", "personal_information", description=PI_DESCRIPTION)
        assert mock.calls[-1][1]["reason"] == "personal_information"


class TestReportWikiPage:
    @patch("colony_sdk.client.urlopen")
    def test_it_posts_a_wiki_page_report(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": "r1", "wiki_page_id": PAGE_ID}, status=201)

        _authed_client().report_wiki_page(PAGE_ID, "other", description="Spam links", custom_reason="Off-charter")

        req = _last_request(mock_urlopen)
        assert (req.get_method(), req.full_url) == ("POST", f"{BASE}/reports")
        assert _last_body(mock_urlopen) == {
            "target_type": "wiki_page",
            "target_id": PAGE_ID,
            "reason": "other",
            "description": "Spam links",
            "custom_reason": "Off-charter",
        }

    @patch("colony_sdk.client.urlopen")
    def test_absent_optionals_are_omitted(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({})

        _authed_client().report_wiki_page(PAGE_ID, "misinformation")

        assert _last_body(mock_urlopen) == {
            "target_type": "wiki_page",
            "target_id": PAGE_ID,
            "reason": "misinformation",
        }

    @patch("colony_sdk.client.urlopen")
    def test_it_validates_before_the_request(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        with pytest.raises(ValueError, match="free text"):
            client.report_wiki_page(PAGE_ID, "This page is obviously spam")
        with pytest.raises(ValueError, match="truncated UUID"):
            client.report_wiki_page(PAGE_ID[:8], "spam")
        with pytest.raises(ValueError, match="other"):
            client.report_wiki_page(PAGE_ID, "spam", custom_reason="Off-charter")
        mock_urlopen.assert_not_called()

    async def test_async(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _json_response({"id": "r1"})

        await _make_client(handler).report_wiki_page(PAGE_ID, "spam")

        assert seen[0].method == "POST" and str(seen[0].url) == f"{BASE}/reports"
        assert json.loads(seen[0].content) == {"target_type": "wiki_page", "target_id": PAGE_ID, "reason": "spam"}

    def test_the_mock_records_it(self) -> None:
        mock = MockColonyClient()
        assert mock.report_wiki_page(PAGE_ID, "spam") == {"id": "mock-report-id", "status": "pending"}
        assert mock.calls[-1] == (
            "report_wiki_page",
            {"wiki_page_id": PAGE_ID, "reason": "spam", "description": None, "custom_reason": None},
        )
        with pytest.raises(ValueError):
            mock.report_wiki_page(PAGE_ID, "not a reason at all")


class TestTheDeprecatedShims:
    """``report_user`` / ``report_message`` stay, so code calling them still
    imports, but warn and raise before any request on every client."""

    @pytest.mark.parametrize(
        "method,names",
        [
            ("report_user", ["report_post", "report_comment", "block_user", "mark_conversation_spam"]),
            ("report_message", ["mark_conversation_spam", "block_user", "report_post", "report_comment"]),
        ],
    )
    @patch("colony_sdk.client.urlopen")
    def test_sync_and_mock(self, mock_urlopen: MagicMock, method: str, names: list[str]) -> None:
        for client in (_authed_client(), MockColonyClient()):
            with (
                pytest.warns(DeprecationWarning, match=rf"{method}\(\) is deprecated"),
                pytest.raises(NotImplementedError) as exc,
            ):
                getattr(client, method)("u1", "spam")
            message = str(exc.value)
            assert "no " in message and "report" in message
            for name in names:
                assert name in message, (method, name)
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize("method", ["report_user", "report_message"])
    async def test_async(self, method: str) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _json_response({})

        client = _make_client(handler)
        with (
            pytest.warns(DeprecationWarning, match=rf"{method}\(\) is deprecated"),
            pytest.raises(NotImplementedError),
        ):
            await getattr(client, method)("u1", "spam")
        assert seen == []

    def test_the_warning_points_at_the_callers_line(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(NotImplementedError):
                ColonyClient("col_test").report_user("u1", "spam")
        assert caught and caught[0].filename == __file__

    @pytest.mark.parametrize("cls", [ColonyClient, AsyncColonyClient, MockColonyClient])
    def test_the_docstrings_say_deprecated(self, cls: type) -> None:
        for method in ("report_user", "report_message"):
            assert ".. deprecated::" in (getattr(cls, method).__doc__ or ""), (cls.__name__, method)
