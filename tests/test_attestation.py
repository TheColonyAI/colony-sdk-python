"""Tests for the colony_sdk.attestation envelope producer (spec v0.1.1).

The strongest test here is *producer↔verifier interop*: an envelope minted by
this module is validated against a vendored copy of the spec's
``envelope.v0.1.schema.json`` AND its ed25519 sigchain is independently
re-verified using the exact peel-not-replace rule from the spec's
``docs/sigchain.md``. If the producer and the reference verifier ever disagree
about what bytes get signed, these tests fail.

The schema fixture (``tests/fixtures/envelope.v0.1.schema.json``) is a vendored
copy of the frozen v0.1.1 schema — kept here only so the suite is hermetic; the
spec repo remains the source of truth.
"""

from __future__ import annotations

import base64
import copy
import json
import pathlib
import sys
from datetime import datetime, timezone

import pytest

# These tests exercise the signed-envelope producer, which needs the optional
# crypto extra (``pip install colony-sdk[attestation]``) plus jsonschema for the
# interop check. Skip cleanly when they're absent so a bare ``pytest`` run on a
# contributor's machine doesn't error on collection.
jsonschema = pytest.importorskip("jsonschema")
pytest.importorskip("nacl.signing")
pytest.importorskip("base58")

from colony_sdk import attestation  # noqa: E402
from colony_sdk.attestation import (  # noqa: E402
    AttestationDependencyError,
    AttestationError,
    Ed25519Signer,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
SCHEMA = json.loads((FIXTURES / "envelope.v0.1.schema.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)

# A fixed seed → deterministic did:key / signatures across runs.
FIXED_SEED = bytes(range(32))


# --------------------------------------------------------------------------- #
# Reference verifier (mirrors the spec's docs/sigchain.md exactly)
# --------------------------------------------------------------------------- #
def _did_key_to_pubkey(did: str) -> bytes:
    import base58

    decoded = base58.b58decode(did[len("did:key:") + 1 :])
    assert decoded[:2] == b"\xed\x01", "did:key multicodec must be ed25519"
    return decoded[2:]


def verify_envelope(env: dict) -> None:
    """Raise if the envelope is not schema-valid or the sigchain doesn't verify."""
    import nacl.signing

    errors = list(VALIDATOR.iter_errors(env))
    assert not errors, f"schema errors: {[e.message for e in errors]}"

    chain = env["sigchain"]
    for i, entry in enumerate(chain):
        assert entry["alg"] == "ed25519"
        stripped = copy.deepcopy(env)
        stripped["sigchain"] = chain[:i]
        message = attestation.canonicalize(stripped)
        pub = _did_key_to_pubkey(entry["key_id"])
        sig = base64.urlsafe_b64decode(entry["sig"] + "=" * (-len(entry["sig"]) % 4))
        nacl.signing.VerifyKey(pub).verify(message, sig)  # raises on bad sig

    # issuer binding: did:key issuer's key_id IS its id.
    assert chain[0].get("role") in (None, "issuer")
    if env["issuer"]["id_scheme"] == "did:key":
        assert chain[0]["key_id"] == env["issuer"]["id"]


# --------------------------------------------------------------------------- #
# Zero-dependency surface
# --------------------------------------------------------------------------- #
def test_module_imports_and_is_lazily_reachable_from_package():
    import colony_sdk

    assert colony_sdk.attestation is attestation
    assert attestation.SPEC_VERSION == "0.1"


def test_data_builders_need_no_crypto(monkeypatch):
    # Block the crypto deps entirely; pure data shaping must still work.
    monkeypatch.setitem(sys.modules, "nacl", None)
    monkeypatch.setitem(sys.modules, "nacl.signing", None)
    monkeypatch.setitem(sys.modules, "base58", None)
    claim = attestation.artifact_published("https://x/y", "sha256:" + "ab" * 32)
    ev = attestation.evidence_platform_receipt("https://x/api", platform_id="x")
    val = attestation.validity_perpetual(datetime(2026, 1, 1), datetime(2030, 1, 1))
    assert claim["claim_type"] == "artifact_published"
    assert ev["platform_id"] == "x"
    assert val["validity_model"] == "perpetual"


# --------------------------------------------------------------------------- #
# Signer / did:key
# --------------------------------------------------------------------------- #
def test_signer_generate_is_random_and_32_bytes():
    a, b = Ed25519Signer.generate(), Ed25519Signer.generate()
    assert len(a.seed) == 32 and a.seed != b.seed


def test_signer_rejects_bad_seed():
    with pytest.raises(AttestationError):
        Ed25519Signer(b"too-short")


def test_did_key_is_deterministic_and_well_formed():
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    assert signer.did_key.startswith("did:key:z")
    # round-trips back to the same 32-byte public key
    assert _did_key_to_pubkey(signer.did_key) == signer.public_key


def test_public_key_to_did_key_rejects_wrong_length():
    with pytest.raises(AttestationError):
        attestation.public_key_to_did_key(b"\x00" * 31)


def test_signing_dep_missing_raises_helpful_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "nacl", None)
    monkeypatch.setitem(sys.modules, "nacl.signing", None)
    with pytest.raises(AttestationDependencyError, match="pip install colony-sdk\\[attestation\\]"):
        Ed25519Signer.from_seed(FIXED_SEED).sign(b"x")


def test_base58_dep_missing_raises_helpful_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "base58", None)
    with pytest.raises(AttestationDependencyError, match="pip install colony-sdk\\[attestation\\]"):
        attestation.public_key_to_did_key(b"\x00" * 32)


# --------------------------------------------------------------------------- #
# Builders: validation
# --------------------------------------------------------------------------- #
def test_artifact_published_rejects_bad_multihash():
    with pytest.raises(AttestationError):
        attestation.artifact_published("https://x/y", "not-a-hash")


def test_evidence_rejects_bad_content_hash():
    with pytest.raises(AttestationError):
        attestation.evidence_immutable_uri("https://x", content_hash="sha256:NOTHEX")


def test_platform_handle_identity_requires_colon():
    with pytest.raises(AttestationError):
        attestation.platform_handle_identity("no-colon-here")


def test_did_key_identity_rejects_non_did_key():
    with pytest.raises(AttestationError):
        attestation.did_key_identity("platform-handle:nope")


def test_coverage_requires_at_least_one_type():
    with pytest.raises(AttestationError):
        attestation.coverage("https://x/cov.json", [])


def test_all_claim_and_evidence_and_validity_builders_shapes():
    assert (
        attestation.action_executed("colony.post.create", "https://x/r", datetime(2026, 1, 1))["action_kind"]
        == "colony.post.create"
    )
    assert attestation.state_transition("a", "b", "https://x/w")["claim_type"] == "state_transition"
    assert attestation.capability_coverage("https://cap/x", "https://x/c")["claim_type"] == "capability_coverage"
    assert attestation.evidence_commit_hash("https://x", "sha1:" + "a" * 40)["pointer_type"] == "commit_hash"
    assert attestation.evidence_transcript_id("https://x", "p")["platform_id"] == "p"
    assert (
        attestation.validity_revocation_checked(datetime(2026, 1, 1), datetime(2027, 1, 1), "https://x/rev")[
            "revocation_uri"
        ]
        == "https://x/rev"
    )
    assert attestation.coverage("https://x/c", ["artifact_published"], datetime(2026, 1, 1))["covered_claim_types"] == [
        "artifact_published"
    ]


# --------------------------------------------------------------------------- #
# export_attestation — interop
# --------------------------------------------------------------------------- #
def test_export_attestation_self_attestation_verifies():
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    env = attestation.export_attestation(
        signer=signer,
        witnessed_claim=attestation.artifact_published("https://thecolony.cc/post/abc", "sha256:" + "0" * 64),
        evidence=[attestation.evidence_platform_receipt("https://thecolony.cc/api/v1/posts/abc", "thecolony.cc")],
        display_name="ColonistOne",
    )
    verify_envelope(env)
    assert env["envelope_version"] == "0.1"
    assert env["issuer"] == env["subject"]  # default self-attestation
    assert env["issuer"]["id"] == signer.did_key
    assert env["validity"]["validity_model"] == "time_bounded"
    assert env["sigchain"][0]["role"] == "issuer"


def test_export_attestation_with_explicit_peer_subject_and_coverage():
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    env = attestation.export_attestation(
        signer=signer,
        witnessed_claim=attestation.action_executed("colony.post.create", "https://thecolony.cc/api/v1/posts/abc"),
        evidence=[attestation.evidence_platform_receipt("https://thecolony.cc/api/v1/posts/abc", "thecolony.cc")],
        subject=attestation.platform_handle_identity("thecolony.cc:someone-else", "Someone"),
        coverage=attestation.coverage("https://thecolony.cc/u/colonist-one/coverage.json", ["action_executed"]),
        validity=attestation.validity_perpetual(datetime(2026, 1, 1), datetime(2030, 1, 1)),
    )
    verify_envelope(env)
    assert env["subject"]["id_scheme"] == "platform-handle"
    assert env["coverage"]["covered_claim_types"] == ["action_executed"]


def test_envelope_id_and_issued_at_are_honoured():
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    eid = "01910c4f-7a2c-7891-8b1d-d1e0b3c0a401"
    env = attestation.export_attestation(
        signer=signer,
        witnessed_claim=attestation.artifact_published("https://x/y", "sha256:" + "0" * 64),
        evidence=[attestation.evidence_immutable_uri("https://x/y")],
        issued_at=datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc),
        envelope_id=eid,
    )
    assert env["envelope_id"] == eid
    assert env["issued_at"] == "2026-06-13T12:00:00Z"
    verify_envelope(env)


def test_generated_envelope_id_matches_uuidv7_pattern():
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    env = attestation.export_attestation(
        signer=signer,
        witnessed_claim=attestation.artifact_published("https://x/y", "sha256:" + "0" * 64),
        evidence=[attestation.evidence_immutable_uri("https://x/y")],
    )
    # schema pattern enforces UUIDv7 (version nibble 7, variant 8-b)
    verify_envelope(env)  # schema validation covers the pattern


def test_signature_actually_binds_content():
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    env = attestation.export_attestation(
        signer=signer,
        witnessed_claim=attestation.artifact_published("https://x/y", "sha256:" + "0" * 64),
        evidence=[attestation.evidence_immutable_uri("https://x/y")],
    )
    verify_envelope(env)
    import nacl.exceptions

    tampered = copy.deepcopy(env)
    tampered["witnessed_claim"]["artifact_uri"] = "https://evil/z"
    with pytest.raises(nacl.exceptions.BadSignatureError):
        verify_envelope(tampered)


def test_build_envelope_requires_evidence():
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    with pytest.raises(AttestationError, match="evidence"):
        attestation.build_envelope(
            issuer=attestation.did_key_identity(signer.did_key),
            subject=attestation.did_key_identity(signer.did_key),
            witnessed_claim=attestation.artifact_published("https://x/y", "sha256:" + "0" * 64),
            evidence=[],
            validity=attestation.validity_perpetual(datetime(2026, 1, 1), datetime(2030, 1, 1)),
            signer=signer,
        )


def test_build_envelope_rejects_floats_in_extensions():
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    with pytest.raises(AttestationError, match="float"):
        attestation.build_envelope(
            issuer=attestation.did_key_identity(signer.did_key),
            subject=attestation.did_key_identity(signer.did_key),
            witnessed_claim=attestation.artifact_published("https://x/y", "sha256:" + "0" * 64),
            evidence=[attestation.evidence_immutable_uri("https://x/y")],
            validity=attestation.validity_perpetual(datetime(2026, 1, 1), datetime(2030, 1, 1)),
            signer=signer,
            extensions={"https://ext/x": 1.5},
        )


def test_build_envelope_role_can_be_omitted():
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    env = attestation.build_envelope(
        issuer=attestation.did_key_identity(signer.did_key),
        subject=attestation.did_key_identity(signer.did_key),
        witnessed_claim=attestation.artifact_published("https://x/y", "sha256:" + "0" * 64),
        evidence=[attestation.evidence_immutable_uri("https://x/y")],
        validity=attestation.validity_perpetual(datetime(2026, 1, 1), datetime(2030, 1, 1)),
        signer=signer,
        role=None,
    )
    assert "role" not in env["sigchain"][0]
    verify_envelope(env)


# --------------------------------------------------------------------------- #
# attest_post (high-level + client method)
# --------------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self, post: dict):
        self._post = post
        self.requested: str | None = None

    def get_post(self, post_id: str) -> dict:
        self.requested = post_id
        return self._post


def test_attest_post_hashes_body_and_builds_artifact_claim():
    post = {"id": "abc", "body": "hello colony", "created_at": "2026-06-13T10:00:00Z"}
    client = _FakeClient(post)
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    env = attestation.attest_post(client, "abc", signer=signer)
    verify_envelope(env)
    assert client.requested == "abc"
    import hashlib

    want = "sha256:" + hashlib.sha256(b"hello colony").hexdigest()
    assert env["witnessed_claim"]["content_hash"] == want
    assert env["witnessed_claim"]["artifact_uri"] == "https://thecolony.ai/post/abc"
    assert env["witnessed_claim"]["published_at"] == "2026-06-13T10:00:00Z"
    assert env["evidence"][0]["uri"] == "https://thecolony.ai/api/v1/posts/abc"
    assert env["evidence"][0]["platform_id"] == "thecolony.ai"


def test_attest_post_handles_missing_body():
    client = _FakeClient({"id": "abc"})
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    env = attestation.attest_post(client, "abc", signer=signer)
    import hashlib

    assert env["witnessed_claim"]["content_hash"] == "sha256:" + hashlib.sha256(b"").hexdigest()
    assert "published_at" not in env["witnessed_claim"]
    verify_envelope(env)


def test_attest_post_custom_base_url():
    client = _FakeClient({"id": "abc", "body": "x"})
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    env = attestation.attest_post(client, "abc", signer=signer, base_url="https://staging.thecolony.cc")
    assert env["witnessed_claim"]["artifact_uri"] == "https://staging.thecolony.cc/post/abc"
    assert env["evidence"][0]["uri"] == "https://staging.thecolony.cc/api/v1/posts/abc"


def test_client_attest_post_method_delegates():
    from colony_sdk import ColonyClient

    client = ColonyClient("col_test_key")
    post = {"id": "abc", "body": "hello", "created_at": "2026-06-13T10:00:00Z"}
    client.get_post = lambda _pid: post  # type: ignore[method-assign]
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    env = client.attest_post("abc", signer=signer)
    verify_envelope(env)
    assert env["witnessed_claim"]["artifact_uri"] == "https://thecolony.ai/post/abc"


def test_mock_client_attest_post():
    from colony_sdk import MockColonyClient

    client = MockColonyClient(
        responses={"get_post": {"id": "abc", "body": "mocked body", "created_at": "2026-06-13T10:00:00Z"}}
    )
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    env = client.attest_post("abc", signer=signer)
    verify_envelope(env)
    import hashlib

    assert env["witnessed_claim"]["content_hash"] == "sha256:" + hashlib.sha256(b"mocked body").hexdigest()
    assert ("attest_post", {"post_id": "abc"}) not in client.calls  # attest_post calls get_post internally
    assert ("get_post", {"post_id": "abc"}) in client.calls


def test_build_post_attestation_directly():
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    post = {"id": "abc", "body": "direct", "created_at": "2026-06-13T10:00:00Z"}
    env = attestation.build_post_attestation(post, "abc", signer=signer)
    verify_envelope(env)
    import hashlib

    assert env["witnessed_claim"]["content_hash"] == "sha256:" + hashlib.sha256(b"direct").hexdigest()


async def test_async_client_attest_post():
    from colony_sdk import AsyncColonyClient

    client = AsyncColonyClient("col_test_key")
    post = {"id": "abc", "body": "async body", "created_at": "2026-06-13T10:00:00Z"}

    async def fake_get_post(_post_id: str) -> dict:
        return post

    client.get_post = fake_get_post  # type: ignore[method-assign]
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    env = await client.attest_post("abc", signer=signer)
    verify_envelope(env)
    import hashlib

    assert env["witnessed_claim"]["content_hash"] == "sha256:" + hashlib.sha256(b"async body").hexdigest()
    assert env["witnessed_claim"]["artifact_uri"] == "https://thecolony.ai/post/abc"


# --------------------------------------------------------------------------- #
# verify() — the offline consumer
# --------------------------------------------------------------------------- #
def _valid_env(**kw):
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    return attestation.export_attestation(
        signer=signer,
        witnessed_claim=attestation.artifact_published("https://x/y", "sha256:" + "0" * 64),
        evidence=[attestation.evidence_immutable_uri("https://x/y")],
        **kw,
    )


def test_verify_accepts_valid_self_attestation():
    res = attestation.verify(_valid_env())
    assert res.ok and bool(res) is True
    assert res.issuer_bound is True
    assert res.reasons == ()
    assert any("verified against" in n for n in res.notes)


def test_verify_rejects_non_object():
    res = attestation.verify(["not", "an", "envelope"])  # type: ignore[arg-type]
    assert not res.ok and res.reasons == ("envelope is not an object",)


def test_verify_rejects_wrong_version():
    env = _valid_env()
    env["envelope_version"] = "9.9"
    res = attestation.verify(env)
    assert not res.ok and any("envelope_version" in r for r in res.reasons)


def test_verify_rejects_missing_field():
    env = _valid_env()
    del env["validity"]
    res = attestation.verify(env)
    assert not res.ok and any("missing required field: validity" in r for r in res.reasons)


def test_verify_rejects_empty_evidence():
    env = _valid_env()
    env["evidence"] = []
    res = attestation.verify(env)
    assert not res.ok and any("evidence must be a non-empty list" in r for r in res.reasons)


def test_verify_rejects_empty_sigchain():
    env = _valid_env()
    env["sigchain"] = []
    res = attestation.verify(env)
    assert not res.ok and any("sigchain must be a non-empty list" in r for r in res.reasons)


def test_verify_rejects_tampered_payload():
    env = _valid_env()
    import base64

    env["sigchain"][0]["sig"] = base64.urlsafe_b64encode(b"\x00" * 64).rstrip(b"=").decode()
    res = attestation.verify(env)
    assert not res.ok and any("does not verify" in r for r in res.reasons)


def test_verify_rejects_bad_sig_encoding():
    env = _valid_env()
    env["sigchain"][0]["sig"] = "@@@@not-base64@@@@"
    res = attestation.verify(env)
    assert not res.ok and any("does not verify" in r for r in res.reasons)


def test_verify_rejects_bad_alg():
    env = _valid_env()
    env["sigchain"][0]["alg"] = "rsa"
    res = attestation.verify(env)
    assert not res.ok and any("unsupported or missing alg" in r for r in res.reasons)


def test_verify_rejects_bad_role_on_issuer_sig():
    env = _valid_env()
    env["sigchain"][0]["role"] = "custodian"
    res = attestation.verify(env)
    assert not res.ok and any("role must be 'issuer'" in r for r in res.reasons)


def test_verify_rejects_non_did_key_key_id():
    env = _valid_env()
    env["sigchain"][0]["key_id"] = "not-a-did-key"
    res = attestation.verify(env)
    assert not res.ok and any("not a resolvable ed25519 did:key" in r for r in res.reasons)


def test_verify_perpetual_ok():
    env = _valid_env(validity=attestation.validity_perpetual(datetime(2026, 1, 1), datetime(2030, 1, 1)))
    res = attestation.verify(env)
    assert res.ok and any("perpetual" in n for n in res.notes)


def test_verify_expired():
    env = _valid_env(validity=attestation.validity_time_bounded(datetime(2020, 1, 1), datetime(2021, 1, 1)))
    res = attestation.verify(env)
    assert not res.ok and any("expired" in r for r in res.reasons)


def test_verify_not_yet_valid():
    env = _valid_env(validity=attestation.validity_time_bounded(datetime(2090, 1, 1), datetime(2091, 1, 1)))
    res = attestation.verify(env)
    assert not res.ok and any("not yet valid" in r for r in res.reasons)


def test_verify_time_bounded_within_with_explicit_now():
    env = _valid_env(validity=attestation.validity_time_bounded(datetime(2026, 1, 1), datetime(2027, 1, 1)))
    res = attestation.verify(env, now=datetime(2026, 6, 13, tzinfo=timezone.utc))
    assert res.ok and any("time_bounded" in n for n in res.notes)


def test_verify_unparseable_validity():
    env = _valid_env()
    env["validity"]["not_after"] = "garbage"
    res = attestation.verify(env)
    assert not res.ok and any("unparseable" in r for r in res.reasons)


def test_verify_revocation_checked_noted_not_failed():
    env = _valid_env(
        validity=attestation.validity_revocation_checked(datetime(2026, 1, 1), datetime(2030, 1, 1), "https://x/revoke")
    )
    res = attestation.verify(env)
    assert res.ok and any("revocation_checked" in n and "NOT confirmed offline" in n for n in res.notes)


def test_verify_unknown_validity_model():
    env = _valid_env()
    env["validity"]["validity_model"] = "vibes"
    res = attestation.verify(env)
    assert not res.ok and any("unknown validity_model" in r for r in res.reasons)


def test_verify_validity_not_object():
    env = _valid_env()
    env["validity"] = "nope"
    res = attestation.verify(env)
    assert not res.ok and any("validity is not an object" in r for r in res.reasons)


def test_verify_signature_valid_but_issuer_unbindable():
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    env = attestation.export_attestation(
        signer=signer,
        witnessed_claim=attestation.artifact_published("https://x/y", "sha256:" + "0" * 64),
        evidence=[attestation.evidence_immutable_uri("https://x/y")],
        issuer=attestation.platform_handle_identity("thecolony.cc:colonist-one"),
    )
    res = attestation.verify(env)
    assert res.ok is True  # signature math is valid
    assert res.issuer_bound is False
    assert any("UNBINDABLE" in n for n in res.notes)


def test_verify_did_key_issuer_mismatch_is_unverified():
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    other = Ed25519Signer.from_seed(bytes(range(1, 33)))
    env = attestation.export_attestation(
        signer=signer,
        witnessed_claim=attestation.artifact_published("https://x/y", "sha256:" + "0" * 64),
        evidence=[attestation.evidence_immutable_uri("https://x/y")],
        issuer=attestation.did_key_identity(other.did_key),  # issuer.id != signer.did_key
    )
    res = attestation.verify(env)
    assert res.ok is True  # sig still verifies (signed by signer)
    assert res.issuer_bound is False
    assert any("key_id != issuer.id" in n for n in res.notes)


def test_verify_issuer_not_object():
    env = _valid_env()
    env["issuer"] = "thecolony.cc:colonist-one"
    res = attestation.verify(env)
    assert res.issuer_bound is False
    assert any("issuer is not an object" in n for n in res.notes)


def test_verify_dep_missing_pynacl(monkeypatch):
    env = _valid_env()
    monkeypatch.setitem(sys.modules, "nacl", None)
    monkeypatch.setitem(sys.modules, "nacl.signing", None)
    monkeypatch.setitem(sys.modules, "nacl.exceptions", None)
    with pytest.raises(AttestationDependencyError, match="pip install colony-sdk\\[attestation\\]"):
        attestation.verify(env)


# ---- did_key_to_public_key ---------------------------------------------------
def test_did_key_to_public_key_roundtrip():
    signer = Ed25519Signer.from_seed(FIXED_SEED)
    assert attestation.did_key_to_public_key(signer.did_key) == signer.public_key


def test_did_key_to_public_key_rejects_non_did_key():
    with pytest.raises(AttestationError):
        attestation.did_key_to_public_key("did:web:example.com")


def test_did_key_to_public_key_rejects_wrong_multicodec():
    import base58

    bad = "did:key:z" + base58.b58encode(b"\x00\x01" + b"\x00" * 32).decode()
    with pytest.raises(AttestationError, match="multicodec"):
        attestation.did_key_to_public_key(bad)


def test_did_key_to_public_key_rejects_wrong_length():
    import base58

    short = "did:key:z" + base58.b58encode(b"\xed\x01" + b"\x00" * 31).decode()
    with pytest.raises(AttestationError, match="32 bytes"):
        attestation.did_key_to_public_key(short)


def test_did_key_to_public_key_dep_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "base58", None)
    with pytest.raises(AttestationDependencyError):
        attestation.did_key_to_public_key("did:key:zABC")
