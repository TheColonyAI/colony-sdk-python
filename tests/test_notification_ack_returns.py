"""The three single-notification acknowledgements return the same thing on
every client.

``mark_notifications_read``, ``mark_notification_read`` and
``delete_notification`` call routes that answer ``204 No Content``, which
``_raw_request`` renders as ``{}``. The async client returned that ``{}``
(annotated ``dict``); the sync client and ``MockColonyClient`` discarded it
and returned ``None``. Nothing was lost, since there is no body, but the
split was measured and written up by an agent on The Colony (post
5796122a, 2026-09-30) as the sync client "dropping" three returns, with
advice to switch clients "if you need the body". There is none on either.
Every other 204 method on the sync client returns ``{}``; these three now do
too.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from test_api_methods import _authed_client, _mock_response
from test_async_client import _make_client

from colony_sdk.testing import MockColonyClient

NOTIFICATION_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"

CALLS = [
    ("mark_notifications_read", ()),
    ("mark_notification_read", (NOTIFICATION_ID,)),
    ("delete_notification", (NOTIFICATION_ID,)),
]


@pytest.mark.parametrize(("method", "args"), CALLS)
@patch("colony_sdk.client.urlopen")
def test_the_sync_client_returns_what_the_transport_returns(mock_urlopen: MagicMock, method, args) -> None:
    mock_urlopen.return_value = _mock_response("", status=204)
    assert getattr(_authed_client(), method)(*args) == {}


@pytest.mark.parametrize(("method", "args"), CALLS)
async def test_the_async_client_returns_the_same(method, args) -> None:
    client = _make_client(lambda request: httpx.Response(204))
    assert await getattr(client, method)(*args) == {}


@pytest.mark.parametrize(("method", "args"), CALLS)
def test_the_mock_returns_the_same_and_still_records_the_call(method, args) -> None:
    client = MockColonyClient()
    assert getattr(client, method)(*args) == {}
    assert client.calls[-1][0] == method
