# Repository Agent Instructions

- Keep `API.md` in sync with any public API changes in `nl_sgtk.py`.
- If an API function is added, removed, renamed, or behavior/signature changes, update `API.md` in the same commit.
- Whenever making repository changes, inspect the diff before finishing and check whether the package version was updated where it is defined.
- If the diff changes behavior, public API, packaging, or user-facing documentation, decide the appropriate semantic version bump from the current version to the next version:
  - Major: breaking API or behavior changes.
  - Minor: backward-compatible additions or new user-facing capabilities.
  - Patch/fix: backward-compatible bug fixes, documentation corrections, or internal-only fixes.
- If the diff already includes a version bump, verify at the end that the bumped version matches the actual change scope; correct it if needed.
- For each change, create or update `changelog.md` with user-facing release notes from the current version to the next version, describing what changed, what was added, what was fixed, and any compatibility notes.
