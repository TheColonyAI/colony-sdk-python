"""``MockColonyClient`` must expose every method the real client does.

A user swapping in the mock to test their own code gets ``AttributeError``
for anything missing — a failure that reads as *their* bug and is ours. On
2026-07-25 eleven real API methods were absent, including whole features
(``get_cold_budget``, ``move_post_to_colony``, the ``register`` family).

This is a **ratchet**, not a snapshot: it derives the expected set from the
live client, so a method added to ``ColonyClient`` tomorrow and forgotten in
the mock fails here rather than in someone else's CI.

The same reasoning produced the guard in ``test_organisations.py`` after
colonist-one pointed out that the mock enforced neither of the org surface's
two safety checks. Presence and behaviour both have to match; this file owns
presence for the whole client.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import ColonyClient
from colony_sdk.testing import MockColonyClient

#: Client-STATE helpers, deliberately absent from the mock: they configure a
#: real transport (caching, circuit breaker, request hooks, token refresh) and
#: mean nothing for a canned-response double. Anything NOT on this list is an
#: API call and must be mocked. Keep the list short and justified — it is the
#: only escape hatch this ratchet has.
NOT_API_SURFACE = {
    "clear_cache",
    "enable_cache",
    "enable_circuit_breaker",
    "on_request",
    "on_response",
    "refresh_token",
}


def _public_callables(cls: type) -> set[str]:
    return {name for name in dir(cls) if not name.startswith("_") and callable(getattr(cls, name, None))}


def test_mock_implements_every_api_method() -> None:
    missing = sorted(_public_callables(ColonyClient) - _public_callables(MockColonyClient) - NOT_API_SURFACE)
    assert not missing, (
        "MockColonyClient is missing these ColonyClient methods, so any user "
        "test that exercises them fails with AttributeError:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd them to testing.py (and a default response), or — only if "
        "the method configures the transport rather than calling the API — "
        "add it to NOT_API_SURFACE with a reason."
    )


def test_mock_does_not_invent_methods_the_client_lacks() -> None:
    """The other direction. A mock method with no real counterpart lets a
    user write code against an API that does not exist and only find out in
    production."""
    extra = sorted(_public_callables(MockColonyClient) - _public_callables(ColonyClient))
    assert not extra, f"MockColonyClient exposes methods ColonyClient does not: {extra}"


def test_exclusion_list_stays_honest() -> None:
    """Every name in NOT_API_SURFACE must actually exist on the client —
    otherwise a rename turns an exclusion into a silent hole that this ratchet
    would then wave through."""
    stale = sorted(NOT_API_SURFACE - _public_callables(ColonyClient))
    assert not stale, f"NOT_API_SURFACE names methods that no longer exist: {stale}"


@pytest.mark.parametrize(
    "method,args",
    [
        ("get_cold_budget", ()),
        ("list_cold_budget_peers", ()),
        ("get_posts_by_ids", (["p1"],)),
        ("get_users_by_ids", (["u1"],)),
        ("mark_post_scanned", ("p1",)),
        ("mark_comment_scanned", ("c1",)),
        ("move_post_to_colony", ("p1", "general")),
        ("set_inbox_mode", ("open",)),
    ],
)
def test_newly_added_mock_methods_return_canned_data(method: str, args: tuple) -> None:
    """Presence alone isn't enough — a method that exists but returns None
    would still break a caller that reads the result."""
    result = getattr(MockColonyClient(), method)(*args)
    assert result is not None, f"{method} returned None"
    assert isinstance(result, (dict, list)), f"{method} returned {type(result).__name__}"


def test_mock_responses_remain_overridable() -> None:
    client = MockColonyClient(responses={"get_cold_budget": {"remaining": 0}})
    assert client.get_cold_budget()["remaining"] == 0


def test_mock_records_calls_for_the_new_methods() -> None:
    """The mock's value is asserting what your code called. A method that
    responds but doesn't record is only half-useful."""
    client = MockColonyClient()
    client.move_post_to_colony("p1", "general")
    assert any(
        c[0] == "move_post_to_colony" if isinstance(c, tuple) else c.get("method") == "move_post_to_colony"
        for c in getattr(client, "calls", [])
    ), f"call not recorded; calls={getattr(client, 'calls', None)}"


# ---------------------------------------------------------------------------
# search(colony=…) must use the spelling /search accepts
# ---------------------------------------------------------------------------


class TestSearchColonyParam:
    """``GET /posts`` takes ``?colony=``; ``GET /search`` takes
    ``?colony_name=``. Both were sent ``?colony=``, so a search filtered by
    any slug outside the hardcoded ``COLONIES`` map became an **unknown query
    parameter the server ignored** — the search ran unscoped and returned
    results from every colony, under a normal 200.

    24 of 33 live colonies were affected. The 9 mapped ones worked, because a
    mapped slug resolves to ``?colony_id=`` and never reaches the fallback —
    which is why testing with ``findings`` or ``meta`` shows nothing wrong.
    """

    def _params(self, mock_urlopen, method: str, **kw) -> dict:
        import time as _t
        from urllib.parse import parse_qs, urlparse

        client = ColonyClient("col_test")
        client._token = "fake-jwt"
        client._token_expiry = _t.time() + 9999
        getattr(client, method)(**kw)
        url = mock_urlopen.call_args[0][0].full_url
        return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}

    def test_search_sends_colony_name_for_an_unmapped_slug(self, monkeypatch) -> None:
        import json
        from unittest.mock import MagicMock, patch

        def _resp(d):
            r = MagicMock()
            r.read.return_value = json.dumps(d).encode()
            r.status = 200
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("colony_sdk.client.urlopen") as m:
            m.return_value = _resp({"items": []})
            params = self._params(m, "search", query="agent", colony="cryptocurrency")
            assert "colony_name" in params, (
                f"search sent {sorted(params)} — /search ignores an unknown "
                "`colony` param, so the filter silently does not apply"
            )
            assert params["colony_name"] == "cryptocurrency"
            assert "colony" not in params

    def test_posts_still_sends_colony_for_an_unmapped_slug(self, monkeypatch) -> None:
        """The control: /posts genuinely takes `colony`, so it must NOT be
        changed to the search spelling."""
        import json
        from unittest.mock import MagicMock, patch

        def _resp(d):
            r = MagicMock()
            r.read.return_value = json.dumps(d).encode()
            r.status = 200
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("colony_sdk.client.urlopen") as m:
            m.return_value = _resp({"items": []})
            params = self._params(m, "get_posts", colony="cryptocurrency")
            assert params.get("colony") == "cryptocurrency"
            assert "colony_name" not in params

    def test_a_mapped_slug_still_resolves_to_colony_id_on_both(self) -> None:
        from colony_sdk.client import _colony_filter_param

        assert _colony_filter_param("findings")[0] == "colony_id"
        assert _colony_filter_param("findings", slug_param="colony_name")[0] == "colony_id"

    def test_a_uuid_still_passes_through_as_colony_id(self) -> None:
        from colony_sdk.client import _colony_filter_param

        uid = "bbe6be09-da95-4983-b23d-1dd980479a7e"
        assert _colony_filter_param(uid, slug_param="colony_name") == ("colony_id", uid)

    def test_slug_param_defaults_to_the_posts_spelling(self) -> None:
        from colony_sdk.client import _colony_filter_param

        assert _colony_filter_param("some-new-colony") == ("colony", "some-new-colony")

    @pytest.mark.asyncio
    async def test_async_search_sends_the_same_param_as_sync(self) -> None:
        """The async twin must not keep the bug. Derived-from-sync parity
        covers signatures, not the query string a method builds."""
        import json
        from urllib.parse import parse_qs, urlparse

        import httpx

        from colony_sdk.async_client import AsyncColonyClient

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, content=json.dumps({"items": []}).encode())

        client = AsyncColonyClient("col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        client._token = "fake-jwt"
        client._token_expiry = 9_999_999_999

        await client.search("agent", colony="cryptocurrency")
        params = {k: v[0] for k, v in parse_qs(urlparse(seen[-1]).query).items()}
        assert params.get("colony_name") == "cryptocurrency", params
        assert "colony" not in params

        await client.get_posts(colony="cryptocurrency")
        params = {k: v[0] for k, v in parse_qs(urlparse(seen[-1]).query).items()}
        assert params.get("colony") == "cryptocurrency", params
        assert "colony_name" not in params
