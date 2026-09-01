# nl_sgtk API Reference

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
