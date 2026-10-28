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

Core publication snapshots and `find_references(..., {"kind": "publish"})`
use the same version parser. In 1.7.0.0, `comp_v005a` becomes numeric version
5, variant `a` and label `v005a`. The last valid version token wins; an
optional single-letter suffix is normalized to lowercase. Codes without a
valid version retain 0 and empty variant/label.

Existing `export_to_json()` exports an import payload to a caller-selected
path and records it in the machine-local SQLite log. It is unchanged and does
not itself constitute a completed shared local publication.

This document tracks the **main public API functions** exposed by `nl_sgtk.py`.

> Maintenance rule: whenever a public API function is added, removed, renamed, or its behavior/signature changes, this file must be updated in the same change.

## Authentication / Session

### Web applications and shared ComfyUI hosts

Use `nl_sgtk.WebAuthenticator` for per-browser authentication. This is
independent of `sgtk_login()`, Toolkit's default user and script credentials.
Install the optional `nl_sgtk[web]` extra. Existing desktop imports and
authentication do not require the extra.

```python
import os
from nl_sgtk import WebAuthenticator, WebAuthenticationRequired

# Generate the Fernet key once and keep it in backend secret storage.
# Reuse the same key after restarts; never put it in the database or JS.
auth = WebAuthenticator(
    encryption_key=os.environ["NL_WEB_AUTH_KEY"].encode("ascii"),
    product="ComfyUI",
    database="web_sessions.sqlite3",
)

# Run this in a worker for an explicitly requested browser sign-in.
# deliver_url must target only that browser's authenticated pending-login
# channel and return False if delivery is cancelled. None/True mean success.
grant = auth.login(browser_open_callback=deliver_url)
try:
    # Set an HttpOnly, Secure, SameSite cookie to grant.browser_token.
    # Return only grant.user in a public JSON response.
    pass
finally:
    grant.close()

# On every protected HTTP request, read that request's cookie.
with auth.authenticate(request_cookie) as session:
    tasks = nl_sgtk.get_user_tasks(session.user, sg=session.sg)
```

- `WebAuthenticator(base_url=SHOTGRID_URL, *, encryption_key,
  product="NL Web App", database=":memory:", session_lifetime=28800,
  idle_timeout=1800, http_proxy=None)` owns an app-scoped SQLite registry.
  The Fernet encryption key is required and must remain outside that database.
  Persistent sessions require both a persistent database and the same key.
  Registry keys are SHA-256 hashes of random 256-bit browser session IDs,
  scoped to the configured Flow site and application. Hashes cannot be
  presented as browser credentials. Flow tokens are encrypted, not hashed,
  because the backend must recover them to authenticate to Flow.
- `auth.login(browser_open_callback, *, keep_waiting_callback=None,
  expected_user_id=None) -> WebSession` blocks for one browser approval,
  verifies the returned HumanUser and creates a fresh app session ID.
  Callback delivery and cancellation must belong to the requesting browser.
  Non-Qt worker callbacks are supported without `force=True`. The global
  `NL_SGTK_ALLOW_INTERACTIVE=0` policy still prohibits browser approval.
  This operation never selects or changes the local Toolkit identity/cache.
- `auth.authenticate(browser_token, *, browser_open_callback=None,
  keep_waiting_callback=None, expected_user_id=None) -> WebSession`
  resolves only this browser's app session and validates its Flow token on
  every call. It returns a new independent raw API client; verification uses
  a 10-second HTTP timeout and one RPC attempt, then restores normal settings.
  Missing, unknown, expired, corrupted and revoked sessions raise
  `WebAuthenticationRequired` when no delivery callback is supplied.
  With a callback, one new approval is attempted. Renewal of a known session
  must retain its HumanUser. Network/transport errors never trigger approval.
  Queued work should pass its server-recorded owner as `expected_user_id`.
- `WebAuthenticationRequired(TrackerAuthenticationError)` has `reason`:
  `missing`, `unknown`, `expired`, `invalid`, `revoked`, or `cancelled`.
  Return HTTP 401 and let the requesting frontend start its login flow.
  `WebIdentityMismatchError(TrackerAuthenticationError)` rejects another
  user's cookie or approval; do not retry work using a replacement identity.
- `WebSession` exposes immutable `identity: WebIdentity`, a fresh `user`
  dictionary in the existing nl_sgtk shape, caller-owned `sg`, secret
  `browser_token` and `renewed`. `renewed=True` means a new approval occurred
  and the adapter must replace the browser cookie. `close()` and context
  management close the API client. Credential fields are excluded from repr.
  The result has no instance dictionary and is not a dataclass, preventing
  accidental credential serialization. Return `session.user` to the frontend.
- `WebIdentity(id, login, name)` is frozen and `as_user()` returns a fresh
  non-secret HumanUser dictionary. Identity is obtained from Flow approval
  and verification, never a frontend username or ComfyUI `client_id`.
- `auth.logout(browser_token)` removes this app session; the adapter must
  clear its cookie. It does not log the browser out of Flow. `auth.close()`
  closes the registry; already returned clients remain caller-owned.

For secure cookies, pending approval delivery, queued work and ComfyUI
integration boundaries, see [web authentication](docs/web_auth.md).

### Optional native Core Task sessions

`nl_sgtk.core` is a Python submodule with a lazy optional dependency on
`nl-core>=1.4.1.1` (`nl_sgtk[core]`). Importing nl_sgtk does not import Core.
The ordinary `get_task_context(task_id, sg=None)` dictionary is unchanged.

```python
import nl_sgtk

with nl_sgtk.get_task(184033) as task:
    data = task.context
    session = task.core_session
    if session is not None:
        work = session.path("work")
```

`get_task(task_id, sg=None) -> Optional[nl_sgtk.core.Task]` returns `None`
for a missing Task. `Task.id` is its exact Flow ID. `core_session` opens once
on first access and is `None` for schema0. `Task.close()` releases its owned
runtime; the context manager does that automatically. Access after close
opens a new session and rechecks current accepted policy.

For dictionary callers, opt in explicitly:

```python
data = nl_sgtk.get_task_context(184033, include_core_session=True)
session = data["core_session"] if data else None
try:
    if session is not None:
        work = session.path("work")
finally:
    if session is not None:
        session.runtime.close()
```

This flag adds a live Python object, so that dictionary is intentionally not
JSON serializable. Do not put it into launch manifests or saved scenes.
`nl_sgtk.core.open_core_session(context, *, sg=None, user=None)` accepts an
already verified Task context (raw or compact). Each returned session owns
a runtime. When using a supplied Flow client, pass its authenticated `user`
to preserve author information for Core filename and publish planning.
These entry points do not materialize directories, initialize projects, or
publish. Unavailable project storage, missing optional dependencies for a
native project, invalid accepted TOML, identity mismatch and refresh errors
raise; they never silently become schema0. The accepted Git configuration
is authoritative; unaccepted working-copy edits do not change its policy.

`NlSgtkProvider(sg=None, user=None)` accepts an existing authenticated client.
The provider now exposes `find_legacy_versions(context)` (all four Version
path channels, exact Task/Project, author and Group tags) and
`find_tasks_for_path(path)` (exact historical file matches only). Shared file
ownership returns all candidates; directory or filename-only guesses do not
select a Task. `fetch_task()` includes the project's `sg_structure_map`.

- `sgtk_login(base_url=SHOTGRID_URL, product=DEFAULT_PRODUCT, *,
  allow_interactive=None, raise_on_error=False)`
  - Returns `(sg, user)` on success, `(None, None)` on failure.
    - `raise_on_error=True` preserves errors; rejected credentials raise
      `TrackerAuthenticationError` with their original cause. Core providers
      use this strict mode.
    - `allow_interactive=False` prohibits login UI, Toolkit session renewal
      and interactive fallback after script authentication fails. It uses
      a saved token with a raw API client. `NL_SGTK_ALLOW_INTERACTIVE=0`
      disables interaction globally, including explicit login/force calls.
      The default allows interaction only on the main thread.
  - Caches the authenticated source session, then deep-copies the ShotGrid
    client and user data for each call so parallel callers do not share one
    mutable API client. Personal verification uses a raw client without
    implicit Toolkit renewal, including for interactive callers. The client's
    normal request settings are restored after bounded login verification.
  - Uses script authentication first when both `STUDIO_SCRIPT_NAME` and `STUDIO_SCRIPT_KEY` are set.
  - Falls back to interactive SGTK user login otherwise.
      Fallback runs only when interaction is allowed.
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
- `launch_interactive_login(base_url=SHOTGRID_URL, product=DEFAULT_PRODUCT,
  browser_open_callback=webbrowser.open, *, force=False)`
  - Runs browser app-session login and caches the session for SGTK.
  - Raises `RuntimeError` outside the main thread unless `force=True`.
    See **Background authentication** for the Qt restriction and safe bypass.
  - Only one browser approval may run at a time in this process. A concurrent
    request raises `TrackerAuthenticationError` without opening another browser.
  - After saving approval, invalidates cached login clients and status data.
  - Reads back the approved token, current site and current user from disk.
    Missing or mismatched data raises `TrackerAuthenticationError`; an older
    valid session cannot make a failed save look successful. Progress logs
    identify approval and persistence stages without recording credentials.
- `renew_session(base_url=SHOTGRID_URL, product=DEFAULT_PRODUCT,
  browser_open_callback=webbrowser.open, *, force=False)`
  - Explicitly requests a personal browser session and returns a verified
    `(sg, user)` pair. Never substitutes script credentials for the artist.
  - Uses the same thread and environment guards as `launch_interactive_login`.
    Browser approval uses SGTK's timeout; verification uses a plain API client
    with a 10-second HTTP timeout and one RPC attempt. Normal client settings
    are restored before returning the connection.
  - Always raises on approval, verification or transport failure. A rejected
    token raises `TrackerSessionExpiredError`; it never opens a second login.
- `invalidate_session_cache()`
  - Clears this process's cached clients and statuses without deleting saved
    SGTK credentials or opening login UI. Call after a reported authentication
    fault, or after updating the SGTK cache outside this library.
  - Subsequent `sgtk_login()` calls and provider-owned connections reacquire
    authentication. Already returned or explicitly injected clients remain
    caller-owned; obtain a new client before retrying. No writes are replayed.
  - Invalidation is process-local. Other applications must reacquire their
    own clients after a shared SGTK cache update; no IPC or credential-file
    watching is introduced.
- `TrackerSessionExpiredError(TrackerAuthenticationError)`
  - Distinguishes rejected personal tokens from missing login and transport
    failures. Strict login (`raise_on_error=True`) preserves this subtype and
    its original `AuthenticationFault` cause. The legacy `(None, None)`
    failure return remains unchanged.
  - Uncached interactive login renews a rejected or missing personal session
    once, including rejection during client construction or HumanUser lookup.
    Noninteractive login never opens UI. Cached successes are not remotely
    revalidated on every call; invalidate them when an operation reports expiry.

```python
# On an explicit sign-in action (main thread by default):
sg, artist = nl_sgtk.renew_session(product="NL Hub")

# A worker can report expiry for its host to handle on the GUI thread:
try:
    sg, artist = nl_sgtk.sgtk_login(
        allow_interactive=False, raise_on_error=True,
    )
except nl_sgtk.TrackerSessionExpiredError:
    # Signal the host to offer renewal; do not open login UI here.
    pass
```

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
  exposes `MIN_DESCRIPTION_LENGTH`, matching the module-level
  `nl_sgtk.MIN_DESCRIPTION_LENGTH` publish-description validation contract:
  at least 3 characters after trimming leading and trailing whitespace.
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
### Task-first launch sources

`NlSgtkProvider.list_task_launch_sources(task_id)` is a read-only exact-Task
query for launch previews. It retains Version `sg_path_to_script` references
and enriches/adds PublishedFile paths with `published_file_id`,
`published_file_type`, `version_number` and `created_at`, when present.
Rows have `provenance="published"`. It does not scan disk or change Task
assignments. Call `localize_launch_path()` before filesystem comparison;
the launcher deduplicates localized paths and orders numeric script versions.
Query errors propagate rather than being interpreted as an empty task.
The existing `list_task_workfiles()` return shape remains unchanged.

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

By default, `launch_interactive_login` requires the Python main thread and
raises `RuntimeError` before opening a browser when called from a worker.
This guard was introduced for Qt hosts such as Nuke to prevent background
operations from starting login UI or callbacks that access Qt outside the
main application thread, potentially hanging or crashing the host.

Pass `launch_interactive_login(force=True)` to bypass only this thread check
when the caller has verified that the login flow and `browser_open_callback`
are safe in the calling thread, for example a browser-only login in a non-Qt
host such as ComfyUI. The keyword-only flag defaults to `False`; existing
callers retain the guard. It does not dispatch work to the main thread or
make Qt operations thread-safe. Authentication and session caching are
unchanged. Automatic calls through `sgtk_login` and `ensure_sgtk_user` retain
the default guard.

By default `sgtk_login` returns `(None, None)` on authentication failure;
background callers should retain work for retry after user sign-in from
the application window. Existing valid cached/script logins remain usable.


### Core vendor slate metadata

`NlSgtkProvider.register_publish(context, request)` accepts an optional
boolean `request["metadata"]["has_slate"]`. Both `True` and `False` are passed
to `ShotgunPublish.movie_has_slate` and stored in `Version.sg_movie_has_slate`.
A non-boolean value fails before authentication or registration. Omitting
the key preserves the publisher default; a UUID retry reuses the existing
Version.

### Ticket project association during creation

Ticket/Note creation validates that `project` exists, but permits its initial
association when schema metadata marks subsequent editing unavailable. Updates
still require editable fields. Other field restrictions, enum validation and
server-side authorization remain in force; failures preserve local diagnostics.
Reference: https://developers.shotgridsoftware.com/python-api/reference.html#shotgun_api3.shotgun.Shotgun.schema_field_read

## Core project publish recovery

`NlSgtkProvider.resume_publish(context, request, receipt, checkpoint)`
resumes registration with the original UUID. `checkpoint(mapping)` must
durably merge the acknowledged `version_id`, `preview_version_id` and
optional `published_files_version_id`. UUID/name/path conflicts fail.
A crash after remote upload but before acknowledgement can repeat the upload
to the same Version. `request.author` is uniquely resolved by HumanUser ID
or login; it never defaults to the automation service identity.
`request.general_publish` retains General Publisher name/description
validation. Core verifies files and Task membership before replay.
Version.entity types unsupported by the site stay linked through sg_task.

Public authentication helpers: ensure_sgtk_user(..., *, allow_interactive=None) and build_shotgun_connection_from_user(..., *, allow_interactive=None) use the same policy as sgtk_login. A noninteractive connection reads the saved token directly and cannot renew through Toolkit UI.

## Linux storage case fallback

`NlSgtkProvider.localize_launch_path()` and `open_core_session()` retry
unavailable Linux paths using optional Core filesystem case matching before
checking storage availability. A configured `/mnt/ALIEN/PROJECTS/123_PROJECT`
can resolve to existing `/mnt/alien/projects/123_PROJECT` without changing the
project name. Exact existing roots retain priority; ambiguous matches and
access errors propagate. Core remains optional for exact schema0 paths. Install
`nl_sgtk[core]` with Core 1.5.3.0 or newer for native sessions.

## Explicit publish file roles

`NlSgtkProvider.register_publish` accepts optional `metadata.file_roles`, a
mapping from exact source paths to one of `sg_path_to_frames`,
`sg_path_to_movie`, `sg_path_to_geometry` or `sg_path_to_script`. It uses the
existing publisher's explicit classification override for interchange or
workflow files whose extension is not configured in the studio. Unknown
fields fail before authentication or registration. Existing classification
remains the default. This does not create or modify PublishedFileTypes.

## Launch thumbnails

`NlSgtkProvider.list_launch_thumbnails(entity_type, entity_id, task_id=0)`
returns optional HTTPS image URLs and labels in Task → entity → Project order.
The Task image is used only when its entity link matches. Unsupported or
unavailable image fields are skipped. This read-only API returns no image
bytes, writes no cache and does not affect launch readiness.


## Prepro task context

`NlSgtkProvider.fetch_task()` enriches `CustomThreadedEntity01` task
contexts with a `prepro` mapping containing portable `name`, `library`
and `library_id`. It reads library links from `sg_type_2_0` and verifies
that the single linked `CustomEntity08` belongs to the task's project.
Missing or multiple links leave these tokens unavailable; enabled nl_core
Prepro policies reject publication instead of guessing a destination.

## Publish request validation (1.7.0.0)

`metadata` is an optional mapping; omission or `None` means no overrides.
`metadata.file_roles` must be a path-to-field mapping. Path matching uses
the provider's portable slash/case normalization. Conflicting roles for the
same normalized source are rejected; files without a role retain automatic
classification. These checks and boolean `metadata.has_slate` validation
run before obtaining a tracker connection or resolving an author.

```python
request["metadata"] = {
    "file_roles": {"/projects/SHOW/work/scene.psd": "sg_path_to_script"},
    "has_slate": False,
}
```

`ShotgunPublish.validate()` counts the description after `.strip()` and
requires `MIN_DESCRIPTION_LENGTH == 3`. Outer whitespace cannot make a
short description valid. The supplied description text is retained.
This standard validator retains its other file, context, range and preview
checks. Explicit trusted/validation-bypass paths retain their existing
contract, including General Publisher's required name/description check.
These additions require no tracker schema migration.
