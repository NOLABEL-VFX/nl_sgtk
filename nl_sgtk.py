from __future__ import annotations
import logging
import os
import webbrowser
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from nl_sgtk_version_check import notify_if_update_available

import sgtk
from shotgun_api3 import shotgun
import re
from urllib.parse import parse_qs, urlparse

log = logging.getLogger(__name__)

# Keep a module version to align with setup.py
__version__ = "0.3.2"

try:
    notify_if_update_available(__version__)
except Exception as exc:
    log.debug("Version update check failed: %s", exc)

# --------------------------------------------------------------------------------------
# Field definitions / constants
# --------------------------------------------------------------------------------------

PROJECT_FIELDS: List[str] = [
    "project.Project.code",
    "project.Project.sg_project_path",
    "project.Project.sg_master_fps",
    "project.Project.sg_master_resolution",
    "project.Project.sg_ocio_config_path",
]

TASK_BASE_FIELDS: List[str] = [
    "entity",
    "sg_status_list",
    "content",
    "step",
    "project",
    "image",
    "start_date",
    "due_date",
]

# Task query also asks for Shot-linked extras (prefixed with entity.Shot.)
TASK_SHOT_FIELDS: List[str] = [
    "entity.Shot.sg_sequence",
    "entity.Shot.assets",
    "entity.Shot.sg_head_in",
    "entity.Shot.sg_tail_out",
    "entity.Shot.sg_scene",
    "entity.Shot.image",
]

SHOT_ENTITY_FIELDS: List[str] = [
    "sg_status_list",
    "code",
    "project",
    "image",
    "sg_sequence",
    "assets",
    "sg_head_in",
    "sg_tail_out",
    "sg_scene",
]

SHOT_ENV_FIELDS: List[str] = [
    "sg_lut_primary",
    "sg_lut_secondary",
    "sg_color_correction_primary",
    "sg_color_correction_secondary",
    "sg_camera_colorspace",
]

ASSET_ENTITY_FIELDS: List[str] = [
    "sg_status_list",
    "code",
    "project",
    "image",
]


ACTIVE_PROJECT_STATUSES: Tuple[str, ...] = ("Active", "Pitch", "Inhouse", "AI")

SHOTGRID_ENV_VAR = "STUDIO_SHOTGUN_LINK"
SHOTGRID_URL = os.environ.get(SHOTGRID_ENV_VAR)

if not SHOTGRID_URL:
    raise RuntimeError(
        f"Please set the {SHOTGRID_ENV_VAR} environment variable for the ShotGrid link."
    )

DEFAULT_PRODUCT = "NL Hub"

_DETAIL_RE = re.compile(r"/detail/(?P<type>[A-Za-z_]\w*)/(?P<id>\d+)(?:/|$)")
_FRAGMENT_RE = re.compile(r"^(?P<type>[A-Za-z_]\w*)_(?P<id>\d+)$")

# --------------------------------------------------------------------------------------
# Query Helpers (API Hidden)
# --------------------------------------------------------------------------------------

def _sg_get_statuses(sg):
    return sg.find("Status", [], ["code", "name"])

@lru_cache(maxsize=1)
def _status_map():
    sg, user = sgtk_login()
    statuses = _sg_get_statuses(sg)
    return {s.get("code"): s.get("name") for s in statuses}

def _status_name(status_code: str) -> str | None:
    return _status_map().get(status_code)


def _parse_shotgrid_entity(url: str) -> Tuple[Optional[str], Optional[int]]:
    """
    Resolve an entity type and ID from common ShotGrid URL forms.

    Supports detail links, page fragments, and task links that expose a
    ``task_id`` query parameter. Use this helper before requesting entity
    context so callers can accept multiple ShotGrid URL formats.

    Args:
        url: ShotGrid URL to parse. Must be a non-empty, absolute URL.

    Returns:
        tuple: ``(entity_type, entity_id)`` when recognized, otherwise
            ``(None, None)``.

    Notes:
        - ``task_id`` query parameters are honored even when the path is not
          ``/my_tasks`` to support alternate ShotGrid task URLs.
    """
    parsed = urlparse(url)

    match = _DETAIL_RE.search(parsed.path)
    if match:
        return match.group("type"), int(match.group("id"))

    if parsed.fragment:
        match = _FRAGMENT_RE.match(parsed.fragment)
        if match:
            return match.group("type"), int(match.group("id"))

    query = parse_qs(parsed.query)
    task_ids = query.get("task_id")
    if task_ids and task_ids[0].isdigit():
        return "Task", int(task_ids[0])

    return None, None

def _require_positive_int(value: Any, name: str = "id") -> int:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _merge_project_meta(target: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize / copy project metadata into consistent keys.
    Mutates and returns `target`.
    """
    project = target.get("project") or {}
    if isinstance(project, dict):
        # Ensure project.code is present (ShotGrid often returns it via project.Project.code)
        code = target.get("project.Project.code")
        if code and not project.get("code"):
            project["code"] = code
        target["project"] = project

    # Copy commonly used project meta to flatter keys too (optional but convenient)
    if target.get("project.Project.sg_project_path"):
        target["sg_project_path"] = target["project.Project.sg_project_path"]
    if target.get("project.Project.sg_master_fps"):
        target["sg_master_fps"] = target["project.Project.sg_master_fps"]

    res = target.get("project.Project.sg_master_resolution")
    left = None
    right = None

    if res and isinstance(res, str):
        match = re.search(r'^(\d+)\s*x\s*(\d+)$', res)
        if match:
            left = int(match.group(1))
            right = int(match.group(2))

    target['master_resolution_width'] = left
    target['master_resolution_height'] = right

    return target


def _task_to_compact_dict(
    task_row: Dict[str, Any], storages: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Convert a Task row returned from sg.find into a consistent, compact dict
    your UI/tools can depend on.

    Notes:
      - Keeps a handful of base fields
      - Adds `fps`, `project_path`
      - If entity is Shot, adds shot-specific context fields
    """
    out: Dict[str, Any] = {k: task_row.get(k) for k in TASK_BASE_FIELDS}

    # project meta
    out["fps"] = task_row.get("project.Project.sg_master_fps")
    out["project_path"] = task_row.get("project.Project.sg_project_path")
    ocio_config_path = task_row.get("project.Project.sg_ocio_config_path")
    if ocio_config_path and storages:
        ocio_config_path = verify_path(ocio_config_path, storages)
    out["ocio_config_path"] = ocio_config_path
    out["env"] = {
        "SHOT_LUT_PRIMARY": "",
        "SHOT_LUT_SECONDARY": "",
        "SHOT_CC_PRIMARY": "",
        "SHOT_CC_SECONDARY": "",
        "SHOT_CAMERA_CS": "",
    }
    res = task_row.get("project.Project.sg_master_resolution")
    left = None
    right = None

    if res and isinstance(res, str):
        match = re.search(r'^(\d+)\s*x\s*(\d+)$', res)
        if match:
            left = int(match.group(1))
            right = int(match.group(2))

    out['master_resolution_width'] = left
    out['master_resolution_height'] = right
    

    project = out.get("project") or {}
    if isinstance(project, dict):
        project_code = task_row.get("project.Project.code")
        if project_code:
            project["code"] = project_code
        out["project"] = project

    # entity context
    entity = out.get("entity")
    is_shot = isinstance(entity, dict) and entity.get("type") == "Shot"

    if is_shot:
        out["assets"] = task_row.get("entity.Shot.assets") or []
        out["first_frame"] = task_row.get("entity.Shot.sg_head_in")
        out["last_frame"] = task_row.get("entity.Shot.sg_tail_out")
        out["sequence"] = task_row.get("entity.Shot.sg_sequence")
        out["scene"] = task_row.get("entity.Shot.sg_scene")

        # If task image is missing, fall back to shot image
        if not out.get("image"):
            out["image"] = task_row.get("entity.Shot.image") or out.get("image")

        env = out["env"]
        if out.get("ocio_config_path"):
            entity = task_row.get("entity")
            if entity and entity.get("type") == "Shot" and entity.get("id"):
                env_entity = task_row.get("_shot_env") or {}
                env["SHOT_LUT_PRIMARY"] = env_entity.get("sg_lut_primary") or ""
                env["SHOT_LUT_SECONDARY"] = env_entity.get("sg_lut_secondary") or ""
                env["SHOT_CC_PRIMARY"] = env_entity.get("sg_color_correction_primary") or ""
                env["SHOT_CC_SECONDARY"] = env_entity.get("sg_color_correction_secondary") or ""
                env["SHOT_CAMERA_CS"] = env_entity.get("sg_camera_colorspace") or ""
    else:
        out.update(
            {
                "assets": [],
                "first_frame": None,
                "last_frame": None,
                "sequence": None,
                "scene": None,
            }
        )

    return out


def _entity_fields(entity_type: str) -> List[str]:
    """
    Returns the fields list for find_one of supported entities.
    """
    if entity_type == "Task":
        return TASK_BASE_FIELDS + PROJECT_FIELDS + TASK_SHOT_FIELDS
    if entity_type == "Shot":
        return SHOT_ENTITY_FIELDS + PROJECT_FIELDS
    if entity_type == "Asset":
        return ASSET_ENTITY_FIELDS + PROJECT_FIELDS
    raise ValueError(f"Unsupported entity type: {entity_type}")


def _fetch_entity_context(
    sg,
    entity_type: str,
    entity_id: int,
) -> Optional[Dict[str, Any]]:
    """
    Generic context fetcher for Task / Shot / Asset.
    """
    _require_positive_int(entity_id, "entity_id")

    fields = _entity_fields(entity_type)
    row = sg.find_one(entity_type, [["id", "is", entity_id]], fields)
    if not row:
        return None

    _merge_project_meta(row)
    _hydrate_entity_env(sg, row, entity_type)

    if 'sg_status_list' in row.keys():
        row['sg_status_list'] = _status_name(row['sg_status_list'])
    return row


def _empty_env_map() -> Dict[str, str]:
    return {
        "SHOT_LUT_PRIMARY": "",
        "SHOT_LUT_SECONDARY": "",
        "SHOT_CC_PRIMARY": "",
        "SHOT_CC_SECONDARY": "",
        "SHOT_CAMERA_CS": "",
    }


def _hydrate_entity_env(sg, row: Dict[str, Any], entity_type: str) -> None:
    """
    Populate row["env"] for parsed entity contexts and normalize ocio path.
    """
    row["env"] = _empty_env_map()

    ocio_config_path = row.get("project.Project.sg_ocio_config_path")
    if ocio_config_path:
        storages = get_storages(sg=sg)
        row["project.Project.sg_ocio_config_path"] = verify_path(ocio_config_path, storages)

    shot_id: Optional[int] = None
    if entity_type == "Task":
        entity = row.get("entity")
        if isinstance(entity, dict) and entity.get("type") == "Shot":
            shot_id = entity.get("id")
    elif entity_type == "Shot":
        shot_id = row.get("id")

    if not shot_id or not row.get("project.Project.sg_ocio_config_path"):
        return

    shot_row = sg.find_one("Shot", [["id", "is", shot_id]], SHOT_ENV_FIELDS) or {}
    row["env"]["SHOT_LUT_PRIMARY"] = shot_row.get("sg_lut_primary") or ""
    row["env"]["SHOT_LUT_SECONDARY"] = shot_row.get("sg_lut_secondary") or ""
    row["env"]["SHOT_CC_PRIMARY"] = shot_row.get("sg_color_correction_primary") or ""
    row["env"]["SHOT_CC_SECONDARY"] = shot_row.get("sg_color_correction_secondary") or ""
    row["env"]["SHOT_CAMERA_CS"] = shot_row.get("sg_camera_colorspace") or ""


def _project_fields() -> List[str]:
    return [
        "code",
        "name",
        "sg_status",
        "is_template",
        "sg_project_path",
        "sg_master_fps",
        "sg_master_resolution",
        "image",
    ]

# --------------------------------------------------------------------------------------
# Shotgun Toolkit helpers (public but technical)
# --------------------------------------------------------------------------------------

def get_user():
    """
    Return the user, login if needed.
    The return value is 
    {
        "type" : "HumanUser"
        "id" : 123,
        "name" : "John Doe",
        "_login": "johndoe"    
    }    
    """
    sg, user = sgtk_login()
    return user

def ensure_sgtk_user(
    base_url: str = SHOTGRID_URL,
    product: str = DEFAULT_PRODUCT,
) -> Optional[sgtk.authentication.ShotgunUser]:
    """
    Return the default SGTK user if available.
    If no default user exists, trigger manual login (browser) and try again.
    """
    authenticator = sgtk.authentication.ShotgunAuthenticator()

    user = authenticator.get_default_user()
    if user:
        return user

    # Trigger interactive login + cache session
    log.info("No default SGTK user found. Launching interactive login for %s", product)
    launch_interactive_login(base_url=base_url, product=product)

    # Try again after caching session
    user = authenticator.get_default_user()

    return user


def launch_interactive_login(
    base_url: str = SHOTGRID_URL,
    product: str = DEFAULT_PRODUCT,
    browser_open_callback=webbrowser.open,
) -> None:
    """
    Perform app-session login and cache the session data so SGTK can pick it up later.
    """
    session_data = sgtk.authentication.app_session_launcher.process(
        base_url,
        product=product,
        browser_open_callback=lambda url: browser_open_callback(url),
    )

    # session_data is typically: (host, login, session_token, session_metadata)
    sgtk.authentication.session_cache.cache_session_data(*session_data)
    sgtk.authentication.session_cache.set_current_user(host=session_data[0], login=session_data[1]) #Update user


def build_shotgun_connection_from_user(
    user: sgtk.authentication.ShotgunUser,
    base_url: str = SHOTGRID_URL,
) -> shotgun.Shotgun:
    """
    Create a shotgun_api3.Shotgun instance using the user's session token.
    """
    sg_sgtk = user.create_sg_connection()
    token = sg_sgtk.get_session_token()
    if not token:
        raise RuntimeError("SGTK connection did not provide a session token.")

    return shotgun.Shotgun(base_url, session_token=token)


def validate_connection(sg: shotgun.Shotgun) -> bool:
    """
    Simple sanity test to confirm the token works.
    """
    filters = [["sg_status", "is", "active"]]
    fields = ["name", "project"]
    results = sg.find("Project", filters, fields)
    return bool(results)


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------

def sgtk_login(
    base_url: str = SHOTGRID_URL,
    product: str = DEFAULT_PRODUCT,
) -> Tuple[Optional[shotgun.Shotgun], Optional[sgtk.authentication.ShotgunUser]]:
    """
    Returns (sg, user) on success, otherwise (None, None).
    """
    try:
        user = ensure_sgtk_user(base_url=base_url, product=product)
        if not user:
            log.error("User authentication failed: no default user after login.")
            return None, None

        sg = build_shotgun_connection_from_user(user, base_url=base_url)

        try:
            if not validate_connection(sg):
                log.error("User test failed: connection works but query returned no results.")
                return None, None
        except shotgun.AuthenticationFault as e:
            log.error(f"{e} / Retrying login with session refresh...")
            launch_interactive_login(base_url=base_url, product=product)
            user = ensure_sgtk_user(base_url=base_url, product=product)
            sg = build_shotgun_connection_from_user(user, base_url=base_url)
            if not validate_connection(sg):
                return None, None
            
        user = sg.find_one("HumanUser", [['login', 'is', user.login]], ['name', 'id', 'login'])
        if "@" in user['login']:
            user['_login'] = user['login'].split("@")[0]
        else:
            user['_login'] = user['login']
        
        del user['login']

        return sg, user

    except Exception as exc:
        # In production you may want narrower exceptions, but keep one catch here for the public API.
        log.exception("SGTK/ShotGrid auth failed: %s", exc)
        return None, None

def get_user_tasks(user: Dict[str, Any], sg=None) -> List[Dict[str, Any]]:
    """
    Gather tasks assigned to a user with important metadata in a stable shape.
    Returns an empty list if none found.

    Args:
        sg: Active ShotGrid API connection.
        user: ShotGrid HumanUser dict (must contain 'id').

    Returns:
        List[dict]: Compact task dictionaries.
    """

    if not sg:
        sg, user = sgtk_login()

    user_id = _require_positive_int(user.get("id"), "user['id']")

    filters = [
        ["task_assignees.HumanUser.id", "is", user_id],
        ["project.Project.sg_status", "in", list(ACTIVE_PROJECT_STATUSES)],
        ["project.Project.is_template", "is", False],
    ]

    fields = TASK_BASE_FIELDS + PROJECT_FIELDS + TASK_SHOT_FIELDS

    rows = sg.find("Task", filters, fields) or []
    storages = get_storages(sg=sg)
    shot_ids = sorted(
        {
            row["entity"]["id"]
            for row in rows
            if isinstance(row.get("entity"), dict)
            and row["entity"].get("type") == "Shot"
            and row["entity"].get("id")
        }
    )
    shot_env_map: Dict[int, Dict[str, Any]] = {}
    if shot_ids:
        shot_rows = sg.find("Shot", [["id", "in", shot_ids]], SHOT_ENV_FIELDS) or []
        shot_env_map = {shot["id"]: shot for shot in shot_rows}

    for row in rows:
        entity = row.get("entity")
        if isinstance(entity, dict) and entity.get("type") == "Shot":
            row["_shot_env"] = shot_env_map.get(entity.get("id"), {})

    return [_task_to_compact_dict(r, storages=storages) for r in rows]


def get_storages(sg=None) -> List[Dict[str, Any]]:
    """
    Return LocalStorage mappings used for cross-platform path normalization.
    """
    if sg:
        return sg.find(
            "LocalStorage",
            [],
            ["code", "windows_path", "linux_path", "mac_path"],
        )
    return _get_storages_cached()


@lru_cache(maxsize=1)
def _get_storages_cached() -> List[Dict[str, Any]]:
    sg, user = sgtk_login()
    return sg.find(
        "LocalStorage",
        [],
        ["code", "windows_path", "linux_path", "mac_path"],
    )


def verify_path(path, storages, system=None):
    """
    Normalize a ShotGrid path to the requested operating-system storage root.

    Use this helper when a path may originate from a different platform than
    the current NL Hub session. The function compares known LocalStorage
    prefixes and rewrites the prefix to the current platform while preserving
    the trailing relative segments.

    Args:
        path: Raw path value from ShotGrid or endpoint payload.
        storages: List of LocalStorage dictionaries containing platform roots.
        system: Optional os.name override (for example ``"nt"`` or ``"posix"``).

    Returns:
        Normalized path that uses forward slashes and platform-correct storage
        prefix when a mapping is available.

    Raises:
        ValueError: If ``path`` is not a string.
        ValueError: If ``storages`` is not a list.

    Side Effects:
        - None.

    Notes:
        - ``os.name`` is used by default to determine the current platform.
        - Storage mappings require both Windows and Linux paths to be defined,
          mirroring legacy NL Hub behavior.
    """
    if type(path) not in [str]:
        raise ValueError("Path is not a string or Path object, cannot be processed!")

    if not isinstance(storages, list):
        raise ValueError("Storages must be a list of dictionaries.")

    platform_map = {
        "nt": "windows",
        "posix": "linux" if os.name != "darwin" else "mac",
    }
    if system:
        current_platform = platform_map.get(system, "linux")
    else:
        current_platform = platform_map.get(os.name, "linux")

    storage_map = {}
    for storage in storages:
        paths = {
            "windows": storage.get("windows_path"),
            "linux": storage.get("linux_path"),
            "mac": storage.get("mac_path"),
        }
        if paths["windows"] and paths["linux"]:
            for value in paths.values():
                if value:
                    storage_map[value.replace("\\", "/").lower()] = paths[current_platform]

    normalized_path = path.replace("\\", "/").lower()
    for old_prefix, new_prefix in storage_map.items():
        if normalized_path.startswith(old_prefix):
            output = os.path.normpath(new_prefix + path[len(old_prefix):])
            output = output.replace("\\", "/")
            return output

    output = os.path.normpath(path)
    output = output.replace("\\", "/")
    return output


def get_task_context(task_id: int, sg=None) -> Optional[Dict[str, Any]]:
    """
    Fetch ShotGrid Task metadata for a specific Task ID.
    """
    if not sg:
        sg, user = sgtk_login()

    return _fetch_entity_context(sg, "Task", task_id)


def get_entity_context(entity_type: str, entity_id: int, sg=None) -> Optional[Dict[str, Any]]:
    """
    Fetch entity metadata for a supported ShotGrid entity type.

    Args:
        entity_type: One of "Task", "Shot", or "Asset".
        entity_id: Entity ID to query.
    """
    if not sg:
        sg, user = sgtk_login()

    return _fetch_entity_context(sg, entity_type, entity_id)


def get_shot_context(shot_id: int, sg=None) -> Optional[Dict[str, Any]]:
    """
    Fetch ShotGrid Shot metadata for a specific Shot ID.
    """

    if not sg:
        sg, user = sgtk_login()

    return _fetch_entity_context(sg, "Shot", shot_id)


def get_asset_context(asset_id: int, sg=None) -> Optional[Dict[str, Any]]:
    """
    Fetch ShotGrid Asset metadata for a specific Asset ID.
    """

    if not sg:
        sg, user = sgtk_login()


    return _fetch_entity_context(sg, "Asset", asset_id)


def get_project_context(project_id: int, sg=None) -> Optional[Dict[str, Any]]:
    """
    Fetch ShotGrid Project metadata for a specific Project ID.
    """
    if not sg:
        sg, user = sgtk_login()

    _require_positive_int(project_id, "project_id")
    row = sg.find_one("Project", [["id", "is", project_id]], _project_fields())
    if not row:
        return None
    if "sg_status" in row:
        row["sg_status"] = _status_name(row.get("sg_status")) or row.get("sg_status")
    return row


def list_active_projects(sg=None) -> List[Dict[str, Any]]:
    """
    Return active, non-template projects with basic metadata.
    """
    if not sg:
        sg, user = sgtk_login()

    filters = [
        ["sg_status", "in", list(ACTIVE_PROJECT_STATUSES)],
        ["is_template", "is", False],
    ]
    rows = sg.find("Project", filters, _project_fields()) or []
    for row in rows:
        if "sg_status" in row:
            row["sg_status"] = _status_name(row.get("sg_status")) or row.get("sg_status")
    return rows


def parse_link(link: str, sg=None) -> Optional[Dict[str, Any]]:
    """
    Resolve entity context from a ShotGrid URL/path.

    Returns:
        dict | None: Entity context for Task, Shot, or Asset.
    """

    if not sg:
        sg, user = sgtk_login()

    entity_type, entity_id = _parse_shotgrid_entity(link)  # assumed to exist
    _require_positive_int(entity_id, "entity_id")

    if entity_type not in {"Task", "Shot", "Asset"}:
        raise ValueError(
            f"{entity_type}:{entity_id} not recognised as Task, Shot, or Asset and cannot be resolved to context"
        )

    return _fetch_entity_context(sg, entity_type, entity_id)
