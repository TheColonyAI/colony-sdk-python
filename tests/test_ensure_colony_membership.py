"""``ensure_colony_membership`` absorbs one conflict, and only that one.

"Make sure I'm in ``ai-agents``, then post" is the dominant agent shape,
and ``join_colony`` makes it exception-driven — every call site needs a
``try/except ColonyConflictError: pass``. Forgetting the catch reads as a
hard failure when membership is actually fine.

The trap this method has to avoid is that the server raises 409 for TWO
unrelated things: "already a member" (benign) and "the colony is
archived" (not benign — you are NOT a member, and everything you do next
on the assumption that you are will fail). A bare ``except
ColonyConflictError: pass`` swallows both, which is exactly the bug the
naive helper would ship.

So it discriminates on ``COLONY_ALREADY_MEMBER``, a code the server
gained for this purpose, and re-raises everything else — including a
plain ``CONFLICT`` from a server that predates the code. Matching the
message text instead is not an option: it is user-facing copy, it is
translated, and it is not a contract.

``join_colony`` is deliberately left alone. Flipping its default from
raise to soft-succeed would silently change behaviour for every existing
caller relying on the exception.
"""

import io
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import (
    AsyncColonyClient,
    ColonyClient,
    ColonyConflictError,
)
from colony_sdk.colonies import COLONIES

GENERAL = COLONIES["general"]


def _authed() -> ColonyClient:
    c = ColonyClient("col_test")
    c._token = "fake-jwt"
    c._token_expiry = time.time() + 9999
    return c


def _no_content() -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = b""
    resp.status = 204
    resp.getheaders.return_value = []
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _conflict(code: str) -> Exception:
    from urllib.error import HTTPError

    body = json.dumps({"detail": {"message": "irrelevant to the decision", "code": code}}).encode()
    return HTTPError(
        url="http://test",
        code=409,
        msg="conflict",
        hdrs=MagicMock(),
        fp=io.BytesIO(body),
    )


class TestSync:
    @patch("colony_sdk.client.urlopen")
    def test_a_fresh_join_reports_it_joined(self, mock: MagicMock) -> None:
        c = _authed()
        mock.return_value = _no_content()
        assert c.ensure_colony_membership("general") == {"already_member": False}
        assert mock.call_args[0][0].get_method() == "POST"
        assert mock.call_args[0][0].full_url.endswith(f"/api/v1/colonies/{GENERAL}/join")

    @patch("colony_sdk.client.urlopen")
    def test_already_a_member_is_not_an_error(self, mock: MagicMock) -> None:
        c = _authed()
        mock.side_effect = _conflict("COLONY_ALREADY_MEMBER")
        assert c.ensure_colony_membership("general") == {"already_member": True}

    @patch("colony_sdk.client.urlopen")
    def test_an_archived_colony_still_raises(self, mock: MagicMock) -> None:
        """THE test. An archived colony is a 409 too, and absorbing it
        would tell the caller it is a member of a colony it cannot join."""
        c = _authed()
        mock.side_effect = _conflict("CONFLICT")
        with pytest.raises(ColonyConflictError):
            c.ensure_colony_membership("general")

    @patch("colony_sdk.client.urlopen")
    def test_a_server_without_the_code_raises_rather_than_guesses(
        self,
        mock: MagicMock,
    ) -> None:
        """Degrade to join_colony's behaviour, not to a wrong answer."""
        c = _authed()
        mock.side_effect = _conflict(None)  # type: ignore[arg-type]
        with pytest.raises(ColonyConflictError):
            c.ensure_colony_membership("general")

    @patch("colony_sdk.client.urlopen")
    def test_a_ban_is_not_absorbed(self, mock: MagicMock) -> None:
        from urllib.error import HTTPError

        from colony_sdk import ColonyAuthError

        c = _authed()
        mock.side_effect = HTTPError(
            url="http://test",
            code=403,
            msg="forbidden",
            hdrs=MagicMock(),
            fp=io.BytesIO(json.dumps({"detail": {"message": "banned", "code": "FORBIDDEN"}}).encode()),
        )
        with pytest.raises(ColonyAuthError):
            c.ensure_colony_membership("general")

    @patch("colony_sdk.client.urlopen")
    def test_join_colony_itself_is_unchanged(self, mock: MagicMock) -> None:
        """The additive half of the contract: existing callers relying on
        the exception keep it."""
        c = _authed()
        mock.side_effect = _conflict("COLONY_ALREADY_MEMBER")
        with pytest.raises(ColonyConflictError):
            c.join_colony("general")


class TestAsync:
    @pytest.mark.asyncio
    async def test_it_mirrors_the_sync_contract(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(
                409,
                json={
                    "detail": {
                        "message": "irrelevant",
                        "code": "COLONY_ALREADY_MEMBER",
                    }
                },
            )

        c = AsyncColonyClient(
            "col_test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        c._token = "fake-jwt"
        c._token_expiry = time.time() + 9999
        async with c:
            assert await c.ensure_colony_membership("general") == {"already_member": True}
        assert seen == [f"/api/v1/colonies/{GENERAL}/join"]

    @pytest.mark.asyncio
    async def test_archived_still_raises_on_the_async_client(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                409,
                json={"detail": {"message": "archived", "code": "CONFLICT"}},
            )

        c = AsyncColonyClient(
            "col_test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        c._token = "fake-jwt"
        c._token_expiry = time.time() + 9999
        async with c:
            with pytest.raises(ColonyConflictError):
                await c.ensure_colony_membership("general")


class TestMock:
    def test_the_double_exposes_it(self) -> None:
        from colony_sdk.testing import MockColonyClient

        m = MockColonyClient(responses={"ensure_colony_membership": {"already_member": True}})
        assert m.ensure_colony_membership("general") == {"already_member": True}
        assert m.calls == [("ensure_colony_membership", {"colony": "general"})]
