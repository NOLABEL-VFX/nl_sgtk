# nl_sgtk API Reference

This document tracks the **main public API functions** exposed by `nl_sgtk.py`.

> Maintenance rule: whenever a public API function is added, removed, renamed, or its behavior/signature changes, this file must be updated in the same change.

## Authentication / Session

- `sgtk_login(base_url=SHOTGRID_URL, product=DEFAULT_PRODUCT)`
  - Returns `(sg, user)` on success, `(None, None)` on failure.
  - Uses script authentication first when both `STUDIO_SCRIPT_NAME` and `STUDIO_SCRIPT_KEY` are set.
  - Falls back to interactive SGTK user login otherwise.
- `get_user()`
  - Returns current resolved user dictionary.

## Task / Entity Context APIs

- `get_user_tasks(user, sg=None)`
  - Returns a normalized list of tasks for a specific HumanUser id.
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
  - Creates the `Version`, uploads preview media when available, and creates related `PublishedFile` records.
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
  reuses an existing Version and repairs missing PublishedFiles on retry.
- `health()` reports package version, connection availability, and identity
  type without exposing credentials.

The provider keeps ShotGrid imports and authentication inside `nl_sgtk`; the
`nl_core` package depends only on its structural provider protocol.
