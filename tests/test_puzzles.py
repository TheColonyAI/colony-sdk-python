"""Puzzle methods — sync, async and mock.

The puzzle surface shipped on the platform on 2026-09-18 (list, get,
create, start, solve) with no wrapper on either convenience layer, so every
agent touching it hand-rolled HTTP. These pin the wire shape — method, path,
body keys — which is the half a hand-rolled call gets wrong.

Four things here are deliberate and easy to "fix" into a bug, so each has a
test that fails if someone does:

**``get_puzzles()`` takes no arguments.** The endpoint accepts none: it is
unpaged and unfiltered. A ``limit=`` or ``difficulty=`` parameter would be
dropped server-side and hand the caller a filter that silently does nothing
— the widening failure this codebase has been bitten by before. The test
asserts the request carries no query string at all.

**``create_puzzle(colony=...)`` sends a NAME, not a UUID.** Its neighbour
``create_post`` resolves a colony to ``colony_id`` before sending; this
endpoint wants the name and refuses a UUID, so resolving locally would spend
a request to produce the wrong value. The test asserts the body key is
``colony`` and that no colony-resolution request is made.

**A wrong answer is a 200.** ``solve_puzzle`` returns ``is_correct: False``
rather than raising, so a caller must branch on the field. The mock's canned
default carries the field for the same reason.

**``content`` is absent until you start.** It is what makes
``solve_time_seconds`` meaningful, so ``Puzzle.to_dict`` omits it rather than
emitting ``null`` — a null would read as "started, and empty".
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client, _last_request, _mock_response
from test_async_client import _json_response, _make_client

from colony_sdk.client import (
    _require_difficulty,
    _require_puzzle_slug,
    _require_puzzle_type,
)
from colony_sdk.models import Puzzle
from colony_sdk.testing import MockColonyClient

PUZZLE_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

#: Titles and slugs that look right and are not.
BAD_SLUGS = [
    "River Crossing",  # the title, pasted
    "RiverCrossing",  # capitals
    "river crossing",  # a space
    "river_crossing",  # an underscore
    "-river-crossing",  # leading hyphen
    "river-crossing-",  # trailing hyphen
    "river--crossing",  # doubled hyphen
    "",  # empty
]

FULL_PAYLOAD = {
    "id": PUZZLE_ID,
    "slug": "river-crossing",
    "title": "River Crossing",
    "description": "A classic, with one twist.",
    "puzzle_type": "logic",
    "difficulty": 2,
    "is_active": True,
    "author": {"username": "arch", "display_name": "Arch"},
    "colony_name": "games",
    "attempt_status": "in_progress",
    "solver_count": 7,
    "best_time": 31.5,
    "created_at": "2026-09-18T10:00:00Z",
    "leaderboard": [{"username": "a", "solve_time_seconds": 9.0}],
}


def _create_kwargs(**over: object) -> dict:
    base = {
        "slug": "river-crossing",
        "title": "River Crossing",
        "description": "A classic.",
        "puzzle_type": "logic",
        "content": "A farmer must ferry a wolf, a goat and a cabbage.",
        "answer": "take the goat first",
    }
    base.update(over)
    return base


class TestModel:
    def test_from_dict_flattens_author_and_keeps_every_field(self) -> None:
        p = Puzzle.from_dict(FULL_PAYLOAD)

        assert p.id == PUZZLE_ID
        assert p.slug == "river-crossing"
        assert p.author_username == "arch"
        assert p.author_display_name == "Arch"
        assert p.colony_name == "games"
        assert p.attempt_status == "in_progress"
        assert p.solver_count == 7
        assert p.best_time == 31.5
        assert p.difficulty == 2
        assert p.leaderboard == [{"username": "a", "solve_time_seconds": 9.0}]
        # Absent from every read until you start it.
        assert p.content is None

    def test_from_dict_survives_a_seeded_sitewide_puzzle(self) -> None:
        """``author`` is null for a platform-seeded puzzle and
        ``colony_name`` is null for a site-wide one. Both are normal."""
        p = Puzzle.from_dict({"id": PUZZLE_ID, "slug": "s", "title": "T", "author": None, "colony_name": None})

        assert p.author_username == ""
        assert p.colony_name == ""
        assert p.attempt_status is None
        assert p.leaderboard == []

    def test_from_dict_accepts_the_start_responses_puzzle_id_spelling(self) -> None:
        p = Puzzle.from_dict({"puzzle_id": PUZZLE_ID, "slug": "s", "title": "T"})
        assert p.id == PUZZLE_ID

    def test_to_dict_omits_content_when_absent(self) -> None:
        """A null would read as "started, and empty" — the absence is the
        meaningful state."""
        d = Puzzle.from_dict(FULL_PAYLOAD).to_dict()
        assert "content" not in d

    def test_to_dict_carries_content_once_started(self) -> None:
        d = Puzzle.from_dict({**FULL_PAYLOAD, "content": "the puzzle"}).to_dict()
        assert d["content"] == "the puzzle"

    def test_to_dict_round_trips(self) -> None:
        p = Puzzle.from_dict(FULL_PAYLOAD)
        assert Puzzle.from_dict(p.to_dict()) == p


class TestValidators:
    @pytest.mark.parametrize("bad", BAD_SLUGS)
    def test_bad_slugs_are_refused(self, bad: str) -> None:
        with pytest.raises(ValueError, match="valid puzzle slug"):
            _require_puzzle_slug(bad)

    def test_a_non_string_slug_names_its_type(self) -> None:
        with pytest.raises(ValueError, match="must be a string"):
            _require_puzzle_slug(7)  # type: ignore[arg-type]

    def test_a_good_slug_is_stripped(self) -> None:
        assert _require_puzzle_slug("  river-crossing  ") == "river-crossing"

    @pytest.mark.parametrize("kind", ["logic", "cipher", "sequence", "code", "math", "wordplay"])
    def test_every_server_type_is_accepted(self, kind: str) -> None:
        assert _require_puzzle_type(kind) == kind

    def test_an_unknown_type_lists_the_valid_ones(self) -> None:
        with pytest.raises(ValueError, match="wordplay"):
            _require_puzzle_type("riddle")

    @pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
    def test_the_whole_difficulty_range_is_accepted(self, n: int) -> None:
        assert _require_difficulty(n) == n

    @pytest.mark.parametrize("bad", [0, 6, -1, 9])
    def test_difficulty_outside_one_to_five_is_refused(self, bad: int) -> None:
        with pytest.raises(ValueError, match="1 to 5"):
            _require_difficulty(bad)

    def test_a_bool_is_not_a_difficulty(self) -> None:
        """``True`` is an ``int`` in Python and would otherwise sail through
        as difficulty 1."""
        with pytest.raises(ValueError, match="1 to 5"):
            _require_difficulty(True)

    def test_a_non_int_difficulty_is_refused(self) -> None:
        with pytest.raises(ValueError, match="1 to 5"):
            _require_difficulty("3")  # type: ignore[arg-type]


class TestSync:
    @patch("colony_sdk.client.urlopen")
    def test_listing_sends_no_query_string_at_all(self, mock_urlopen: MagicMock) -> None:
        """The endpoint accepts no parameters. Anything we invented here
        would be dropped server-side and read as a working filter."""
        mock_urlopen.return_value = _mock_response(json.dumps({"items": [], "total": 0, "has_more": False}))
        client = _authed_client()

        client.get_puzzles()

        url = _last_request(mock_urlopen).full_url
        assert url.endswith("/puzzles"), url
        assert "?" not in url, url

    @patch("colony_sdk.client.urlopen")
    def test_get_addresses_the_puzzle_by_id(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(json.dumps(FULL_PAYLOAD))
        client = _authed_client()

        client.get_puzzle(PUZZLE_ID)

        assert _last_request(mock_urlopen).full_url.endswith(f"/puzzles/{PUZZLE_ID}")

    @patch("colony_sdk.client.urlopen")
    def test_a_truncated_id_never_reaches_the_server(self, mock_urlopen: MagicMock) -> None:
        """``_require_uuid`` is deliberately narrow: it rejects an id that is
        hex-and-hyphens but INCOMPLETE — the ``print(p["id"][:8])`` pasted
        back out of a log — and passes an arbitrary placeholder like ``"p1"``
        straight through, so mocked-transport suites keep working. These three
        methods inherit exactly that, no more.
        """
        client = _authed_client()
        for call in (client.get_puzzle, client.start_puzzle):
            with pytest.raises(ValueError, match="truncated UUID"):
                call(PUZZLE_ID[:8])
        with pytest.raises(ValueError, match="truncated UUID"):
            client.solve_puzzle(PUZZLE_ID[:13], "x")
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_create_sends_every_field_and_omits_an_absent_colony(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(json.dumps(FULL_PAYLOAD))
        client = _authed_client()

        client.create_puzzle(**_create_kwargs())  # type: ignore[arg-type]

        req = _last_request(mock_urlopen)
        body = json.loads(req.data)
        assert req.get_method() == "POST"
        assert req.full_url.endswith("/puzzles")
        assert body["slug"] == "river-crossing"
        assert body["puzzle_type"] == "logic"
        assert body["answer"] == "take the goat first"
        # Defaulted, not omitted: the server defaults it too, but sending it
        # keeps the client's stated default honest.
        assert body["difficulty"] == 3
        assert "colony" not in body

    @patch("colony_sdk.client.urlopen")
    def test_create_sends_the_colony_NAME_and_resolves_nothing(self, mock_urlopen: MagicMock) -> None:
        """``create_post`` resolves a colony to a UUID first. This endpoint
        takes the name and refuses a UUID, so resolving would cost a request
        AND send the wrong value. One request, name intact."""
        mock_urlopen.return_value = _mock_response(json.dumps(FULL_PAYLOAD))
        client = _authed_client()

        client.create_puzzle(**_create_kwargs(colony="games"))  # type: ignore[arg-type]

        assert json.loads(_last_request(mock_urlopen).data)["colony"] == "games"
        assert mock_urlopen.call_count == 1

    @patch("colony_sdk.client.urlopen")
    def test_create_refuses_bad_input_before_the_request(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()

        with pytest.raises(ValueError, match="valid puzzle slug"):
            client.create_puzzle(**_create_kwargs(slug="River Crossing"))  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="puzzle_type"):
            client.create_puzzle(**_create_kwargs(puzzle_type="riddle"))  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="1 to 5"):
            client.create_puzzle(**_create_kwargs(difficulty=9))  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="title"):
            client.create_puzzle(**_create_kwargs(title="   "))  # type: ignore[arg-type]

        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_start_posts_to_the_start_path(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            json.dumps({"puzzle_id": PUZZLE_ID, "content": "c", "started_at": "t"})
        )
        client = _authed_client()

        out = client.start_puzzle(PUZZLE_ID)

        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url.endswith(f"/puzzles/{PUZZLE_ID}/start")
        # The one read that carries the puzzle text.
        assert out["content"] == "c"

    @patch("colony_sdk.client.urlopen")
    def test_solve_sends_the_answer_and_reports_a_wrong_one_as_a_200(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            json.dumps({"is_correct": False, "solve_time_seconds": 4.0, "leaderboard_rank": None})
        )
        client = _authed_client()

        out = client.solve_puzzle(PUZZLE_ID, "  wrong  ")

        req = _last_request(mock_urlopen)
        assert req.full_url.endswith(f"/puzzles/{PUZZLE_ID}/solve")
        # Sent VERBATIM, whitespace included. ``_require_nonempty`` refuses a
        # whitespace-only value rather than stripping one, on purpose — and
        # the server compares the answer case-insensitively after stripping,
        # so there is nothing for the client to normalise here.
        assert json.loads(req.data) == {"answer": "  wrong  "}
        # Not an exception — the caller branches on the field.
        assert out["is_correct"] is False
        assert out["leaderboard_rank"] is None

    @patch("colony_sdk.client.urlopen")
    def test_a_blank_answer_is_refused(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        with pytest.raises(ValueError, match="answer"):
            client.solve_puzzle(PUZZLE_ID, "   ")
        mock_urlopen.assert_not_called()

    @patch("colony_sdk.client.urlopen")
    def test_typed_mode_wraps_the_detail_and_create_reads(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(json.dumps(FULL_PAYLOAD))
        client = _authed_client()
        client.typed = True

        got = client.get_puzzle(PUZZLE_ID)
        made = client.create_puzzle(**_create_kwargs())  # type: ignore[arg-type]

        assert isinstance(got, Puzzle)
        assert isinstance(made, Puzzle)
        assert got.slug == "river-crossing"

    @patch("colony_sdk.client.urlopen")
    def test_typed_mode_leaves_the_list_envelope_alone(self, mock_urlopen: MagicMock) -> None:
        """The list returns the envelope raw, as ``get_posts`` does — the
        items stay dicts so ``total``/``has_more`` survive."""
        mock_urlopen.return_value = _mock_response(json.dumps({"items": [FULL_PAYLOAD], "total": 1, "has_more": False}))
        client = _authed_client()
        client.typed = True

        out = client.get_puzzles()

        assert out["total"] == 1
        assert isinstance(out["items"][0], dict)


class TestAsyncParity:
    @pytest.mark.asyncio
    async def test_every_method_hits_the_same_path_and_verb(self) -> None:
        seen: list[tuple[str, str, bytes]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path, request.content))
            return _json_response(FULL_PAYLOAD)

        client = _make_client(handler)
        await client.get_puzzles()
        await client.get_puzzle(PUZZLE_ID)
        await client.create_puzzle(**_create_kwargs(colony="games"))
        await client.start_puzzle(PUZZLE_ID)
        await client.solve_puzzle(PUZZLE_ID, "take the goat first")
        await client.aclose()

        assert [m for m, _, _ in seen] == ["GET", "GET", "POST", "POST", "POST"]
        assert [p for _, p, _ in seen] == [
            "/api/v1/puzzles",
            f"/api/v1/puzzles/{PUZZLE_ID}",
            "/api/v1/puzzles",
            f"/api/v1/puzzles/{PUZZLE_ID}/start",
            f"/api/v1/puzzles/{PUZZLE_ID}/solve",
        ]
        assert json.loads(seen[2][2])["colony"] == "games"
        assert json.loads(seen[4][2]) == {"answer": "take the goat first"}

    @pytest.mark.asyncio
    async def test_async_applies_the_same_guards(self) -> None:
        client = _make_client(lambda r: _json_response({}))

        with pytest.raises(ValueError, match="valid puzzle slug"):
            await client.create_puzzle(**_create_kwargs(slug="River Crossing"))
        with pytest.raises(ValueError, match="puzzle_type"):
            await client.create_puzzle(**_create_kwargs(puzzle_type="riddle"))
        with pytest.raises(ValueError, match="1 to 5"):
            await client.create_puzzle(**_create_kwargs(difficulty=0))
        with pytest.raises(ValueError, match="truncated UUID"):
            await client.get_puzzle(PUZZLE_ID[:8])
        await client.aclose()

    @pytest.mark.asyncio
    async def test_async_typed_mode_wraps(self) -> None:
        client = _make_client(lambda r: _json_response(FULL_PAYLOAD))
        client.typed = True

        got = await client.get_puzzle(PUZZLE_ID)
        await client.aclose()

        assert isinstance(got, Puzzle)


class TestMock:
    def test_every_method_is_present_and_records_its_call(self) -> None:
        mock = MockColonyClient()

        mock.get_puzzles()
        mock.get_puzzle(PUZZLE_ID)
        mock.create_puzzle(**_create_kwargs(colony="games"))
        mock.start_puzzle(PUZZLE_ID)
        mock.solve_puzzle(PUZZLE_ID, "an answer")

        assert [name for name, _ in mock.calls] == [
            "get_puzzles",
            "get_puzzle",
            "create_puzzle",
            "start_puzzle",
            "solve_puzzle",
        ]
        assert mock.calls[2][1]["colony"] == "games"
        assert mock.calls[4][1]["answer"] == "an answer"

    def test_the_defaults_carry_the_fields_a_caller_reads(self) -> None:
        """``content`` and ``is_correct`` are the two a caller indexes
        immediately; a bare ``{}`` would raise from the double rather than
        from the code under test."""
        mock = MockColonyClient()

        assert mock.start_puzzle(PUZZLE_ID)["content"]
        assert mock.solve_puzzle(PUZZLE_ID, "a")["is_correct"] is True
        assert mock.get_puzzles() == {"items": [], "total": 0, "has_more": False}

    def test_the_mock_refuses_what_the_real_client_refuses(self) -> None:
        """A double that accepts a bad slug, type or difficulty lets a test
        pass against values the real client rejects."""
        mock = MockColonyClient()

        with pytest.raises(ValueError, match="valid puzzle slug"):
            mock.create_puzzle(**_create_kwargs(slug="River Crossing"))
        with pytest.raises(ValueError, match="puzzle_type"):
            mock.create_puzzle(**_create_kwargs(puzzle_type="riddle"))
        with pytest.raises(ValueError, match="1 to 5"):
            mock.create_puzzle(**_create_kwargs(difficulty=6))

    def test_responses_can_be_overridden(self) -> None:
        mock = MockColonyClient(responses={"get_puzzles": {"items": [FULL_PAYLOAD], "total": 1}})
        assert mock.get_puzzles()["total"] == 1
