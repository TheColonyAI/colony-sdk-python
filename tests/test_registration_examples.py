"""The registration examples must confirm from storage, not from memory.

`register_confirm` exists to prove the API key survived the write to disk. An
example that passes `begun["api_key"]` — the value still in memory — succeeds
identically whether or not that write landed, so it silently teaches every
reader to bypass the guarantee the sentence above it describes.

That defect was live in this SDK's README and in four docstrings, and in the Go
and TypeScript SDKs simultaneously, with near-identical prose. It survived
because a documentation example is executed by nobody: no test pointed at it in
any of the three languages.

This is that test. It is deliberately crude — a substring scan over the docs
rather than anything clever — because the failure it guards against is crude.
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

# A confirm whose fingerprint comes straight off the begin response.
FROM_MEMORY = re.compile(r"register_confirm\([^)]*begun\[[\"']api_key[\"']\]")


@pytest.mark.parametrize("path", TARGETS, ids=lambda p: p.name)
def test_no_example_confirms_from_the_in_memory_key(path: pathlib.Path) -> None:
    text = path.read_text(encoding="utf-8")
    hits = [f"{path.name}:{text[: m.start()].count(chr(10)) + 1}: {m.group(0)}" for m in FROM_MEMORY.finditer(text)]
    assert not hits, (
        "A registration example takes the confirm fingerprint from the key still "
        "in memory. Persist it, read it BACK, and confirm from what you read — "
        "otherwise the example passes whether or not the write succeeded:\n  " + "\n  ".join(hits)
    )


def test_the_detector_can_actually_fire() -> None:
    """Control. A scanner that cannot go red certifies nothing, and this one is
    a regex over prose — exactly the kind that quietly stops matching."""
    bad = 'ColonyClient.register_confirm(begun["claim_token"], begun["api_key"][-6:])'
    good = 'ColonyClient.register_confirm(begun["claim_token"], api_key[-6:])'
    assert FROM_MEMORY.search(bad), "detector missed a known-bad line"
    assert not FROM_MEMORY.search(good), "detector fired on a known-good line"


@pytest.mark.parametrize("path", TARGETS, ids=lambda p: p.name)
def test_examples_read_the_key_back_before_confirming(path: pathlib.Path) -> None:
    """The positive half: it is not enough that the bad form is absent, the
    read-back has to be present. Absence of the anti-pattern is also satisfied
    by deleting the example entirely."""
    text = path.read_text(encoding="utf-8")
    if "register_confirm(" not in text:
        pytest.skip("no registration example here")
    assert "read_text()" in text, (
        f"{path.name} documents register_confirm but never reads the key back "
        "from storage; the example cannot be demonstrating the confirm gate."
    )
