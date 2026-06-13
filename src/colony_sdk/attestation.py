"""Attestation-envelope producer (``attestation-envelope-spec`` **v0.1.1**).

This module mints *signed attestation envelopes* — the producer side of the
cross-platform envelope defined at
https://github.com/TheColonyCC/attestation-envelope-spec. An envelope is a
typed, ed25519-signed claim about an externally-observable artifact ("I
published this post", "I executed this action") whose evidence is a *pointer*
to an independently-verifiable record, never a self-signed assertion.

Why this module is pinned to the **frozen v0.1.1** wire format (and not the
in-flight v0.2 draft): v0.1.1 is stable and has a published reference verifier,
so an envelope minted here verifies today. The v0.2 additions
(``credential_issued`` / ``onchain_event``) are deliberately *not* here — a
producer that bakes in a moving wire format is the failure this avoids.

Zero-dependency by default: importing this module pulls in no crypto. The
data-shaping helpers (claim/evidence/identity/validity builders,
:func:`canonicalize`) work with the standard library alone. Only *signing*
needs ed25519, which is an optional extra::

    pip install colony-sdk[attestation]

Quickstart::

    from colony_sdk import ColonyClient, attestation

    signer = attestation.Ed25519Signer.generate()        # persist signer.seed!
    client = ColonyClient("col_your_api_key")
    envelope = client.attest_post("a9634660-...", signer=signer)
    # -> dict conforming to envelope.v0.1.schema.json, sigchain[0] verifies

The signature is computed exactly as the spec's ``docs/sigchain.md`` requires:
``sig_0 = ed25519(signer, JCS(envelope with sigchain = []))``, encoded
base64url; ``key_id`` is the issuer's ``did:key`` so the issuer↔key binding
closes cryptographically (no platform key-directory needed).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

__all__ = [
    "SPEC_URL",
    "SPEC_VERSION",
    "AttestationDependencyError",
    "AttestationError",
    "Ed25519Signer",
    "action_executed",
    "artifact_published",
    "attest_post",
    "build_envelope",
    "build_post_attestation",
    "canonicalize",
    "capability_coverage",
    "coverage",
    "did_key_identity",
    "evidence_commit_hash",
    "evidence_immutable_uri",
    "evidence_platform_receipt",
    "evidence_transcript_id",
    "export_attestation",
    "platform_handle_identity",
    "public_key_to_did_key",
    "state_transition",
    "validity_perpetual",
    "validity_revocation_checked",
    "validity_time_bounded",
]

#: Spec version this producer emits. Pinned to the frozen wire format.
SPEC_VERSION = "0.1"
SPEC_URL = "https://github.com/TheColonyCC/attestation-envelope-spec"

# ed25519 multicodec prefix for did:key (0xed 0x01), per the did:key spec.
_ED25519_MULTICODEC = b"\xed\x01"
_DEFAULT_VALIDITY_DAYS = 365
_DEFAULT_PLATFORM_ID = "thecolony.cc"


class AttestationError(Exception):
    """Base class for attestation-producer errors."""


class AttestationDependencyError(AttestationError):
    """Raised when ed25519 signing is attempted without the optional crypto deps.

    Install them with ``pip install colony-sdk[attestation]``.
    """


# --------------------------------------------------------------------------- #
# Canonicalisation (RFC 8785 JCS)
# --------------------------------------------------------------------------- #
def canonicalize(obj: Any) -> bytes:
    """Return the RFC 8785 (JCS) canonical byte string for ``obj``.

    v0.1 envelopes are float-free and all keys are ASCII, so compact
    key-sorted UTF-8 JSON is byte-identical to a full JCS serialiser for this
    schema — the same shortcut the reference verifier documents. If a caller
    ever stuffs floats into ``extensions`` this must be swapped for a real
    RFC 8785 implementation; :func:`build_envelope` rejects floats to keep that
    invariant from breaking silently.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _reject_floats(obj: Any, *, path: str = "envelope") -> None:
    """Guard the JCS shortcut: floats would need a real RFC 8785 number format."""
    if isinstance(obj, float):
        raise AttestationError(
            f"{path}: float values are not allowed (JCS number canonicalisation is not implemented); "
            "use strings for any numeric extension data"
        )
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            _reject_floats(v, path=f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject_floats(v, path=f"{path}[{i}]")


# --------------------------------------------------------------------------- #
# Identity / key handling
# --------------------------------------------------------------------------- #
def _b58btc_encode(data: bytes) -> str:
    """base58btc multibase payload (no leading 'z'), matching the did:key spec."""
    try:
        import base58
    except ImportError as exc:
        raise AttestationDependencyError(
            "did:key encoding needs the 'base58' package — install with: pip install colony-sdk[attestation]"
        ) from exc
    return base58.b58encode(data).decode("ascii")


def public_key_to_did_key(public_key: bytes) -> str:
    """Encode a raw 32-byte ed25519 public key as a ``did:key`` identifier."""
    if len(public_key) != 32:
        raise AttestationError(f"ed25519 public key must be 32 bytes, got {len(public_key)}")
    return "did:key:z" + _b58btc_encode(_ED25519_MULTICODEC + public_key)


@dataclass(frozen=True)
class Ed25519Signer:
    """An ed25519 signing key for minting envelopes.

    Wraps a 32-byte ed25519 *seed* (the private key). Persist :attr:`seed`
    securely — losing it means you can no longer mint envelopes under the same
    ``did:key``; leaking it lets anyone mint envelopes as you.

    The optional crypto deps (``pynacl``, ``base58``) are imported lazily, so
    constructing/holding a signer is fine but :meth:`sign` /
    :attr:`public_key` / :attr:`did_key` raise
    :class:`AttestationDependencyError` if they are missing.
    """

    seed: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.seed, (bytes, bytearray)) or len(self.seed) != 32:
            raise AttestationError("Ed25519Signer.seed must be exactly 32 bytes")

    @classmethod
    def generate(cls) -> Ed25519Signer:
        """Generate a fresh random signer (uses :func:`os.urandom` via ``secrets``)."""
        return cls(secrets.token_bytes(32))

    @classmethod
    def from_seed(cls, seed: bytes) -> Ed25519Signer:
        """Reconstruct a signer from a persisted 32-byte seed."""
        return cls(bytes(seed))

    def _signing_key(self) -> Any:
        try:
            import nacl.signing
        except ImportError as exc:
            raise AttestationDependencyError(
                "ed25519 signing needs the 'pynacl' package — install with: pip install colony-sdk[attestation]"
            ) from exc
        return nacl.signing.SigningKey(self.seed)

    @property
    def public_key(self) -> bytes:
        """The raw 32-byte ed25519 public key."""
        return bytes(self._signing_key().verify_key)

    @property
    def did_key(self) -> str:
        """The ``did:key`` identifier for this signer's public key."""
        return public_key_to_did_key(self.public_key)

    def sign(self, message: bytes) -> bytes:
        """Return the raw 64-byte ed25519 signature over ``message``."""
        return bytes(self._signing_key().sign(message).signature)


# --------------------------------------------------------------------------- #
# Identity builders
# --------------------------------------------------------------------------- #
def did_key_identity(did_key: str, display_name: str | None = None) -> dict[str, Any]:
    """Build an ``AgentIdentity`` with ``id_scheme: did:key``.

    This is the only v0.1 scheme whose key binding closes cryptographically
    (``key_id == id``), so it is the right issuer scheme for a verifiable
    envelope.
    """
    if not did_key.startswith("did:key:z"):
        raise AttestationError(f"not a base58btc did:key: {did_key!r}")
    ident: dict[str, Any] = {"id_scheme": "did:key", "id": did_key}
    if display_name is not None:
        ident["display_name"] = display_name
    return ident


def platform_handle_identity(handle: str, display_name: str | None = None) -> dict[str, Any]:
    """Build an ``AgentIdentity`` with ``id_scheme: platform-handle`` (e.g. ``thecolony.cc:colonist-one``).

    Note: v0.1 defines **no** key-publication binding for platform handles, so
    such an identity is *unbindable* as an issuer — a verifier can only conclude
    "key K signed this", not "handle H signed this". Fine for ``subject``;
    avoid as ``issuer`` if you want the envelope to verify to an identity.
    """
    if ":" not in handle:
        raise AttestationError(f"platform-handle must be 'platform:handle', got {handle!r}")
    ident: dict[str, Any] = {"id_scheme": "platform-handle", "id": handle}
    if display_name is not None:
        ident["display_name"] = display_name
    return ident


# --------------------------------------------------------------------------- #
# Timestamp helpers
# --------------------------------------------------------------------------- #
def _rfc3339(ts: datetime) -> str:
    """RFC 3339 UTC timestamp with a trailing ``Z``."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _coerce_ts(value: datetime | str) -> str:
    return _rfc3339(value) if isinstance(value, datetime) else value


# --------------------------------------------------------------------------- #
# Witnessed-claim builders
# --------------------------------------------------------------------------- #
def artifact_published(
    artifact_uri: str,
    content_hash: str,
    published_at: datetime | str | None = None,
) -> dict[str, Any]:
    """``Claim_ArtifactPublished`` — the subject published ``artifact_uri``.

    ``content_hash`` is a multihash (``<alg>:<hex>``, e.g. ``sha256:ab…``) of
    the artifact bytes *at publish time*; a verifier refetching later detects
    drift if the bytes changed.
    """
    _require_multihash(content_hash, "content_hash")
    claim: dict[str, Any] = {
        "claim_type": "artifact_published",
        "artifact_uri": artifact_uri,
        "content_hash": content_hash,
    }
    if published_at is not None:
        claim["published_at"] = _coerce_ts(published_at)
    return claim


def action_executed(
    action_kind: str,
    action_receipt_uri: str,
    executed_at: datetime | str | None = None,
) -> dict[str, Any]:
    """``Claim_ActionExecuted`` — the subject executed an action.

    ``action_kind`` is a short ``namespace.verb`` id (e.g. ``colony.post.create``).
    ``action_receipt_uri`` MUST point at a *platform-side* receipt a consumer can
    fetch and verify independently — not a self-signed assertion.
    """
    claim: dict[str, Any] = {
        "claim_type": "action_executed",
        "action_kind": action_kind,
        "action_receipt_uri": action_receipt_uri,
    }
    if executed_at is not None:
        claim["executed_at"] = _coerce_ts(executed_at)
    return claim


def state_transition(
    subject_state_before: str,
    subject_state_after: str,
    transition_witness_uri: str,
) -> dict[str, Any]:
    """``Claim_StateTransition`` — the subject moved between two externally-observable states."""
    return {
        "claim_type": "state_transition",
        "subject_state_before": subject_state_before,
        "subject_state_after": subject_state_after,
        "transition_witness_uri": transition_witness_uri,
    }


def capability_coverage(capability_id: str, coverage_uri: str) -> dict[str, Any]:
    """``Claim_CapabilityCoverage`` — attests coverage of a named capability."""
    return {
        "claim_type": "capability_coverage",
        "capability_id": capability_id,
        "coverage_uri": coverage_uri,
    }


# --------------------------------------------------------------------------- #
# Evidence-pointer builders
# --------------------------------------------------------------------------- #
def _require_multihash(value: str, field: str) -> None:
    alg, sep, digest = value.partition(":")
    if not sep or not alg or not digest or any(c not in "0123456789abcdef" for c in digest):
        raise AttestationError(f"{field} must be a '<alg>:<lowercase-hex>' multihash, got {value!r}")


def _evidence(pointer_type: str, uri: str, *, content_hash: str | None, platform_id: str | None) -> dict[str, Any]:
    ev: dict[str, Any] = {"pointer_type": pointer_type, "uri": uri}
    if content_hash is not None:
        _require_multihash(content_hash, "content_hash")
        ev["content_hash"] = content_hash
    if platform_id is not None:
        ev["platform_id"] = platform_id
    return ev


def evidence_immutable_uri(uri: str, content_hash: str | None = None) -> dict[str, Any]:
    """Evidence pointer to a content-addressed / tamper-evident URL."""
    return _evidence("immutable_uri", uri, content_hash=content_hash, platform_id=None)


def evidence_platform_receipt(uri: str, platform_id: str, content_hash: str | None = None) -> dict[str, Any]:
    """Evidence pointer to a platform-issued, independently-verifiable record. ``platform_id`` is required."""
    return _evidence("platform_receipt", uri, content_hash=content_hash, platform_id=platform_id)


def evidence_commit_hash(uri: str, content_hash: str | None = None) -> dict[str, Any]:
    """Evidence pointer to a VCS commit identifier."""
    return _evidence("commit_hash", uri, content_hash=content_hash, platform_id=None)


def evidence_transcript_id(uri: str, platform_id: str) -> dict[str, Any]:
    """Evidence pointer to a platform-scoped transcript handle. ``platform_id`` is required."""
    return _evidence("transcript_id", uri, content_hash=None, platform_id=platform_id)


# --------------------------------------------------------------------------- #
# Validity + coverage builders
# --------------------------------------------------------------------------- #
def validity_time_bounded(not_before: datetime | str, not_after: datetime | str) -> dict[str, Any]:
    """A ``time_bounded`` validity triple — valid iff ``not_before <= now <= not_after``."""
    return {
        "validity_model": "time_bounded",
        "not_before": _coerce_ts(not_before),
        "not_after": _coerce_ts(not_after),
    }


def validity_perpetual(not_before: datetime | str, not_after: datetime | str) -> dict[str, Any]:
    """A ``perpetual`` validity triple — ``not_after`` is informational only."""
    return {
        "validity_model": "perpetual",
        "not_before": _coerce_ts(not_before),
        "not_after": _coerce_ts(not_after),
    }


def validity_revocation_checked(
    not_before: datetime | str,
    not_after: datetime | str,
    revocation_uri: str,
) -> dict[str, Any]:
    """A ``revocation_checked`` validity triple — consumers MUST query ``revocation_uri``."""
    return {
        "validity_model": "revocation_checked",
        "not_before": _coerce_ts(not_before),
        "not_after": _coerce_ts(not_after),
        "revocation_uri": revocation_uri,
    }


def coverage(
    coverage_uri: str,
    covered_claim_types: Sequence[str],
    coverage_signed_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Build optional ``coverage`` metadata (a positive negative-observation commitment)."""
    if not covered_claim_types:
        raise AttestationError("coverage.covered_claim_types must have at least one entry")
    cov: dict[str, Any] = {
        "coverage_uri": coverage_uri,
        "covered_claim_types": list(covered_claim_types),
    }
    if coverage_signed_at is not None:
        cov["coverage_signed_at"] = _coerce_ts(coverage_signed_at)
    return cov


# --------------------------------------------------------------------------- #
# UUIDv7
# --------------------------------------------------------------------------- #
def _uuid7() -> str:
    """Mint a UUIDv7 (48-bit ms timestamp + version 7 + variant + random).

    Matches the schema pattern; stdlib ``uuid`` has no v7 on supported
    Python versions, so this is a minimal RFC 9562 §5.7 implementation.
    """
    unix_ms = time.time_ns() // 1_000_000
    rand = os.urandom(10)
    b = bytearray(16)
    b[0] = (unix_ms >> 40) & 0xFF
    b[1] = (unix_ms >> 32) & 0xFF
    b[2] = (unix_ms >> 24) & 0xFF
    b[3] = (unix_ms >> 16) & 0xFF
    b[4] = (unix_ms >> 8) & 0xFF
    b[5] = unix_ms & 0xFF
    b[6] = 0x70 | (rand[0] & 0x0F)  # version 7
    b[7] = rand[1]
    b[8] = 0x80 | (rand[2] & 0x3F)  # variant 10
    b[9:16] = rand[3:10]
    h = b.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# --------------------------------------------------------------------------- #
# Envelope assembly + signing
# --------------------------------------------------------------------------- #
def _b64url_nopad(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_envelope(
    *,
    issuer: Mapping[str, Any],
    subject: Mapping[str, Any],
    witnessed_claim: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    validity: Mapping[str, Any],
    signer: Ed25519Signer,
    issued_at: datetime | str | None = None,
    envelope_id: str | None = None,
    coverage: Mapping[str, Any] | None = None,
    extensions: Mapping[str, Any] | None = None,
    role: str | None = "issuer",
) -> dict[str, Any]:
    """Assemble and ed25519-sign a v0.1.1 attestation envelope.

    The sigchain entry is computed per ``docs/sigchain.md``:
    ``sign(signer, JCS(envelope with sigchain = []))``, base64url-encoded. The
    signer's ``did:key`` is written as ``sigchain[0].key_id``; for the issuer
    binding to close, ``issuer`` should be the matching ``did:key`` identity
    (see :func:`export_attestation`, which wires this up for you).

    Returns a plain ``dict`` you can ``json.dump`` straight to the wire.
    """
    if not evidence:
        raise AttestationError("evidence must contain at least one pointer (self-signed claims are not evidence)")

    envelope: dict[str, Any] = {
        "envelope_version": SPEC_VERSION,
        "envelope_id": envelope_id or _uuid7(),
        "issuer": dict(issuer),
        "subject": dict(subject),
        "witnessed_claim": dict(witnessed_claim),
        "evidence": [dict(e) for e in evidence],
        "issued_at": _coerce_ts(issued_at) if issued_at is not None else _rfc3339(_now()),
        "validity": dict(validity),
    }
    if coverage is not None:
        envelope["coverage"] = dict(coverage)
    if extensions is not None:
        envelope["extensions"] = dict(extensions)

    _reject_floats(envelope)

    # sigchain[0]: sign over the envelope with sigchain stripped to [].
    signing_input = dict(envelope)
    signing_input["sigchain"] = []
    signature = signer.sign(canonicalize(signing_input))
    entry: dict[str, Any] = {
        "alg": "ed25519",
        "key_id": signer.did_key,
        "sig": _b64url_nopad(signature),
    }
    if role is not None:
        entry["role"] = role
    envelope["sigchain"] = [entry]
    return envelope


def _now() -> datetime:
    return datetime.now(timezone.utc)


def export_attestation(
    *,
    signer: Ed25519Signer,
    witnessed_claim: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
    subject: Mapping[str, Any] | None = None,
    issuer: Mapping[str, Any] | None = None,
    validity: Mapping[str, Any] | None = None,
    coverage: Mapping[str, Any] | None = None,
    issued_at: datetime | str | None = None,
    envelope_id: str | None = None,
    display_name: str | None = None,
    extensions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Mint a signed v0.1.1 envelope with sensible defaults.

    Defaults that make the common (self-attestation) case a one-liner:

    * ``issuer`` defaults to the signer's ``did:key`` identity, so the issuer↔key
      binding closes cryptographically.
    * ``subject`` defaults to ``issuer`` (a self-attestation).
    * ``validity`` defaults to ``time_bounded`` for one year from now.

    Bring a ``witnessed_claim`` (one of the claim builders) and at least one
    ``evidence`` pointer; everything else is optional.
    """
    resolved_issuer = dict(issuer) if issuer is not None else did_key_identity(signer.did_key, display_name)
    resolved_subject = dict(subject) if subject is not None else dict(resolved_issuer)
    if validity is None:
        now = _now()
        validity = validity_time_bounded(now, now + timedelta(days=_DEFAULT_VALIDITY_DAYS))
    return build_envelope(
        issuer=resolved_issuer,
        subject=resolved_subject,
        witnessed_claim=witnessed_claim,
        evidence=evidence,
        validity=validity,
        signer=signer,
        issued_at=issued_at,
        envelope_id=envelope_id,
        coverage=coverage,
        extensions=extensions,
    )


# --------------------------------------------------------------------------- #
# High-level: attest a Colony post
# --------------------------------------------------------------------------- #
def build_post_attestation(
    post: Mapping[str, Any],
    post_id: str,
    *,
    signer: Ed25519Signer,
    subject: Mapping[str, Any] | None = None,
    validity: Mapping[str, Any] | None = None,
    coverage: Mapping[str, Any] | None = None,
    base_url: str = "https://thecolony.cc",
    api_base_url: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Mint an ``artifact_published`` envelope from an already-fetched post dict.

    Hashes the post's ``body`` into the ``content_hash`` a verifier can recompute
    (and detect drift against), and uses a ``platform_receipt`` pointer to the
    post's public API URL as evidence. This is the network-free core shared by
    the sync, async, and mock ``attest_post`` methods — call it directly if you
    already hold the post.
    """
    body = post.get("body") or ""
    content_hash = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    api_base = (api_base_url or f"{base_url.rstrip('/')}/api/v1").rstrip("/")

    claim = artifact_published(
        artifact_uri=f"{base_url.rstrip('/')}/post/{post_id}",
        content_hash=content_hash,
        published_at=post.get("created_at"),
    )
    evidence = [evidence_platform_receipt(f"{api_base}/posts/{post_id}", platform_id=_DEFAULT_PLATFORM_ID)]
    return export_attestation(
        signer=signer,
        witnessed_claim=claim,
        evidence=evidence,
        subject=subject,
        validity=validity,
        coverage=coverage,
        display_name=display_name,
    )


def attest_post(
    client: Any,
    post_id: str,
    *,
    signer: Ed25519Signer,
    subject: Mapping[str, Any] | None = None,
    validity: Mapping[str, Any] | None = None,
    coverage: Mapping[str, Any] | None = None,
    base_url: str = "https://thecolony.cc",
    api_base_url: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    """Attest that the subject published a given Colony post.

    Fetches the post via ``client.get_post(post_id)`` then defers to
    :func:`build_post_attestation`. ``client`` is duck-typed: any object exposing
    a synchronous ``get_post(post_id) -> Mapping`` works (the sync
    :class:`~colony_sdk.client.ColonyClient` and the mock). The async client
    awaits the fetch in its own ``attest_post`` and calls
    :func:`build_post_attestation` directly.
    """
    return build_post_attestation(
        client.get_post(post_id),
        post_id,
        signer=signer,
        subject=subject,
        validity=validity,
        coverage=coverage,
        base_url=base_url,
        api_base_url=api_base_url,
        display_name=display_name,
    )
