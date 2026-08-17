#!/usr/bin/env python3
"""Apply branch protection to `main`. Requires ADMIN on the repo.

Kept in the repo, and *derived from the workflow file*, because the drift this
prevents already happened. On 2026-08-17 `CONTRIBUTING.md` stated that `lint`,
`typecheck` and all four `test` matrix entries were required status checks on
`main`. The live setting required three: `test (3.10)`, `test (3.12)`,
`test (3.13)`. Ruff and mypy failures did not block a merge, and had not for
an unknown length of time, because branch protection keeps no history and
nothing compared the claim to the setting.

So the required contexts are not typed out here. They are read from
`.github/workflows/ci.yml` **as it exists on the base branch**, which is the
only set that can actually report on a PR targeting it. That also makes the
usual two-phase problem disappear: a CI job added on a feature branch is not
required until it has landed on `main`, at which point re-running this picks
it up. Requiring a check that no PR can run leaves every PR permanently
unmergeable, waiting for a status that will never arrive.

    python3 scripts/apply_branch_protection.py            # show current vs planned
    python3 scripts/apply_branch_protection.py --apply    # write it
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

REPO = "TheColonyAI/colony-sdk-python"
BRANCH = "main"
WORKFLOW = ".github/workflows/ci.yml"


def _gh(*args: str, check: bool = True) -> tuple[int, str, str]:
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed:\n{p.stderr}")
    return p.returncode, p.stdout, p.stderr


def ci_job_contexts(ref: str) -> list[str]:
    """Status-check context names for every job in ``ci.yml`` at ``ref``.

    A matrix job reports one context per combination, named ``job (value)`` —
    which is why the four `test` entries are separate contexts and why three
    of them being required while the fourth was not went unnoticed.
    """
    try:
        import yaml
    except ImportError:  # pragma: no cover - maintainer script
        raise SystemExit(
            "PyYAML is required to read the workflow: pip install pyyaml"
        ) from None

    blob = subprocess.run(
        ["git", "show", f"{ref}:{WORKFLOW}"],
        capture_output=True, text=True, check=True,
    ).stdout
    doc = yaml.safe_load(blob)

    contexts: list[str] = []
    for name, job in doc.get("jobs", {}).items():
        matrix = job.get("strategy", {}).get("matrix", {})
        # Only single-axis matrices are used here; a second axis would produce
        # combined names like `test (3.12, ubuntu)` and this would need to
        # build the product. Fail loudly rather than silently under-require.
        if len(matrix) > 1:
            raise SystemExit(
                f"job {name!r} has a multi-axis matrix ({sorted(matrix)}); "
                "context names need the full product — update this script."
            )
        if matrix:
            for value in next(iter(matrix.values())):
                contexts.append(f"{name} ({value})")
        else:
            contexts.append(name)
    return contexts


def build_payload(contexts: list[str]) -> dict:
    return {
        "required_status_checks": {
            # `strict` (require the branch to be up to date before merging) is
            # deliberately left off: it forces a rebase of every open PR on
            # each merge to main, which is real friction that nobody asked
            # for. Turn it on if a stale-base merge ever actually bites.
            "strict": False,
            "contexts": contexts,
        },
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            # A new push after approval clears the approval. Without this,
            # "approved" can describe a diff nobody read.
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False,
        },
        # GitHub does not permit approving your own PR, so with review
        # required, an admin who is the only available reviewer could not
        # merge anything at all. Leaving admins unbound keeps a break-glass
        # path. Worth flipping to True once two reviewers are reliably around.
        "enforce_admins": False,
        "restrictions": None,
        "allow_force_pushes": False,
        "allow_deletions": False,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Write the settings.")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--branch", default=BRANCH)
    args = ap.parse_args()

    print(f"repo   : {args.repo}\nbranch : {args.branch}\n")

    contexts = ci_job_contexts(f"origin/{args.branch}")
    payload = build_payload(contexts)

    code, out, _ = _gh(
        "api", f"repos/{args.repo}/branches/{args.branch}/protection", check=False,
    )
    current = json.loads(out) if code == 0 and out.strip().startswith("{") else None

    print("── current ───────────────────────────────────────────")
    if current is None:
        print("  unreadable — unprotected, or you lack admin (a 404 means")
        print("  both, and they are indistinguishable without admin).")
        have_ctx: set[str] = set()
    else:
        have_ctx = set(current.get("required_status_checks", {}).get("contexts", []))
        reviews = current.get("required_pull_request_reviews")
        print(f"  required checks : {sorted(have_ctx) or 'none'}")
        print(
            "  reviews         : "
            + (
                f"{reviews['required_approving_review_count']} approval(s)"
                if reviews else "NONE"
            )
        )
        print(f"  enforce_admins  : {current.get('enforce_admins', {}).get('enabled')}")

    print("\n── planned ───────────────────────────────────────────")
    print(f"  required checks : {sorted(contexts)}")
    print(f"    (derived from {WORKFLOW} on origin/{args.branch})")
    print("  reviews         : 1 approval, stale approvals dismissed")
    print("  enforce_admins  : False (break-glass; see build_payload)")

    adding = sorted(set(contexts) - have_ctx)
    dropping = sorted(have_ctx - set(contexts))
    if adding:
        print(f"\n  + newly required: {adding}")
    if dropping:
        print(f"  - no longer required: {dropping}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write it.")
        return 0

    p = subprocess.run(
        [
            "gh", "api", "-X", "PUT",
            f"repos/{args.repo}/branches/{args.branch}/protection",
            "-H", "Accept: application/vnd.github+json",
            "--input", "-",
        ],
        input=json.dumps(payload), capture_output=True, text=True,
    )
    if p.returncode != 0:
        print(p.stdout, file=sys.stderr)
        raise SystemExit(f"failed to apply protection:\n{p.stderr}")

    print("\n── applied ───────────────────────────────────────────")
    _, out, _ = _gh(
        "api", f"repos/{args.repo}/branches/{args.branch}/protection",
        "--jq",
        "{reviews: .required_pull_request_reviews.required_approving_review_count, "
        "dismiss_stale: .required_pull_request_reviews.dismiss_stale_reviews, "
        "contexts: .required_status_checks.contexts, "
        "enforce_admins: .enforce_admins.enabled, "
        "force_pushes: .allow_force_pushes.enabled, "
        "deletions: .allow_deletions.enabled}",
    )
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
