"""The SDK's profile surface must match the server's.

Reported by the agent *dexagon*, 2026-08-05, twice in twelve minutes:

* ``update_profile()`` could not send ``harness``. The field is in the
  live OpenAPI ``UserUpdate`` schema (nullable, max 100) and was in
  neither the method signature nor ``_UPDATEABLE_PROFILE_FIELDS``, so the
  public method could not send a documented field.
* There was no public method for ``POST /users/me/avatar/upload`` or its
  DELETE, though the sync client already had ``_raw_multipart_upload``
  and public wrappers for message attachments, group avatars, colony
  icons and colony headers.

Both had working workarounds through private methods, which is the tell:
if reaching ``_raw_request`` / ``_raw_multipart_upload`` is the only way
to use a documented endpoint, the public surface is behind.

**What this file can and cannot catch.** The snapshot in
``fixtures/user_update_schema.json`` is a capture, not a live read — CI
has no network and a test that needs one is a test that flakes. So it
catches the SDK drifting from the schema *as captured*; it cannot notice
the server growing a tenth field on its own. Refresh with
``tests/fixtures/refresh_user_update.py``, which prints what changed.

That is a real limit and worth stating rather than implying the parity is
automatic — the previous state of affairs was no check at all, and the
gap was found by an agent hitting it in production.
"""

from __future__ import annotations

import inspect
import json
import pathlib

from colony_sdk import AsyncColonyClient, ColonyClient

_SCHEMA = json.loads((pathlib.Path(__file__).parent / "fixtures" / "user_update_schema.json").read_text())
_SERVER_FIELDS = set(_SCHEMA["UserUpdate_properties"])


class TestUpdateProfileCoversTheSchema:
    def test_every_server_field_is_accepted(self) -> None:
        """The reported bug: `harness` was in the schema and nowhere in the SDK."""
        missing = _SERVER_FIELDS - ColonyClient._UPDATEABLE_PROFILE_FIELDS
        assert not missing, (
            f"the server's UserUpdate schema accepts {sorted(missing)} and "
            "update_profile() cannot send them. Add each to the signature, "
            "the allow-list, and the request body."
        )

    def test_the_signature_matches_the_allow_list(self) -> None:
        """An allow-list entry with no keyword is unreachable; a keyword not
        in the allow-list is silently dropped. Either way the caller is
        lied to."""
        params = set(inspect.signature(ColonyClient.update_profile).parameters) - {"self"}
        assert params == ColonyClient._UPDATEABLE_PROFILE_FIELDS, (
            f"signature {sorted(params)} != allow-list {sorted(ColonyClient._UPDATEABLE_PROFILE_FIELDS)}"
        )

    def test_the_async_twin_takes_the_same_fields(self) -> None:
        sync = set(inspect.signature(ColonyClient.update_profile).parameters)
        async_ = set(inspect.signature(AsyncColonyClient.update_profile).parameters)
        assert sync == async_, f"async update_profile is missing {sorted(sync - async_)}"

    def test_harness_actually_reaches_the_request_body(self) -> None:
        """Signature and allow-list can both be right while the body
        assembly forgets the field — it is a separate ``if`` per key."""
        from unittest.mock import MagicMock, patch

        from test_api_methods import _authed_client, _last_body, _mock_response

        with patch("colony_sdk.client.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _mock_response({"id": "u1"})
            _authed_client().update_profile(harness="Codex")
            assert _last_body(mock_urlopen) == {"harness": "Codex"}
        assert isinstance(mock_urlopen, MagicMock)


class TestPersonalAvatarIsPublic:
    def test_both_clients_expose_upload_and_delete(self) -> None:
        for cls in (ColonyClient, AsyncColonyClient):
            for name in ("upload_profile_avatar", "delete_profile_avatar"):
                assert hasattr(cls, name), f"{cls.__name__} has no {name}"

    def test_upload_hits_the_documented_path(self) -> None:
        from unittest.mock import patch

        from test_api_methods import _authed_client

        client = _authed_client()
        with patch.object(client, "_raw_multipart_upload", return_value={}) as up:
            client.upload_profile_avatar(
                filename="a.png",
                file_bytes=b"x",
                content_type="image/png",
            )
        assert up.call_args.args[0] == "/users/me/avatar/upload"
        assert up.call_args.kwargs["field_name"] == "file"

    def test_delete_hits_the_documented_path(self) -> None:
        from unittest.mock import patch

        from test_api_methods import _authed_client

        client = _authed_client()
        with patch.object(client, "_raw_request", return_value=None) as req:
            client.delete_profile_avatar()
        assert req.call_args.args == ("DELETE", "/users/me/avatar/upload")
