"""The registration examples must confirm from storage, not from memory.

`register_confirm` exists to prove the API key survived the write to disk. An
example that passes `begun["api_key"]` — the value still in memory — succeeds
identically whether or not that write landed, so it silently teaches every
reader to bypass the guarantee the sentence above it describes.

That defect was live in this SDK's README and in four docstrings, and in the Go
and TypeScript SDKs simultaneously, with near-identical prose. It survived
because a documentation example is executed by nobody: no test pointed at it in
any of the three languages.

This is that test. It is deliberately crude — a scan over the docs rather than
anything clever — because the failure it guards against is crude.

WHY IT LOOKS AT A WINDOW RATHER THAN A LINE
-------------------------------------------
The first version of this guard matched `begun["api_key"]` *inside* the
`register_confirm(...)` call. That is one of the two shapes the defect takes,
and it was not the one the README had::

    api_key = begun["api_key"]
    # >>> Persist api_key to durable storage NOW, then read it back. <<<
    ColonyClient.register_confirm(begun["claim_token"], api_key[-6:])

There is no `begun["api_key"]` on the confirm line, so the detector scored zero
hits on the exact text that motivated the fix. The file was still protected, but
only by the positive arm — and that arm asked whether `read_text()` appeared
ANYWHERE in the file, which in a ~6,900-line `client.py` the first unrelated
vault example would have satisfied on its behalf.

So this version reads provenance instead: within the example block, if the key
comes off the begin response, a read-back has to sit between that and the
confirm. Both shapes fail; the fixed form passes; and the check no longer
depends on a token appearing somewhere else in the file.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
# CHANGELOG is a historical record of what shipped; rewriting it to look
# correct in hindsight would be a small dishonesty, so it is out of scope.
TARGETS = [
    ROOT / "README.md",
    ROOT / "src" / "colony_sdk" / "client.py",
    ROOT / "src" / "colony_sdk" / "async_client.py",
]

#: How many lines above a confirm call count as its example block. The examples
#: run to about ten lines; fifteen leaves room without reaching the next one.
WINDOW = 15

CONFIRM = re.compile(r"register_confirm\(")
#: `def register_confirm(...)` is the method itself, not an example of calling it.
DEFINITION = re.compile(r"^\s*(?:async\s+)?def\s+register_confirm\b")
#: Comment lines are stripped before the provenance check, because the fixed
#: docstrings name `begun["api_key"]` precisely to tell you not to use it.
COMMENT_LINE = re.compile(r"(?m)^\s*#.*$")
BEGUN_KEY = re.compile(r"begun\[[\"']api_key[\"']\]")
#: Anything that fetches the key back from where it was put.
READ_BACK = re.compile(r"read_text\(|\.read\(\)|getenv\(|environ\[|read_bytes\(")


def _examples(text: str) -> list[tuple[int, str]]:
    """Every registration EXAMPLE in ``text``, as (line number, block).

    An occurrence counts as an example only when the begin response appears in
    its block. That is what separates the two things this must not flag: the
    method definitions, and the signature row in the README's API table, which
    names `register_confirm(claim_token, key_fingerprint)` with no code around
    it at all.
    """
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        if not CONFIRM.search(line) or DEFINITION.match(line):
            continue
        block = "\n".join(lines[max(0, i - WINDOW) : i + 1])
        code = COMMENT_LINE.sub("", block)
        if "begun[" not in code:
            continue
        out.append((i + 1, code))
    return out


def _confirms_from_memory(code: str) -> bool:
    """True when the fingerprint traces to the begin response with no read-back.

    Covers both shapes: the key assigned from `begun["api_key"]` earlier in the
    block, and `begun["api_key"][-6:]` passed straight into the call. In the
    second the match sits on the confirm line itself, so there is trivially
    nothing after it to read the key back.
    """
    matches = list(BEGUN_KEY.finditer(code))
    if not matches:
        return False
    return not READ_BACK.search(code[matches[-1].end() :])


@pytest.mark.parametrize("path", TARGETS, ids=lambda p: p.name)
def test_no_example_confirms_from_the_in_memory_key(path: pathlib.Path) -> None:
    text = path.read_text(encoding="utf-8")
    hits = [f"{path.name}:{line}" for line, code in _examples(text) if _confirms_from_memory(code)]
    assert not hits, (
        "A registration example takes the confirm fingerprint from the key still "
        "in memory. Persist it, read it BACK, and confirm from what you read — "
        "otherwise the example passes whether or not the write succeeded:\n  " + "\n  ".join(hits)
    )


@pytest.mark.parametrize("path", TARGETS, ids=lambda p: p.name)
def test_each_file_still_documents_the_flow(path: pathlib.Path) -> None:
    """The positive half. Absence of the anti-pattern is equally satisfied by
    deleting the example, so each file has to still carry one."""
    assert _examples(path.read_text(encoding="utf-8")), (
        f"{path.name} no longer contains a registration example. If that is "
        "deliberate, drop it from TARGETS rather than leaving a check that "
        "passes because there is nothing left to look at."
    )


# --- controls -------------------------------------------------------------
# A scanner that cannot go red certifies nothing, and this one is a regex over
# prose. Both shapes of the defect are pinned against the detector directly,
# including the assignment form that the previous version of this guard missed.

ASSIGNMENT_FORM = """\
begun = ColonyClient.register_begin("my-agent", "My Agent", "What I do")
api_key = begun["api_key"]

# >>> Persist api_key to durable storage NOW, then read it back. <<<

ColonyClient.register_confirm(begun["claim_token"], api_key[-6:])
"""

INLINE_FORM = """\
begun = ColonyClient.register_begin("my-agent", "My Agent", "What I do")
ColonyClient.register_confirm(begun["claim_token"], begun["api_key"][-6:])
"""

FIXED_FORM = """\
begun = ColonyClient.register_begin("my-agent", "My Agent", "What I do")
key_path.write_text(begun["api_key"])
api_key = key_path.read_text().strip()
ColonyClient.register_confirm(begun["claim_token"], api_key[-6:])
"""

#: The README's API table. Names the method with no example around it, and must
#: not be mistaken for one.
SIGNATURE_ROW = (
    "| `ColonyClient.register_confirm(claim_token, key_fingerprint)` | Step 2: "
    "prove you kept the key (its last 6 chars) and activate the account. |\n"
)


@pytest.mark.parametrize(
    "label,source,expected",
    [
        ("assignment form", ASSIGNMENT_FORM, True),
        ("inline form", INLINE_FORM, True),
        ("fixed form", FIXED_FORM, False),
    ],
)
def test_the_detector_fires_on_both_shapes(label: str, source: str, expected: bool) -> None:
    examples = _examples(source)
    assert examples, f"{label}: not recognised as an example at all"
    assert any(_confirms_from_memory(code) for _, code in examples) is expected, (
        f"{label}: detector expected to {'fire' if expected else 'stay quiet'} and did not"
    )


def test_a_signature_row_is_not_an_example() -> None:
    """Otherwise the README's API table would be asked to demonstrate a flow."""
    assert not _examples(SIGNATURE_ROW)


def test_a_method_definition_is_not_an_example() -> None:
    source = "begun = {}\n    def register_confirm(cls, claim_token: str, key_fingerprint: str):\n"
    assert not _examples(source)
