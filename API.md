# nl_sgtk API Reference

The current major release series is `1.0.0`. Versions use
`Major.Minor.Fix.build`: local builds advance the fourth component, while a
semantic target is selected once before release commit/push. The numeric
update checker treats an omitted build as zero, so `1.0.0` and `1.0.0.0`
compare equally. The provider protocol remains 1.0. See sibling
`nl_core/VERSIONING.md` for coordinated build commands.

## Publication manifests (0.16)

`ShotgunPublish.publish(..., registration="auto")` now completes geometry-only
exports without a preview locally through Core's Task manifest index. The
result has `local_only=True`, `manifest_path` and the publication UUID, with no
Shotgun Version `id`. Use `registration="tracker"` to explicitly create a
Version, or `registration="local"` for other local exports. Local index failure
raises; it never silently creates a Version. Core configuration is required.
SGTK construction still authenticates; use Core for fully offline publishing.

Tracker publication writes a UUID JSON mirror using `Version.sg__publish_uuid`.
Direct mirror failures are logged for repair with
`nl_core.open_session(task_id).publish.manifests.sync()`. Core-provider publishing
uses its durable same-UUID outbox. No PublishedFile creation is introduced.

`NlSgtkProvider.publish_manifest_snapshots(context)` reads normalized Task
Versions for Core. `version_snapshot()` in `nl_sgtk.publish_manifests` maps
frames/movie/geometry/script source roles and supplies stable UUIDs for legacy
Versions without one. `retrieve_version_info(resolve_dependencies=False)`
avoids tracker dependency lookup during local-only completion.

Existing `export_to_json()` exports an import payload to a caller-selected
path and records it in the machine-local SQLite log. It is unchanged and does
not itself constitute a completed shared local publication.

This document tracks the **main public API functions** exposed by `nl_sgtk.py`.

> Maintenance rule: whenever a public API function is added, removed, renamed, or its behavior/signature changes, this file must be updated in the same change.

## Authentication / Session

- `sgtk_login(base_url=SHOTGRID_URL, product=DEFAULT_PRODUCT)`
  - Returns `(sg, user)` on success, `(None, None)` on failure.
  - Caches the authenticated source session, then deep-copies the ShotGrid
    client and user data for each call so parallel callers do not share one
    mutable API client.
  - Uses script authentication first when both `STUDIO_SCRIPT_NAME` and `STUDIO_SCRIPT_KEY` are set.
  - Falls back to interactive SGTK user login otherwise.
  - On every successful login, updates the current profile's `last_accessed`
    value in `~/.nolabel/local/nl_core/nl_core.sqlite3`. Rich non-secret user
    metadata (including department, permission rule set, groups, projects,
    email, and status) is refreshed from ShotGrid when it is at least 24 hours
    old. A SQLite refresh lease prevents concurrent logins from duplicating the
    same ShotGrid query. Failed refreshes preserve the last good profile, record
    the access and failure status, and do not turn a valid login into a failure.
    Only explicitly allowlisted non-secret identity fields are persisted.
  - Profiles unused for more than 90 days are moved atomically from
    `nl_sgtk_user_data` to `nl_sgtk_user_data_old`.
- `get_user()`
  - Returns current resolved user dictionary.

## Task / Entity Context APIs

- `get_user_tasks(user, sg=None)`
  - Returns a normalized list of tasks for a specific HumanUser id.
  - Every compact task includes its stable ShotGrid `id` and `type`, allowing
    the selected row's `id` to be passed directly to `get_task_context()`.
- `get_task_context(task_id, sg=None)`
  - Returns context for a ShotGrid Task id.
- `get_entity_context(entity_type, entity_id, sg=None)`
  - Generic context lookup for `Task`, `Shot`, or `Asset`.
- `get_shot_context(shot_id, sg=None)`
  - Returns Shot context payload.
- `get_asset_context(asset_id, sg=None)`
  - Returns Asset context payload.
- `get_project_context(project_id, sg=None)`
  - Returns Project metadata.
- `parse_link(link, sg=None)`
  - Parses a ShotGrid URL and returns Task/Shot/Asset context.

## Project / Utility APIs

- `list_active_projects(sg=None)`
  - Returns active non-template projects.
- `get_storages(sg=None)`
  - Returns `LocalStorage` mappings.
- `verify_path(path, storages, system=None)`
  - Normalizes storage paths across platforms.

## Technical Ticket APIs

Import these from `nl_sgtk` or `nl_sgtk.tickets`.

- `create_ticket(topic, content, *, ticket_type=TicketType.BUG, priority=TicketPriority.MEDIUM, user_group=pipeline_group.PIPELINE, was_error=True, attachments=(), metadata=None, session_id=None, deduplicate=True, sg=None, user=None, occurred_at=None)`
  - Authenticates through `sgtk_login(product="NL SGTK Technical Tickets")`
    unless a paired `sg` and current `user` are injected.
  - Creates a Ticket in Project `00_IN_HOUSE`, defaults it to status `wtg`,
    verifies the created record by readback, and then uploads validated files.
  - Adds current-user and UTC occurrence metadata above the supplied content.
    Caller metadata is serialized deterministically, and common credential,
    token, password, bearer, and session-URL patterns are redacted.
  - Accepts `PipelineGroup`/`pipeline_group` enum routing or a complete
    ShotGrid `Group`/`HumanUser` entity dictionary. The supplied entity is
    verified before Ticket creation.
  - Returns an immutable `TicketResult` containing the verified Ticket,
    attachment IDs, and attachment paths.
- `pipeline_group` / `PipelineGroup`
  - Members: `PIPELINE`, `COMFY`, `MAX`, and `HOUDINI`.
  - Routes: `PIPELINE` → `Pipeline Development`, `COMFY` →
    `ComfyUI Development`, `MAX` → `3DMax Development`, and `HOUDINI` →
    `Houdini Development`.
  - Each configured Group ID is verified before Ticket creation. The current
    Group name is read dynamically and may change without breaking routing.
  - Writes `was_error` to the Ticket's `sg_was_error` checkbox. Technical
    reports default to `True`; pass `False` for a manual user report.
  - Stores versioned, redacted canonical correlation data in
    `sg_metadata_json`. Schema version 2 separates stable error identity from
    volatile timestamps, durations, log sizes, host telemetry, and package
    versions; affected application/package versions are tracked separately.
  - Initializes ShotGrid's existing `sg_occurances` number field to 1 and
    increments it with `occurrence_count` for every correlated report.
  - Matching error Tickets from the same reporter with the same stable error
    code/signature receive a linked Note and Note attachments instead of
    another Ticket. Active canonicals are preferred over resolved duplicates;
    a recurring Resolved or Closed canonical is reopened to Open. A session ID
    narrows correlation but never merges different error signatures. Pass
    `deduplicate=False` to force creation.
  - `TicketResult.created`, `TicketResult.note`, and `TicketResult.note_id`
    identify correlated occurrences.
- `TicketType`
  - Live ShotGrid types: `BUG`, `FEATURE`, `SOFTWARE_NEED`, and
    `DATA_WRANGLING`.
  - Semantic aliases: `ERROR` maps to `BUG`; `REQUEST` maps to `FEATURE`.
- `TicketPriority`
  - Members: `LOW`, `MEDIUM`, `HIGH`, `URGENT`, and `CRITICAL`.
- `format_ticket_content(content, user, *, metadata=None, occurred_at=None)`
  - Builds the structured and redacted Ticket description without writing.
- Ticket exceptions
  - `TicketValidationError`, `TicketAuthenticationError`,
    `TicketSchemaError`, `TicketRoutingError`, `TicketCreationError`,
    `TicketReadbackError`, and `TicketAttachmentError` derive from
    `TicketError`.
  - `TicketReadbackError.ticket_id` identifies a record that was created but
    could not be verified.
  - `TicketAttachmentError` retains `ticket_id`, `failed_path`, and
    `uploaded_paths`; callers must inspect the existing Ticket before retrying
    because attachment uploads are non-atomic.

## Publishing APIs

- `ShotgunPublish(logger=None, script_user=None, script_key=None, override_user=None, sg=None, user=None, validate_paths=True, trusted=False)`
  - Creates ShotGrid `Version` records and related `PublishedFile` records.
  - Creates a unique publish UUID for each class instance and attaches it to the `sg__publish_uuid` Version field.
  - Supports script-user, injected ShotGrid connection, or normal `sgtk_login()` authentication.
  - `validate_paths=False` skips local path existence checks for already-verified payloads.
  - `trusted=True` allows imported source-of-truth payloads to skip publish-time validation unless explicitly requested.
- `ShotgunPublish.set_context(context=None, validate=None)`
  - Sets project/entity/task context from a context dictionary, serialized `Project:1;Shot:2;Task:3` string, or supported ShotGrid URL.
- `ShotgunPublish.add_file(file_path, force_version_field=None, verify=None)`
  - Adds a publish file to one of `sg_path_to_frames`, `sg_path_to_movie`, `sg_path_to_geometry`, or `sg_path_to_script`.
  - Pass `force_version_field` to override extension-based classification.
  - Pass `verify=False` to skip path existence checks for verified source data.
- `ShotgunPublish.set_preview_file(file_path, verify=None)`
  - Sets the upload preview file. Pass `verify=False` to skip path existence checks.
- `ShotgunPublish.import_from_json(file_path, validate=True, trusted=False)`
  - Imports a publish payload from JSON.
  - Use `trusted=True` or `validate=False` when the JSON comes from a verified source of truth.
- `ShotgunPublish.import_data(data, validate=True, trusted=False)`
  - Imports a publish payload from an in-memory dictionary.
- `ShotgunPublish.export_to_json(file_path, validate=True)`
  - Exports the current publish payload to JSON.
  - Registers the exported publish payload in `~/.nolabel/.data/context_logger.db`.
- `ShotgunPublish.retrieve_version_info(validate=True)`
  - Returns the ShotGrid `Version` payload, converting file lists to semicolon-separated ShotGrid path fields.
- `ShotgunPublish.publish(validate=True, upload_preview=True)`
  - Creates the `Version` and uploads preview media when available.
  - PublishedFile creation is currently disabled globally.
  - The `published_files_enabled(project=None)` feature-gate function currently
    always returns `False`; its project argument is reserved for a future
    project-settings implementation.
  - Raises `PublishedFilePublishError` with `version_id` and `publish_uuid`
    attributes if optional PublishedFile creation fails after Version creation.
  - Registers publish state updates in `~/.nolabel/.data/context_logger.db` for NL Hub.

## Environment Variables

- `STUDIO_SHOTGUN_LINK` (required): ShotGrid host URL.
- `STUDIO_SCRIPT_NAME` (optional): ShotGrid script user name.
- `STUDIO_SCRIPT_KEY` (optional): ShotGrid script user key.

When `STUDIO_SCRIPT_NAME` + `STUDIO_SCRIPT_KEY` are both populated, they are used as the primary authentication entry point.

## nl_core tracker provider

### `NlSgtkProvider`

Import from `nl_sgtk.provider`. The provider implements protocol version
`1.0` for automatic discovery through the `nl_core.tracker_providers` Python
entry-point group.

- `fetch_task(task_id)` uses `get_task_context()`, hydrates the Step short code,
  and returns the source Task payload expected by `nl_core`.
- `resolve_launch_source(entity_type: str, entity_id: int,
  source_fields: Sequence[str] = ('sg_path_to_script',),
  application_version_field: str = '') -> Mapping[str, Any]` reads exactly
  one `Version` by ID. Other record types raise `ValueError`; a missing Version
  raises `LookupError`. Configured source and application-version fields must
  exist in the Version schema, otherwise `ValueError` names the missing fields.
  A schema read failure raises `RuntimeError` with the original cause.
  Values must be text or null; omitted response fields raise `ValueError`.
  Publisher path fields (`sg_path_to_script`, `sg_path_to_geometry`,
  `sg_path_to_movie`, `sg_path_to_frames`) use the documented publisher
  semicolon-list encoding and are split in configured field/path order.
  Custom source fields each supply one path, preserving literal semicolons
  and other command-like characters. Surrounding whitespace and empty entries
  are removed. The publisher encoding cannot represent literal semicolons
  inside a path; use a custom single-path field for such filenames.
  Null or blank values
  produce no sources; an empty `source_fields` sequence requests context only.
  Duplicate field names are read once. Paths are not checked on disk.
  The returned mapping contains:

  ```python
  {
      "record_type": "Version",
      "record_id": 123,
      "project": {"type": "Project", "id": 1},
      "entity": {"type": "Shot", "id": 2},
      "task": {"type": "Task", "id": 3},  # {} when unlinked
      "sources": [{"path": "//server/show/comp.nk",
                   "field": "sg_path_to_script"}],
      "application_version": "",
  }
  ```

  Links retain only type and ID. Missing/malformed Project or entity links,
  malformed linked Tasks, and invalid IDs raise `ValueError`. Application
  version stays empty unless its field is explicitly configured and populated.
  There is no inference from Version code, no implicit movie/frame fallback,
  and no search for a latest Version.
- `list_launch_tasks(entity_type: str, entity_id: int)
  -> List[Mapping[str, Any]]` queries Tasks linked to exactly the supplied
  entity type and ID, normally the `entity` returned above (Shot or Asset),
  **not** the Version's `record_type` and `record_id`. Each result contains
  `id`, `name` (Task content), `project` and `entity` (type/ID links). Results
  are ordered by content then ID; no matches return `[]`. Other linked entity
  types are supported with the same exact-link filter. Invalid IDs or missing
  required context links raise `ValueError`.
- `find_publishes(context, output)` queries registered `Version` and
  `PublishedFile` rows for the supplied Task before local fallback is
  considered. Partial Versions still protect version allocation.
- `register_publish(context, request)` uses the validated `ShotgunPublish`
  workflow and preserves the caller's `sg__publish_uuid` for idempotency. It
  reuses an existing Version. PublishedFile repair remains behind the disabled
  feature gate.
- `health()` reports package version, connection availability, and identity
  type without exposing credentials.

The provider keeps ShotGrid imports and authentication inside `nl_sgtk`; the
`nl_core` package depends only on its structural provider protocol.
The two launch APIs only read ShotGrid data and return plain mappings for
Hub/core's generic launch models; they do not launch applications or write
tracker records. Tests supply a fake connection without authentication.
# DCC launcher provider API

`resolve_launch_software(project_id)` reads
`Project.custom_non_project_entity04_sg_projects_custom_non_project_entity04s`
and returns linked `CustomNonProjectEntity04` facts: `id`, `name`,
`software_names`, `windows_path`, and `linux_path`. It raises if any linked
record is unreadable. NL Hub owns product/version normalization and policy
merging; this provider never executes those paths or mutates tracker data.

`NlSgtkProvider` exposes `current_user()`, `resolve_launch_project()`,
`resolve_launch_context()` includes the localized project OCIO path as
`metadata.ocio_config`, alongside the shot color environment. Launchers can
resolve relative LUT names in nested OCIO config folders. An unset project
OCIO path is represented by an empty string.

`list_launch_projects()`, `resolve_launch_context()`, `list_launch_tasks()`, and
`list_task_workfiles()` for trusted NL Hub launch routing.
`resolve_launch_project(project_id)` requests only the Project name, code and
root needed for menu-time software policy. LocalStorage mappings are cached by
the provider. `list_launch_projects()` returns the same minimal fields for all
active non-template Projects so NL Hub can warm its policy cache in one query.
These methods return plain mappings and never accept executable,
environment, or command overrides from browser actions.
# Version publication and relationship provider (0.15)

The current publish contract is output plus Version creation. `ShotgunPublish`
classifies files into `sg_path_to_frames`, `sg_path_to_movie`,
`sg_path_to_geometry` and `sg_path_to_script`; preview upload is explicit.
`published_files_enabled()` returns false, including development projects.
PublishedFile migration remains deferred. Existing records are not deleted.

`NlSgtkProvider.register_publish(context, request)` reuses the publish UUID
on retry. Core owns reservation/outbox orchestration; SGTK owns Version fields,
authentication and upload. Each normal calling thread receives its own cached
Shotgun client. Explicit private client injection remains a compatibility path
and must not be shared concurrently by callers.

`find_entity_relationships(entity, relationship_types)` returns direct links
with `entity`, `relationship_type`, `direction` and `source`. Sequence `shots`
and `sg_scenes` are distinct relations; querying Shots never expands Scenes.
Studio-specific membership and dependency fields remain in this adapter.
`find_references(context, request)` supplies Version-backed `version` and
`publish` references. With a Step it queries that Step on the same entity;
without a Step it queries the session's exact Task. Project/entity filters are
always present. Source-path fields and numeric version ordering are retained.
Application descriptors are not generated by this provider.
# Publication metadata and vendor attribution (0.17)

Core publication requests accept exact dependency links and JSON metadata.
The provider maps dependencies to `Version.sg_dependiencies`, metadata to
`sg_files_metadata`, and mirrors metadata in local UUID manifests. The shared
`colorspace` metadata key allows application adapters to restore source color.
`metadata.vendor` is validated through `ShotgunPublish.set_vendor_by_data()`:
only a Group with a VENDOR tag may become `Version.user`. Geometry-only local
publications still do not require a Version or tracker upload.
`publish(upload_preview=False)` validates context and files without requiring a
preview. Image/script registrations can therefore complete before review media
is available. The default still requires and uploads a preview.

### Background authentication

`launch_interactive_login` requires the Python main thread and raises
`RuntimeError` before opening a browser when called from a worker.
`sgtk_login` continues to return `(None, None)` on authentication failure;
background callers should retain work for retry after user sign-in from
the application window. Existing valid cached/script logins remain usable.
