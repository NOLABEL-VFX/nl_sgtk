from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import os
import pathlib
import re
import sqlite3
import traceback
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

from shotgun_api3 import shotgun

from .nl_sgtk import SHOTGRID_URL, get_storages, parse_link, sgtk_login, verify_path


log = logging.getLogger(__name__)

_VERSION_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?P<full>v0{0,4}(?P<num>[1-9][0-9]{0,4}))(?![A-Za-z0-9])"
)
_CONTEXT_LOGGER_DB = pathlib.Path.home() / ".nolabel" / ".data" / "context_logger.db"
_PUBLISH_LOG_TABLE = "shotgun_publish_log"


def published_files_enabled(project: Optional[Dict[str, Any]] = None) -> bool:
    """Return whether PublishedFile registration is enabled.

    ``project`` is accepted now so this function can read a project setting in
    a future release. PublishedFile registration is intentionally disabled
    globally until that integration is implemented.
    """
    del project
    return False


class PublishedFilePublishError(RuntimeError):
    """Raised when Version creation succeeds but PublishedFile creation fails."""

    def __init__(self, version_id: int, publish_uuid: str) -> None:
        self.version_id = version_id
        self.publish_uuid = publish_uuid
        super().__init__(
            "PublishedFile creation failed after creating Version "
            f"{version_id} (publish UUID: {publish_uuid})."
        )


@dataclass
class _PublishData:
    code: str
    name: str
    path: Dict[str, str]
    sg_path_string: str
    entity: Optional[dict]
    task: Optional[dict]
    project: Optional[dict]
    published_file_type: Optional[dict]
    version: Optional[dict]
    version_number: Optional[int]
    description: str

    def to_payload(self) -> Dict[str, Any]:
        return {
            "request_type": "create",
            "entity_type": "PublishedFile",
            "data": asdict(self),
        }


def _split_name_and_version(filename: str) -> Tuple[str, Optional[str], Optional[int]]:
    name = pathlib.Path(filename).name
    match = _VERSION_RE.search(name)
    if not match:
        return name, None, None
    base = name[: match.start()].rstrip("._-")
    return base, match.group("full"), int(match.group("num"))


def _norm_sequence(path: str) -> str:
    try:
        import fileseq  # type: ignore

        seq = fileseq.findSequenceOnDisk(path)
        return os.path.normpath(
            "".join(
                [
                    seq.dirname(),
                    seq.basename(),
                    "#" * len(str(seq.end())),
                    seq.extension(),
                ]
            )
        ).replace(os.sep, "/")
    except Exception:
        return path


def _compact_entity(entity: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not entity:
        return None
    valid_keys = {"type", "id", "name", "code", "content"}
    return {key: value for key, value in entity.items() if key in valid_keys}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entity_id(entity: Optional[Dict[str, Any]]) -> Optional[int]:
    if not isinstance(entity, dict):
        return None
    entity_id = entity.get("id")
    return entity_id if isinstance(entity_id, int) else None


def _ensure_context_logger_schema(db_path: Optional[pathlib.Path] = None) -> None:
    db_path = db_path or _CONTEXT_LOGGER_DB
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_context_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                context_date TEXT NOT NULL,
                project_id INTEGER,
                shot_id INTEGER,
                task_id INTEGER,
                dcc TEXT,
                timestamp TEXT NOT NULL
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_PUBLISH_LOG_TABLE} (
                publish_uuid TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                project_id INTEGER,
                entity_type TEXT,
                entity_id INTEGER,
                task_id INTEGER,
                version_id INTEGER,
                version_code TEXT,
                export_path TEXT,
                preview_path TEXT,
                payload_json TEXT NOT NULL,
                error TEXT
            )
            """
        )


class ShotgunPublish:
    """Create ShotGrid Version and PublishedFile records for a publish payload.

    The class is ported from ``nl_drop_handlers.registry.ShotgunPublish`` and
    keeps the same common setters while adding explicit trust/validation
    overrides for JSON-driven or already-verified publish payloads.
    """

    SCRIPT = "sg_path_to_script"
    MOVIE = "sg_path_to_movie"
    GEOMETRY = "sg_path_to_geometry"
    FRAMES = "sg_path_to_frames"

    FILE_FIELDS = (FRAMES, MOVIE, GEOMETRY, SCRIPT)

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        script_user: Optional[str] = None,
        script_key: Optional[str] = None,
        override_user: Optional[str] = None,
        sg: Optional[shotgun.Shotgun] = None,
        user: Optional[Dict[str, Any]] = None,
        validate_paths: bool = True,
        trusted: bool = False,
    ) -> None:
        self.script_user = script_user
        self.script_key = script_key
        self.logger = logger or logging.getLogger("ShotgunPublish")
        self.validate_paths = validate_paths
        self.trusted = trusted
        self.publish_uuid = str(uuid.uuid4())

        if sg:
            self.sg = sg
            self.user = user
        elif self.script_user and self.script_key:
            if override_user:
                self.sg = shotgun.Shotgun(
                    SHOTGRID_URL,
                    script_name=self.script_user,
                    api_key=self.script_key,
                    sudo_as_login=override_user,
                )
                self.user = self.sg.find_one(
                    "HumanUser", [["login", "is", override_user]], ["name", "id", "login"]
                )
            else:
                self.sg = shotgun.Shotgun(
                    SHOTGRID_URL,
                    script_name=self.script_user,
                    api_key=self.script_key,
                )
                self.user = user
        else:
            self.sg, self.user = sgtk_login()

        if not self.sg:
            raise ConnectionRefusedError("Failed to connect to ShotGrid.")

        self.storages = get_storages(sg=self.sg)
        self.published_file_types = self.sg.find(
            "PublishedFileType", [], ["code", "tags", "sg_extensions"]
        )
        self.valid_extensions_map = self.valid_extensions()
        self.valid_extenions = self.valid_extensions_map
        self.preview: Optional[str] = None

        self.context = {
            "project": None,
            "entity": None,
            "task": None,
        }
        self.version = self._empty_version()

    def _empty_version(self) -> Dict[str, Any]:
        return {
            "project": None,
            "entity": None,
            "sg_task": None,
            "code": None,
            self.MOVIE: [],
            self.FRAMES: [],
            self.GEOMETRY: [],
            self.SCRIPT: [],
            "sg_first_frame": 1,
            "sg_last_frame": 1,
            "sg_files_metadata": None,
            "sg_files_metadata__short_": None,
            "description": None,
            "sg_published_file_types": [],
            "sg_dependiencies": [],
            "sg__publish_uuid": self.publish_uuid,
        }

    def _register_publish_state(
        self,
        status: str,
        payload: Optional[Dict[str, Any]] = None,
        export_path: Optional[str] = None,
        version_id: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        payload = payload or self.retrieve_version_info(validate=False)
        payload["sg__publish_uuid"] = self.publish_uuid

        project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
        entity = payload.get("entity") if isinstance(payload.get("entity"), dict) else {}
        task = payload.get("sg_task") if isinstance(payload.get("sg_task"), dict) else {}
        created_at = _utc_now()
        updated_at = created_at

        _ensure_context_logger_schema()
        with sqlite3.connect(_CONTEXT_LOGGER_DB) as connection:
            existing = connection.execute(
                f"SELECT created_at FROM {_PUBLISH_LOG_TABLE} WHERE publish_uuid = ?",
                (self.publish_uuid,),
            ).fetchone()
            if existing:
                created_at = existing[0]

            connection.execute(
                f"""
                INSERT INTO {_PUBLISH_LOG_TABLE} (
                    publish_uuid,
                    status,
                    created_at,
                    updated_at,
                    project_id,
                    entity_type,
                    entity_id,
                    task_id,
                    version_id,
                    version_code,
                    export_path,
                    preview_path,
                    payload_json,
                    error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(publish_uuid) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    project_id = excluded.project_id,
                    entity_type = excluded.entity_type,
                    entity_id = excluded.entity_id,
                    task_id = excluded.task_id,
                    version_id = excluded.version_id,
                    version_code = excluded.version_code,
                    export_path = COALESCE(excluded.export_path, {_PUBLISH_LOG_TABLE}.export_path),
                    preview_path = excluded.preview_path,
                    payload_json = excluded.payload_json,
                    error = excluded.error
                """,
                (
                    self.publish_uuid,
                    status,
                    created_at,
                    updated_at,
                    _entity_id(project),
                    entity.get("type"),
                    _entity_id(entity),
                    _entity_id(task),
                    version_id or payload.get("id"),
                    payload.get("code"),
                    export_path,
                    self.preview,
                    json.dumps(payload, default=str, sort_keys=True),
                    error,
                ),
            )

    def set_logger(self, logger: logging.Logger) -> None:
        self.logger = logger

    def set_sudo(self, user: Union[int, str]) -> bool:
        """Set a sudo user for the publish artist when using a session token."""
        if not isinstance(user, (int, str)):
            raise ValueError(
                f"set_sudo expects an integer id or string login. Got {type(user)}."
            )

        if isinstance(user, int):
            sg_user = self.sg.find_one("HumanUser", [["id", "is", user]], ["name", "id", "login"])
            if not sg_user:
                raise ValueError(f"Failed to find user with ID: {user}")
        else:
            sg_user = self.sg.find_one(
                "HumanUser", [["login", "is", user]], ["name", "id", "login"]
            )
            if not sg_user:
                raise ValueError(f"Failed to find user with login: {user}")

        session_token = self.sg.get_session_token()
        self.sg = shotgun.Shotgun(
            SHOTGRID_URL,
            session_token=session_token,
            sudo_as_login=sg_user["login"],
        )
        self.user = sg_user
        self.version["user"] = sg_user
        return True

    def set_vendor_by_name(self, vendor_name: str) -> bool:
        if not isinstance(vendor_name, str):
            raise ValueError("Vendor name must be a string.")

        vendor = self.sg.find_one(
            "Group",
            [["code", "is", vendor_name], ["tags.Tag.name", "is", "Vendor"]],
            ["code", "id"],
        )
        if not vendor:
            raise ValueError(f"Failed to find vendor with name: {vendor_name}")

        self.version["user"] = vendor
        return True

    def add_dependiency(self, path: str) -> None:
        self.version.setdefault("sg_dependiencies", []).append(path)

    def add_dependency(self, path: str) -> None:
        self.add_dependiency(path)

    def valid_extensions(self) -> Dict[str, List[str]]:
        valid_extensions = {
            "File": [],
            "Movie": [],
            "Geometry": [],
            "Script": [],
        }

        for file_type in self.published_file_types:
            tags = file_type.get("tags") or []
            for tag in tags:
                tag_name = tag.get("name") if isinstance(tag, dict) else None
                if tag_name in valid_extensions:
                    extensions = [
                        ext.strip().lower()
                        for ext in (file_type.get("sg_extensions") or "").split(",")
                        if ext.strip()
                    ]
                    valid_extensions[tag_name] += extensions
        return valid_extensions

    def check_file_with_extensions(self, file_path: str) -> Optional[str]:
        remap = {
            "Movie": self.MOVIE,
            "Geometry": self.GEOMETRY,
            "Script": self.SCRIPT,
            "File": self.FRAMES,
        }

        lower_path = file_path.lower()
        for key, value in self.valid_extensions_map.items():
            if any(lower_path.endswith(extension) for extension in value):
                return remap[key]
        return None

    def find_published_fileType(self, file_path: str) -> List[Dict[str, Any]]:
        found = []
        lower_path = file_path.lower()

        for file_type in self.published_file_types:
            extensions = [
                ext.strip().lower()
                for ext in (file_type.get("sg_extensions") or "").split(",")
                if ext.strip()
            ]
            if any(lower_path.endswith(extension) for extension in extensions):
                if file_type not in self.version["sg_published_file_types"]:
                    self.version["sg_published_file_types"].append(file_type)
                found.append(file_type)

        return found

    def _verify_existing_directory(self, file_path: str) -> None:
        directory = os.path.dirname(file_path)
        if directory and not os.path.exists(verify_path(directory, self.storages)):
            raise ValueError(f"Path to file does not exist: {directory}")

    def _resolve_context_string(self, context: str) -> Dict[str, Any]:
        matches = re.findall(r"(\w+):(\d+)", context)
        if matches:
            resolved: Dict[str, Any] = {"project": None, "entity": None, "task": None}
            for entity_type, entity_id in matches[:3]:
                entity_id_int = int(entity_id)
                if entity_type == "Project":
                    resolved["project"] = self.sg.find_one(
                        "Project", [["id", "is", entity_id_int]], ["name", "code"]
                    )
                elif entity_type == "Task":
                    task = self.sg.find_one(
                        "Task",
                        [["id", "is", entity_id_int]],
                        ["content", "entity", "project"],
                    )
                    resolved["task"] = task
                    if task and not resolved["entity"]:
                        resolved["entity"] = task.get("entity")
                    if task and not resolved["project"]:
                        resolved["project"] = task.get("project")
                else:
                    entity = self.sg.find_one(
                        entity_type,
                        [["id", "is", entity_id_int]],
                        ["code", "name", "project"],
                    )
                    resolved["entity"] = entity
                    if entity and not resolved["project"]:
                        resolved["project"] = entity.get("project")
            return resolved

        row = parse_link(context, sg=self.sg)
        if not row:
            raise ValueError(f"Could not resolve context from string: {context}")

        if row.get("type") == "Task":
            return {
                "project": row.get("project"),
                "entity": row.get("entity"),
                "task": {
                    "type": "Task",
                    "id": row.get("id"),
                    "content": row.get("content"),
                },
            }

        return {
            "project": row.get("project"),
            "entity": {
                "type": row.get("type"),
                "id": row.get("id"),
                "code": row.get("code"),
                "name": row.get("name"),
            },
            "task": None,
        }

    def set_context(
        self,
        context: Optional[Union[Dict[str, Any], str]] = None,
        validate: Optional[bool] = None,
    ) -> None:
        if context is None:
            context = self.context

        if isinstance(context, str):
            context = self._resolve_context_string(context)
        elif not isinstance(context, dict):
            raise ValueError("Context must be a dictionary or a string.")

        should_validate = (not self.trusted) if validate is None else validate
        project = _compact_entity(context.get("project"))
        entity = _compact_entity(context.get("entity"))
        task = _compact_entity(context.get("task") or context.get("sg_task"))

        if should_validate and not project:
            raise ValueError("Context does not contain project value.")
        if should_validate and (not entity or not task):
            raise ValueError("Context does not contain entity or task value.")

        self.context = {
            "project": project,
            "entity": entity,
            "task": task,
        }
        self.version["project"] = project
        self.version["entity"] = entity
        self.version["sg_task"] = task

    def set_description(self, description: str) -> None:
        if not isinstance(description, str):
            raise ValueError("Description must be a string.")
        self.version["description"] = description

    def set_version_name(self, code: str) -> None:
        if not isinstance(code, str):
            raise ValueError("Version name must be a string.")
        self.version["code"] = code

    def movie_has_slate(self, boolean: Optional[bool]) -> None:
        if not isinstance(boolean, bool) and boolean is not None:
            raise ValueError("Movie has slate flag must be a boolean or None.")
        self.version["sg_movie_has_slate"] = boolean

    def set_preview_file(self, file_path: str, verify: Optional[bool] = None) -> None:
        if not isinstance(file_path, str):
            raise ValueError("Preview file must be a string.")

        should_verify = self.validate_paths if verify is None else verify
        if should_verify:
            self._verify_existing_directory(file_path)

        if any(symbol in file_path for symbol in ["$", "#", "%"]):
            raise ValueError("Preview file cannot be a sequence.")

        self.preview = file_path

    def add_file(
        self,
        file_path: str,
        force_version_field: Optional[str] = None,
        verify: Optional[bool] = None,
    ) -> None:
        if not isinstance(file_path, str):
            raise ValueError("Path to files must be a string.")

        should_verify = self.validate_paths if verify is None else verify
        if should_verify:
            self._verify_existing_directory(file_path)

        version_field = force_version_field or self.check_file_with_extensions(file_path)
        if version_field not in self.FILE_FIELDS:
            raise ValueError(
                "File extension is not supported. Pass force_version_field to override."
            )

        self.find_published_fileType(file_path)

        if version_field == self.MOVIE and not self.preview:
            self.preview = file_path

        self.version[version_field].append(file_path)

    def set_frame_range(self, first_frame: int, last_frame: int) -> None:
        if not isinstance(first_frame, int) or not isinstance(last_frame, int):
            raise ValueError("Frame range must be integers.")
        self.version["sg_first_frame"] = first_frame
        self.version["sg_last_frame"] = last_frame

    def set_metadata(self, metadata: str) -> None:
        if not isinstance(metadata, str):
            raise ValueError("Metadata must be a string.")
        self.version["sg_files_metadata"] = metadata

    def set_short_metadata(self, metadata: str) -> None:
        if not isinstance(metadata, str):
            raise ValueError("Metadata must be a string.")
        self.version["sg_files_metadata__short_"] = metadata

    def _coerce_imported_version(self, data: Dict[str, Any]) -> Dict[str, Any]:
        version = self._empty_version()
        version.update(data)
        for key in self.FILE_FIELDS:
            value = version.get(key)
            if isinstance(value, str):
                version[key] = [part for part in value.split(";") if part]
            elif value is None:
                version[key] = []
        version.setdefault("sg_dependiencies", [])
        version.setdefault("sg_published_file_types", [])
        return version

    def import_data(
        self,
        data: Dict[str, Any],
        validate: bool = True,
        trusted: bool = False,
    ) -> None:
        if not isinstance(data, dict):
            raise ValueError("Publish data must be a dictionary.")

        self.version = self._coerce_imported_version(data)
        self.version["sg__publish_uuid"] = self.publish_uuid
        self.context = {
            "project": self.version.get("project"),
            "entity": self.version.get("entity"),
            "task": self.version.get("sg_task"),
        }

        if trusted:
            self.trusted = True
            return
        if validate:
            self.validate()

    def import_from_json(
        self,
        file_path: str,
        validate: bool = True,
        trusted: bool = False,
    ) -> None:
        if not isinstance(file_path, str):
            raise ValueError("File path must be a string.")
        if not os.path.exists(file_path):
            raise ValueError("File path does not exist.")

        with open(file_path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
        self.import_data(data, validate=validate, trusted=trusted)

    def publish_request_from_file(
        self,
        path: str,
        version: Dict[str, Any],
    ) -> Dict[str, Any]:
        name, _, version_number = _split_name_and_version(pathlib.Path(path).name)
        filetypes = self.find_published_fileType(path)
        filetype = filetypes[0] if filetypes else None

        data = _PublishData(
            code=pathlib.Path(path).name,
            name=name,
            path={"local_path": path},
            sg_path_string=path,
            entity=self.context.get("entity"),
            task=self.context.get("task"),
            project=self.context.get("project"),
            published_file_type=filetype,
            version=version,
            version_number=version_number,
            description=version.get("description", ""),
        )
        return data.to_payload()

    def published_files_enabled(self) -> bool:
        """Return the central PublishedFile feature flag for this project."""
        project = self.context.get("project") or self.version.get("project") or {}
        return published_files_enabled(project)

    def validate(self, require_preview: bool = True) -> None:
        if not any(self.version[key] for key in self.FILE_FIELDS):
            raise ValueError("No files to publish.")
        if not self.version["code"]:
            raise ValueError("No version name set.")
        if not self.version["description"] or len(self.version["description"]) < 5:
            raise ValueError("Invalid description.")
        if self.version["sg_first_frame"] is None or self.version["sg_last_frame"] is None:
            raise ValueError("Frame range is not set.")
        if not self.version["project"]:
            raise ValueError("Project is not set.")
        if not self.version["entity"]:
            raise ValueError("Entity is not set.")
        if not self.version["sg_task"]:
            raise ValueError("Task is not set.")

        if self.script_user and self.script_key and self.user:
            self.version["user"] = self.user

        if not self.preview and self.version[self.MOVIE]:
            self.preview = self.version[self.MOVIE][0]
        elif not self.preview and self.version[self.FRAMES]:
            for path in self.version[self.FRAMES]:
                if not any(symbol in path for symbol in ["#", "%", "$"]):
                    self.preview = path
                    break

        if require_preview and not self.preview:
            raise ValueError("No preview file found.")

    def extract_filepaths(self) -> List[str]:
        filepaths = []
        for key in self.FILE_FIELDS:
            value = self.version[key]
            if isinstance(value, list):
                filepaths.extend(verify_path(path, self.storages) for path in value)
            elif isinstance(value, str):
                filepaths.append(verify_path(value, self.storages))
        return filepaths

    def export_to_json(self, file_path: str, validate: bool = True) -> None:
        if not isinstance(file_path, str):
            raise ValueError("File path must be a string.")
        if validate:
            self.validate()

        payload = self.retrieve_version_info(validate=False)

        with open(file_path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=4)

        self._register_publish_state("exported", payload=payload, export_path=file_path)

    def find_dependent_versions(self) -> List[Dict[str, Any]]:
        dependencies = self.version.get("sg_dependiencies") or []
        resolved_dependencies = [
            dependency for dependency in dependencies if isinstance(dependency, dict)
        ]
        paths = [dependency for dependency in dependencies if isinstance(dependency, str)]
        if not paths:
            return resolved_dependencies

        paths = [verify_path(os.path.normpath(path), self.storages) for path in paths]
        fields = ["sg_task", self.SCRIPT, self.MOVIE, self.GEOMETRY, self.FRAMES, "code"]
        entity = self.version.get("entity") or {}
        if not entity.get("type") or not entity.get("id"):
            return resolved_dependencies

        versions = self.sg.find(
            "Version",
            [[f"entity.{entity['type']}.id", "is", entity["id"]]],
            fields,
        ) or []
        if entity["type"] == "Shot":
            versions += self.sg.find(
                "Version",
                [[f"entity.Asset.shots.{entity['type']}.id", "is", entity["id"]]],
                fields,
            ) or []

        matched_versions = list(resolved_dependencies)
        seen = {
            dependency_id
            for dependency in resolved_dependencies
            for dependency_id in [dependency.get("id") or dependency.get("code")]
            if dependency_id is not None
        }
        for version in versions:
            for key in self.FILE_FIELDS:
                path_value = version.get(key)
                if not path_value:
                    continue

                check_paths = paths
                path_value = verify_path(os.path.normpath(path_value), self.storages)
                if key == self.FRAMES:
                    path_value = _norm_sequence(path_value)
                    check_paths = [_norm_sequence(path) for path in paths]

                if path_value.lower() in [path.lower() for path in check_paths]:
                    version_id = version.get("id") or version.get("code")
                    if version_id not in seen:
                        matched_versions.append(version)
                        seen.add(version_id)
                    break
        return matched_versions

    def retrieve_version_info(self, validate: bool = True) -> Dict[str, Any]:
        if validate and not self.trusted:
            self.validate()

        version = self.version.copy()
        version["sg__publish_uuid"] = self.publish_uuid
        for key in self.FILE_FIELDS:
            if isinstance(self.version[key], list):
                version[key] = ";".join(
                    verify_path(path, self.storages, system="nt")
                    for path in self.version[key]
                )

        version["sg_dependiencies"] = self.find_dependent_versions()
        return version

    def publish(self, validate: bool = True, upload_preview: bool = True) -> Dict[str, Any]:
        if validate and not self.trusted:
            self.validate()

        version_payload = self.retrieve_version_info(validate=False)
        self._register_publish_state("publishing", payload=version_payload)

        try:
            version = self.sg.create("Version", version_payload)
        except Exception as exc:
            self._register_publish_state(
                "failed",
                payload=version_payload,
                error=traceback.format_exc(),
            )
            raise

        if upload_preview and self.preview:
            try:
                self.sg.upload("Version", version["id"], self.preview, "sg_uploaded_movie")
            except Exception:
                version_payload["id"] = version["id"]
                self._register_publish_state(
                    "failed",
                    payload=version_payload,
                    version_id=version["id"],
                    error=traceback.format_exc(),
                )
                raise

        publish_error = None
        if self.published_files_enabled():
            try:
                requests = [
                    self.publish_request_from_file(file_path, version)
                    for file_path in self.extract_filepaths()
                ]
                if requests:
                    self.sg.batch(requests)
            except Exception:
                publish_error = traceback.format_exc()
                self.logger.error("Failed to create PublishedFile records:\n%s", publish_error)

        self.version["id"] = version["id"]
        version_payload["id"] = version["id"]
        self._register_publish_state(
            "published_with_errors" if publish_error else "published",
            payload=version_payload,
            version_id=version["id"],
            error=publish_error,
        )
        if publish_error:
            raise PublishedFilePublishError(
                int(version["id"]),
                self.publish_uuid,
            )
        return self.version
