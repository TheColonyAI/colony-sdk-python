"""A query parameter the API ignored is reported (platform, 2026-11-02).

The platform names, in ``X-Colony-Ignored-Params``, each query parameter a
request sent that its route does not accept, which therefore did nothing:
an ignored filter answers with more than was asked for, an ignored page
parameter with the first page again. The client turns the header into a
:class:`ColonyIgnoredParamsWarning`, once per route and name.

Its body twin, ``X-Colony-Ignored-Fields`` (platform release 2026-11-08a),
names each top-level key of a request's JSON body that the route ignored, on a
response under 400: at most ten names, ``...`` for any beyond those or unfit
for a header. It is reported the same way, worded as a body field.
"""

from __future__ import annotations

import json
import warnings
from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client
from test_async_client import _make_client

import colony_sdk
from colony_sdk import ColonyIgnoredParamsWarning

HEADER = "X-Colony-Ignored-Params"
FIELDS_HEADER = "X-Colony-Ignored-Fields"
POST_ID = "9cb7ff7d-51a0-46c6-acd2-d731164c792a"


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


# ---------------------------------------------------------------------------
# X-Colony-Ignored-Fields: the JSON body's twin (platform release 2026-11-08a)
# ---------------------------------------------------------------------------


def _ignored(caught: list[warnings.WarningMessage]) -> list[str]:
    return [str(w.message) for w in caught if w.category is ColonyIgnoredParamsWarning]


@patch("colony_sdk.client.urlopen")
def test_each_ignored_body_field_warns_once_per_route(mock_urlopen: MagicMock) -> None:
    client = _authed_client()
    mock_urlopen.return_value = _mock_response({FIELDS_HEADER: "content, colour"})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client._raw_request("PUT", f"/posts/{POST_ID}", body={"title": "T", "content": "x", "colour": "red"})
        other = "0b5c7e3a-6f2d-4a1b-9c8d-7e6f5a4b3c2d"
        client._raw_request("PUT", f"/posts/{other}", body={"title": "T", "content": "x", "colour": "red"})
    messages = _ignored(caught)
    assert messages == [
        "The Colony API ignored body field 'content': PUT /posts/{id} does not accept it, so it changed nothing",
        "The Colony API ignored body field 'colour': PUT /posts/{id} does not accept it, so it changed nothing",
    ]


@patch("colony_sdk.client.urlopen")
def test_the_elided_body_marker_is_reported_as_such(mock_urlopen: MagicMock) -> None:
    client = _authed_client()
    mock_urlopen.return_value = _mock_response({FIELDS_HEADER: "colony, ..."})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client._raw_request("POST", "/posts", body={"title": "T", "colony": "x"})
    messages = _ignored(caught)
    assert len(messages) == 2
    assert "body field 'colony'" in messages[0]
    assert messages[1] == (
        "The Colony API ignored body fields it would not echo: "
        "POST /posts does not accept them, so they changed nothing"
    )


@patch("colony_sdk.client.urlopen")
def test_a_query_parameter_and_a_body_field_of_one_name_each_warn(mock_urlopen: MagicMock) -> None:
    """Both headers on one response, naming the same key: two different
    mistakes, so neither swallows the other's warning."""
    client = _authed_client()
    mock_urlopen.return_value = _mock_response({HEADER: "tags", FIELDS_HEADER: "tags"})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client._raw_request("PUT", f"/posts/{POST_ID}?tags=a", body={"tags": ["a"]})
        client._raw_request("PUT", f"/posts/{POST_ID}?tags=a", body={"tags": ["a"]})
    messages = _ignored(caught)
    assert len(messages) == 2
    assert "query parameter 'tags'" in messages[0]
    assert "body field 'tags'" in messages[1]


@patch("colony_sdk.client.urlopen")
def test_the_body_field_warning_points_at_the_callers_line(mock_urlopen: MagicMock) -> None:
    mock_urlopen.return_value = _mock_response({FIELDS_HEADER: "content"})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _authed_client().update_post(POST_ID, title="T")
    [warning] = [w for w in caught if w.category is ColonyIgnoredParamsWarning]
    assert warning.filename == __file__


@pytest.mark.parametrize("value", ["", " , ", None])
@patch("colony_sdk.client.urlopen")
def test_an_empty_fields_header_warns_nothing(mock_urlopen: MagicMock, value: str | None) -> None:
    client = _authed_client()
    mock_urlopen.return_value = _mock_response({} if value is None else {FIELDS_HEADER: value})
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        client._raw_request("POST", "/posts", body={"title": "T"})
    assert not _ignored(caught)


async def test_the_async_client_warns_about_body_fields_too() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"id": "p"}', headers={FIELDS_HEADER: "content, ..."})

    client = _make_client(handler)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await client._raw_request("PUT", f"/posts/{POST_ID}", body={"content": "x"})
        await client._raw_request("PUT", f"/posts/{POST_ID}", body={"content": "x"})
    messages = _ignored(caught)
    assert len(messages) == 2
    assert "body field 'content': PUT /posts/{id}" in messages[0]
    assert "body fields it would not echo" in messages[1]


async def test_the_async_body_field_warning_points_at_the_callers_line() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"id": "p"}', headers={FIELDS_HEADER: "content"})

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await _make_client(handler).update_post(POST_ID, body="B")
    [warning] = [w for w in caught if w.category is ColonyIgnoredParamsWarning]
    assert warning.filename == __file__
