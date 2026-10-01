"""The page walkers stop when the server says so (``has_more``), not on a short page.

``iter_posts``, ``iter_comments``, ``iter_echoes`` and ``iter_wiki_pages``
ended their walk on the first page shorter than their page size, and
``iter_comments`` compared against a literal ``20``. The server answers every
one of these lists with ``has_more``, documented as the field to branch on.
The length check was right only while every page but the last was full and the
walker's page size matched the server's; an agent on The Colony (post
fc1416a0, 2026-09-30) pointed out that the walk's end was never a census.

``has_more`` decides now; a response without it (an older server, a mocked
transport) keeps the length check.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from test_api_methods import _authed_client, _mock_response
from test_async_client import _make_client

POST_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"

# (method, positional args, keyword args, full page size)
WALKERS = [
    ("iter_posts", (), {"page_size": 3}, 3),
    ("iter_comments", (POST_ID,), {}, 20),
    ("iter_echoes", (), {"page_size": 3}, 3),
    ("iter_wiki_pages", (), {"page_size": 3}, 3),
]


def _page(n: int, start: int = 0, has_more: bool | None = None) -> dict:
    body: dict = {"items": [{"id": f"x{start + i}"} for i in range(n)]}
    if has_more is not None:
        body["has_more"] = has_more
    return body


def _sync_walk(method, args, kwargs, pages):
    with patch("colony_sdk.client.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [_mock_response(p) for p in pages]
        items = list(getattr(_authed_client(), method)(*args, **kwargs))
        return items, mock_urlopen.call_count


async def _async_walk(method, args, kwargs, pages):
    served: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = pages[len(served)]
        served.append(body)
        return httpx.Response(200, json=body)

    client = _make_client(handler)
    items = [item async for item in getattr(client, method)(*args, **kwargs)]
    return items, len(served)


@pytest.mark.parametrize(("method", "args", "kwargs", "size"), WALKERS)
def test_a_short_page_with_more_to_come_is_not_the_end(method, args, kwargs, size) -> None:
    pages = [_page(2, 0, has_more=True), _page(2, 2, has_more=False)]
    items, requests = _sync_walk(method, args, kwargs, pages)
    assert (len(items), requests) == (4, 2)


@pytest.mark.parametrize(("method", "args", "kwargs", "size"), WALKERS)
def test_a_full_last_page_is_the_end(method, args, kwargs, size) -> None:
    items, requests = _sync_walk(method, args, kwargs, [_page(size, has_more=False)])
    assert (len(items), requests) == (size, 1), "no extra request past has_more=false"


@pytest.mark.parametrize(("method", "args", "kwargs", "size"), WALKERS)
def test_without_has_more_the_length_still_decides(method, args, kwargs, size) -> None:
    items, requests = _sync_walk(method, args, kwargs, [_page(size), _page(1, size)])
    assert (len(items), requests) == (size + 1, 2)


@pytest.mark.parametrize(("method", "args", "kwargs", "size"), WALKERS)
async def test_the_async_client_walks_the_same_way(method, args, kwargs, size) -> None:
    items, requests = await _async_walk(method, args, kwargs, [_page(2, 0, has_more=True), _page(2, 2, has_more=False)])
    assert (len(items), requests) == (4, 2)
    items, requests = await _async_walk(method, args, kwargs, [_page(size, has_more=False)])
    assert (len(items), requests) == (size, 1)


def test_a_walker_still_ends_on_an_empty_page_whatever_has_more_says() -> None:
    """A server that says has_more but sends nothing must not loop forever."""
    items, requests = _sync_walk("iter_comments", (POST_ID,), {}, [_page(0, has_more=True)])
    assert (items, requests) == ([], 1)
