# Repository instructions

## Release workflow

Use one short-lived branch for each semantic version and keep `main` as the only
long-lived branch.

1. Choose the version as `X.Y.Z` and create `release/X.Y.Z` from the latest `main`.
2. Update every version source, including `pyproject.toml`,
   `src/karaoke_forge/__init__.py`, the README files, and `CHANGELOG.md`.
3. Run the complete test and lint suite before publishing the branch.
4. Push only `release/X.Y.Z`; do not push release commits directly to `main`.
5. Open a pull request from `release/X.Y.Z` to `main` and wait for all required CI
   checks to pass. Never merge or tag a failing build.
6. Merge the pull request, update the local `main`, and create the annotated tag
   `vX.Y.Z` on the exact merged `main` commit.
7. Push the tag, then delete the remote and local `release/X.Y.Z` branch.

Release tags are immutable: never move or reuse an existing `vX.Y.Z` tag. Create a
GitHub Release only when the user explicitly asks for one.
