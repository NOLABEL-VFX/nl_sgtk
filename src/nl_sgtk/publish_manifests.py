"""Normalize Shotgun Versions for Core's local publication index."""
from __future__ import annotations

import re
import json
from typing import Any, Mapping
import uuid


SOURCE_FIELDS = {"frames": "sg_path_to_frames", "movie": "sg_path_to_movie",
                 "geometry": "sg_path_to_geometry", "script": "sg_path_to_script"}
VERSION_FIELDS = ["code", "project", "entity", "sg_task", "sg__publish_uuid",
                  "sg_status_list", "sg_first_frame", "sg_last_frame",
                  "sg_dependiencies", "sg_files_metadata",
                  *SOURCE_FIELDS.values()]


def version_snapshot(row: Mapping[str, Any], tracker: str, *,
                     local_only: bool = False) -> dict[str, Any]:
    """Use the stored publish UUID or a deterministic legacy Version UUID."""
    identity = row.get("sg__publish_uuid")
    if local_only and not identity:
        raise ValueError("A local publication requires its own publish UUID")
    if not identity:
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL,
            tracker.rstrip("/").lower() + "/Version/" + str(row["id"])))
    identity = str(uuid.UUID(str(identity)))
    code = str(row.get("code") or "")
    try:
        metadata = json.loads(row.get("sg_files_metadata") or "{}")
    except (ValueError, TypeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    versions = re.findall(r"(?:^|[_.-])v(\d+)(?:$|[_.-])", code, re.I)
    return {"publish_uuid": identity, "code": code, "metadata": metadata,
        "sync_state": "local_only" if local_only else "registered",
        "project_id": (row.get("project") or {}).get("id"),
        "task_id": (row.get("sg_task") or {}).get("id"),
        "version": int(versions[-1]) if versions else 0,
        "status": row.get("sg_status_list") or "",
        "first_frame": row.get("sg_first_frame"),
        "last_frame": row.get("sg_last_frame"),
        "sources": {role: [part.strip() for part in str(row.get(field) or "").split(";")
                           if part.strip()] for role, field in SOURCE_FIELDS.items()},
        "tracker": {} if local_only else {
                    "provider": "nl_sgtk", "site": tracker.rstrip("/"),
                    "type": "Version", "id": row["id"],
                    "source_fields": dict(SOURCE_FIELDS)},
        "dependencies": [{"type": link["type"], "id": link["id"]}
                         for link in row.get("sg_dependiencies") or []]}


def write_core_manifest(row: Mapping[str, Any], tracker: str, *,
                        local_only: bool = False) -> Any:
    """Mirror a direct publisher Version when its Task is Core-configured."""
    import nl_core
    task = row.get("sg_task") or {}
    if not task.get("id"):
        if local_only:
            raise ValueError("Local publication requires a Task")
        return None
    session = nl_core.open_session(int(task["id"]))
    if session.config.schema_version == 0:
        if local_only:
            raise ValueError("Local publication requires Core configuration")
        return None
    snapshot = version_snapshot(row, tracker, local_only=local_only)
    return session.publish.manifests.import_version(snapshot)
