"""No test may import its helpers as ``tests.<module>``.

pytest puts the rootdir on ``sys.path`` when you run from the repo root,
so ``from tests.test_api_methods import ...`` resolves locally and
``ModuleNotFoundError: No module named 'tests'`` in CI. There is no
``tests/__init__.py``; the convention every other file follows is
``from test_api_methods import ...``.

Written after making the mistake twice in one afternoon — once in
tests/test_bootstrap.py (caught by CI) and again in
tests/test_profile_surface_parity.py, where the imports sit inside
function bodies so the local run never touched them.
"""

from __future__ import annotations

import pathlib
import re

_TESTS = pathlib.Path(__file__).parent
_BAD = re.compile(r"^\s*from\s+tests\.|^\s*import\s+tests\b", re.MULTILINE)


def test_no_test_module_imports_the_tests_package() -> None:
    offenders = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        if path.name == pathlib.Path(__file__).name:
            continue
        for m in _BAD.finditer(path.read_text()):
            line = path.read_text()[: m.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(_TESTS)}:{line}")

    assert not offenders, (
        "test file(s) import helpers via the `tests.` package:\n  "
        + "\n  ".join(offenders)
        + "\n\nThere is no tests/__init__.py. This resolves when pytest is "
        "run from the repo root and fails in CI. Use `from test_api_methods "
        "import ...` like every other file here."
    )
