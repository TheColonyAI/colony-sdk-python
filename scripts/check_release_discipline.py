#!/usr/bin/env python3
"""Keep version bumps out of feature PRs, and keep release PRs standalone.

``RELEASING.md`` has always said the version bump goes in its own PR (step 7)
and that entries land under ``## Unreleased`` until a release promotes them
(step 6). Nothing enforced either, and both decayed: ``CHANGELOG.md`` lost its
``Unreleased`` section entirely, and a feature branch shipped a version bump
inline on 2026-08-17 without anything objecting.

A rule that lives only in a document is a rule that holds until someone is in
a hurry. This is the same rule, in a form that can fail.

The policy, in three parts:

1. **A feature PR must not change the version.** Bumping per-change makes the
   version a running commentary on the branch history rather than a statement
   about a release, and it forces a release for every merge.
2. **A release PR must change nothing else.** "Standalone" is the whole point:
   a reviewer of a release PR should be able to see the entire diff at a
   glance, and a release should be revertible without taking a feature with
   it.
3. **A feature PR must not open a new version heading in the changelog.**
   That is the batching mechanism — entries accumulate under ``## Unreleased``
   and a release promotes them together.

A release PR is identified by its branch name (``release/*``). Deliberately
not a label: a label can be added after review, which would let a PR change
meaning between approval and merge.

Run locally before pushing::

    python3 scripts/check_release_discipline.py --base main

Exit code 0 = compliant, 1 = one or more violations (printed).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

#: Files whose contents define the released version.
VERSION_FILES = ("pyproject.toml", "src/colony_sdk/__init__.py")

#: The complete set a release PR is allowed to touch.
RELEASE_ALLOWED = frozenset({*VERSION_FILES, "CHANGELOG.md"})

#: `## 1.34.0 — 2026-08-17` and friends. The em dash is what the release
#: workflow's awk extraction keys on, so the shape is load-bearing elsewhere;
#: here we only need to spot that a NEW numbered heading appeared.
VERSION_HEADING = re.compile(r"^\+##\s+v?\d+\.\d+\.\d+", re.M)

RELEASE_BRANCH_PREFIX = "release/"


def evaluate(
    *,
    changed_files: list[str],
    version_changed: bool,
    adds_version_heading: bool,
    head_ref: str,
) -> list[str]:
    """Pure policy decision. Returns a list of human-readable violations.

    Split out from the git plumbing so it can be tested against synthetic
    inputs rather than a fabricated repository — see
    ``tests/test_release_discipline.py``.
    """
    problems: list[str] = []
    is_release = head_ref.startswith(RELEASE_BRANCH_PREFIX)

    if is_release:
        stray = sorted(set(changed_files) - RELEASE_ALLOWED)
        if stray:
            problems.append(
                "A release PR must be standalone, but this one also changes:\n"
                + "".join(f"    {f}\n" for f in stray)
                + "  Move those to their own PR. A release should be "
                "reviewable at a glance and revertible without taking a "
                "feature with it."
            )
        if not version_changed:
            problems.append(
                f"Branch is '{head_ref}' but the version in "
                f"{' / '.join(VERSION_FILES)} is unchanged. A release/* PR "
                "that does not bump the version is either mis-named or "
                "unfinished."
            )
    else:
        if version_changed:
            problems.append(
                "This PR changes the package version, which belongs in its "
                "own release PR.\n"
                "  Version bumps are batched: land the change with its "
                "CHANGELOG entry under '## Unreleased', then cut a release "
                "separately on a 'release/X.Y.Z' branch.\n"
                "  See RELEASING.md."
            )
        if adds_version_heading:
            problems.append(
                "This PR adds a new '## X.Y.Z' heading to CHANGELOG.md.\n"
                "  Put the entry under '## Unreleased' instead — that is how "
                "changes accumulate into one release rather than forcing a "
                "release per merge. A release/* PR promotes the section."
            )

    return problems


def _run(*args: str) -> str:
    return subprocess.run(
        args, capture_output=True, text=True, check=True,
    ).stdout


def _version_in(ref: str, path: str) -> str | None:
    """Read the version literal from ``path`` as of ``ref``.

    Compares the VALUE, not whether the file changed: ``pyproject.toml``
    legitimately changes for dependency and tooling edits, and failing those
    would train people to route around this check.
    """
    try:
        blob = _run("git", "show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return None
    m = re.search(r'^(?:__version__|version)\s*=\s*["\']([^"\']+)["\']', blob, re.M)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--base",
        default=os.environ.get("BASE_SHA") or "origin/main",
        help="Base ref to diff against (default: $BASE_SHA or origin/main).",
    )
    ap.add_argument(
        "--head",
        default=os.environ.get("HEAD_SHA") or "HEAD",
        help="Head ref (default: $HEAD_SHA or HEAD).",
    )
    ap.add_argument(
        "--head-ref",
        default=os.environ.get("HEAD_REF") or "",
        help=(
            "Branch name of the PR head. Defaults to $HEAD_REF, then the "
            "current branch. This is what identifies a release PR."
        ),
    )
    args = ap.parse_args()

    head_ref = args.head_ref
    if not head_ref:
        head_ref = _run("git", "rev-parse", "--abbrev-ref", "HEAD").strip()

    changed = [
        f for f in _run(
            "git", "diff", "--name-only", f"{args.base}...{args.head}",
        ).splitlines() if f
    ]
    if not changed:
        print("release-discipline: no changed files; nothing to check.")
        return 0

    version_changed = any(
        _version_in(args.base, p) != _version_in(args.head, p)
        for p in VERSION_FILES
    )

    adds_version_heading = False
    if "CHANGELOG.md" in changed:
        diff = _run(
            "git", "diff", f"{args.base}...{args.head}", "--", "CHANGELOG.md",
        )
        adds_version_heading = bool(VERSION_HEADING.search(diff))

    problems = evaluate(
        changed_files=changed,
        version_changed=version_changed,
        adds_version_heading=adds_version_heading,
        head_ref=head_ref,
    )

    if not problems:
        kind = (
            "release" if head_ref.startswith(RELEASE_BRANCH_PREFIX) else "feature"
        )
        print(f"release-discipline: OK ({kind} PR, {len(changed)} file(s) changed)")
        return 0

    print("release-discipline: FAILED\n", file=sys.stderr)
    for p in problems:
        print(f"  - {p}\n", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
