"""The release-discipline gate must actually refuse things.

``RELEASING.md`` has required a standalone release PR (step 7) and an
``## Unreleased`` staging section (step 6) for a long time, and both decayed
anyway: the changelog lost its ``Unreleased`` heading entirely, and a feature
branch shipped a version bump inline on 2026-08-17. Neither produced a signal.

So the gate exists now — and a gate nobody has watched fail is just a
comment with a CI badge. These tests drive the policy function directly, one
case per way it is meant to say no.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_release_discipline import (
    RELEASE_ALLOWED,
    VERSION_FILES,
    VERSION_HEADING,
    evaluate,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_release_discipline.py"


def _eval(**kw):
    base = {
        "changed_files": ["src/colony_sdk/client.py"],
        "version_changed": False,
        "adds_version_heading": False,
        "head_ref": "feat/thing",
    }
    base.update(kw)
    return evaluate(**base)


class TestFeaturePrMayNotBumpTheVersion:
    """Part 1 of the policy — the rule that was broken on 2026-08-17."""

    def test_version_bump_on_a_feature_branch_is_refused(self):
        problems = _eval(version_changed=True)
        assert problems
        assert any("own release PR" in p for p in problems)

    def test_a_feature_pr_without_a_bump_is_fine(self):
        assert _eval() == []

    @pytest.mark.parametrize(
        "branch",
        [
            "feat/x",
            "fix/y",
            "chore/z",
            "main",
            "renovate/dep",
            "released/nope",
            # Near-misses on the release prefix must NOT be treated as releases.
            "release",
            "releases/1.0.0",
            "prerelease/1.0.0",
        ],
    )
    def test_only_the_release_prefix_grants_the_exemption(self, branch):
        """A branch merely containing the word must not unlock a version
        bump — that would make the gate bypassable by naming."""
        problems = _eval(version_changed=True, head_ref=branch)
        assert problems, f"{branch!r} was allowed to bump the version"


class TestReleasePrMustBeStandalone:
    """Part 2 — 'a standalone merge, not part of a feature branch merge'."""

    def test_a_clean_release_pr_passes(self):
        assert (
            _eval(
                head_ref="release/1.34.0",
                version_changed=True,
                changed_files=["pyproject.toml", "src/colony_sdk/__init__.py", "CHANGELOG.md"],
            )
            == []
        )

    def test_a_release_pr_carrying_source_changes_is_refused(self):
        problems = _eval(
            head_ref="release/1.34.0",
            version_changed=True,
            changed_files=["pyproject.toml", "CHANGELOG.md", "src/colony_sdk/client.py"],
        )
        assert problems
        assert any("standalone" in p for p in problems)
        assert any("src/colony_sdk/client.py" in p for p in problems)

    def test_a_release_pr_that_forgets_to_bump_is_refused(self):
        """Otherwise a mis-named branch silently skips part 1's check as
        well — the exemption would apply with nothing to exempt."""
        problems = _eval(
            head_ref="release/1.34.0",
            version_changed=False,
            changed_files=["CHANGELOG.md"],
        )
        assert problems
        assert any("unchanged" in p for p in problems)

    def test_the_allowed_set_is_exactly_version_files_plus_changelog(self):
        """Pinning the set, because widening it is how 'standalone' erodes:
        one 'just this once' addition and the rule means nothing."""
        assert {*VERSION_FILES, "CHANGELOG.md"} == RELEASE_ALLOWED

    def test_release_pr_may_not_smuggle_changes_via_ci_config(self):
        problems = _eval(
            head_ref="release/1.34.0",
            version_changed=True,
            changed_files=["pyproject.toml", ".github/workflows/release.yml"],
        )
        assert problems


class TestChangelogEntriesBatchUnderUnreleased:
    """Part 3 — the batching mechanism itself."""

    def test_a_feature_pr_opening_a_version_heading_is_refused(self):
        problems = _eval(adds_version_heading=True, changed_files=["CHANGELOG.md"])
        assert problems
        assert any("Unreleased" in p for p in problems)

    def test_a_release_pr_may_open_a_version_heading(self):
        """Promoting Unreleased to a numbered section is exactly its job."""
        assert (
            _eval(
                head_ref="release/1.34.0",
                version_changed=True,
                adds_version_heading=True,
                changed_files=["CHANGELOG.md", "pyproject.toml"],
            )
            == []
        )

    @pytest.mark.parametrize(
        "line",
        [
            "+## 1.34.0 — 2026-08-17",
            "+## v1.34.0",
            "+##   2.0.0 - 2026-01-01",
        ],
    )
    def test_the_heading_pattern_matches_real_headings(self, line):
        assert VERSION_HEADING.search(line)

    @pytest.mark.parametrize(
        "line",
        [
            "+## Unreleased",
            "+### Added",
            "+Some prose mentioning 1.34.0 in passing",
            "-## 1.33.0 — 2026-08-16",  # a REMOVED heading is not an added one
        ],
    )
    def test_the_heading_pattern_ignores_everything_else(self, line):
        assert not VERSION_HEADING.search(line)


class TestTheScriptRuns:
    """The policy function being right is worth nothing if the entry point
    is broken — the failure mode where CI reports success because the check
    crashed is the one that matters."""

    def test_it_is_executable_and_reports_on_this_repo(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--base", "HEAD", "--head", "HEAD"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert "release-discipline" in proc.stdout

    def test_help_works(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        assert "--base" in proc.stdout
