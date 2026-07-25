"""The organisations surface — sync client, async client, and the mock.

An organisation is an IDENTITY object rather than a forum actor, so none of
this touches karma or ranking; what it does touch is what a third party is
allowed to learn about an agent, which is why the disclosure and delegation
methods get the most adversarial treatment below.

Three things are pinned, in rough order of how badly they'd hurt if wrong:

1. **Method, URL and body** for all 30 endpoints. A method that silently
   POSTs to the wrong path, or drops a field from the body, produces a
   plausible-looking 404/422 that reads as a server fault.
2. **Sync/async parity by construction** — the async twins were derived from
   the sync block mechanically, and ``test_parity`` re-checks that they
   haven't diverged since. Parity that is asserted only by having written
   both is not parity.
3. **Local validation before the round-trip**, matching the SDK's existing
   posture: reject what is knowably wrong here, pass everything else to the
   server, which is the only party that can judge existence.
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
from colony_sdk.models import (
    Organisation,
    OrgDelegationGrant,
    OrgDisclosureRecipient,
    OrgDomainChallenge,
    OrgInvitation,
    OrgMember,
    OrgMembership,
    OrgPendingInvite,
    OrgResource,
)

BASE = "https://thecolony.ai/api/v1"
UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"


def _mock_response(data: dict | list | str = "", status: int = 200) -> MagicMock:
    body = json.dumps(data).encode() if isinstance(data, (dict, list)) else data.encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _authed_client(typed: bool = False) -> ColonyClient:
    client = ColonyClient("col_test", typed=typed)
    client._token = "fake-jwt"
    client._token_expiry = time.time() + 9999
    return client


def _last_request(mock_urlopen: MagicMock) -> MagicMock:
    return mock_urlopen.call_args[0][0]


def _last_body(mock_urlopen: MagicMock) -> dict:
    return json.loads(_last_request(mock_urlopen).data.decode())


# ---------------------------------------------------------------------------
# Wiring: method + URL + body for every endpoint
# ---------------------------------------------------------------------------


class TestOrgLifecycle:
    @patch("colony_sdk.client.urlopen")
    def test_list_my_orgs(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([{"slug": "acme", "name": "Acme", "role": "owner"}])
        result = _authed_client().list_my_orgs()
        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/orgs"
        assert result[0]["role"] == "owner"

    @patch("colony_sdk.client.urlopen")
    def test_create_org(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"slug": "acme"})
        _authed_client().create_org("Acme", "acme", description="We make things")
        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/orgs"
        assert _last_body(mock_urlopen) == {
            "name": "Acme",
            "slug": "acme",
            "description": "We make things",
        }

    @patch("colony_sdk.client.urlopen")
    def test_create_org_omits_description_when_not_given(self, mock_urlopen: MagicMock) -> None:
        """An omitted optional must be ABSENT, not null — sending
        ``description: None`` asks the server to blank the field."""
        mock_urlopen.return_value = _mock_response({"slug": "acme"})
        _authed_client().create_org("Acme", "acme")
        assert "description" not in _last_body(mock_urlopen)

    @patch("colony_sdk.client.urlopen")
    def test_get_org(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"slug": "acme", "name": "Acme"})
        _authed_client().get_org("acme")
        assert _last_request(mock_urlopen).full_url == f"{BASE}/orgs/acme"

    @patch("colony_sdk.client.urlopen")
    def test_rename_org(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"status": "ok"})
        _authed_client().rename_org("acme", "acme-corp")
        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/orgs/acme/rename"
        assert _last_body(mock_urlopen) == {"new_slug": "acme-corp"}

    @patch("colony_sdk.client.urlopen")
    def test_leave_org(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"left": True})
        _authed_client().leave_org("acme")
        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/orgs/acme/leave"


class TestOrgInvitations:
    @patch("colony_sdk.client.urlopen")
    def test_list_my_org_invitations(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([{"invitation_id": UUID_A, "slug": "acme"}])
        _authed_client().list_my_org_invitations()
        assert _last_request(mock_urlopen).full_url == f"{BASE}/orgs/invitations"

    @patch("colony_sdk.client.urlopen")
    def test_accept_and_decline_target_the_invitation_not_the_org(self, mock_urlopen: MagicMock) -> None:
        """Both address an invitation id. Keying on the slug instead would be
        ambiguous — you can hold more than one invitation to an org over
        time, and accepting 'the acme invitation' would not say which."""
        client = _authed_client()
        mock_urlopen.return_value = _mock_response({"slug": "acme"})
        client.accept_org_invitation(UUID_A)
        assert _last_request(mock_urlopen).full_url == f"{BASE}/orgs/invitations/{UUID_A}/accept"

        mock_urlopen.return_value = _mock_response({"status": "declined"})
        client.decline_org_invitation(UUID_A)
        assert _last_request(mock_urlopen).full_url == f"{BASE}/orgs/invitations/{UUID_A}/decline"

    @patch("colony_sdk.client.urlopen")
    def test_invite_org_member_with_and_without_role(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        mock_urlopen.return_value = _mock_response({"status": "ok"})
        client.invite_org_member("acme", "reticuli", role="admin")
        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/orgs/acme/invitations"
        assert _last_body(mock_urlopen) == {"username": "reticuli", "role": "admin"}

        client.invite_org_member("acme", "reticuli")
        assert _last_body(mock_urlopen) == {"username": "reticuli"}

    @patch("colony_sdk.client.urlopen")
    def test_list_org_pending_invitations(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([])
        _authed_client().list_org_pending_invitations("acme")
        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/orgs/acme/invitations"


class TestOrgMembers:
    @patch("colony_sdk.client.urlopen")
    def test_list_org_members(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([{"user_id": UUID_B, "username": "a"}])
        _authed_client().list_org_members("acme")
        assert _last_request(mock_urlopen).full_url == f"{BASE}/orgs/acme/members"

    @patch("colony_sdk.client.urlopen")
    def test_set_org_member_role(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"status": "ok"})
        _authed_client().set_org_member_role("acme", UUID_B, "admin")
        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/orgs/acme/members/{UUID_B}/role"
        assert _last_body(mock_urlopen) == {"role": "admin"}

    @patch("colony_sdk.client.urlopen")
    def test_remove_org_member(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"status": "ok"})
        _authed_client().remove_org_member("acme", UUID_B)
        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/orgs/acme/members/{UUID_B}"

    @patch("colony_sdk.client.urlopen")
    def test_transfer_org_ownership(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"status": "ok"})
        _authed_client().transfer_org_ownership("acme", UUID_B)
        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/orgs/acme/transfer"
        assert _last_body(mock_urlopen) == {"user_id": UUID_B}

    @patch("colony_sdk.client.urlopen")
    def test_add_org_operated_agent(self, mock_urlopen: MagicMock) -> None:
        """The only path that creates a membership without the invitee
        accepting. It addresses the agent by HANDLE, not id — the server
        gates it on a shared confirmed operator, which is what makes the
        missing acceptance step legitimate."""
        mock_urlopen.return_value = _mock_response({"status": "ok"})
        _authed_client().add_org_operated_agent("acme", "my-agent")
        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/orgs/acme/operated-agents"
        assert _last_body(mock_urlopen) == {"username": "my-agent"}


class TestOrgDisclosure:
    @patch("colony_sdk.client.urlopen")
    def test_set_org_disclosure(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"status": "ok"})
        _authed_client().set_org_disclosure("acme", "opaque")
        req = _last_request(mock_urlopen)
        assert req.get_method() == "PUT"
        assert req.full_url == f"{BASE}/orgs/acme/disclosure"
        assert _last_body(mock_urlopen) == {"mode": "opaque"}

    @patch("colony_sdk.client.urlopen")
    def test_set_org_visibility_sends_a_real_boolean(self, mock_urlopen: MagicMock) -> None:
        """``visible`` gates whether a third party learns the affiliation, so
        it must arrive as a JSON boolean. A truthy string would serialise as
        ``"false"`` — which is true — and silently expose a membership the
        caller asked to hide."""
        mock_urlopen.return_value = _mock_response({"status": "ok"})
        _authed_client().set_org_visibility("acme", False)
        assert _last_body(mock_urlopen) == {"visible": False}
        assert _last_body(mock_urlopen)["visible"] is False

    def test_set_org_visibility_rejects_a_non_bool(self) -> None:
        with pytest.raises(TypeError, match="visible must be a bool"):
            _authed_client().set_org_visibility("acme", "false")  # type: ignore[arg-type]

    @patch("colony_sdk.client.urlopen")
    def test_list_org_disclosure_recipients(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([{"client_id": "rp"}])
        _authed_client().list_org_disclosure_recipients()
        assert _last_request(mock_urlopen).full_url == f"{BASE}/orgs/disclosure-recipients"


class TestOrgDomain:
    @patch("colony_sdk.client.urlopen")
    def test_start_org_domain_challenge(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"token": "x"})
        _authed_client().start_org_domain_challenge("acme", "acme.example", "dns")
        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/orgs/acme/domain"
        assert _last_body(mock_urlopen) == {"domain": "acme.example", "method": "dns"}

    @patch("colony_sdk.client.urlopen")
    def test_verify_org_domain(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"status": "verified"})
        _authed_client().verify_org_domain("acme")
        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/orgs/acme/domain/verify"

    @patch("colony_sdk.client.urlopen")
    def test_list_org_domain_challenges(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([{"domain": "acme.example"}])
        _authed_client().list_org_domain_challenges("acme")
        req = _last_request(mock_urlopen)
        assert req.get_method() == "GET"
        assert req.full_url == f"{BASE}/orgs/acme/domain"


class TestOrgResourcesAndDelegation:
    @patch("colony_sdk.client.urlopen")
    def test_list_and_add_and_remove_resource(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        mock_urlopen.return_value = _mock_response([])
        client.list_org_resources("acme")
        assert _last_request(mock_urlopen).full_url == f"{BASE}/orgs/acme/resources"

        mock_urlopen.return_value = _mock_response({"id": UUID_A})
        client.add_org_resource("acme", "https://api.acme.example", label="Acme API")
        assert _last_body(mock_urlopen) == {
            "identifier": "https://api.acme.example",
            "label": "Acme API",
        }
        client.add_org_resource("acme", "https://api.acme.example")
        assert "label" not in _last_body(mock_urlopen)

        client.remove_org_resource("acme", UUID_A)
        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/orgs/acme/resources/{UUID_A}"

    @patch("colony_sdk.client.urlopen")
    def test_add_org_delegation_grant_full_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response({"id": UUID_A})
        _authed_client().add_org_delegation_grant(
            "acme",
            "https://api.acme.example",
            ["read", "write"],
            min_role="admin",
            max_ttl_seconds=900,
        )
        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/orgs/acme/delegation-grants"
        assert _last_body(mock_urlopen) == {
            "resource": "https://api.acme.example",
            "scopes": ["read", "write"],
            "min_role": "admin",
            "max_ttl_seconds": 900,
        }

    @patch("colony_sdk.client.urlopen")
    def test_delegation_grant_omits_unset_limits(self, mock_urlopen: MagicMock) -> None:
        """``min_role`` and ``max_ttl_seconds`` NARROW a grant. Sending them
        as null when the caller omitted them would ask the server to clear a
        limit rather than leave it at the default — widening the widest
        permission in the org surface by accident."""
        mock_urlopen.return_value = _mock_response({"id": UUID_A})
        _authed_client().add_org_delegation_grant("acme", "https://api.acme.example", ["read"])
        body = _last_body(mock_urlopen)
        assert body == {"resource": "https://api.acme.example", "scopes": ["read"]}
        assert "min_role" not in body and "max_ttl_seconds" not in body

    @pytest.mark.parametrize("bad", [[], "read", None])
    def test_delegation_grant_rejects_empty_or_non_list_scopes(self, bad: object) -> None:
        """A grant with no scopes authorises nothing, so an empty list is
        always a bug — a caller who built it from a filtered list that came
        back empty, not someone deliberately granting nothing."""
        with pytest.raises(ValueError, match="non-empty list"):
            _authed_client().add_org_delegation_grant(
                "acme",
                "https://api.acme.example",
                bad,  # type: ignore[arg-type]
            )

    @patch("colony_sdk.client.urlopen")
    def test_list_and_remove_delegation_grant(self, mock_urlopen: MagicMock) -> None:
        client = _authed_client()
        mock_urlopen.return_value = _mock_response([])
        client.list_org_delegation_grants("acme")
        assert _last_request(mock_urlopen).full_url == f"{BASE}/orgs/acme/delegation-grants"

        client.remove_org_delegation_grant("acme", UUID_A)
        req = _last_request(mock_urlopen)
        assert req.get_method() == "DELETE"
        assert req.full_url == f"{BASE}/orgs/acme/delegation-grants/{UUID_A}"


class TestOrgDeletion:
    @patch("colony_sdk.client.urlopen")
    def test_request_cancel_and_status_share_one_path_by_verb(self, mock_urlopen: MagicMock) -> None:
        """All three are ``/deletion``, distinguished only by verb — so a
        verb mix-up would CANCEL a deletion when asked to check it, or the
        reverse. Worth pinning together rather than apart."""
        client = _authed_client()
        mock_urlopen.return_value = _mock_response({"status": "pending"})

        client.request_org_deletion("acme", reason="no longer needed")
        req = _last_request(mock_urlopen)
        assert req.get_method() == "POST"
        assert req.full_url == f"{BASE}/orgs/acme/deletion"
        assert _last_body(mock_urlopen) == {"reason": "no longer needed"}

        client.cancel_org_deletion("acme")
        assert _last_request(mock_urlopen).get_method() == "DELETE"
        assert _last_request(mock_urlopen).full_url == f"{BASE}/orgs/acme/deletion"

        client.get_org_deletion_status("acme")
        assert _last_request(mock_urlopen).get_method() == "GET"
        assert _last_request(mock_urlopen).full_url == f"{BASE}/orgs/acme/deletion"


# ---------------------------------------------------------------------------
# Local validation — before the round-trip
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_slug_is_rejected_locally(self, blank: str) -> None:
        with pytest.raises(ValueError, match="slug"):
            _authed_client().get_org(blank)

    def test_truncated_uuid_is_rejected_locally(self) -> None:
        """The SDK's existing posture: an id shortened for display and pasted
        back reads as 'the org deleted that member' when it 404s. Catch it
        where the cause is still visible."""
        with pytest.raises(ValueError):
            _authed_client().remove_org_member("acme", UUID_B[:8])

    def test_a_slug_is_not_required_to_be_a_uuid(self) -> None:
        """Slugs are handles. Validating them as UUIDs would reject every
        real org — this pins that the two identifier kinds stay distinct."""
        client = _authed_client()
        with patch("colony_sdk.client.urlopen") as m:
            m.return_value = _mock_response({"slug": "acme"})
            client.get_org("acme")  # must not raise


# ---------------------------------------------------------------------------
# Typed models
# ---------------------------------------------------------------------------


class TestTypedModels:
    @patch("colony_sdk.client.urlopen")
    def test_typed_mode_returns_models_not_dicts(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(
            [{"slug": "acme", "name": "Acme", "role": "admin", "disclosure_mode": "public"}]
        )
        result = _authed_client(typed=True).list_my_orgs()
        assert isinstance(result[0], OrgMembership)
        assert result[0].role == "admin"

    @patch("colony_sdk.client.urlopen")
    def test_untyped_mode_still_returns_plain_dicts(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response([{"slug": "acme", "name": "Acme"}])
        result = _authed_client(typed=False).list_my_orgs()
        assert isinstance(result[0], dict)

    def test_member_visible_defaults_closed(self) -> None:
        """A row with the field missing must not read as visible. This is a
        disclosure gate: defaulting it True would report an affiliation as
        third-party-visible on the strength of an absent field."""
        assert OrgMember.from_dict({"user_id": UUID_B, "username": "a"}).member_visible is False

    def test_models_round_trip(self) -> None:
        cases = [
            (
                Organisation,
                {
                    "slug": "acme",
                    "name": "Acme",
                    "disclosure_mode": "public",
                    "member_count": 3,
                    "verified_domain": "acme.example",
                },
            ),
            (
                OrgMembership,
                {"slug": "acme", "name": "Acme", "role": "owner", "disclosure_mode": "opaque", "verified_domain": None},
            ),
            (
                OrgMember,
                {
                    "user_id": UUID_B,
                    "username": "a",
                    "display_name": "A",
                    "user_type": "agent",
                    "role": "admin",
                    "member_visible": True,
                    "joined_at": "2026-07-25T00:00:00Z",
                },
            ),
            (
                OrgInvitation,
                {
                    "invitation_id": UUID_A,
                    "slug": "acme",
                    "name": "Acme",
                    "role": "member",
                    "disclosure_mode": "public",
                    "verified_domain": None,
                },
            ),
            (
                OrgPendingInvite,
                {
                    "invitation_id": UUID_A,
                    "user_id": UUID_B,
                    "username": "a",
                    "display_name": "A",
                    "user_type": "agent",
                    "role": "member",
                    "member_visible": False,
                    "joined_at": None,
                },
            ),
            (OrgResource, {"id": UUID_A, "identifier": "https://x.example", "label": "X", "created_at": None}),
            (
                OrgDelegationGrant,
                {
                    "id": UUID_A,
                    "resource": "https://x.example",
                    "allowed_scopes": ["read"],
                    "min_role": "admin",
                    "max_ttl_seconds": 60,
                    "member_user_id": None,
                    "is_active": True,
                    "created_at": None,
                },
            ),
            (
                OrgDomainChallenge,
                {
                    "domain": "acme.example",
                    "method": "dns",
                    "status": "pending",
                    "created_at": None,
                    "expires_at": None,
                    "verified_at": None,
                },
            ),
            (
                OrgDisclosureRecipient,
                {"client_id": "rp", "client_name": "RP", "scopes": ["colony:orgs"], "last_used_at": None},
            ),
        ]
        for model, payload in cases:
            assert model.from_dict(payload).to_dict() == payload, model.__name__

    def test_scope_lists_are_copied_not_aliased(self) -> None:
        """``from_dict`` must not retain the caller's list. Sharing it would
        let a later mutation of the response dict silently rewrite a grant's
        scopes — the one field where being wrong widens a permission."""
        scopes = ["read"]
        grant = OrgDelegationGrant.from_dict({"id": UUID_A, "resource": "r", "allowed_scopes": scopes})
        scopes.append("write")
        assert grant.allowed_scopes == ["read"]


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------


class TestParity:
    def _org_methods(self, cls: type) -> dict[str, inspect.Signature]:
        return {
            n: inspect.signature(getattr(cls, n))
            for n in dir(cls)
            if "org" in n and callable(getattr(cls, n, None)) and not n.startswith("_")
        }

    def test_async_client_has_every_sync_org_method_with_the_same_signature(self) -> None:
        """The async twins were derived from the sync block mechanically, so
        they agreed on the day they were written. This is what keeps them
        agreeing: an argument added to one side only shows up here."""
        from colony_sdk.async_client import AsyncColonyClient

        sync = self._org_methods(ColonyClient)
        aio = self._org_methods(AsyncColonyClient)
        assert len(sync) == 30, f"expected 30 org methods, found {len(sync)}"
        assert set(sync) == set(aio), (
            f"only on sync: {sorted(set(sync) - set(aio))}; only on async: {sorted(set(aio) - set(sync))}"
        )
        mismatched = {n: (str(sync[n]), str(aio[n])) for n in sync if sync[n] != aio[n]}
        assert not mismatched, f"signature drift: {mismatched}"

    def test_mock_client_has_every_org_method(self) -> None:
        """A mock missing a method fails a user's test suite with
        AttributeError, which looks like their bug and is ours."""
        from colony_sdk.testing import MockColonyClient

        missing = set(self._org_methods(ColonyClient)) - set(self._org_methods(MockColonyClient))
        assert not missing, f"MockColonyClient is missing: {sorted(missing)}"

    def test_every_org_method_is_documented_in_the_readme(self) -> None:
        readme = (Path(__file__).parent.parent / "README.md").read_text()
        missing = [n for n in self._org_methods(ColonyClient) if f"`{n}(" not in readme]
        assert not missing, f"undocumented in README: {sorted(missing)}"


class TestMockClient:
    def test_mock_returns_canned_org_data(self) -> None:
        from colony_sdk.testing import MockColonyClient

        client = MockColonyClient()
        assert client.list_my_orgs()[0]["slug"] == "acme"
        assert client.get_org("acme")["name"] == "Acme"
        assert client.list_org_members("acme")[0]["member_visible"] is True

    def test_mock_responses_are_overridable(self) -> None:
        from colony_sdk.testing import MockColonyClient

        client = MockColonyClient(responses={"list_my_orgs": [{"slug": "other"}]})
        assert client.list_my_orgs()[0]["slug"] == "other"


# ---------------------------------------------------------------------------
# Async — the twins actually hit the same endpoints
# ---------------------------------------------------------------------------
#
# TestParity proves the two clients agree on NAMES and SIGNATURES. It cannot
# prove the async body sends the same request, because the derivation that
# produced them only rewrote the transport call. So the async surface is
# exercised here for real, through httpx.MockTransport: every method is
# driven, and its verb/path/body recorded and compared against the sync
# client's for the same arguments. A divergence in either is what this
# catches and the signature check cannot.


class TestAsyncOrgs:
    @pytest.mark.asyncio
    async def test_every_async_org_method_matches_its_sync_twins_request(self) -> None:
        import httpx

        from colony_sdk.async_client import AsyncColonyClient

        # One representative call per method, arguments identical for both
        # clients so the two requests must come out identical.
        calls: list[tuple[str, tuple, dict]] = [
            ("list_my_orgs", (), {}),
            ("create_org", ("Acme", "acme"), {"description": "d"}),
            ("get_org", ("acme",), {}),
            ("rename_org", ("acme", "acme2"), {}),
            ("leave_org", ("acme",), {}),
            ("list_my_org_invitations", (), {}),
            ("accept_org_invitation", (UUID_A,), {}),
            ("decline_org_invitation", (UUID_A,), {}),
            ("invite_org_member", ("acme", "bob"), {"role": "admin"}),
            ("list_org_pending_invitations", ("acme",), {}),
            ("list_org_members", ("acme",), {}),
            ("set_org_member_role", ("acme", UUID_B, "admin"), {}),
            ("remove_org_member", ("acme", UUID_B), {}),
            ("transfer_org_ownership", ("acme", UUID_B), {}),
            ("add_org_operated_agent", ("acme", "my-agent"), {}),
            ("set_org_disclosure", ("acme", "opaque"), {}),
            ("set_org_visibility", ("acme", False), {}),
            ("list_org_disclosure_recipients", (), {}),
            ("start_org_domain_challenge", ("acme", "acme.example", "dns"), {}),
            ("verify_org_domain", ("acme",), {}),
            ("list_org_domain_challenges", ("acme",), {}),
            ("list_org_resources", ("acme",), {}),
            ("add_org_resource", ("acme", "https://x.example"), {"label": "X"}),
            ("remove_org_resource", ("acme", UUID_A), {}),
            ("list_org_delegation_grants", ("acme",), {}),
            (
                "add_org_delegation_grant",
                ("acme", "https://x.example", ["read"]),
                {"min_role": "admin", "max_ttl_seconds": 60},
            ),
            ("remove_org_delegation_grant", ("acme", UUID_A), {}),
            ("request_org_deletion", ("acme",), {"reason": "r"}),
            ("cancel_org_deletion", ("acme",), {}),
            ("get_org_deletion_status", ("acme",), {}),
        ]
        assert len(calls) == 30, "every org method must be exercised, not a sample"

        seen: list[tuple[str, str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(
                (
                    request.method,
                    str(request.url),
                    request.content.decode() if request.content else "",
                )
            )
            return httpx.Response(200, content=b"[]")

        transport = httpx.MockTransport(handler)
        aclient = AsyncColonyClient("col_test", client=httpx.AsyncClient(transport=transport))
        aclient._token = "fake-jwt"
        aclient._token_expiry = 9_999_999_999

        for name, args, kwargs in calls:
            await getattr(aclient, name)(*args, **kwargs)

        # Now drive the sync client with the same arguments and compare.
        sync_seen: list[tuple[str, str, str]] = []
        client = _authed_client()
        with patch("colony_sdk.client.urlopen") as m:
            m.return_value = _mock_response([])
            for name, args, kwargs in calls:
                getattr(client, name)(*args, **kwargs)
                req = _last_request(m)
                sync_seen.append(
                    (
                        req.get_method(),
                        req.full_url,
                        req.data.decode() if req.data else "",
                    )
                )

        for (name, _, _), a, s in zip(calls, seen, sync_seen, strict=True):
            assert a[0] == s[0], f"{name}: async verb {a[0]} != sync {s[0]}"
            assert a[1] == s[1], f"{name}: async url {a[1]} != sync {s[1]}"
            assert a[2] == s[2], f"{name}: async body {a[2]!r} != sync {s[2]!r}"

    @pytest.mark.asyncio
    async def test_async_validation_fires_before_the_request(self) -> None:
        """Local validation must run on the async path too — otherwise the
        async client is quietly laxer than the sync one."""
        import httpx

        from colony_sdk.async_client import AsyncColonyClient

        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, content=b"{}")

        aclient = AsyncColonyClient("col_test", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        aclient._token = "fake-jwt"
        aclient._token_expiry = 9_999_999_999

        with pytest.raises(ValueError, match="non-empty list"):
            await aclient.add_org_delegation_grant("acme", "https://x.example", [])
        with pytest.raises(TypeError, match="visible must be a bool"):
            await aclient.set_org_visibility("acme", "false")  # type: ignore[arg-type]
        assert not called, "validation should reject before any request is sent"
