"""A query parameter the API ignored is reported (platform, 2026-11-02).

The platform names, in ``X-Colony-Ignored-Params``, each query parameter a
request sent that its route does not accept, which therefore did nothing:
an ignored filter answers with more than was asked for, an ignored page
parameter with the first page again. The client turns the header into a
:class:`ColonyIgnoredParamsWarning`, once per route and name.
"""

from __future__ import annotations

import json
import warnings
from unittest.mock import MagicMock, patch

import httpx
from test_api_methods import _authed_client
from test_async_client import _make_client

import colony_sdk
from colony_sdk import ColonyIgnoredParamsWarning

HEADER = "X-Colony-Ignored-Params"


def _mock_response(headers: dict[str, str]) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps({"items": []}).encode()
    resp.status = 200
    resp.getheaders.return_value = list(headers.items())
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_exported_and_not_a_deprecation() -> None:
    assert colony_sdk.ColonyIgnoredParamsWarning is ColonyIgnoredParamsWarning
    assert issubclass(ColonyIgnoredParamsWarning, UserWarning)
    assert not issubclass(ColonyIgnoredParamsWarning, DeprecationWarning)


@patch("colony_sdk.client.urlopen")
def test_each_ignored_name_warns_once_per_route(mock_urlopen: MagicMock) -> None:
    client = _authed_client()
    mock_urlopen.return_value = _mock_response({HEADER: "skip, take"})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client._raw_request("GET", "/posts?skip=5&take=5")
        client._raw_request("GET", "/posts?skip=10&take=5")
    messages = [str(w.message) for w in caught if w.category is ColonyIgnoredParamsWarning]
    assert len(messages) == 2
    assert "'skip'" in messages[0] and "GET /posts" in messages[0]
    assert "'take'" in messages[1]


@patch("colony_sdk.client.urlopen")
def test_the_elided_marker_is_reported_as_such(mock_urlopen: MagicMock) -> None:
    client = _authed_client()
    mock_urlopen.return_value = _mock_response({HEADER: "ok, ..."})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client._raw_request("GET", "/posts?ok=1")
    messages = [str(w.message) for w in caught if w.category is ColonyIgnoredParamsWarning]
    assert any("would not echo" in m for m in messages)


@patch("colony_sdk.client.urlopen")
def test_no_header_no_warning(mock_urlopen: MagicMock) -> None:
    client = _authed_client()
    mock_urlopen.return_value = _mock_response({})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client._raw_request("GET", "/posts?limit=5")
    assert not [w for w in caught if w.category is ColonyIgnoredParamsWarning]


async def test_the_async_client_warns_too() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"items": []}', headers={HEADER: "submolt"})

    client = _make_client(handler)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await client._raw_request("GET", "/posts?submolt=x")
    assert [w for w in caught if w.category is ColonyIgnoredParamsWarning]
