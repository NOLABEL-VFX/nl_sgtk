# Changelog

## 0.12.0

### Added

- Index standardized diagnostic application version and build metadata in
  `affected_versions`, including host applications such as Nuke and 3ds Max.

## 0.11.0

### Added

- Added metadata schema version 2 for technical Tickets. Stable error identity
  is stored separately from affected application, Python, platform, and package
  versions in `sg_metadata_json`.
- Added synchronization of ShotGrid's existing `sg_occurances` field with the
  canonical JSON `occurrence_count`.
- Synchronized the runtime module, compatibility shim, and package metadata on
  version 0.11.0 so reported `nl_sgtk` versions are reliable.

### Changed

- Error deduplication now matches the same reporter and normalized error
  code/signature while excluding volatile timestamps, elapsed durations,
  memory addresses, log sizes, and other per-occurrence telemetry. Repeated
  Nuke `AbnormalExit` reports no longer receive different identities merely
  because they arrived seconds apart.
- Session IDs no longer merge different errors from one application session.
- Correlated occurrences merge their observed version sets and continue to add
  a linked Note with the fresh diagnostic attachments.
- Active canonical Tickets are preferred over resolved legacy duplicates. If
  the only canonical match is Resolved or Closed, a verified recurrence
  reopens it to Open instead of creating a new Ticket or leaving it closed.

### Compatibility

- This is a backward-compatible user-facing capability update. Existing
  `create_ticket` calls and arguments are unchanged. New Ticket metadata uses
  schema version 2; existing schema version 1 records remain readable and have
  their stable identity reconstructed from the stored metadata when matched.
- The package minor version moves from 0.10.0 to 0.11.0.

## 0.10.0

### Added

- Added canonical, redacted Ticket correlation metadata in
  `sg_metadata_json`, including session ID, error fingerprint, reporter,
  occurrence timestamps, and occurrence count.
- Added `session_id` and `deduplicate` options to `create_ticket`.
- Repeated open error reports now create a linked Note and upload fresh logs or
  screenshots to that Note. `TicketResult` exposes `created`, `note`, and
  `note_id` so callers can distinguish a new Ticket from a correlated event.

### Compatibility

- This is a backward-compatible capability addition. Error reporting now
  deduplicates by default; callers that require one Ticket per report can pass
  `deduplicate=False`. Manual reports (`was_error=False`) remain independent.
- The package minor version moves from 0.9.2 to 0.10.0.

## 0.9.2

### Added

- Added `was_error` to `create_ticket`, writing the new ShotGrid Ticket field
  `sg_was_error`. Technical reports default to `True`; manual report flows can
  pass `False`.

### Compatibility

- This is a backward-compatible API addition. Existing technical-report calls
  require no changes and are marked as originating from an error.

## 0.9.1

### Fixed

- Changed enum-based Ticket routing to validate only the stable ShotGrid Group
  ID. Group names are now read dynamically, so renaming a configured
  Development Group does not prevent technical reports from being created.

### Compatibility

- This is a backward-compatible routing fix. Enum names, Group IDs, function
  signatures, and caller-supplied Group/HumanUser routing remain unchanged.

## 0.9.0

### Added

- Added the Qt- and DCC-independent `nl_sgtk.tickets` module for authenticated
  technical Ticket creation.
- Added `PipelineGroup` and the requested `pipeline_group` alias with
  `PIPELINE`, `COMFY`, `MAX`, and `HOUDINI` members. They route to Pipeline
  Development, ComfyUI Development, 3DMax Development, and Houdini Development
  respectively.
- Added enum-based `TicketType` and `TicketPriority` contracts while allowing
  callers to supply a complete Group or HumanUser entity link as the recipient.
- Aligned enum values with the live Ticket schema. `ERROR` safely aliases
  `BUG`, `REQUEST` aliases `FEATURE`, and native Software Need, Data Wrangling,
  and Urgent values are available.
- Added structured metadata formatting with automatic reporter and UTC
  occurrence information, deterministic values, and secret redaction.
- Added validated multi-file attachments and immutable `TicketResult` output.
- Added specific validation, authentication, schema, routing, creation,
  readback, and partial-attachment exceptions.
- Added `REPORT_API.md` with a compact integration example and option summary.

### Safety

- Ticket fields and enum values are checked against the live ShotGrid schema
  before creation.
- The configured `00_IN_HOUSE` Project and selected Development Group, or a
  caller-supplied Group/HumanUser, are verified before writing.
- Every created Ticket is read back before attachment uploads start.
- Attachment paths are validated before Ticket creation. Partial upload errors
  retain the created Ticket ID and successful paths so callers do not blindly
  create duplicates.
- Common tokens, credentials, passwords, bearer values, and session-bearing
  URL queries are redacted from metadata and content.

### Compatibility

- Existing APIs and Toolkit behavior are unchanged. The new Ticket API is
  additive and bumps the package minor version from 0.8.0 to 0.9.0.

## 0.8.0

### Added

- Successful logins now maintain a concurrency-safe, indexed user profile
  cache in `~/.nolabel/local/nl_core/nl_core.sqlite3` for use by `nl_core`.
- Cached profiles include non-secret ShotGrid identity, department,
  permission rule set, group, project, email, and account-status data, plus a
  JSON refresh-status record and timestamps.
- Full profile data refreshes at most once every 24 hours, while every login
  updates `last_accessed`. Profiles unused for more than 90 days move to the
  separate `nl_sgtk_user_data_old` archive table.

### Fixed

- Concurrent login processes now coordinate profile refreshes through a
  recoverable SQLite lease instead of issuing duplicate ShotGrid queries.
- Concurrent first-time database connections tolerate the transient Windows
  lock raised while another connection enables WAL journal mode.
- Failed ShotGrid refreshes preserve the last successful profile, update
  `last_accessed`, record a non-secret failure status, and clear the lease for
  a later retry.
- Login and ShotGrid payloads are explicitly allowlisted so unexpected
  credential or session fields cannot be written to the cache.
- Existing pre-lease 0.8 cache tables are migrated in place.

### Compatibility

- Existing `nl_core` tables and schema ownership are unchanged. User-cache
  write failures are non-fatal to authentication. No credentials or session
  tokens are stored. This backward-compatible capability bumps the minor
  version.
- Compact rows returned by `get_user_tasks()` now preserve their ShotGrid
  `id` and `type`. Existing fields and function signatures are unchanged, and
  callers can pass a selected row's `id` directly to `get_task_context()`.

## 0.7.0

### Added

- Added `published_files_enabled(project=None)` as the future project-settings
  integration point. It intentionally returns `False` for every project in
  this release, so PublishedFile creation and repair remain globally disabled
  while Version creation and movie upload stay active.
- Added `PublishedFilePublishError`, which exposes the created Version ID and
  publish UUID when an enabled PublishedFile batch fails.

### Fixed

- Each `sgtk_login()` call now receives a deep-copied ShotGrid client from the
  cached authenticated session, avoiding shared-client concurrency failures.
- PublishedFile `path` values now use ShotGrid's `{"local_path": path}` shape,
  while `sg_path_string` remains a plain string.

### Compatibility

- PublishedFile registration cannot be enabled in this release. The reserved
  feature-gate function will be connected to project settings in a future
  implementation. This capability release bumps the minor version.

## 0.6.2

### Fixed

- Updated the GitHub Actions checkout and Python setup actions to their
  Node.js 24-compatible major versions, removing Node.js 20 deprecation
  warnings from the test matrix.

### Compatibility

- This patch changes only CI configuration; runtime behavior and public APIs
  are unchanged.

## 0.6.1

### Fixed

- Made the provider tests self-contained on clean CI runners by supplying a
  non-routable ShotGrid URL during test collection.

### Compatibility

- This patch changes only test setup; runtime behavior and public APIs are
  unchanged.

## 0.6.0

### Added

- Added the `NlSgtkProvider` entry point for tracker-neutral `nl_core`
  integration.
- Added SGTK-first Task context and output-aware Version/PublishedFile
  discovery, including partial Version collision protection.
- Added idempotent registration preflight by publish UUID, Task, path, and
  Version code.
- Added recovery that reuses a Version and repairs missing PublishedFiles.
- Added Windows and Linux CI coverage for Python 3.9 through 3.13.

### Compatibility

- Authentication and ShotGrid access remain inside `nl_sgtk`.
- Existing public helpers remain backward compatible.

## 0.5.0

### Added

- Added `ShotgunPublish` as a public publishing API for creating ShotGrid `Version` records and related `PublishedFile` records.
- Added support for publishing files into movie, frame, geometry, and script path fields.
- Added JSON import/export helpers for publish payloads.
- Added validation controls for trusted or already-verified publish payloads.
- Added per-publish UUID tracking through the ShotGrid `sg__publish_uuid` field.
- Added local publish state registration in `~/.nolabel/.data/context_logger.db` for NL Hub workflows.
- Added public API documentation for the new publishing workflow.

### Changed

- Updated the README to mention ShotGrid publishing support and outline planned publishing-related improvements.

### Compatibility

- This is a backward-compatible minor release. Existing APIs remain available.

## Reconstructed History

These entries were reconstructed from git commits, version declarations, and tags. Tags are present for `0.2.0`, `0.2.1`, and `0.3.2`; the other historical version entries are based on version bumps found in the repository history.

## 0.4.3

### Changed

- Moved the package implementation into the `src/nl_sgtk` layout.
- Added a root-level compatibility shim so older consumers that import or inspect `nl_sgtk.py` can still resolve the package and version.
- Switched package metadata to the `pyproject.toml` project version.

### Compatibility

- Existing imports remain supported through the compatibility shim.

## 0.4.2

### Added

- Added `API.md` as a public API reference for the main `nl_sgtk` functions.
- Added repository maintenance instructions for keeping API documentation in sync with public API changes.

### Changed

- Prioritized ShotGrid script-user authentication when `STUDIO_SCRIPT_NAME` and `STUDIO_SCRIPT_KEY` are configured.

## 0.4.1

### Changed

- Cached only successful `sgtk_login()` results so failed login attempts are not reused.
- Set the current ShotGrid host during interactive login.

## 0.3.2

### Added

- Added a helper for resolving the current ShotGrid user.

### Changed

- Reverted an earlier login-cache change before it was reintroduced in `0.4.1`.

## 0.3.1

### Added

- Added storage path normalization helpers.
- Added ShotGrid local storage lookup support.
- Added richer task and entity context data for Shot, Asset, and Project workflows.

### Changed

- Improved task environment payloads so expected keys are present consistently.
- Mapped OCIO paths through configured storage roots.
- Hydrated environment data in parsed entity contexts.

## 0.2.3

### Changed

- Pinned the `tk-core` dependency to a specific version for more predictable installs.

## 0.2.2

### Fixed

- Fixed string formatting in the version update checker for broader Python compatibility.

## 0.2.1

### Added

- Added an import-time update check that compares the local `nl_sgtk` version with the remote version.

## 0.2.0

### Changed

- Bumped the module version to `0.2.0`.

## 0.1.0

### Added

- Added initial package metadata and setup configuration.
- Added the first `nl_sgtk` module implementation.
- Added initial README documentation.
