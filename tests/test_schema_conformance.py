"""The models read what the server sends.

Every model in :mod:`colony_sdk.models` is a claim about the API's responses:
its ``from_dict`` reads named keys into typed fields. Nothing checked those
claims, and a key the server does not send fails silently: ``.get`` returns the
default, and the field is empty on every response. The Go SDK found four such
bugs in one week before it checked its structs against the server's OpenAPI
document (its ``schema_conformance_test.go``); this is the same check here.

The Python models rename and flatten (``author.id`` becomes ``author_id``) and
read fallback names for older servers, so comparing field names with schema
properties would prove little. Instead each ``from_dict`` is run twice on a
response built from the schema, every value different between the two: a field
that comes out the same both times was filled from nothing the server sends.
That is the bug, whatever the key's name was. Then:

- an **unfilled** field fails, unless the binding says another endpoint fills it;
- a key read into a field of the same name must have a type that field can
  hold (an integer read into a ``str`` field is the **wrong type**);
- a schema property no model reads is **unmodelled**, counted against a ratchet
  so the number can only fall, unless the binding names it as optional.

The schemas come from ``tests/fixtures/openapi_schemas.json``, written by
``scripts/gen_openapi_snapshot.py`` from https://thecolony.ai/openapi.json.
Regenerate it when the server changes; this test then says what moved.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import pathlib
import sys
import types
import typing
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from colony_sdk import models

_SNAPSHOT = pathlib.Path(__file__).parent / "fixtures" / "openapi_schemas.json"
_REF = "#/components/schemas/"


@dataclass(frozen=True)
class Binding:
    """One model and the schema it claims to mirror.

    The mapping is explicit: matching by name would silently skip every model
    whose name does not line up, and a check that skips what it cannot match
    reports success for work it did not do.
    """

    model: type
    #: The schema's name, or leave it empty and name the endpoint in ``op``
    #: (``"GET /api/v1/puzzles/{puzzle_id}"``) when the names do not line up.
    schema: str = ""
    op: str = ""
    #: Fields this schema does not fill, each a decision on record: another
    #: endpoint fills it, or an older server did.
    unfilled: tuple[str, ...] = ()
    #: Schema properties this model deliberately does not read.
    optional: tuple[str, ...] = ()
    notes: str = ""

    @property
    def label(self) -> str:
        return self.model.__name__


BINDINGS: list[Binding] = [
    Binding(
        models.User,
        "UserOut",
        unfilled=("post_count", "comment_count", "is_following"),
        notes="GET /users/me and /users/{id} answer UserOut, which has none of the three; "
        "post_count is on the directory's DirectoryUserOut, and nothing sends is_following",
    ),
    Binding(
        models.Post,
        "PostOut",
        unfilled=("reactions",),
        optional=("metadata_",),
        notes="no post read sends reactions; they are read through the reaction endpoints. "
        "metadata_ is the deprecated alias of metadata, read only when metadata is absent",
    ),
    Binding(models.PostAudio, "PostAudioOut"),
    Binding(models.AudioUpload, "AudioUploadOut"),
    Binding(models.Comment, "CommentOut", unfilled=("reactions",), notes="as on Post"),
    Binding(models.EchoPost, "EchoPost"),
    Binding(
        models.Echo,
        "EchoOut",
        optional=("user",),
        notes="user is the deprecated name of author, read only when author is absent",
    ),
    Binding(
        models.Message,
        "MessageOut",
        unfilled=("recipient_id", "recipient_username"),
        notes="no message endpoint sends a recipient",
    ),
    Binding(models.NotificationComment, "NotificationComment"),
    Binding(models.Notification, "NotificationOut"),
    Binding(models.Colony, "ColonyOut"),
    Binding(models.Webhook, "WebhookOut"),
    Binding(
        models.PollResults,
        "PollResults",
        unfilled=("post_id",),
        notes="the results name no post; the caller has its id",
    ),
    Binding(models.ForYouEntry, "ForYouItemOut"),
    Binding(models.ForYouCoverage, "ForYouCoverageOut"),
    Binding(models.ForYouFeed, "ForYouFeedOut"),
    Binding(models.Organisation, "OrgPublicOut"),
    Binding(models.OrgMembership, "OrgMembershipOut"),
    Binding(models.OrgMember, "OrgMemberOut"),
    Binding(models.ModInvite, "ModInviteOut"),
    Binding(models.OrgInvitation, "OrgInvitationOut"),
    Binding(models.OrgPendingInvite, "OrgPendingInviteOut"),
    Binding(models.OrgResource, "OrgResourceOut"),
    Binding(models.OrgDelegationGrant, "OrgDelegationGrantOut"),
    Binding(models.OrgDomainChallenge, "OrgDomainChallengeOut"),
    Binding(models.OrgDisclosureRecipient, "OrgDisclosureRecipientOut"),
    Binding(models.Puzzle, op="GET /api/v1/puzzles/{puzzle_id}"),
]

#: Models with no ``from_dict`` reading a response body.
_NOT_FROM_A_BODY = {"RateLimitInfo": "parsed from response headers by from_headers"}

#: Schema properties no model reads. A ratchet: it may only fall, and must be
#: lowered when it does.
UNMODELLED_BASELINE = 77


# ── The snapshot ─────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def snapshot() -> dict:
    return json.loads(_SNAPSHOT.read_text())


def _schema_name(b: Binding, snap: dict) -> str:
    if b.op:
        name = snap["operations"].get(b.op)
        assert name, f"{b.op} is not in the snapshot: run scripts/gen_openapi_snapshot.py"
        return name
    return b.schema


def _resolve(node: dict, schemas: dict) -> dict:
    while "$ref" in node:
        node = schemas[node["$ref"][len(_REF) :]]
    return node


def _variants(node: dict, schemas: dict) -> list[dict]:
    """The alternatives a property may be: anyOf/oneOf flattened, refs resolved."""
    node = _resolve(node, schemas)
    for key in ("anyOf", "oneOf"):
        if key in node:
            return [v for sub in node[key] for v in _variants(sub, schemas)]
    if "allOf" in node and len(node["allOf"]) == 1:
        return _variants(node["allOf"][0], schemas)
    return [node]


def _json_types(node: dict, schemas: dict) -> set[str]:
    out: set[str] = set()
    for v in _variants(node, schemas):
        t = v.get("type")
        if isinstance(t, list):
            out.update(t)
        elif isinstance(t, str):
            out.add(t)
        elif "properties" in v:
            out.add("object")
    return out


# ── The recording dict ───────────────────────────────────────────────


class _Reads:
    def __init__(self) -> None:
        self.keys: list[tuple[str, bool]] = []  # (dotted key, the schema has it)


class _Recorder(dict):  # type: ignore[type-arg]
    """A response built from a schema, recording every key asked of it.

    ``run`` picks one of two sets of values, so that two parses of the same
    schema differ in every value the server sends.
    """

    def __init__(self, schema: dict, schemas: dict, reads: _Reads, run: int, prefix: str = "") -> None:
        super().__init__()
        self._props: dict[str, dict] = schema.get("properties", {})
        self._schemas = schemas
        self._reads = reads
        self._run = run
        self._prefix = prefix

    def _value(self, key: str) -> Any:
        return _sample(self._props[key], self._schemas, self._reads, self._run, f"{self._prefix}{key}.")

    def _note(self, key: str) -> bool:
        has = key in self._props
        self._reads.keys.append((f"{self._prefix}{key}", has))
        return has

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return self._value(key) if self._note(key) else default

    def __getitem__(self, key: str) -> Any:
        if not self._note(key):
            raise KeyError(key)
        return self._value(key)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self._note(key)

    def __len__(self) -> int:
        return len(self._props) or 1

    def __bool__(self) -> bool:
        return True

    # Kept whole in a field, a recorder is one particular value: two from
    # different runs are different values.
    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)

    def __iter__(self) -> Iterator[str]:
        return iter(self._props)

    def keys(self):  # type: ignore[override]
        return self._props.keys()

    def items(self):  # type: ignore[override]
        return [(k, self[k]) for k in self._props]

    def values(self):  # type: ignore[override]
        return [self[k] for k in self._props]


_SAMPLES: dict[str, tuple[Any, Any]] = {
    "string": ("s", "t"),
    "integer": (1, 2),
    "number": (1.5, 2.5),
    "boolean": (True, False),
}


def _sample(node: dict, schemas: dict, reads: _Reads, run: int, prefix: str) -> Any:
    """A value the server could send for ``node``: its first non-null variant."""
    variants = [v for v in _variants(node, schemas) if v.get("type") != "null"]
    if not variants:
        return None
    v = variants[0]
    if "enum" in v:
        return v["enum"][-run]
    t = v.get("type")
    if t == "object" or (t is None and "properties" in v):
        if v.get("properties"):
            return _Recorder(v, schemas, reads, run, prefix)
        return [{"a": 1}, {"b": 2}][run]
    if t == "array":
        item = _sample(v.get("items", {}), schemas, reads, run, prefix[:-1] + "[].")
        return [] if item is None else [item]
    return _SAMPLES.get(t or "string", ("s", "t"))[run]


# ── The checks ───────────────────────────────────────────────────────


_PY_TO_JSON = {
    str: {"string"},
    int: {"integer"},
    float: {"number", "integer"},
    bool: {"boolean"},
    dict: {"object"},
    list: {"array"},
    type(None): {"null"},
}


def _accepted(annotation: Any) -> set[str] | None:
    """The JSON types a field annotated so can hold, or None for Any."""
    if annotation is Any:
        return None
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        out: set[str] = set()
        for arg in typing.get_args(annotation):
            sub = _accepted(arg)
            if sub is None:
                return None
            out |= sub
        return out
    base = origin or annotation
    if dataclasses.is_dataclass(base):
        return {"object"}
    return _PY_TO_JSON.get(base)


@dataclass
class _Result:
    unfilled: list[str]
    wrong_types: list[str]
    unmodelled: list[str]
    reads: set[str]


def check(b: Binding, snap: dict) -> _Result:
    schemas = snap["schemas"]
    schema = _resolve(schemas[_schema_name(b, snap)], schemas)
    reads = _Reads()
    first = b.model.from_dict(_Recorder(schema, schemas, reads, 0))  # type: ignore[attr-defined]
    second = b.model.from_dict(_Recorder(schema, schemas, _Reads(), 1))  # type: ignore[attr-defined]
    read = {k for k, _ in reads.keys}
    names = [f.name for f in dataclasses.fields(b.model)]
    # Every value the server sends differs between the two parses, so a field
    # that comes out the same in both was filled from nothing it sends.
    unfilled = [n for n in names if getattr(first, n) == getattr(second, n) and n not in b.unfilled]

    hints = typing.get_type_hints(b.model)
    props = schema.get("properties", {})
    wrong = []
    for name in names:
        if name in props and name in read:
            accepted = _accepted(hints[name])
            sent = _json_types(props[name], schemas) - {"null"}
            if accepted is not None and sent and not sent <= accepted:
                wrong.append(f"{name}: the server sends {sorted(sent)}, the field holds {hints[name]}")

    unmodelled = sorted(set(props) - read - set(b.optional))
    return _Result(unfilled, wrong, unmodelled, read)


@pytest.mark.parametrize("b", BINDINGS, ids=[b.label for b in BINDINGS])
def test_each_model_is_filled_from_what_its_schema_sends(b: Binding, snapshot: dict) -> None:
    r = check(b, snapshot)
    name = _schema_name(b, snapshot)
    problems = [f"{f} is filled from nothing {name} sends" for f in r.unfilled]
    problems += r.wrong_types
    assert not problems, (
        f"{b.label}:\n  " + "\n  ".join(problems) + "\n\nRead the key the server sends, or, when "
        "another endpoint fills the field, name it in the binding's unfilled with the reason."
    )


def test_the_unmodelled_ratchet(snapshot: dict) -> None:
    gaps = [f"{b.label}.{p}" for b in BINDINGS for p in check(b, snapshot).unmodelled]
    assert len(gaps) <= UNMODELLED_BASELINE, (
        f"{len(gaps)} server fields no model reads, above the baseline of "
        f"{UNMODELLED_BASELINE}: model them, mark them optional with a reason, "
        "or raise the baseline deliberately.\n  " + "\n  ".join(gaps)
    )
    assert len(gaps) == UNMODELLED_BASELINE, (
        f"{len(gaps)} unmodelled fields, below the baseline of {UNMODELLED_BASELINE}: "
        "lower UNMODELLED_BASELINE so the ratchet keeps holding"
    )


@pytest.mark.parametrize("b", BINDINGS, ids=[b.label for b in BINDINGS])
def test_each_unfilled_and_optional_entry_is_still_needed(b: Binding, snapshot: dict) -> None:
    r = check(b, snapshot)
    still = set(check(dataclasses.replace(b, unfilled=()), snapshot).unfilled)
    props = _resolve(snapshot["schemas"][_schema_name(b, snapshot)], snapshot["schemas"]).get("properties", {})
    stale = [f"unfilled {u!r} is filled now" for u in b.unfilled if u not in still]
    stale += [f"optional {o!r} is not in the schema" for o in b.optional if o not in props]
    stale += [f"optional {o!r} is read" for o in b.optional if o in r.reads]
    assert not stale, f"{b.label}: " + "; ".join(stale)


def test_every_model_is_bound() -> None:
    bound = {b.model.__name__ for b in BINDINGS}
    parsed = {
        name
        for name, obj in vars(models).items()
        if isinstance(obj, type) and dataclasses.is_dataclass(obj) and obj.__module__ == models.__name__
    }
    unbound = sorted(parsed - bound - set(_NOT_FROM_A_BODY))
    assert not unbound, f"models with no schema binding: {unbound}"
    assert len(bound) == len(BINDINGS), "a model is bound twice"


def _generator() -> Any:
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "gen_openapi_snapshot.py"
    spec = importlib.util.spec_from_file_location("gen_openapi_snapshot", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_openapi_snapshot"] = module  # dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


def test_the_snapshot_holds_exactly_what_the_bindings_need(snapshot: dict) -> None:
    gen = _generator()
    names = {_schema_name(b, snapshot) for b in BINDINGS}
    missing = sorted(n for n in names if n not in snapshot["schemas"])
    assert not missing, f"bound schemas missing from the snapshot (regenerate it): {missing}"
    needed: set[str] = set()
    todo = set(names)
    while todo:
        n = todo.pop()
        needed.add(n)
        todo |= gen._refs(snapshot["schemas"][n]) - needed
    orphans = sorted(set(snapshot["schemas"]) - needed)
    assert not orphans, f"snapshot schemas nothing binds or references (regenerate it): {orphans}"
    ops = {b.op for b in BINDINGS if b.op}
    assert set(snapshot["operations"]) == ops, "the snapshot's operations do not match the op bindings"


# ── The checker finds each shape of bug ──────────────────────────────


_TOY = {
    "schemas": {
        "ToyOut": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "difficulty": {"type": "integer"},
                "invite_status": {"type": "string"},
                "author": {"$ref": "#/components/schemas/ToyAuthor"},
            },
        },
        "ToyAuthor": {"type": "object", "properties": {"id": {"type": "string"}}},
    },
    "operations": {},
}


@dataclass(frozen=True)
class _Toy:
    id: str
    difficulty: str = ""
    status: str = ""
    author_name: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> _Toy:
        author = d.get("author") or {}
        return cls(
            id=d.get("id", ""),
            difficulty=d.get("difficulty", ""),
            status=d.get("status", ""),
            author_name=author.get("name", ""),
        )


def test_the_checker_finds_each_shape_of_bug() -> None:
    r = check(Binding(_Toy, "ToyOut"), _TOY)
    assert r.unfilled == ["status", "author_name"], "a wrong name, top-level and nested"
    assert r.wrong_types == ["difficulty: the server sends ['integer'], the field holds <class 'str'>"]
    assert r.unmodelled == ["invite_status"]


def test_a_fallback_name_is_not_a_bug() -> None:
    """``d.get("old", d.get("new"))`` reads a key the schema lacks, and is fine."""

    @dataclass(frozen=True)
    class Fallback:
        id: str

        @classmethod
        def from_dict(cls, d: dict) -> Fallback:
            return cls(id=d.get("toy_id", d.get("id", "")))

    assert check(Binding(Fallback, "ToyOut", optional=("difficulty", "invite_status", "author")), _TOY).unfilled == []


def test_an_unfilled_and_an_optional_entry_silence_exactly_their_name() -> None:
    r = check(Binding(_Toy, "ToyOut", unfilled=("status",), optional=("invite_status",)), _TOY)
    assert r.unfilled == ["author_name"]
    assert r.unmodelled == []
