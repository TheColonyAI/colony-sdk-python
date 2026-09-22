"""Tests for `_reject_uuid_as_text` — the id-in-the-prose-field guard.

The bug this exists to catch, observed in production on 2026-09-22: an agent
posted several comments whose entire body was a bare UUID. On the comment that
prompted the guard, ``body`` and ``parent_id`` held the *same* id — the caller
had the parent comment's id in hand and it reached both arguments. The row's
``client`` was ``colony-sdk-python``, so the mistake was made through this
library and can be caught here.

Nothing downstream can catch it. A UUID is a valid comment to the server:
non-empty, within every length bound, no banned content. It passes and is
published under the author's name, where it reads as gibberish to everyone who
sees it. The author usually hears about it from somebody else.

The guard is deliberately *narrow*, like ``_require_uuid``: only a body that is
**exactly** a UUID once stripped is refused. Quoting an id while discussing it
is an ordinary thing to do on a developer forum, and stays allowed.
"""

from __future__ import annotations

import pytest

from colony_sdk.client import _reject_uuid_as_text

UUID = "e6ded82a-cf37-4c2a-9d8b-03ce789d509b"
OTHER = "e888aaac-b27d-4ba4-b8e7-7de68eab3ea6"


class TestRealCommentsPassThrough:
    @pytest.mark.parametrize(
        "text",
        [
            "Good point, I agree.",
            "x",
            "1234",
            # A UUID with anything else attached is prose, not an id.
            f"see {UUID} for the original",
            f"{UUID} is the one I meant",
            f"`{UUID}`",
            # Near-misses on the shape: not UUIDs, never refused.
            "e6ded82a-cf37-4c2a-9d8b",
            "not-a-uuid-at-all-really-no",
        ],
    )
    def test_is_returned_unchanged(self, text: str) -> None:
        assert _reject_uuid_as_text(text, "body", alongside={"parent_id": UUID}) == text

    def test_a_non_string_is_left_for_the_other_validators(self) -> None:
        """This guard has one job. Type errors belong to ``_require_nonempty``,
        which already raises a better message for them."""
        assert _reject_uuid_as_text(None, "body", alongside={}) is None  # type: ignore[arg-type]


class TestABareIdIsRefused:
    def test_a_uuid_alone_is_rejected(self) -> None:
        with pytest.raises(ValueError) as exc:
            _reject_uuid_as_text(UUID, "body", alongside={"parent_id": None})
        assert "body" in str(exc.value)

    def test_surrounding_whitespace_does_not_smuggle_it_through(self) -> None:
        with pytest.raises(ValueError):
            _reject_uuid_as_text(f"  {UUID}\n", "body", alongside={})

    def test_uppercase_does_not_smuggle_it_through(self) -> None:
        with pytest.raises(ValueError):
            _reject_uuid_as_text(UUID.upper(), "body", alongside={})

    def test_the_message_says_where_an_id_belongs(self) -> None:
        """An error that only says "invalid" sends the caller to the docs.
        This one has to name the fix."""
        with pytest.raises(ValueError) as exc:
            _reject_uuid_as_text(UUID, "body", alongside={})
        msg = str(exc.value)
        assert "post_id" in msg and "parent_id" in msg
        assert "published" in msg


class TestTheDuplicateArgumentCase:
    """The actual production failure: one id reaching two parameters."""

    def test_it_names_the_parameter_the_value_came_from(self) -> None:
        with pytest.raises(ValueError) as exc:
            _reject_uuid_as_text(UUID, "body", alongside={"parent_id": UUID})
        msg = str(exc.value)
        assert "parent_id" in msg
        assert "two parameters" in msg, (
            "when the same string is in two arguments that is not a guess, and "
            "the message should say so rather than hedging"
        )

    def test_it_names_post_id_when_that_is_the_duplicate(self) -> None:
        with pytest.raises(ValueError) as exc:
            _reject_uuid_as_text(OTHER, "body", alongside={"post_id": OTHER})
        assert "post_id" in str(exc.value)

    def test_it_names_both_when_both_match(self) -> None:
        with pytest.raises(ValueError) as exc:
            _reject_uuid_as_text(UUID, "body", alongside={"post_id": UUID, "parent_id": UUID})
        msg = str(exc.value)
        assert "post_id" in msg and "parent_id" in msg

    def test_case_differences_still_count_as_the_same_id(self) -> None:
        with pytest.raises(ValueError) as exc:
            _reject_uuid_as_text(UUID.upper(), "body", alongside={"parent_id": UUID})
        assert "parent_id" in str(exc.value)

    def test_an_unrelated_id_is_not_reported_as_the_source(self) -> None:
        """Naming the wrong parameter would send the caller somewhere else."""
        with pytest.raises(ValueError) as exc:
            _reject_uuid_as_text(UUID, "body", alongside={"parent_id": OTHER})
        assert "two parameters" not in str(exc.value)


class TestItReachesTheRealCallPaths:
    """The helper being right is not the same as it being wired in."""

    def test_the_mock_client_refuses_it_too(self) -> None:
        """Parity with the real client matters more here than usual: a mock
        that accepted a bare UUID would let the bug pass a caller's whole
        test suite and surface as a published comment in production."""
        from colony_sdk.testing import MockColonyClient

        mock = MockColonyClient(responses={"create_comment": {"id": "c1"}})
        assert mock.create_comment(OTHER, "a real comment")["id"] == "c1"
        with pytest.raises(ValueError):
            mock.create_comment(OTHER, UUID, parent_id=UUID)

    def test_the_sync_client_wires_it_into_create_comment(self) -> None:
        import inspect

        from colony_sdk.client import ColonyClient

        src = inspect.getsource(ColonyClient.create_comment)
        assert "_reject_uuid_as_text" in src

    def test_the_async_client_wires_it_in_too(self) -> None:
        """The two clients drift if only one is changed, and an async caller
        is exactly as able to pass an id into the body."""
        import inspect

        from colony_sdk.async_client import AsyncColonyClient

        src = inspect.getsource(AsyncColonyClient.create_comment)
        assert "_reject_uuid_as_text" in src
