"""Extract the response schemas this SDK's models claim to mirror.

Every model in :mod:`colony_sdk.models` is a claim about what the server
sends, and its ``from_dict`` reads named keys. ``tests/test_schema_conformance.py``
checks those claims against the server's own OpenAPI document; this script
writes the part of that document the check needs to
``tests/fixtures/openapi_schemas.json``, so the check runs offline on every
pull request.

Usage::

    python scripts/gen_openapi_snapshot.py

``COLONY_OPENAPI_URL`` reads the document from somewhere else, for a server
release that is not live yet (``http://localhost:8000/openapi.json``). The
snapshot records the address it came from.

The schemas written are exactly the ones the test's ``BINDINGS`` name, by
schema or by endpoint, plus every schema those reference. The test fails on a
binding the snapshot lacks and on a schema nothing binds, so the two cannot
drift apart.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "fixtures" / "openapi_schemas.json"
SPEC_URL = "https://thecolony.ai/openapi.json"
_REF = "#/components/schemas/"


def _bindings() -> list:
    path = ROOT / "tests" / "test_schema_conformance.py"
    sys.path[:0] = [str(ROOT / "src"), str(path.parent)]
    spec = importlib.util.spec_from_file_location("test_schema_conformance", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_schema_conformance"] = module  # dataclasses look their module up there
    spec.loader.exec_module(module)
    return list(module.BINDINGS)


def response_schema(spec: dict, op: str) -> str | None:
    """The schema name an endpoint answers with: ``"GET /api/v1/x"``.

    A list response names its item schema.
    """
    method, path = op.split(" ", 1)
    operation = spec.get("paths", {}).get(path, {}).get(method.lower())
    if not operation:
        return None
    for status in ("200", "201"):
        content = operation.get("responses", {}).get(status, {}).get("content", {})
        schema = content.get("application/json", {}).get("schema") or {}
        schema = schema.get("items", schema)
        ref = schema.get("$ref", "")
        if ref.startswith(_REF):
            return ref[len(_REF) :]
    return None


def _refs(node: object) -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_REF):
            out.add(ref[len(_REF) :])
        for value in node.values():
            out |= _refs(value)
    elif isinstance(node, list):
        for value in node:
            out |= _refs(value)
    return out


def snapshot(spec: dict, bindings: list, source: str) -> dict:
    schemas = spec.get("components", {}).get("schemas", {})
    operations: dict[str, str] = {}
    wanted: set[str] = set()
    missing: list[str] = []
    for b in bindings:
        if b.op:
            name = response_schema(spec, b.op)
            if name is None:
                missing.append(b.op)
                continue
            operations[b.op] = name
            wanted.add(name)
        else:
            wanted.add(b.schema)
    missing += sorted(n for n in wanted if n not in schemas)
    if missing:
        raise SystemExit(f"the spec at {source} no longer declares: {missing}")
    closed: set[str] = set()
    todo = set(wanted)
    while todo:
        name = todo.pop()
        closed.add(name)
        todo |= _refs(schemas[name]) - closed
    return {
        "fetched_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "schemas": {name: schemas[name] for name in sorted(closed)},
        "operations": dict(sorted(operations.items())),
    }


def main() -> None:
    source = os.environ.get("COLONY_OPENAPI_URL", SPEC_URL)
    req = urllib.request.Request(source, headers={"User-Agent": "colony-sdk-python/gen-openapi-snapshot"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        spec = json.load(resp)
    out = snapshot(spec, _bindings(), source)
    SNAPSHOT.write_text(json.dumps(out, indent=1, sort_keys=False) + "\n")
    print(f"wrote {len(out['schemas'])} schemas and {len(out['operations'])} operations to {SNAPSHOT}")


if __name__ == "__main__":
    main()
