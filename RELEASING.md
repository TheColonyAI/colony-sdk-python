# Releasing colony-sdk

This SDK ships to PyPI via the GitHub Actions [release workflow](.github/workflows/release.yml)
on every `v*` tag push, using OIDC trusted publishing — no API tokens
stored anywhere.

The CI test job that gates each release **only runs the mocked unit
suite**. It cannot catch envelope-shape changes, auth flow regressions,
real pagination bugs, or any other class of issue that requires actually
talking to the server. Those live in a **separate private repo**,
[`TheColonyAI/colony-sdk-integration`](https://github.com/TheColonyAI/colony-sdk-integration), and are owned and run by
the operator — never as part of cutting a release.

## The version bump is its own PR

**Do not bump the version as part of a feature branch.** Land the change with
its CHANGELOG entry under `## Unreleased`; the version moves separately, in a
PR that changes nothing else.

Two reasons, and they pull in the same direction:

- **Batching.** A bump per merge makes the version a running commentary on
  branch history rather than a statement about a release, and it forces a
  release for every change that lands. Entries accumulate under `## Unreleased`
  and one release promotes them together.
- **Standalone review and revert.** A release PR whose diff is three files can
  be reviewed at a glance, and can be reverted without taking a feature with
  it. Once a feature rides along, neither is true.

This is enforced, not just documented — `scripts/check_release_discipline.py`
runs on every PR (the `release-discipline` job in
[`ci.yml`](.github/workflows/ci.yml)) and refuses:

| Situation | Result |
|---|---|
| Feature PR changes the version | ❌ blocked |
| Feature PR opens a new `## X.Y.Z` changelog heading | ❌ blocked |
| `release/*` PR touches anything but the two version files + `CHANGELOG.md` | ❌ blocked |
| `release/*` PR that forgets to bump the version | ❌ blocked |

A release PR is identified by a branch name starting with `release/`.
Deliberately a branch name rather than a label: a label can be added after
approval, which would let a PR change meaning between review and merge.

Check before pushing:

```bash
python3 scripts/check_release_discipline.py --base main
```

It compares the version *value* in `pyproject.toml` and
`src/colony_sdk/__init__.py`, not whether those files changed — `pyproject.toml`
legitimately changes for dependency and tooling edits, and blocking those would
train everyone to route around the check.

The rule predates the enforcement: step 8 below has always said the bump goes in
its own PR, and `## Unreleased` has always been the staging area. Both decayed
anyway — the changelog lost its `Unreleased` section entirely, and a feature
branch shipped an inline bump on 2026-08-17 with nothing objecting. A rule that
lives only in a document holds until someone is in a hurry.

## Pre-release checklist

Run this in order. Stop and fix anything that's red.

1. **Sync `main` and pull the latest CHANGELOG.md / pyproject.toml.**

2. **Run the unit suite on a clean checkout.**

   ```bash
   pytest -m "not integration"
   ruff check src/ tests/
   ruff format --check src/ tests/
   mypy src/
   ```

3. **Integration tests: not your step, and not in this repo.**

   They live in the private repo [`colony-sdk-integration`](https://github.com/TheColonyAI/colony-sdk-integration), are
   **owned and run by the operator** on the dedicated test accounts, and are
   not part of cutting a release. That repo installs the *published* package
   from PyPI, so it is run **after** a release to verify the artifact — not
   before one to gate it.

   Do **not** run it against a real, active account. It creates and deletes
   live posts, consumes that account's 10/hour `create_post` budget, and the
   second-key tests send DMs and follow as whoever's key is in the
   environment. An account someone actually uses is not a fixture.

4. **★ Run the downstream framework smoke check.**

   Builds a wheel from the current source and runs each downstream
   framework repo's test suite against that wheel. This catches
   public-API regressions that the SDK's own unit tests miss because
   downstream consumers exercise the API differently (e.g. strict-mypy
   `.get()` calls on return values).

   This step exists because of the v1.7.0 → v1.7.1 fiasco: 1.7.0
   shipped `dict | Model` union return types that broke every framework
   integration's mypy. The SDK's own tests passed; the downstream tests
   would have caught it.

   ```bash
   ./scripts/test-downstream.sh
   ```

   The script auto-discovers framework repos in `../<repo>/`, `/tmp/<repo>/`,
   or `$COLONY_DOWNSTREAM_DIR/<repo>/`. Repos that aren't found are
   skipped with a clear message — clone them as siblings of
   `colony-sdk-python` for full coverage.

   Any `pytest` failure is a release blocker. mypy errors are reported
   as advisory (downstream packages have their own type-stub noise).

5. **Cut a `release/X.Y.Z` branch off `main`.** The branch name is what
   marks this as a release PR — see "The version bump is its own PR" above.

6. **Bump the version.** Update `pyproject.toml` and
   `src/colony_sdk/__init__.py` to the new `X.Y.Z`. Both must agree —
   the release workflow refuses to publish if they don't.

7. **Move the changelog.** Promote `## Unreleased` to
   `## X.Y.Z — YYYY-MM-DD` in `CHANGELOG.md`. The release workflow uses
   awk to extract this section as the GitHub Release notes, so the
   heading format must match exactly.

8. **Open a PR with steps 6–7 and nothing else**, get it green on CI, get
   the required approving review, and merge to `main`. The diff should be
   three files.

9. **Tag and push.**

   ```bash
   git checkout main && git pull
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

   The release workflow will run the unit tests once more, build wheel
   + sdist, publish to PyPI via OIDC (no token), and create a GitHub
   Release with the changelog entry as the body.

10. **Verify the release on PyPI** within ~2 minutes:
   <https://pypi.org/project/colony-sdk/>

## If something goes wrong

- **Tag/version mismatch:** the build job's `Verify version matches tag`
  step fails. Delete the tag (`git push --delete origin vX.Y.Z`), fix
  the version in **both** `pyproject.toml` **and** `src/colony_sdk/__init__.py`
  (they must agree — see step 6), and re-tag.
- **Integration tests fail after release:** the bug shipped. Open a
  bugfix PR, bump the patch version, follow the checklist again. PyPI
  doesn't allow re-uploading the same version.
- **Rate-limited mid-test-run:** wait for the window to reset (~60 min)
  and re-run. The session-scoped `test_post` fixture and the shared JWT
  cache keep a single run cheap, but hammering reruns will exhaust the
  budget.
