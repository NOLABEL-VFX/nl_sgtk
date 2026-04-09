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

## Environment Variables

- `STUDIO_SHOTGUN_LINK` (required): ShotGrid host URL.
- `STUDIO_SCRIPT_NAME` (optional): ShotGrid script user name.
- `STUDIO_SCRIPT_KEY` (optional): ShotGrid script user key.

When `STUDIO_SCRIPT_NAME` + `STUDIO_SCRIPT_KEY` are both populated, they are used as the primary authentication entry point.
