"""``MockColonyClient`` answers in the shapes the server sends.

The mock exists so people can test their own code without the network. A
default response carrying a key the server never sends lets that code pass its
tests and then fail in production. Until 2026-11-12, ``vote_post`` answered
``{"score": 1}`` where the server sends ``new_score``; eleven other defaults
carried keys of the same kind, including ``set_recovery_email``'s
``verification_sent``, the very field ``test_agent_email.py`` exists to keep out
of that response. ``test_mock_completeness.py`` checks that every method is
mocked; this file checks what the mocks say.

Each bound default is checked against the success response its route declares
in the server's OpenAPI document, recursing into nested objects and list items:

- an **invented** key, one the schema does not declare, fails, unless
  ``SDK_ADDED`` records it as a key the real client adds to the body itself;
- a value of the **wrong type** fails (``None`` where the field is not
  nullable, a string where it is an integer);
- a **missing required** key is counted against a ratchet. Many defaults are
  deliberately small, so the count may only fall, and must be lowered when it
  does;
- a default with **no binding** is counted against a second ratchet, so a new
  mock method gets bound to its route rather than skipping the check.

``MOCK_OPS`` is explicit: matching by name would skip what it couldn't match and
report success for it. A guard checks that each bound operation's path appears
in the real client method's source, so a binding can't quietly name the wrong
route.

The schemas come from ``tests/fixtures/openapi_mock_schemas.json``, written by
``scripts/gen_openapi_snapshot.py`` (``--mocks-only`` writes that file alone).
"""

from __future__ import annotations

import inspect
import json
import pathlib
import re
from typing import Any

import pytest

from colony_sdk import ColonyClient
from colony_sdk.testing import _DEFAULTS

_SNAPSHOT = pathlib.Path(__file__).parent / "fixtures" / "openapi_mock_schemas.json"
_REF = "#/components/schemas/"

#: Mock method -> the operation its ``ColonyClient`` method calls.
MOCK_OPS: dict[str, str] = {
    "accept_colony_mod_invitation": "POST /api/v1/colonies/mod-invites/{invitation_id}/accept",
    "accept_org_invitation": "POST /api/v1/orgs/invitations/{invitation_id}/accept",
    "add_org_delegation_grant": "POST /api/v1/orgs/{slug}/delegation-grants",
    "add_org_resource": "POST /api/v1/orgs/{slug}/resources",
    "archive_conversation": "POST /api/v1/messages/conversations/{username}/archive",
    "block_user": "POST /api/v1/users/{user_id}/block",
    "close_poll": "POST /api/v1/polls/{post_id}/close",
    "confirm_2fa": "POST /api/v1/auth/2fa/confirm",
    "confirm_claim": "POST /api/v1/claims/{claim_id}/confirm",
    "confirm_key_recovery": "POST /api/v1/auth/recover-key/confirm",
    "create_bug_report": "POST /api/v1/bugs",
    "create_comment": "POST /api/v1/posts/{post_id}/comments",
    "create_echo": "POST /api/v1/echoes",
    "create_org": "POST /api/v1/orgs",
    "create_post": "POST /api/v1/posts",
    "create_project": "POST /api/v1/projects",
    "create_puzzle": "POST /api/v1/puzzles",
    "create_webhook": "POST /api/v1/webhooks",
    "create_wiki_page": "POST /api/v1/wiki",
    "decline_colony_mod_invitation": "POST /api/v1/colonies/mod-invites/{invitation_id}/decline",
    "decline_org_invitation": "POST /api/v1/orgs/invitations/{invitation_id}/decline",
    "delete_notifications": "POST /api/v1/notifications/delete",
    "delete_read_notifications": "POST /api/v1/notifications/delete-read",
    "disable_2fa": "POST /api/v1/auth/2fa/disable",
    "disable_og_image": "POST /api/v1/posts/{post_id}/og-image/disable",
    "enroll_2fa": "POST /api/v1/auth/2fa/enroll",
    "follow": "POST /api/v1/users/{user_id}/follow",
    "follow_by_username": "POST /api/v1/users/by-username/{username}/follow",
    "get_2fa_status": "GET /api/v1/auth/2fa/status",
    "get_audio": "GET /api/v1/audio/{audio_id}",
    "get_audio_quota": "GET /api/v1/audio/quota",
    "get_claim": "GET /api/v1/claims/{claim_id}",
    "get_cold_budget": "GET /api/v1/me/cold-budget",
    "get_conversation": "GET /api/v1/messages/conversations/{username}",
    "get_crypto_section": "GET /api/v1/crypto",
    "get_crypto_symbol": "GET /api/v1/crypto/symbols/{symbol}",
    "get_deprecations": "GET /api/v1/deprecations",
    "get_email": "GET /api/v1/auth/email",
    "get_me": "GET /api/v1/users/me",
    "get_my_status": "GET /api/v1/users/me/status",
    "get_notification_settings": "GET /api/v1/me/notification-settings",
    "get_org": "GET /api/v1/orgs/{slug}",
    "get_poll": "GET /api/v1/polls/{post_id}/results",
    "get_post": "GET /api/v1/posts/{post_id}",
    "get_premium_history": "GET /api/v1/premium/history",
    "get_premium_invoice": "GET /api/v1/premium/invoice/{payment_hash}",
    "get_premium_pricing": "GET /api/v1/premium/pricing",
    "get_premium_status": "GET /api/v1/premium/status",
    "get_puzzle": "GET /api/v1/puzzles/{puzzle_id}",
    "get_puzzles": "GET /api/v1/puzzles",
    "get_recovery_email": "GET /api/v1/auth/email",
    "get_relationship": "GET /api/v1/users/{user_id}/relationship",
    "get_relationship_by_username": "GET /api/v1/users/by-username/{username}/relationship",
    "get_system_notifications": "GET /api/v1/system/notifications",
    "get_unread_count": "GET /api/v1/messages/unread-count",
    "get_user": "GET /api/v1/users/{user_id}",
    "get_user_by_username": "GET /api/v1/users/by-username/{username}",
    "get_webhooks": "GET /api/v1/webhooks",
    "invite_colony_moderator": "POST /api/v1/colonies/{colony_id}/mod-invites",
    "leave_org": "POST /api/v1/orgs/{slug}/leave",
    "list_blocked": "GET /api/v1/users/me/blocked",
    "list_claims": "GET /api/v1/claims",
    "list_colony_mod_invitations": "GET /api/v1/colonies/{colony_id}/mod-invites",
    "list_message_edits": "GET /api/v1/messages/{message_id}/edits",
    "list_my_colony_mod_invitations": "GET /api/v1/colonies/mod-invites/received",
    "list_my_org_invitations": "GET /api/v1/orgs/invitations",
    "list_my_orgs": "GET /api/v1/orgs",
    "list_org_delegation_grants": "GET /api/v1/orgs/{slug}/delegation-grants",
    "list_org_disclosure_recipients": "GET /api/v1/orgs/disclosure-recipients",
    "list_org_domain_challenges": "GET /api/v1/orgs/{slug}/domain",
    "list_org_members": "GET /api/v1/orgs/{slug}/members",
    "list_org_pending_invitations": "GET /api/v1/orgs/{slug}/invitations",
    "list_org_resources": "GET /api/v1/orgs/{slug}/resources",
    "mark_conversation_read": "POST /api/v1/messages/conversations/{username}/read",
    "mark_conversation_spam": "POST /api/v1/messages/conversations/{username}/spam",
    "mark_notifications_read_batch": "POST /api/v1/notifications/read",
    "mute_conversation": "POST /api/v1/messages/conversations/{username}/mute",
    "react_comment": "POST /api/v1/reactions/toggle",
    "react_post": "POST /api/v1/reactions/toggle",
    "recover_key": "POST /api/v1/auth/recover-key",
    "regenerate_recovery_codes": "POST /api/v1/auth/2fa/recovery-codes/regenerate",
    "reject_claim": "POST /api/v1/claims/{claim_id}/reject",
    "remove_comment_audio": "DELETE /api/v1/comments/{comment_id}/audio",
    "remove_email": "DELETE /api/v1/auth/email",
    "remove_post_audio": "DELETE /api/v1/posts/{post_id}/audio",
    "report_comment": "POST /api/v1/reports",
    "report_post": "POST /api/v1/reports",
    "report_project": "POST /api/v1/reports",
    "report_wiki_page": "POST /api/v1/reports",
    "retract_poll_vote": "DELETE /api/v1/polls/{post_id}/vote",
    "revoke_colony_mod_invitation": "POST /api/v1/colonies/{colony_id}/mod-invites/{invitation_id}/revoke",
    "rotate_key": "POST /api/v1/auth/rotate-key",
    "send_message": "POST /api/v1/messages/send/{username}",
    "set_comment_audio": "PUT /api/v1/comments/{comment_id}/audio",
    "set_email": "POST /api/v1/auth/email",
    "set_inbox_mode": "PATCH /api/v1/me/inbox",
    "set_my_status": "PUT /api/v1/users/me/status",
    "set_post_audio": "PUT /api/v1/posts/{post_id}/audio",
    "set_premium_auto_renew": "POST /api/v1/premium/auto-renew",
    "set_recovery_email": "POST /api/v1/auth/email",
    "solve_puzzle": "POST /api/v1/puzzles/{puzzle_id}/solve",
    "start_puzzle": "POST /api/v1/puzzles/{puzzle_id}/start",
    "subscribe_premium": "POST /api/v1/premium/subscribe",
    "unarchive_conversation": "POST /api/v1/messages/conversations/{username}/unarchive",
    "unmark_conversation_spam": "DELETE /api/v1/messages/conversations/{username}/spam",
    "unmute_conversation": "POST /api/v1/messages/conversations/{username}/unmute",
    "update_notification_settings": "PATCH /api/v1/me/notification-settings",
    "update_poll": "PATCH /api/v1/polls/{post_id}",
    "update_post": "PUT /api/v1/posts/{post_id}",
    "update_profile": "PUT /api/v1/users/me",
    "update_webhook": "PUT /api/v1/webhooks/{webhook_id}",
    "verify_email": "POST /api/v1/auth/email/verify",
    "vote_comment": "POST /api/v1/comments/{comment_id}/vote",
    "vote_poll": "POST /api/v1/polls/{post_id}/vote",
    "vote_post": "POST /api/v1/posts/{post_id}/vote",
}

#: Keys the real client adds to the server's body, so the mock must carry them
#: too. Each needs the reason.
SDK_ADDED: dict[str, tuple[str, ...]] = {
    # ColonyClient.mark_conversation_spam adds it from the Idempotent-Replay
    # (or legacy X-Idempotency-Replayed) response header.
    "mark_conversation_spam": ("idempotency_replayed",),
}

#: Methods whose ColonyClient method returns one key of the body rather than
#: the body, so the default is checked against that key's schema.
UNWRAPPED: dict[str, str] = {
    # Both answer ModInviteListOut and return its invites list.
    "list_colony_mod_invitations": "invites",
    "list_my_colony_mod_invitations": "invites",
}

#: Required keys missing from bound defaults, summed. A ratchet: it may only
#: fall, and must be lowered when it does.
MISSING_REQUIRED_BASELINE = 39

#: Defaults with no entry in MOCK_OPS: their method calls more than one route,
#: or its route declares no named response schema. A ratchet, as above.
UNBOUND_BASELINE = 80


def _snapshot() -> dict[str, Any]:
    return json.loads(_SNAPSHOT.read_text())


def _resolve(schema: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        schema = schemas[schema["$ref"][len(_REF) :]]
    return schema


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


def _type_ok(value: Any, kind: str) -> bool:
    if kind in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES.get(kind, (object,)))


def check(value: Any, schema: dict[str, Any], schemas: dict[str, Any], path: str, out: dict[str, list[str]]) -> None:
    """Append what is wrong with ``value`` under ``schema`` to ``out``."""
    schema = _resolve(schema, schemas)
    branches = schema.get("anyOf") or schema.get("oneOf")
    if branches:
        # The branch the value is shaped for: null for None, an object or
        # array branch for a dict or a list, otherwise the scalar branches.
        fitting = [b for b in branches if _shape_fits(value, _resolve(b, schemas))]
        if not fitting:
            out["wrong_type"].append(f"{path}: {type(value).__name__} fits none of the declared types")
            return
        best: dict[str, list[str]] | None = None
        for branch in fitting:
            trial: dict[str, list[str]] = {"invented": [], "wrong_type": [], "missing": []}
            check(value, branch, schemas, path, trial)
            if best is None or sum(map(len, trial.values())) < sum(map(len, best.values())):
                best = trial
        assert best is not None
        for key, items in best.items():
            out[key] += items
        return
    kind = schema.get("type")
    if isinstance(kind, str) and not _type_ok(value, kind):
        out["wrong_type"].append(f"{path}: {type(value).__name__} where the schema says {kind}")
        return
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        if props:
            if not schema.get("additionalProperties"):
                out["invented"] += [f"{path}.{k}" for k in value if k not in props]
            out["missing"] += [f"{path}.{k}" for k in schema.get("required") or [] if k not in value]
            for key, item in value.items():
                if key in props:
                    check(item, props[key], schemas, f"{path}.{key}", out)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            check(item, schema["items"], schemas, f"{path}[{i}]", out)


def _shape_fits(value: Any, schema: dict[str, Any]) -> bool:
    kind = schema.get("type")
    if value is None:
        return kind == "null"
    if isinstance(value, dict):
        return kind == "object" or "properties" in schema or (kind is None and "anyOf" not in schema)
    if isinstance(value, list):
        return kind == "array"
    return kind not in ("null", "object", "array")


def _problems(method: str) -> dict[str, list[str]]:
    snap = _snapshot()
    out: dict[str, list[str]] = {"invented": [], "wrong_type": [], "missing": []}
    schema = snap["operations"][MOCK_OPS[method]]
    if method in UNWRAPPED:
        schema = _resolve(schema, snap["schemas"])["properties"][UNWRAPPED[method]]
    check(_DEFAULTS[method], schema, snap["schemas"], method, out)
    allowed = {f"{method}.{k}" for k in SDK_ADDED.get(method, ())}
    out["invented"] = [p for p in out["invented"] if p not in allowed]
    return out


@pytest.mark.parametrize("method", sorted(MOCK_OPS))
def test_default_invents_no_key_and_has_no_wrong_type(method: str) -> None:
    found = _problems(method)
    assert not found["invented"], (
        f"MockColonyClient.{method}'s default carries keys {MOCK_OPS[method]} never sends: "
        f"{found['invented']}. Code written against the mock reads them and passes its tests, "
        "then finds them absent in production."
    )
    assert not found["wrong_type"], f"MockColonyClient.{method}'s default has wrong types: {found['wrong_type']}"


def test_missing_required_keys_only_fall() -> None:
    missing = sorted(p for m in MOCK_OPS for p in _problems(m)["missing"])
    assert len(missing) <= MISSING_REQUIRED_BASELINE, (
        f"{len(missing)} required keys are missing from bound defaults, more than the baseline "
        f"{MISSING_REQUIRED_BASELINE}. Give the new default its required keys."
    )
    assert len(missing) == MISSING_REQUIRED_BASELINE, (
        f"Only {len(missing)} required keys are missing now: lower MISSING_REQUIRED_BASELINE to {len(missing)}."
    )


def test_unbound_defaults_only_fall() -> None:
    unbound = sorted(set(_DEFAULTS) - set(MOCK_OPS))
    assert len(unbound) <= UNBOUND_BASELINE, (
        f"{len(unbound)} defaults have no binding in MOCK_OPS, more than the baseline {UNBOUND_BASELINE}. "
        "Bind the new mock method to the operation its ColonyClient method calls."
    )
    assert len(unbound) == UNBOUND_BASELINE, f"Only {len(unbound)} are unbound now: lower UNBOUND_BASELINE."


def test_every_binding_names_a_mocked_method_and_a_snapshotted_operation() -> None:
    snap = _snapshot()
    assert not sorted(set(MOCK_OPS) - set(_DEFAULTS)), "MOCK_OPS names methods the mock has no default for"
    absent = sorted(op for op in set(MOCK_OPS.values()) if op not in snap["operations"])
    assert not absent, f"not in the snapshot, run scripts/gen_openapi_snapshot.py --mocks-only: {absent}"
    extra = sorted(set(snap["operations"]) - set(MOCK_OPS.values()))
    assert not extra, f"the snapshot holds operations nothing binds; regenerate it: {extra}"


@pytest.mark.parametrize("method", sorted(MOCK_OPS))
def test_binding_names_the_route_the_client_calls(method: str) -> None:
    """``MOCK_OPS`` must point at the route the real method requests, or the
    check above compares the mock with the wrong schema."""
    verb, path = MOCK_OPS[method].split(" ", 1)
    assert path.startswith("/api/v1/")
    pattern = re.escape(path[len("/api/v1") :])
    pattern = re.sub(r"\\\{[^}]*\\\}", r"\\{.+?\\}", pattern)
    source = inspect.getsource(getattr(ColonyClient, method))
    # A path split over adjacent string literals reads as one string.
    source = re.sub(r'"\s*\n\s*f?"', "", source)
    assert f'"{verb}"' in source, f"ColonyClient.{method} does not send {verb}"
    assert re.search(pattern, source), f"ColonyClient.{method} does not request {path}"
    if method in UNWRAPPED:
        assert f'"{UNWRAPPED[method]}"' in source, f"ColonyClient.{method} no longer unwraps {UNWRAPPED[method]!r}"


def test_the_check_catches_what_it_exists_to_catch() -> None:
    """Must-fail controls, so a check that passes everything can't hide here."""
    schemas = {"Vote": {"type": "object", "properties": {"new_score": {"type": "integer"}}, "required": ["new_score"]}}
    ref = {"$ref": _REF + "Vote"}
    out: dict[str, list[str]] = {"invented": [], "wrong_type": [], "missing": []}
    check({"score": 1}, ref, schemas, "vote", out)
    assert out == {"invented": ["vote.score"], "wrong_type": [], "missing": ["vote.new_score"]}
    out = {"invented": [], "wrong_type": [], "missing": []}
    check({"new_score": "1"}, ref, schemas, "vote", out)
    assert out["wrong_type"] == ["vote.new_score: str where the schema says integer"]
    out = {"invented": [], "wrong_type": [], "missing": []}
    check([{"new_score": True}], {"type": "array", "items": ref}, schemas, "votes", out)
    assert out["wrong_type"] == ["votes[0].new_score: bool where the schema says integer"]
    nullable = {"anyOf": [ref, {"type": "null"}]}
    out = {"invented": [], "wrong_type": [], "missing": []}
    check(None, nullable, schemas, "maybe", out)
    check({"new_score": 2, "extra": 1}, nullable, schemas, "maybe", out)
    assert out == {"invented": ["maybe.extra"], "wrong_type": [], "missing": []}
