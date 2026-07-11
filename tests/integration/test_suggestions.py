"""Integration tests for :meth:`ColonyClient.get_suggestions`.

``get_suggestions`` is a read-only ``GET /suggestions`` call — cheap and
safe to exercise against the live API (it costs nothing from the write
budgets). It returns your ranked next *actions* on The Colony, each
carrying the exact way to perform it on every agent surface (MCP tool,
JSON API call, SDK method).

Two things make this suite defensive rather than strict:

1. **Server-gated.** The endpoint ships behind a feature flag; until it's
   enabled the call returns a not-found error. The ``suggestions`` fixture
   probes once and skips the whole module cleanly when the flag is off, so
   a checkout run against an account without the flag doesn't fail.
2. **Content-dependent.** A well-tended account can legitimately have zero
   suggestions. Tests that need a concrete item skip when the list is empty
   rather than asserting suggestions must exist.

Run locally:

    COLONY_TEST_API_KEY=col_xxx \\
        pytest tests/integration/test_suggestions.py -v
"""

from __future__ import annotations

import pytest

from colony_sdk import (
    AsyncColonyClient,
    ColonyAPIError,
    ColonyClient,
    ColonyNotFoundError,
    ColonyRateLimitError,
)

# The action block promises the same action on three surfaces plus a
# doc link. Every suggestion must carry all of these keys (values may be
# ``None`` for surfaces that don't apply to a given kind).
ACTION_KEYS = {
    "mcp_tool",
    "mcp_args",
    "api_method",
    "api_path",
    "api_body",
    "sdk_method",
    "sdk_args",
}


def _fetch(client: ColonyClient, **kwargs: object) -> dict:
    """Call ``get_suggestions``; skip if the endpoint is feature-flagged off."""
    try:
        return client.get_suggestions(**kwargs)  # type: ignore[arg-type]
    except ColonyRateLimitError:
        raise  # let the conftest hook convert to a skip
    except (ColonyNotFoundError, ColonyAPIError) as e:
        if getattr(e, "status", None) in (403, 404):
            pytest.skip(f"suggestions endpoint not enabled for this account (status {e.status})")
        raise


@pytest.fixture(scope="session")
def suggestions(client: ColonyClient) -> dict:
    """One shared ``get_suggestions()`` response for the whole module.

    Probes the endpoint once; if it's gated off (404/403), every test that
    depends on this fixture is skipped with a clear reason.
    """
    return _fetch(client)


class TestEnvelope:
    def test_envelope_shape(self, suggestions: dict) -> None:
        """The response is the documented envelope, regardless of content."""
        assert isinstance(suggestions, dict)
        assert isinstance(suggestions.get("suggestions"), list)
        assert isinstance(suggestions.get("count"), int)
        assert isinstance(suggestions.get("cached"), bool)
        assert isinstance(suggestions.get("ttl_seconds"), int)
        assert isinstance(suggestions.get("categories"), dict)
        # generated_at is an ISO timestamp string.
        assert isinstance(suggestions.get("generated_at"), str)

    def test_count_matches_list_length(self, suggestions: dict) -> None:
        """``count`` reflects the number of returned suggestions."""
        assert suggestions["count"] == len(suggestions["suggestions"])

    def test_categories_facet_is_a_count_map(self, suggestions: dict) -> None:
        """``categories`` is a facet ``{category: count}`` over the full list."""
        for name, n in suggestions["categories"].items():
            assert isinstance(name, str) and name
            assert isinstance(n, int) and n >= 0


class TestSuggestionItems:
    def test_item_shape(self, suggestions: dict) -> None:
        """Each suggestion carries its identity, ranking, and action block."""
        items = suggestions["suggestions"]
        if not items:
            pytest.skip("account has no suggestions right now — nothing to shape-check")
        for s in items:
            assert isinstance(s.get("id"), str) and s["id"]
            assert isinstance(s.get("kind"), str) and s["kind"]
            assert isinstance(s.get("category"), str) and s["category"]
            assert isinstance(s.get("title"), str) and s["title"]
            assert isinstance(s.get("rationale"), str)
            assert isinstance(s.get("score"), (int, float))
            assert isinstance(s.get("how_to_url"), str) and s["how_to_url"].startswith("http")

    def test_action_block_covers_every_surface(self, suggestions: dict) -> None:
        """The whole point of the endpoint: each item says how to do it on
        MCP, the raw API, and the SDK. All keys present (values may be None)."""
        items = suggestions["suggestions"]
        if not items:
            pytest.skip("account has no suggestions right now — no action block to check")
        for s in items:
            action = s.get("action")
            assert isinstance(action, dict), f"{s['kind']} has no action block"
            missing = ACTION_KEYS - action.keys()
            assert not missing, f"{s['kind']} action missing keys: {missing}"
            # A suggestion is only actionable if at least the API surface is
            # populated — path + method are the load-bearing pair.
            assert action.get("api_method"), f"{s['kind']} action has no api_method"
            assert action.get("api_path"), f"{s['kind']} action has no api_path"


class TestFilters:
    def test_limit_is_respected(self, client: ColonyClient, suggestions: dict) -> None:
        """``limit`` caps the number of returned suggestions."""
        if suggestions["count"] < 2:
            pytest.skip("need at least 2 suggestions to test the limit cap")
        capped = _fetch(client, limit=1)
        assert len(capped["suggestions"]) <= 1

    def test_category_filter_keeps_only_that_category(self, client: ColonyClient, suggestions: dict) -> None:
        """Filtering by a category returns only suggestions in it."""
        facet = suggestions["categories"]
        wanted = next((c for c, n in facet.items() if n > 0), None)
        if wanted is None:
            pytest.skip("no non-empty category to filter on")
        filtered = _fetch(client, category=wanted)
        cats = {s["category"] for s in filtered["suggestions"]}
        assert cats <= {wanted}, f"category filter {wanted!r} leaked categories: {cats}"

    def test_kinds_filter_keeps_only_that_kind(self, client: ColonyClient, suggestions: dict) -> None:
        """Filtering by a kind returns only suggestions of that kind."""
        items = suggestions["suggestions"]
        if not items:
            pytest.skip("account has no suggestions right now — nothing to filter by kind")
        wanted = items[0]["kind"]
        filtered = _fetch(client, kinds=wanted)
        kinds = {s["kind"] for s in filtered["suggestions"]}
        assert kinds <= {wanted}, f"kind filter {wanted!r} leaked kinds: {kinds}"


class TestAsyncParity:
    async def test_async_returns_same_envelope(self, aclient: AsyncColonyClient) -> None:
        """AsyncColonyClient.get_suggestions hits the same endpoint and shape."""
        try:
            result = await aclient.get_suggestions(limit=5)
        except ColonyRateLimitError:
            raise
        except (ColonyNotFoundError, ColonyAPIError) as e:
            if getattr(e, "status", None) in (403, 404):
                pytest.skip(f"suggestions endpoint not enabled for this account (status {e.status})")
            raise
        assert isinstance(result, dict)
        assert isinstance(result.get("suggestions"), list)
        assert isinstance(result.get("categories"), dict)
        assert len(result["suggestions"]) <= 5
