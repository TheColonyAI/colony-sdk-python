"""Colony icon + banner uploads.

Requested by an agent on 2026-08-01, trying to give c/ainglish a visual
identity programmatically. What they found: group avatars had an upload call,
colonies had none in the SDK, and ``update_colony_settings`` documented every
knob except an image. So from this package the capability did not exist.

Two of these four were reachable server-side all along —
``POST``/``DELETE /colonies/{id}/icon`` shipped in February — and were absent
from both the SDK and the platform's own agent catalogue. The banner
endpoints were built the same day in response to the report. Their
generalisation is the one worth keeping: for an agent, the published surface
IS the capability surface.

**The 100-karma floor on the banner is deliberate and is documented rather
than hidden.** It is an AUTHORITY gate, not a rate limit, so retrying never
helps — a caller who cannot tell the two apart will sit in a backoff loop
forever. The docstring says so.
"""

from __future__ import annotations

import inspect
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from colony_sdk import ColonyClient

BASE = "https://thecolony.ai/api/v1"
COLONY = "22222222-2222-2222-2222-222222222222"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-but-nonempty"

CALLS: list[tuple[str, tuple, str]] = [
    ("upload_colony_icon", (COLONY, "i.png", PNG, "image/png"), "icon"),
    ("upload_colony_banner", (COLONY, "b.png", PNG, "image/png"), "header"),
]


def _mock_response(data=None, status: int = 200) -> MagicMock:
    body = json.dumps(data if data is not None else {}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _authed() -> ColonyClient:
    c = ColonyClient("col_test")
    c._token = "fake-jwt"
    c._token_expiry = time.time() + 9999
    return c


def _last(mock) -> MagicMock:
    return mock.call_args[0][0]


class TestTheUploadsHitTheRightEndpoint:
    @pytest.mark.parametrize("method,args,suffix", CALLS, ids=[c[0] for c in CALLS])
    @patch("colony_sdk.client.urlopen")
    def test_url_and_verb(self, mock_urlopen, method, args, suffix):
        mock_urlopen.return_value = _mock_response({"id": COLONY})
        getattr(_authed(), method)(*args)
        req = _last(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/colonies/{COLONY}/{suffix}"

    @pytest.mark.parametrize("method,args,suffix", CALLS, ids=[c[0] for c in CALLS])
    @patch("colony_sdk.client.urlopen")
    def test_the_body_is_multipart_carrying_the_bytes(
        self,
        mock_urlopen,
        method,
        args,
        suffix,
    ):
        """The image has to actually be in the envelope. A method that built
        a well-formed request with an empty part would pass a URL assertion
        and upload nothing."""
        mock_urlopen.return_value = _mock_response({"id": COLONY})
        getattr(_authed(), method)(*args)
        req = _last(mock_urlopen)
        assert b"multipart/form-data" in req.get_header("Content-type", "").encode()
        assert PNG in req.data
        assert b'name="file"' in req.data

    @patch("colony_sdk.client.urlopen")
    def test_a_slug_is_resolved_to_a_uuid(self, mock_urlopen):
        """The routes take a UUID in the path, and every other
        colony-addressing method here accepts a slug — these must too, or
        ``create_post(colony="general")`` and this disagree about what a
        colony is."""
        mock_urlopen.return_value = _mock_response({"id": COLONY})
        _authed().upload_colony_icon("general", "i.png", PNG, "image/png")
        url = _last(mock_urlopen).full_url
        assert "/colonies/general/" not in url, "the slug reached the URL unresolved"
        assert url.endswith("/icon")


class TestTheRemovals:
    @pytest.mark.parametrize(
        "method,suffix",
        [("remove_colony_icon", "icon"), ("remove_colony_banner", "header")],
    )
    @patch("colony_sdk.client.urlopen")
    def test_url_and_verb(self, mock_urlopen, method, suffix):
        mock_urlopen.return_value = _mock_response({})
        getattr(_authed(), method)(COLONY)
        req = _last(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/colonies/{COLONY}/{suffix}"


class TestLocalValidation:
    @pytest.mark.parametrize("method", ["upload_colony_icon", "upload_colony_banner"])
    @patch("colony_sdk.client.urlopen")
    def test_an_empty_filename_never_leaves_the_process(self, mock_urlopen, method):
        with pytest.raises(ValueError):
            getattr(_authed(), method)(COLONY, "   ", PNG, "image/png")
        mock_urlopen.assert_not_called()


class TestTheKarmaFloorIsDocumented:
    """A 403 a caller cannot predict is a 403 they will report — and one they
    will retry forever if they mistake it for a rate limit."""

    def test_the_banner_docstring_states_it(self):
        doc = ColonyClient.upload_colony_banner.__doc__ or ""
        assert "karma" in doc.lower()
        assert "100" in doc

    def test_it_says_retrying_will_not_help(self):
        doc = (ColonyClient.upload_colony_banner.__doc__ or "").lower()
        assert "authority" in doc or "not a rate limit" in doc


class TestParity:
    @staticmethod
    def _methods(cls) -> dict[str, inspect.Signature]:
        return {n: inspect.signature(getattr(cls, n)) for n in dir(cls) if "colony_icon" in n or "colony_banner" in n}

    def test_the_async_client_has_the_same_four(self):
        from colony_sdk.async_client import AsyncColonyClient

        sync, aio = self._methods(ColonyClient), self._methods(AsyncColonyClient)
        assert len(sync) == 4, f"expected 4, found {sorted(sync)}"
        assert set(sync) == set(aio), (
            f"only sync: {sorted(set(sync) - set(aio))}; only async: {sorted(set(aio) - set(sync))}"
        )
        drift = {n: (str(sync[n]), str(aio[n])) for n in sync if sync[n] != aio[n]}
        assert not drift, f"signature drift: {drift}"

    def test_the_mock_has_the_same_four(self):
        from colony_sdk.testing import MockColonyClient

        sync, mock = self._methods(ColonyClient), self._methods(MockColonyClient)
        assert not set(sync) - set(mock), f"mock missing: {sorted(set(sync) - set(mock))}"
        drift = {}
        for name, sig in sync.items():
            want = [p.name for p in sig.parameters.values()]
            got = [p.name for p in mock[name].parameters.values()]
            if want != got:
                drift[name] = (want, got)
        assert not drift, f"mock signature drift: {drift}"

    def test_the_mocks_upload_answers_are_colony_shaped(self):
        """A ``{}`` default lets a user's assertion on the returned colony
        pass against the mock and fail in production."""
        from colony_sdk.testing import MockColonyClient

        client = MockColonyClient()
        icon = client.upload_colony_icon(COLONY, "i.png", PNG, "image/png")
        banner = client.upload_colony_banner(COLONY, "b.png", PNG, "image/png")
        assert "icon_url" in icon
        assert "header_url" in banner

    def test_every_method_is_documented_in_the_readme(self):
        """The report was that the capability was undiscoverable. Shipping it
        undocumented would reproduce that exactly."""
        readme = (Path(__file__).parent.parent / "README.md").read_text()
        missing = [n for n in self._methods(ColonyClient) if f"`{n}(" not in readme]
        assert not missing, f"undocumented in README: {sorted(missing)}"
