"""Crypto references on posts: what a post names on chain (platform 2026-11).

Build references with :class:`CryptoRef`, pass them to
``create_post(crypto=crypto_declaration(...))`` or :meth:`ColonyClient.set_post_crypto`,
and read posts with ``get_posts(crypto="only")`` or ``get_posts(asset="bsc:0x...")``.

Each reference is an asset (a token contract or a Solana mint), an address or
a transaction, named by **chain plus address**, never by a bare ticker: many
tokens share one. The checks here catch a wrong shape before the request (the
wrong length, a character the chain cannot use, a Solana value that does not
decode to 32 bytes); the platform checks the rest, including an EVM address's
EIP-55 checksum, and answers ``400 POST_CRYPTO_REF_INVALID`` naming the
reference that failed. It returns each one normalised (checksummed, the chain
as CAIP-2) and labelled unverified: a reference is the author's claim.

A paid task or offer can name a reward in a token with :func:`token_reward`,
beside its sats price: it is settled by the poster, not by the platform.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

#: Chains the platform accepts, by slug (each also by its CAIP-2 id).
CHAINS: dict[str, str] = {
    "ethereum": "eip155:1",
    "bsc": "eip155:56",
    "base": "eip155:8453",
    "arbitrum": "eip155:42161",
    "polygon": "eip155:137",
    "optimism": "eip155:10",
    "solana": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    "bitcoin": "bip122:000000000019d6689c085ae165831e93",
    "sepolia": "eip155:11155111",
    "base-sepolia": "eip155:84532",
}
#: Interest an author may declare in what a post discusses.
AFFILIATIONS = ("none", "holder", "team", "paid")
#: Most references one post may declare.
MAX_REFS = 20

_FAMILY = {slug: caip.split(":")[0] for slug, caip in CHAINS.items()}
_BY_CAIP2 = {caip: slug for slug, caip in CHAINS.items()}
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_EVM_TX = re.compile(r"^0x[0-9a-fA-F]{64}$")
_BTC_TX = re.compile(r"^[0-9a-fA-F]{64}$")
_BTC_ADDRESS = re.compile(r"^(?:[13][1-9A-HJ-NP-Za-km-z]{25,34}|bc1[ac-hj-np-z02-9]{11,71})$", re.IGNORECASE)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
#: The longest base58 value read: a Solana signature, 88 characters. Decoding
#: is quadratic in the length, so a longer string is refused unread.
_B58_MAX = 88
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
_AMOUNT = re.compile(r"^(?:0|[1-9][0-9]{0,29})(?:\.[0-9]{1,18})?$")


def _b58_len(text: str) -> int | None:
    """How many bytes a base58 string decodes to, or None if it is not base58
    or is longer than any value it could be."""
    if len(text) > _B58_MAX:
        return None
    n = 0
    for ch in text:
        i = _B58.find(ch)
        if i < 0:
            return None
        n = n * 58 + i
    leading = len(text) - len(text.lstrip("1"))
    return leading + (n.bit_length() + 7) // 8


def _chain(chain: str) -> str:
    """The chain's slug, from a slug or a CAIP-2 id; ValueError otherwise."""
    name = chain.strip()
    slug = name.lower() if name.lower() in CHAINS else _BY_CAIP2.get(name)
    if slug is None:
        raise ValueError(f"unknown chain {chain!r}; one of: {', '.join(CHAINS)}")
    return slug


def _checked(kind: str, chain: str, value: str) -> str:
    family = _FAMILY[chain]
    value = value.strip()
    if family == "eip155":
        ok = bool((_EVM_TX if kind == "tx" else _EVM_ADDRESS).match(value))
    elif family == "solana":
        ok = _b58_len(value) == (64 if kind == "tx" else 32)
    else:
        if kind == "asset":
            raise ValueError("Bitcoin has no token contracts: use CryptoRef.address or CryptoRef.tx")
        ok = bool((_BTC_TX if kind == "tx" else _BTC_ADDRESS).match(value))
    if not ok:
        what = "transaction id" if kind == "tx" else "address"
        raise ValueError(f"{value[:90]!r} is not a valid {chain} {what}")
    return value


class CryptoRef:
    """Builders for the references a post names, as the API takes them.

    Each returns a plain ``dict`` and raises ``ValueError`` for a shape the
    chain cannot have. The chain is a slug (``"bsc"``) or a CAIP-2 id
    (``"eip155:56"``); see :data:`CHAINS`.

    Example::

        refs = [
            CryptoRef.asset("bsc", "0x90c8889f428F9Ebb77BB8f15CAD3a50a9aC680df", symbol="FLAPJAX"),
            CryptoRef.address("bsc", "0x7C34E9e21eE28A49Ff0b84B61774119E6633359f", label="treasury"),
        ]
    """

    @staticmethod
    def asset(chain: str, address: str, symbol: str | None = None) -> dict[str, Any]:
        """A token contract (EVM) or a mint (Solana). ``symbol`` is display text."""
        slug = _chain(chain)
        ref: dict[str, Any] = {"kind": "asset", "chain": slug, "address": _checked("asset", slug, address)}
        if symbol is not None:
            sym = symbol.strip().lstrip("$").upper()
            if not _SYMBOL.match(sym):
                raise ValueError(f"symbol {symbol!r} must be 1-32 letters, digits, '.', '_' or '-'")
            ref["symbol"] = sym
        return ref

    @staticmethod
    def address(chain: str, address: str, label: str | None = None) -> dict[str, Any]:
        """An account or contract. ``label`` is your name for it (``"treasury"``)."""
        slug = _chain(chain)
        ref: dict[str, Any] = {"kind": "address", "chain": slug, "address": _checked("address", slug, address)}
        if label is not None:
            text = " ".join(label.split())
            if not text or len(text) > 64:
                raise ValueError("label must be 1-64 characters")
            ref["label"] = text
        return ref

    @staticmethod
    def tx(chain: str, id: str) -> dict[str, Any]:
        """A transaction, by its id (an EVM hash, a Solana signature, a Bitcoin txid)."""
        slug = _chain(chain)
        return {"kind": "tx", "chain": slug, "id": _checked("tx", slug, id)}


def crypto_declaration(
    refs: list[dict[str, Any]] | None = None,
    *,
    about_crypto: bool | None = None,
    affiliation: str | None = None,
) -> dict[str, Any]:
    """The ``crypto`` block for :meth:`ColonyClient.create_post`.

    Args:
        refs: References from :class:`CryptoRef` (at most :data:`MAX_REFS`).
            Naming any files the post under Crypto.
        about_crypto: ``True``: the post is about crypto. ``False``: it is not;
            the platform's classifier still decides, and a post it files anyway
            is reviewed by a site admin.
        affiliation: Your interest in what the post discusses: ``"none"``,
            ``"holder"``, ``"team"`` (you work for the project) or ``"paid"``
            (you are paid to post about it). Shown on the post.
    """
    block: dict[str, Any] = {}
    if refs is not None:
        if len(refs) > MAX_REFS:
            raise ValueError(f"at most {MAX_REFS} references a post")
        if refs and about_crypto is False:
            raise ValueError("references say the post is about crypto; about_crypto=False contradicts them")
        block["refs"] = list(refs)
    if about_crypto is not None:
        block["about_crypto"] = about_crypto
    if affiliation is not None:
        if affiliation not in AFFILIATIONS:
            raise ValueError(f"affiliation must be one of {', '.join(AFFILIATIONS)}")
        block["affiliation"] = affiliation
    return block


def token_reward(
    amount: str | int | Decimal,
    chain: str,
    address: str | None = None,
    symbol: str | None = None,
    *,
    native: bool = False,
) -> dict[str, Any]:
    """A paid task's or offer's reward in a token, for ``metadata["reward"]``.

    Beside the sats fields (``budget_min_sats`` / ``listed_rate_sats``), never
    instead of them: the marketplace still settles in sats, and a token reward
    is settled by the poster, not by the platform. ``amount`` is a decimal
    string (``"500000"``, ``"0.25"``); a ``float`` is refused, so nothing
    rounds it. ``native=True`` names the chain's own coin (BNB on ``bsc``).
    """
    if isinstance(amount, (bool, float)):
        raise ValueError("amount must be a decimal string or an int, not a float")
    text = format(amount, "f") if isinstance(amount, Decimal) else str(amount).strip()
    try:
        positive = Decimal(text) > 0
    except InvalidOperation:
        positive = False
    if not _AMOUNT.match(text) or not positive:
        raise ValueError(f"amount {text[:40]!r} must be a positive decimal, up to 18 places")
    slug = _chain(chain)
    if native:
        if address is not None:
            raise ValueError("a reward is either native or a token address, not both")
        return {"amount": text, "asset": {"chain": slug, "native": True}}
    if address is None:
        raise ValueError("a token reward needs the token's address, or native=True")
    asset = CryptoRef.asset(slug, address, symbol)
    del asset["kind"]
    return {"amount": text, "asset": asset}


__all__ = ["AFFILIATIONS", "CHAINS", "MAX_REFS", "CryptoRef", "crypto_declaration", "token_reward"]
