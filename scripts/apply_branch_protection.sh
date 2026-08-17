#!/usr/bin/env bash
#
# Apply branch protection to `main`. Requires ADMIN on the repo.
#
# Kept in the repo rather than run as a one-off console click so the
# protection is reviewable, reproducible, and re-appliable — GitHub's branch
# protection has no history, so an undocumented setting silently drifts and
# nobody can tell what it used to be.
#
#   ./scripts/apply_branch_protection.sh            # show current + planned
#   ./scripts/apply_branch_protection.sh --apply    # write it
#
set -euo pipefail

REPO="${REPO:-TheColonyAI/colony-sdk-python}"
BRANCH="${BRANCH:-main}"

# These must match the job names in .github/workflows/ci.yml EXACTLY. A
# required context that never reports leaves every PR permanently unmergeable,
# and a renamed job silently stops being required — see the "do not rename CI
# job names" item in CONTRIBUTING.md.
read -r -d '' PAYLOAD <<'JSON' || true
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "release-discipline",
      "lint",
      "typecheck",
      "test (3.10)",
      "test (3.11)",
      "test (3.12)",
      "test (3.13)"
    ]
  },
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false
  },
  "enforce_admins": false,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON

echo "repo   : $REPO"
echo "branch : $BRANCH"
echo

echo "── current ───────────────────────────────────────────────"
if ! gh api "repos/$REPO/branches/$BRANCH/protection" 2>/dev/null; then
  echo "(none readable — either unprotected, or you lack admin: a 404 here"
  echo " means BOTH, and they are indistinguishable without admin rights)"
fi
echo

echo "── planned ───────────────────────────────────────────────"
echo "$PAYLOAD"
echo

if [[ "${1:-}" != "--apply" ]]; then
  echo "Dry run. Re-run with --apply to write it."
  exit 0
fi

echo "$PAYLOAD" | gh api -X PUT "repos/$REPO/branches/$BRANCH/protection" \
  -H "Accept: application/vnd.github+json" --input -

echo
echo "── applied ───────────────────────────────────────────────"
gh api "repos/$REPO/branches/$BRANCH/protection" \
  --jq '{
    reviews: .required_pull_request_reviews.required_approving_review_count,
    dismiss_stale: .required_pull_request_reviews.dismiss_stale_reviews,
    strict: .required_status_checks.strict,
    contexts: .required_status_checks.contexts,
    enforce_admins: .enforce_admins.enabled,
    force_pushes: .allow_force_pushes.enabled,
    deletions: .allow_deletions.enabled
  }'

cat <<'NOTE'

Two things worth knowing about what this just set:

* `enforce_admins: false` on purpose. GitHub does not allow approving your
  own PR, so with review required an admin who is the only available
  reviewer would be unable to merge anything. Leaving admins unbound keeps a
  break-glass path. Flip it to true once there are reliably two reviewers.

* `dismiss_stale_reviews: true` means a new push after approval clears the
  approval. That is the point of requiring review — otherwise "approved" can
  describe a diff nobody read.
NOTE
