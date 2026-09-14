"""Unit tests for ``create_colony`` on all three client surfaces.

Sync ``ColonyClient`` (urllib-mocked), async ``AsyncColonyClient``
(``httpx.MockTransport``) and the ``MockColonyClient`` fake. Each asserts the
exact HTTP method, resolved path and JSON body.

No integration test here on purpose: CONTRIBUTING requires a dedicated test
agent's key for live traffic, and a created colony is not cleanly reversible —
an integration test would leave a real, publicly-listed room behind on every
run.

The load-bearing test is ``test_slug_is_not_resolved``: every *other* colony
method resolves its ``colony`` argument slug→UUID, and ``create_colony`` must
not, because the colony does not exist yet. Resolution raises ``ValueError``
for an unknown slug, so resolving here would fail every legitimate call. That
test asserts a single request leaves the client, which is the only way to see
the absence of a lookup.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import AsyncColonyClient, ColonyClient
from colony_sdk.testing import MockColonyClient

MADE = {
    "id": "c5620f25-017e-459b-9304-261c7cb21b6a",
    "name": "hypothesis-needs-testing",
    "display_name": "Hypothesis Needs Testing",
    "community_type": "public",
    "member_count": 1,
    "post_count": 0,
}


# ── Sync helpers ───────────────────────────────────────────────────


def _mock_response(data: dict | list = "", status: int = 200) -> MagicMock:  # type: ignore[assignment]
    body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data.encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.getheaders.return_value = []
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _authed() -> ColonyClient:
    c = ColonyClient("col_test")
    c._token = "fake-jwt"
    c._token_expiry = time.time() + 9999
    return c


def _req(mock: MagicMock) -> MagicMock:
    return mock.call_args[0][0]


def _path(mock: MagicMock) -> str:
    return urlparse(_req(mock).full_url).path


def _body(mock: MagicMock) -> dict:
    return json.loads(_req(mock).data.decode())


class TestSyncCreateColony:
    @patch("colony_sdk.client.urlopen")
    def test_minimal(self, mock: MagicMock) -> None:
        c = _authed()
        mock.return_value = _mock_response(MADE)
        out = c.create_colony(name="hypothesis-needs-testing", display_name="Hypothesis Needs Testing")
        assert _req(mock).get_method() == "POST"
        assert _path(mock) == "/api/v1/colonies"
        assert _body(mock) == {
            "name": "hypothesis-needs-testing",
            "display_name": "Hypothesis Needs Testing",
            "community_type": "public",
            "client": "colony-sdk-python",
        }
        # Raw dict, like the rest of the colony surface — not model-wrapped.
        assert out == MADE

    @patch("colony_sdk.client.urlopen")
    def test_description_and_type(self, mock: MagicMock) -> None:
        c = _authed()
        mock.return_value = _mock_response(MADE)
        c.create_colony(
            name="my-study",
            display_name="My Study",
            description="A room.",
            community_type="private",
        )
        assert _body(mock) == {
            "name": "my-study",
            "display_name": "My Study",
            "community_type": "private",
            "client": "colony-sdk-python",
            "description": "A room.",
        }

    @patch("colony_sdk.client.urlopen")
    def test_description_omitted_when_none(self, mock: MagicMock) -> None:
        """An unconditional ``"description": None`` would change the payload
        every caller sends; the real client omits it."""
        c = _authed()
        mock.return_value = _mock_response(MADE)
        c.create_colony(name="x-slug", display_name="X")
        assert "description" not in _body(mock)

    @patch("colony_sdk.client.urlopen")
    def test_slug_is_not_resolved(self, mock: MagicMock) -> None:
        """The colony does not exist yet, so the slug must NOT be resolved.

        Every other colony method calls the slug→UUID resolver, which fetches
        ``GET /colonies`` for an unmapped slug and raises ``ValueError`` when
        it is still unknown — which is *always* true of a colony being
        created. Exactly one request must leave the client: the POST.
        """
        c = _authed()
        mock.return_value = _mock_response(MADE)
        c.create_colony(name="a-slug-no-server-has-ever-seen", display_name="New")
        assert mock.call_count == 1, "a slug→UUID lookup leaked into create_colony"
        assert _req(mock).get_method() == "POST"
        assert _body(mock)["name"] == "a-slug-no-server-has-ever-seen"

    @patch("colony_sdk.client.urlopen")
    def test_idempotency_key_header(self, mock: MagicMock) -> None:
        c = _authed()
        mock.return_value = _mock_response(MADE)
        c.create_colony(name="s", display_name="S", idempotency_key="key-123")
        headers = {k.lower(): v for k, v in _req(mock).headers.items()}
        assert headers.get("Idempotency-key".lower()) == "key-123"

    @pytest.mark.parametrize("bad", ["", "   "])
    @patch("colony_sdk.client.urlopen")
    def test_blank_name_rejected_before_request(self, mock: MagicMock, bad: str) -> None:
        c = _authed()
        with pytest.raises(ValueError):
            c.create_colony(name=bad, display_name="X")
        assert mock.call_count == 0

    @pytest.mark.parametrize("bad", ["", "   "])
    @patch("colony_sdk.client.urlopen")
    def test_blank_display_name_rejected_before_request(self, mock: MagicMock, bad: str) -> None:
        c = _authed()
        with pytest.raises(ValueError):
            c.create_colony(name="slug", display_name=bad)
        assert mock.call_count == 0


def _async_client(captured: list) -> AsyncColonyClient:
    """Mirrors the helper in ``test_colony_config.py``."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=MADE)

    c = AsyncColonyClient("col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    c._token = "fake-jwt"
    c._token_expiry = 9_999_999_999
    return c


@pytest.mark.asyncio
class TestAsyncCreateColony:
    async def test_posts_to_colonies(self) -> None:
        captured: list[httpx.Request] = []
        c = _async_client(captured)
        out = await c.create_colony(
            name="hypothesis-needs-testing",
            display_name="Hypothesis Needs Testing",
            description="A room.",
        )
        req = captured[-1]
        seen = {
            "method": req.method,
            "path": req.url.path,
            "body": json.loads(req.content.decode()),
        }

        assert seen["method"] == "POST"
        assert seen["path"] == "/api/v1/colonies"
        assert seen["body"] == {
            "name": "hypothesis-needs-testing",
            "display_name": "Hypothesis Needs Testing",
            "community_type": "public",
            "client": "colony-sdk-python",
            "description": "A room.",
        }
        assert out == MADE


class TestMockCreateColony:
    def test_records_the_call(self) -> None:
        m = MockColonyClient(responses={"create_colony": MADE})
        out = m.create_colony(name="s", display_name="S", community_type="restricted")
        assert out == MADE
        call = m.calls[-1]
        assert call[0] == "create_colony"
        assert call[1] == {"name": "s", "display_name": "S", "community_type": "restricted"}

    def test_optional_fields_omitted(self) -> None:
        m = MockColonyClient(responses={"create_colony": MADE})
        m.create_colony(name="s", display_name="S")
        assert "description" not in m.calls[-1][1]
        assert "idempotency_key" not in m.calls[-1][1]
