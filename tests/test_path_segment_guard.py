"""Every caller-supplied value in a request path is guarded, on BOTH clients.

WHY THIS EXISTS
===============
``quote()`` leaves dots alone, and a URL resolver (nginx in front of the API,
and httpx itself on the async client) collapses ``.`` and ``..``. Most ids
were not even escaped: they were interpolated raw, so
``get_post("../users/me")`` built ``/posts/../users/me`` and the authenticated
request reached ``/users/me``. An agent that passes a value it read from
content (a post id, a username, an org slug) could be steered to a different
endpoint with its own credentials. #205 guarded the project methods and a few
older ones; this file covers the rest, and keeps it covered.

The list of methods is NOT hand-written. It is discovered:

* every public method of ``ColonyClient`` and ``AsyncColonyClient`` is called
  with a distinct sentinel in each argument, against a recording transport
  (nothing reaches the network), and an argument whose sentinel turns up in
  the request PATH is a path parameter;
* ``test_every_path_building_method_was_probed`` fails if a method that calls
  ``_path_segment`` / ``_project_path`` could not be probed (add an entry to
  ``OVERRIDES``), so a new method cannot quietly drop out of the table;
* ``TestNoRawInterpolation`` fails on any request-path f-string that
  interpolates something other than ``_path_segment(...)`` /
  ``_project_path(...)`` / ``urlencode(...)``, or a local built only from those.

Then, for every (method, path parameter):

* ``..``, ``.``, ``""`` and values containing ``/`` are refused with
  ``ValueError`` and NO request (no token exchange either);
* the controls -- ``...``, ``a.b``, ``.well-known``, a UUID, a hyphenated
  username, and ``a b#c`` -- are still sent, as one escaped segment.

The vault filename is the one value that may contain ``/`` (the route is
``{filename:path}``): it keeps its separators, and no segment of it may be
empty, ``.`` or ``..``.

Red-checked by disabling ``_check_path_segment``: the refusal tests fail for
every method that has no other local validator in front of its path.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import sys
import time
import uuid
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx

from colony_sdk import ColonyClient, RetryConfig
from colony_sdk.async_client import AsyncColonyClient
from colony_sdk.client import _check_path_segment, _path_segment

SRC = Path(__file__).parent.parent / "src" / "colony_sdk"
BASE = "https://thecolony.ai/api/v1"
_NS = uuid.UUID("6f1c0c38-7a51-4a2e-9d3c-1d5c6b0f0a11")

#: Transport-state helpers and static constructors: they build no
#: caller-addressed path (``register_*`` take a base URL, not a client).
NOT_PROBED = {
    "clear_cache",
    "enable_cache",
    "enable_circuit_breaker",
    "on_request",
    "on_response",
    "refresh_token",
    "register_begin",
    "register_confirm",
    "close",
    "aclose",
}

#: Arguments the automatic filler cannot guess: a validator in front of the
#: path that a sentinel would fail. ``SENTINEL`` means "probe this argument",
#: for an optional one that is really the path parameter.
SENTINEL = object()
OVERRIDES: dict[str, dict[str, Any]] = {
    "create_comment": {"body": "a comment"},
    "create_puzzle": {"puzzle_type": "logic"},
    "react_post": {"emoji": "fire"},
    "react_comment": {"emoji": "heart"},
    "report_post": {"reason": "spam"},
    "report_comment": {"reason": "spam"},
    "report_project": {"reason": "spam"},
    "report_wiki_page": {"reason": "spam"},
    "accept_colony_mod_invitation": {"invitation_id": SENTINEL},
    "decline_colony_mod_invitation": {"invitation_id": SENTINEL},
    "revoke_colony_mod_invitation": {"invitation_id": SENTINEL},
    "crosspost": {"colony": SENTINEL},
    "mod_queue_action": {"source": "post", "action": "approve"},
    "search_group_messages": {"query": "hello"},
    "update_webhook": {"secret": "a-long-enough-secret-value"},
    "vote_poll": {"option_ids": ["opt1"]},
    "exchange_token": {"subject_token": "aaa.bbb.ccc"},
}

REFUSED = ["..", ".", "", "a/b", "../users/me", "x/.."]
VAULT_REFUSED = ["..", ".", "", "a/../b.md", "a/./b.md", "a//b.md", "/a.md", "a.md/", "../a.md"]
CONTROL_UUID = "b01f554e-769b-43d0-89d7-1a2eeb7bd912"
CONTROLS = ["...", "a.b", ".well-known", CONTROL_UUID, "colonist-one", "a b#c"]
VAULT_CONTROLS = ["notes/a b.md", ".well-known/x.md", "a..b/c.md", "notes.md"]

#: Arguments with a stricter local validator in front of the path, which
#: refuses the dotted controls (rightly: no such value exists) before the path
#: is built. They keep every refusal test, and get the controls their own
#: rule allows. Keep the reasons; a new entry needs one.
RESTRICTED_CONTROLS: dict[str, tuple[str, list[str]]] = {
    "colony": ("_resolve_colony_uuid looks an unknown name up on the server", [CONTROL_UUID]),
    "file_id": ("_require_file_id sends only a canonical UUID", [CONTROL_UUID]),
    "wiki slug": ("_require_wiki_slug mirrors the server's slug grammar", ["colonist-one", CONTROL_UUID]),
}


def _controls_for(method: str, param: str) -> list[str]:
    key = "wiki slug" if "wiki" in method and param == "slug" else param
    return RESTRICTED_CONTROLS[key][1] if key in RESTRICTED_CONTROLS else CONTROLS


# ---------------------------------------------------------------------------
# Recording transports. Nothing in this file can reach the network.
# ---------------------------------------------------------------------------


def _body_for(url: str) -> bytes:
    return b"[]" if "/colonies?limit=" in url else b"{}"


class _SyncWire:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, req: Any, timeout: float | None = None) -> MagicMock:
        self.urls.append(req.full_url)
        resp = MagicMock()
        resp.read.return_value = _body_for(req.full_url)
        resp.status = 200
        resp.getheaders.return_value = []
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp


def _sync_call(name: str, kwargs: dict[str, Any]) -> tuple[list[str], BaseException | None]:
    wire = _SyncWire()
    client = ColonyClient("col_test", retry=RetryConfig(max_retries=0), cache_token=False)
    client._token = "fake-jwt"
    client._token_expiry = time.time() + 9999
    err: BaseException | None = None
    with patch("colony_sdk.client.urlopen", wire), warnings.catch_warnings():
        # The probe passes every optional argument, deprecated aliases included.
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            result = getattr(client, name)(**kwargs)
            if inspect.isgenerator(result):
                next(result, None)
        except Exception as e:  # recorded; the caller decides what it means
            err = e
    return wire.urls, err


def _async_call(name: str, kwargs: dict[str, Any]) -> tuple[list[str], BaseException | None]:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, content=_body_for(str(request.url)))

    async def run() -> BaseException | None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = AsyncColonyClient("col_test", client=http, retry=RetryConfig(max_retries=0), cache_token=False)
        client._token = "fake-jwt"
        client._token_expiry = time.time() + 9999
        try:
            result = getattr(client, name)(**kwargs)
            if inspect.isasyncgen(result):
                await anext(result, None)
            elif inspect.isawaitable(result):
                await result
        except Exception as e:
            return e
        finally:
            await http.aclose()
        return None

    # The register_* constructors open their own httpx client; they are in
    # NOT_PROBED, and nothing else does.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        err = asyncio.run(run())
    return urls, err


CLIENTS: dict[str, tuple[type, Callable[[str, dict[str, Any]], tuple[list[str], BaseException | None]]]] = {
    "sync": (ColonyClient, _sync_call),
    "async": (AsyncColonyClient, _async_call),
}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _sentinel(method: str, param: str) -> str:
    return str(uuid.uuid5(_NS, f"{method}.{param}"))


def _fill(method: str, p: inspect.Parameter) -> Any:
    ann = str(p.annotation)
    if "list" in ann:
        return [_sentinel(method, p.name)]
    if "dict" in ann:
        return {}
    if ann.startswith("bool") or ann == "<class 'bool'>":
        return True
    if ann.startswith("int") or ann == "<class 'int'>":
        return 1
    if ann.startswith("float"):
        return 1.0
    if ann.startswith("bytes"):
        return b"x"
    return _sentinel(method, p.name)


def _public_methods(cls: type) -> list[str]:
    out = []
    for name in sorted(dir(cls)):
        if name.startswith("_") or name in NOT_PROBED:
            continue
        raw = inspect.getattr_static(cls, name)
        if isinstance(raw, (staticmethod, classmethod, property)) or not callable(raw):
            continue
        out.append(name)
    return out


def _params(cls: type, name: str) -> list[inspect.Parameter]:
    sig = inspect.signature(getattr(cls, name))
    return [
        p
        for p in list(sig.parameters.values())[1:]
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]


def _base_kwargs(cls: type, name: str) -> dict[str, Any]:
    overrides = OVERRIDES.get(name, {})
    kwargs: dict[str, Any] = {}
    for p in _params(cls, name):
        if p.name in overrides:
            value = overrides[p.name]
            kwargs[p.name] = _sentinel(name, p.name) if value is SENTINEL else value
        elif p.default is inspect.Parameter.empty:
            kwargs[p.name] = _fill(name, p)
    return kwargs


def _in_path(urls: list[str], needle: str) -> bool:
    return any(needle in u.split("?", 1)[0] for u in urls)


def _discover(kind: str) -> dict[str, tuple[dict[str, Any], list[str]]]:
    """method -> (kwargs that reach the wire, the parameters that land in its path)."""
    cls, call = CLIENTS[kind]
    found: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for name in _public_methods(cls):
        base = _base_kwargs(cls, name)
        candidates = [dict(base)]
        # An optional str argument may be the path parameter (a renamed kwarg,
        # or one of an either-or pair): try each on its own as well.
        for p in _params(cls, name):
            if p.name not in base and "str" in str(p.annotation):
                candidates.append({**base, p.name: _fill(name, p)})
        for kwargs in candidates:
            urls, _ = call(name, kwargs)
            if not urls:
                continue
            hits = [k for k, v in kwargs.items() if isinstance(v, str) and _in_path(urls, v)]
            if hits:
                prev = found.get(name)
                if prev is None:
                    found[name] = (kwargs, hits)
                else:  # an either-or pair: keep both, each with its own kwargs
                    for h in hits:
                        if h not in prev[1]:
                            found[f"{name}#{h}"] = (kwargs, [h])
    return found


DISCOVERED = {kind: _discover(kind) for kind in CLIENTS}

CASES = [
    pytest.param(kind, method, param, id=f"{kind}-{method.split('#')[0]}-{param}")
    for kind, table in DISCOVERED.items()
    for method, (_, params) in sorted(table.items())
    for param in params
]


def _call_with(kind: str, method: str, param: str, value: str) -> tuple[list[str], BaseException | None]:
    kwargs, _ = DISCOVERED[kind][method]
    return CLIENTS[kind][1](method.split("#")[0], {**kwargs, param: value})


def _is_vault(method: str, param: str) -> bool:
    return method.startswith("vault_") and param == "filename"


# ---------------------------------------------------------------------------
# The table is real
# ---------------------------------------------------------------------------


class TestTheTableIsComplete:
    def test_both_clients_find_the_same_path_parameters(self) -> None:
        sync = {(m, tuple(p)) for m, (_, p) in DISCOVERED["sync"].items()}
        asyn = {(m, tuple(p)) for m, (_, p) in DISCOVERED["async"].items()}
        assert sync == asyn, f"only sync: {sorted(sync - asyn)}; only async: {sorted(asyn - sync)}"

    def test_the_table_is_not_vacuous(self) -> None:
        """A floor, so a broken probe (every call raising before the wire)
        fails loudly instead of parametrising over nothing. Measured
        2026-10-24: 229 methods, 277 (method, parameter) pairs per client.
        Raise it when you add a path-building method."""
        for kind, table in DISCOVERED.items():
            methods = {m.split("#")[0] for m in table}
            pairs = sum(len(p) for _, p in table.values())
            assert len(methods) >= 229, (kind, len(methods))
            assert pairs >= 277, (kind, pairs)

    @pytest.mark.parametrize("kind,source", [("sync", "client.py"), ("async", "async_client.py")])
    def test_every_path_building_method_was_probed(self, kind: str, source: str) -> None:
        """Every method that builds a path from an argument must be in the
        table. One the probe could not reach (a validator rejected the
        sentinels) needs an ``OVERRIDES`` entry, or it would silently escape
        every test below."""
        cls = CLIENTS[kind][0]
        tree = ast.parse((SRC / source).read_text())
        builders: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == cls.__name__:
                for fn in node.body:
                    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name.startswith("_"):
                        continue
                    for sub in ast.walk(fn):
                        if (
                            isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Name)
                            and sub.func.id in ("_path_segment", "_project_path")
                        ):
                            builders.add(fn.name)
        probed = {m.split("#")[0] for m in DISCOVERED[kind]}
        missing = sorted(builders - probed)
        assert len(builders) >= 200, len(builders)
        assert not missing, f"{kind}: these build a path but the probe never reached it; add OVERRIDES: {missing}"


# ---------------------------------------------------------------------------
# Refused, before any request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind,method,param", CASES)
def test_values_that_leave_the_segment_are_refused_before_any_request(kind: str, method: str, param: str) -> None:
    values = VAULT_REFUSED if _is_vault(method, param) else REFUSED
    for value in values:
        urls, err = _call_with(kind, method, param, value)
        assert urls == [], f"{method}({param}={value!r}) sent {urls}"
        assert isinstance(err, ValueError), f"{method}({param}={value!r}) raised {err!r}, not ValueError"


# ---------------------------------------------------------------------------
# Still sent, as one escaped segment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind,method,param", CASES)
def test_names_with_dots_are_still_sent_escaped(kind: str, method: str, param: str) -> None:
    if _is_vault(method, param):
        values = VAULT_CONTROLS
        escape = lambda v: quote(v, safe="/")  # noqa: E731
    else:
        values = _controls_for(method, param)
        escape = lambda v: quote(v, safe="")  # noqa: E731
    for value in values:
        urls, err = _call_with(kind, method, param, value)
        expected = f"/{escape(value)}"
        assert any((u.split("?", 1)[0] + "/").find(expected + "/") != -1 for u in urls), (
            f"{method}({param}={value!r}) did not send {expected!r} as a segment: {urls} ({err!r})"
        )


# ---------------------------------------------------------------------------
# The helper itself, and the vault form
# ---------------------------------------------------------------------------


class TestTheHelper:
    @pytest.mark.parametrize("value", ["a/b", "/a", "a/", "../x", "x/..", "/"])
    def test_a_slash_is_refused(self, value: str) -> None:
        with pytest.raises(ValueError, match="contains '/'"):
            _path_segment(value, "post_id")

    @pytest.mark.parametrize("value", ["", ".", ".."])
    def test_dot_segments_are_refused(self, value: str) -> None:
        with pytest.raises(ValueError, match="path segment"):
            _path_segment(value, "post_id")

    @pytest.mark.parametrize("value", ["...", "a.b", ".well-known", "p1", CONTROL_UUID, "colonist-one"])
    def test_names_pass_unchanged(self, value: str) -> None:
        assert _path_segment(value, "post_id") == value

    def test_a_non_string_is_sent_as_the_f_string_sent_it(self) -> None:
        """An int or a ``uuid.UUID`` interpolated raw went through ``str()``;
        it still does, so a caller passing one is not broken."""
        assert _path_segment(7, "revision_id") == "7"
        assert _path_segment(uuid.UUID(CONTROL_UUID), "post_id") == CONTROL_UUID

    @pytest.mark.parametrize("value", VAULT_REFUSED[3:])
    def test_the_vault_form_refuses_a_bad_segment(self, value: str) -> None:
        with pytest.raises(ValueError, match="segment"):
            _path_segment(value, "filename", path=True)

    def test_the_vault_form_keeps_its_separators(self) -> None:
        assert _path_segment("notes/day 1.md", "filename", path=True) == "notes/day%201.md"

    def test_the_mock_shares_the_check(self) -> None:
        """``MockColonyClient`` calls ``_check_path_segment`` where it checks a
        path argument, so it refuses what the clients refuse."""
        from colony_sdk.testing import MockColonyClient

        with pytest.raises(ValueError, match="contains '/'"):
            _check_path_segment("a/b", "slug")
        mock = MockColonyClient()
        for call in (
            lambda: mock.get_project("a/b"),
            lambda: mock.get_project(".."),
            lambda: mock.add_project_collaborator("star-map", "a/b"),
            lambda: mock.remove_project_collaborator("star-map", ".."),
        ):
            with pytest.raises(ValueError):
                call()


# ---------------------------------------------------------------------------
# The static half: no raw interpolation in a request path
# ---------------------------------------------------------------------------

#: Calls that produce a safe piece of a URL.
_SAFE_CALLS = {"_path_segment", "_project_path", "urlencode", "_require_file_id", "_require_version_number"}


def _safe_expr(e: ast.expr, assigns: dict[str, list[ast.expr]], seen: frozenset[str] = frozenset()) -> bool:
    if isinstance(e, ast.Constant):
        return isinstance(e.value, str)
    if isinstance(e, ast.Call) and isinstance(e.func, ast.Name):
        return e.func.id in _SAFE_CALLS
    if isinstance(e, ast.IfExp):
        return _safe_expr(e.body, assigns, seen) and _safe_expr(e.orelse, assigns, seen)
    if isinstance(e, ast.JoinedStr):
        return all(
            isinstance(v, ast.Constant) or (isinstance(v, ast.FormattedValue) and _safe_expr(v.value, assigns, seen))
            for v in e.values
        )
    if isinstance(e, ast.Name) and e.id not in seen and e.id in assigns:
        return all(_safe_expr(v, assigns, seen | {e.id}) for v in assigns[e.id])
    return False


def _raw_interpolations(source: str, cls_name: str) -> list[str]:
    tree = ast.parse(source)
    problems: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == cls_name):
            continue
        for fn in node.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigns: dict[str, list[ast.expr]] = {}
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            assigns.setdefault(t.id, []).append(sub.value)
                elif (
                    isinstance(sub, (ast.AnnAssign, ast.AugAssign))
                    and isinstance(sub.target, ast.Name)
                    and sub.value is not None
                ):
                    assigns.setdefault(sub.target.id, []).append(sub.value)
            for sub in ast.walk(fn):
                if not isinstance(sub, ast.JoinedStr) or not sub.values:
                    continue
                first = sub.values[0]
                is_path = (isinstance(first, ast.Constant) and str(first.value).startswith("/")) or (
                    isinstance(first, ast.FormattedValue) and _safe_expr(first.value, {}, frozenset())
                )
                if not is_path:
                    continue
                for v in sub.values:
                    if isinstance(v, ast.FormattedValue) and not _safe_expr(v.value, assigns):
                        problems.append(f"{fn.name} (line {sub.lineno}): {{{ast.unparse(v.value)}}}")
    return problems


class TestNoRawInterpolation:
    @pytest.mark.parametrize(
        "source,cls_name", [("client.py", "ColonyClient"), ("async_client.py", "AsyncColonyClient")]
    )
    def test_every_interpolation_in_a_request_path_is_escaped(self, source: str, cls_name: str) -> None:
        problems = _raw_interpolations((SRC / source).read_text(), cls_name)
        assert not problems, (
            "A request path interpolates a value that is not escaped and checked as a path "
            "segment. Wrap it: f\"/x/{_path_segment(value, 'param')}\" (a query string goes "
            "through urlencode):\n  " + "\n  ".join(problems)
        )

    def test_the_lint_catches_a_raw_id(self) -> None:
        """The lint's own red check: the shape every one of these sites had."""
        src = (
            "class ColonyClient:\n"
            "    def get_post(self, post_id):\n"
            "        return self._raw_request('GET', f'/posts/{post_id}')\n"
            "    def tagged(self, tag):\n"
            "        suffix = f'/{tag}'\n"
            "        return self._raw_request('GET', f'/tags{suffix}')\n"
        )
        problems = _raw_interpolations(src, "ColonyClient")
        assert [p.split(" ")[0] for p in problems] == ["get_post", "tagged", "tagged"], problems


def test_the_recording_transports_reach_nothing() -> None:
    """Sanity for the harness: a call is recorded, and json in the canned body
    parses, so 'no request was made' above means what it says."""
    urls, err = _sync_call("get_post", {"post_id": CONTROL_UUID})
    assert urls == [f"{BASE}/posts/{CONTROL_UUID}"] and err is None
    urls, err = _async_call("get_post", {"post_id": CONTROL_UUID})
    assert urls == [f"{BASE}/posts/{CONTROL_UUID}"] and err is None
    assert json.loads(_body_for("/x")) == {}
