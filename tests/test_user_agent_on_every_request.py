"""Every outbound request identifies itself as the SDK.

``register_begin`` and ``register_confirm`` did not. They are
``@staticmethod``s — you call them before you have an api_key, so there
is no instance and they bypass the shared request helper. Anything added
centrally therefore misses them, which is the actual defect; the missing
User-Agent was one symptom of it.

What went out instead was the transport default: ``Python-urllib/3.x``
on the sync path, ``python-httpx/x.y`` on the async one. Not absent, so a
"block empty UA" rule would not have caught it — but a generic scripting
signature is scored *harder* than a missing one by most bot rulesets, and
registration is the FIRST call an agent ever makes. An agent blocked
there has no key, no session and no support path, and experiences it as
"the API is down" while every other endpoint works fine.

Two checks, because they fail differently:

* a **static** sweep, so a NEW request site cannot ship without the
  header — the whole point, since the gap was created by adding methods
  that skipped the shared helper;
* a **behavioural** check against a local listener, which reads the
  header off the wire rather than off the source. A static test alone
  would pass on a header dict that some later transport call ignores.
"""

from __future__ import annotations

import ast
import contextlib
import http.server
import json
import pathlib
import threading
from typing import ClassVar

import pytest

from colony_sdk import ColonyClient, __version__

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "colony_sdk"
EXPECTED_PREFIX = "colony-sdk-python/"


# --------------------------------------------------------------- static
def _request_sites(path: pathlib.Path) -> list[tuple[int, str]]:
    """(line, enclosing function) for every outbound HTTP call."""
    tree = ast.parse(path.read_text())
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def enclosing(node: ast.AST) -> str:
        cur = node
        while cur in parents:
            cur = parents[cur]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur.name
        return "<module>"

    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", getattr(fn, "id", ""))
        # urllib Request construction, and httpx verb calls.
        if name == "Request" or (
            name in {"post", "get", "put", "patch", "delete"}
            and isinstance(fn, ast.Attribute)
            and getattr(fn.value, "id", "") in {"client", "session", "_client"}
        ):
            sites.append((node.lineno, enclosing(node)))
    return sites


@pytest.mark.parametrize("filename", ["client.py", "async_client.py"])
def test_every_request_site_sets_the_sdk_user_agent(filename: str) -> None:
    path = SRC / filename
    src_lines = path.read_text().splitlines()
    sites = _request_sites(path)
    assert sites, f"found no request sites in {filename} — the scan is broken"

    missing = []
    for lineno, func in sites:
        # The header may be built a few lines above the call (a headers
        # dict) or inline in it. Look at the enclosing call expression
        # plus a short lead-in.
        window = "\n".join(src_lines[max(0, lineno - 30) : lineno + 12])
        if "User-Agent" not in window:
            missing.append(f"{filename}:{lineno} in {func}()")

    assert not missing, (
        "outbound request(s) with no SDK User-Agent:\n  "
        + "\n  ".join(missing)
        + f"\n\nSet 'User-Agent': f'{EXPECTED_PREFIX}{{__version__}}'. "
        "Transport defaults (Python-urllib/…, python-httpx/…) are generic "
        "scripting signatures that bot rulesets score against."
    )


def test_the_scan_would_notice_a_missing_header() -> None:
    """Anti-vacuity: the check above is a substring test over a window,
    which is exactly the shape that passes when it should not. Prove the
    predicate is capable of failing."""
    window_without = 'req = Request(url, data=payload, method="POST")'
    assert "User-Agent" not in window_without


# ---------------------------------------------------------- behavioural
class _Capture(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[dict[str, str | None]] = {}

    def do_POST(self) -> None:
        _Capture.seen[self.path] = self.headers.get("User-Agent")
        body = json.dumps(
            {
                "api_key": "col_" + "x" * 43,
                "claim_token": "rct_x",
                "status": "pending",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def listener():
    _Capture.seen = {}
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/api/v1"
    srv.shutdown()


def test_registration_sends_the_sdk_user_agent_on_the_wire(listener) -> None:
    """Reads the header off an actual request. The static sweep cannot
    tell whether a headers dict is passed to the transport or quietly
    dropped by it."""
    # Only the captured header matters; the fake response is not a real
    # registration, so the SDK may well reject it.
    with contextlib.suppress(Exception):
        ColonyClient.register_begin("a", "A", "bio", base_url=listener)
    with contextlib.suppress(Exception):
        ColonyClient.register_confirm("rct_x", "abc123", base_url=listener)

    assert _Capture.seen, "the listener saw no requests at all"
    for path, ua in _Capture.seen.items():
        assert ua and ua.startswith(EXPECTED_PREFIX), f"{path} sent User-Agent {ua!r}; expected {EXPECTED_PREFIX}*"
        assert __version__ in ua, f"{path} sent {ua!r}, which does not carry the SDK version"
