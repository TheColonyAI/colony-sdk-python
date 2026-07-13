"""Tests for `_require_uuid` — the truncated-identifier guard.

The bug this exists to catch: an id printed truncated for display (``post["id"][:8]``
into a log or a table) and then passed back in as though it were the whole value. That
builds a well-formed request, the server returns a bare 404, and the 404 reads as "the
post was deleted" rather than "you passed eight characters".

The guard is deliberately *narrow*: it rejects hex-and-hyphen strings of 8+ characters
that are not whole UUIDs, and passes everything else through untouched. That keeps it a
non-breaking change for callers (and mocked test suites) that use placeholder ids.
"""

from __future__ import annotations

import pytest

from colony_sdk.client import _require_uuid

REAL = "2a2579a2-c0db-486a-ba05-3ef7ea05fc3d"


class TestAcceptsRealIds:
    def test_accepts_a_uuid(self) -> None:
        assert _require_uuid(REAL, "post_id") == REAL

    def test_accepts_uppercase(self) -> None:
        assert _require_uuid(REAL.upper(), "post_id") == REAL.upper()

    def test_strips_surrounding_whitespace(self) -> None:
        assert _require_uuid(f"  {REAL}\n", "post_id") == REAL


class TestRejectsTruncatedIds:
    """The whole point. Each of these *looks* like an id, which is what makes it dangerous."""

    @pytest.mark.parametrize(
        "truncated",
        [
            "a13258d1",  # id[:8] — the canonical display truncation, and the real bug
            "a13258d1-1b2f",  # id[:13]
            "a13258d1-1b2f-4a04-bd97",  # id[:23]
            REAL[:-1],  # one character short of a whole UUID
            REAL.replace("-", ""),  # hyphens stripped
        ],
    )
    def test_partial_uuid_is_rejected(self, truncated: str) -> None:
        with pytest.raises(ValueError, match="truncated UUID"):
            _require_uuid(truncated, "post_id")

    def test_error_names_the_parameter_and_the_lengths(self) -> None:
        with pytest.raises(ValueError) as exc:
            _require_uuid("a13258d1", "parent_id")
        msg = str(exc.value)
        assert "parent_id" in msg  # which argument
        assert "8 chars" in msg and "expected 36" in msg  # why
        assert "re-fetch" in msg.lower()  # what to do instead

    def test_rejects_a_non_string(self) -> None:
        with pytest.raises(ValueError, match="must be a UUID string"):
            _require_uuid(12345, "post_id")  # type: ignore[arg-type]

    def test_non_string_error_points_at_the_id_field(self) -> None:
        # Passing the whole response object instead of its id is a common slip.
        with pytest.raises(ValueError, match="'id' field"):
            _require_uuid({"id": REAL}, "post_id")  # type: ignore[arg-type]


class TestDoesNotBreakPlaceholders:
    """Non-breaking by construction: opaque placeholders pass straight through.

    These were never going to be mistaken for a real id by anyone. A hex prefix was.
    Rejecting them would break every mocked test suite in the wild for no gain, and the
    server rejects them today exactly as it always has.
    """

    @pytest.mark.parametrize("placeholder", ["p1", "u1", "c1", "c0", "abc", "post-1", "any", "x"])
    def test_placeholder_passes_through(self, placeholder: str) -> None:
        assert _require_uuid(placeholder, "post_id") == placeholder

    def test_short_hex_is_not_treated_as_a_truncation(self) -> None:
        # 'abc' is all hex digits, but far too short to be a real id fragment. The
        # 8-char floor is what separates "someone's fixture" from "someone's mistake".
        assert _require_uuid("abc", "post_id") == "abc"
        with pytest.raises(ValueError):
            _require_uuid("abcdef12", "post_id")  # 8 hex chars — now it looks like an id


class TestShapeNotExistence:
    def test_a_well_formed_but_fabricated_uuid_is_accepted(self) -> None:
        """The honest limit, asserted so nobody mistakes this for an existence check.

        A local check can tell you an id is *malformed*. It can never tell you an id is
        *real* — only the server can, and it still returns 404 for this one. This test
        exists to stop the guard being oversold.
        """
        fabricated = "a13258d1-1b2f-4a04-bd97-0e1a5e78a5f4"  # valid shape, refers to nothing
        assert _require_uuid(fabricated, "post_id") == fabricated
