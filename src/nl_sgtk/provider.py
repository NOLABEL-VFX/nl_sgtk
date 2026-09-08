from __future__ import annotations

import threading
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


# ShotgunPublish.FILE_FIELDS use semicolon lists in retrieve_version_info()
# and _coerce_imported_version(). Custom fields have no such contract.
_PUBLISHER_PATH_FIELDS = frozenset({
    "sg_path_to_frames", "sg_path_to_movie", "sg_path_to_geometry",
    "sg_path_to_script",
})
_ACTIVE_PROJECT_STATUSES = ("Active", "Pitch", "Inhouse", "AI")


class NlSgtkProvider:
    """Adapt nl_sgtk to the tracker-neutral nl_core provider protocol."""

    name = "nl_sgtk"
    protocol_version = "1.0"

    def __init__(self) -> None:
        self._sg: Any = None
        self._user: Optional[Dict[str, Any]] = None
        self._connections = threading.local()
        self._storages: Optional[List[Dict[str, Any]]] = None
        self._storage_lock = threading.Lock()

    def resolve_launch_source(
        self,
        entity_type: str,
        entity_id: int,
        source_fields: Sequence[str] = ("sg_path_to_script",),
        application_version_field: str = "",
    ) -> Mapping[str, Any]:
        """Read launch paths and context from an exact Version ID.

        Configured fields must exist in the Version schema. Empty path
        values yield no sources; no latest-Version or media fallback runs.
        Application version is read only from its explicitly named field.
        """

        if entity_type != "Version":
            raise ValueError("Launch source resolution supports only Version")
        _launch_link(entity_type, entity_id)
        if isinstance(source_fields, (str, bytes)) or not isinstance(
            source_fields, Sequence
        ):
            raise ValueError("source_fields must be a sequence of field names")
        if any(
            not isinstance(field, str) or not field.strip()
            for field in source_fields
        ):
            raise ValueError("source_fields must contain non-empty names")
        if not isinstance(application_version_field, str) or (
            application_version_field and not application_version_field.strip()
        ):
            raise ValueError("application_version_field must be a field name")
        configured = list(dict.fromkeys(source_fields))
        if application_version_field:
            configured.append(application_version_field)
        sg, _ = self._connection()
        if configured:
            try:
                schema = sg.schema_field_read("Version")
            except Exception as exc:
                raise RuntimeError(
                    "Could not read Version schema for launch fields"
                ) from exc
            missing = [field for field in configured if field not in schema]
            if missing:
                raise ValueError(
                    "Configured Version launch fields are unavailable: %s"
                    % ", ".join(missing)
                )
        fields = list(dict.fromkeys(
            ["project", "entity", "sg_task"] + configured
        ))
        row = sg.find_one("Version", [["id", "is", entity_id]], fields)
        if not row:
            raise LookupError("ShotGrid Version %s was not found" % entity_id)
        sources = []
        for field in dict.fromkeys(source_fields):
            value = _launch_text(row, field)
            paths = value.split(";") if field in _PUBLISHER_PATH_FIELDS else [
                value
            ]
            sources.extend(
                {"path": path.strip(), "field": field}
                for path in paths if path.strip()
            )
        return {
            "record_type": "Version",
            "record_id": entity_id,
            "project": _launch_row_link(row, "project", "Project"),
            "entity": _launch_row_link(row, "entity"),
            "task": _launch_row_link(row, "sg_task", "Task", optional=True),
            "sources": sources,
            "application_version": (
                _launch_text(row, application_version_field).strip()
                if application_version_field else ""
            ),
        }

    def find_entity_relationships(
        self, entity: Any, relationship_types: Sequence[str],
    ) -> List[Mapping[str, Any]]:
        """Return direct studio links with explicit graph direction.

        Sequence Shots come only from ``shots``. Scene expansion belongs
        to an explicit traversal, never implicit membership resolution.
        """
        requested = set(relationship_types)
        rules = {
            "Scene": [("shots", "scene_shot_membership", "downstream"),
                      ("sg_sequence", "sequence_scene_membership", "upstream"),
                      ("assets", "asset_usage", "upstream")],
            "Sequence": [("shots", "sequence_shot_membership", "downstream"),
                         ("sg_scenes", "sequence_scene_membership", "downstream"),
                         ("assets", "asset_usage", "upstream")],
            "Shot": [("sg_scene", "scene_shot_membership", "upstream"),
                     ("sg_sequence", "sequence_shot_membership", "upstream"),
                     ("assets", "asset_usage", "upstream")],
            "Asset": [("shots", "asset_usage", "downstream")],
            "Task": [("upstream_tasks", "task_dependency", "upstream"),
                     ("downstream_tasks", "task_dependency", "downstream")],
        }
        fields = [(field, kind, direction) for field, kind, direction
                  in rules.get(entity.type, ()) if kind in requested]
        if not fields:
            return []
        sg, _ = self._connection()
        row = sg.find_one(entity.type, [["id", "is", entity.id]],
                          [field for field, _, _ in fields]) or {}
        result = []
        for field, kind, direction in fields:
            value = row.get(field)
            links = value if isinstance(value, list) else [value]
            for link in links:
                if isinstance(link, Mapping) and link.get("id"):
                    result.append({"entity": dict(link),
                                   "relationship_type": kind,
                                   "direction": direction,
                                   "source": self.name})
        return result

    def find_references(
        self, context: Any, request: Mapping[str, Any],
    ) -> List[Mapping[str, Any]]:
        """Discover Version sources for a Task or same-entity upstream Step.

        A requested Step scopes to the current entity, never the whole
        Project. Without a Step, only the session's exact Task is queried.
        Descriptor generation remains an application-adapter capability.
        """
        if request.get("kind") not in {"version", "publish"}:
            return []
        filters = [
            ["project", "is", {"type": "Project", "id": context.project.id}],
            ["entity", "is", {"type": context.entity.type,
                               "id": context.entity.id}],
        ]
        step = request.get("step")
        if step:
            filters.append({"filter_operator": "any", "filters": [
                ["sg_task.Task.step.Step.short_name", "is", str(step)],
                ["sg_task.Task.step.Step.code", "is", str(step)],
            ]})
        else:
            filters.append(["sg_task", "is", {"type": "Task",
                                               "id": context.task.id}])
        if request.get("status"):
            filters.append(["sg_status_list", "is", request["status"]])
        sg, _ = self._connection()
        rows = sg.find("Version", filters, [
            "code", "project", "entity", "sg_task", "sg_status_list",
            "sg_first_frame", "sg_last_frame", "sg__publish_uuid",
            "sg_task.Task.step.Step.short_name", "created_at",
            *_PUBLISHER_PATH_FIELDS,
        ], order=[{"field_name": "created_at", "direction": "desc"}]) or []
        result = []
        for row in rows:
            code = str(row.get("code") or "")
            numbers = re.findall(r"(?:^|[_\.-])v(\d+)(?:$|[_\.-])", code,
                                 flags=re.IGNORECASE)
            paths = [row.get(field) for field in (
                "sg_path_to_frames", "sg_path_to_geometry",
                "sg_path_to_script", "sg_path_to_movie") if row.get(field)]
            result.append(dict(row, record_type="Version", source=self.name,
                version_number=int(numbers[-1]) if numbers else 0,
                step_code=str(step or row.get("sg_task.Task.step.Step.short_name") or ""),
                publish_uuid=row.get("sg__publish_uuid"),
                path=paths[0].split(";")[0] if paths else ""))
        return result

    def list_launch_tasks(
        self,
        entity_type: str,
        entity_id: int,
    ) -> List[Mapping[str, Any]]:
        """Return Tasks linked to exactly this entity (e.g. Shot or Asset).

        Pass the launch source's linked entity, not its Version record ID.
        Task names come from content; links contain only type and ID.
        """

        entity = _launch_link(entity_type, entity_id)
        sg, _ = self._connection()
        rows = sg.find(
            "Task",
            [["entity", "is", entity]],
            [
                "content",
                "project",
                "entity",
                "task_assignees",
                "sg_status_list",
                "step",
            ],
            order=[
                {"field_name": "content", "direction": "asc"},
                {"field_name": "id", "direction": "asc"},
            ],
        ) or []
        tasks = []
        for row in rows:
            linked_entity = _launch_row_link(row, "entity")
            if linked_entity != entity:
                continue
            task = {
                "id": _launch_link("Task", row.get("id"))["id"],
                "name": row.get("content") or "",
                "project": _launch_row_link(row, "project", "Project"),
                "entity": linked_entity,
            }
            for field in ("task_assignees", "sg_status_list", "step"):
                if field in row:
                    task[field] = row.get(field)
            tasks.append(task)
        return tasks

    def current_user(self) -> Mapping[str, Any]:
        """Return the authenticated HumanUser used for artist routing."""

        _, user = self._connection()
        if user.get("type") != "HumanUser" or not user.get("id"):
            raise RuntimeError(
                "DCC launch assignment routing requires a HumanUser"
            )
        return {
            "type": "HumanUser",
            "id": int(user["id"]),
            "name": str(user.get("name") or ""),
        }

    def resolve_launch_context(
        self,
        entity_type: str,
        entity_id: int,
    ) -> Mapping[str, Any]:
        """Return normalized Shot/Asset context for an entity launch."""

        if entity_type not in {"Shot", "Asset"}:
            raise ValueError("Launch context supports only Shot and Asset")
        _launch_link(entity_type, entity_id)
        from .nl_sgtk import get_entity_context, verify_path

        sg, user = self._connection()
        row = get_entity_context(entity_type, entity_id, sg=sg)
        if not row:
            raise LookupError(
                "%s %s was not found" % (entity_type, entity_id)
            )
        project_value = row.get("project") or {}
        project = _launch_row_link(row, "project", "Project")
        root = str(row.get("sg_project_path") or "")
        if root:
            root = verify_path(root, self._storage_mappings())
        metadata = {
            "fps": row.get("sg_master_fps") or "",
            "resolution_width": row.get("master_resolution_width") or "",
            "resolution_height": row.get("master_resolution_height") or "",
            "first_frame": row.get("sg_head_in") or "",
            "last_frame": row.get("sg_tail_out") or "",
            "sequence": row.get("sg_sequence") or "",
            "scene": row.get("sg_scene") or "",
        }
        metadata.update(row.get("env") or {})
        return {
            "project_id": project["id"],
            "project_name": (
                project_value.get("name")
                or project_value.get("code")
                or ""
            ),
            "project_root": root,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_name": str(row.get("code") or entity_id),
            "task_id": 0,
            "task_name": "",
            "step": "",
            "user_id": int(user.get("id") or 0),
            "metadata": metadata,
        }

    def resolve_launch_project(
        self,
        project_id: int,
    ) -> Mapping[str, Any]:
        """Return only the Project facts required for menu policy lookup."""

        project = _launch_link("Project", project_id)
        sg, _ = self._connection()
        row = sg.find_one(
            "Project",
            [["id", "is", project["id"]]],
            ["name", "code", "sg_project_path"],
        )
        if not row:
            raise LookupError(
                "ShotGrid Project %s was not found" % project_id
            )
        root = str(row.get("sg_project_path") or "")
        if root:
            root = self.localize_launch_path(root)
        return {
            "project_id": project["id"],
            "project_name": str(
                row.get("name") or row.get("code") or ""
            ),
            "project_root": root,
        }

    def list_launch_projects(self) -> List[Mapping[str, Any]]:
        """Return minimal active Project rows for launcher cache warming."""

        sg, _ = self._connection()
        rows = sg.find(
            "Project",
            [
                ["sg_status", "in", list(_ACTIVE_PROJECT_STATUSES)],
                ["is_template", "is", False],
            ],
            ["name", "code", "sg_project_path"],
        ) or []
        projects = []
        for row in rows:
            project = _launch_link("Project", row.get("id"))
            root = str(row.get("sg_project_path") or "")
            if root:
                root = self.localize_launch_path(root)
            projects.append({
                "project_id": project["id"],
                "project_name": str(
                    row.get("name") or row.get("code") or ""
                ),
                "project_root": root,
            })
        return projects

    def list_task_workfiles(
        self,
        task_id: int,
        source_fields: Sequence[str] = ("sg_path_to_script",),
    ) -> List[Mapping[str, Any]]:
        """Return non-empty Version source paths linked to one exact Task."""

        task = _launch_link("Task", task_id)
        if isinstance(source_fields, (str, bytes)) or not isinstance(
            source_fields, Sequence
        ):
            raise ValueError("source_fields must be a sequence")
        fields = list(dict.fromkeys(source_fields))
        if not fields:
            return []
        sg, _ = self._connection()
        rows = sg.find(
            "Version",
            [["sg_task", "is", task]],
            ["code", "created_at", *fields],
            order=[{"field_name": "created_at", "direction": "desc"}],
        ) or []
        result = []
        seen = set()
        for row in rows:
            for field in fields:
                value = _launch_text(row, field)
                values = (
                    value.split(";")
                    if field in _PUBLISHER_PATH_FIELDS
                    else [value]
                )
                for raw in values:
                    path = raw.strip()
                    if not path or path in seen:
                        continue
                    seen.add(path)
                    result.append(
                        {
                            "path": path,
                            "field": field,
                            "version_id": row.get("id"),
                            "code": row.get("code") or "",
                        }
                    )
        return result

    def localize_launch_path(self, path: str) -> str:
        """Map a trusted ShotGrid path through LocalStorage definitions."""

        if not isinstance(path, str) or not path.strip():
            raise ValueError("Launch path must be non-empty text")
        from .nl_sgtk import verify_path

        return verify_path(path, self._storage_mappings())

    def _storage_mappings(self) -> List[Dict[str, Any]]:
        """Read LocalStorage rows once for this authenticated provider."""

        with self._storage_lock:
            if self._storages is None:
                from .nl_sgtk import get_storages

                sg, _ = self._connection()
                self._storages = list(get_storages(sg=sg) or ())
            return list(self._storages)

    def fetch_task(self, task_id: int) -> Mapping[str, Any]:
        """Return normalized source data for one ShotGrid Task ID."""

        from .nl_sgtk import get_task_context

        sg, _ = self._connection()
        context = get_task_context(task_id, sg=sg)
        if not context:
            raise LookupError("ShotGrid Task %s was not found" % task_id)
        step = context.get("step")
        if isinstance(step, Mapping) and step.get("id"):
            step_row = sg.find_one(
                "Step",
                [["id", "is", int(step["id"])]],
                ["code", "short_name"],
            )
            if step_row:
                step_code = (
                    step_row.get("short_name") or step_row.get("code")
                )
                if step_code:
                    step_data = dict(step)
                    step_data["code"] = str(step_code)
                    context["step"] = step_data
        entity = context.get("entity")
        if (
            isinstance(entity, Mapping)
            and entity.get("type") == "Asset"
            and entity.get("id")
        ):
            asset = sg.find_one(
                "Asset",
                [["id", "is", int(entity["id"])]],
                ["sg_asset_type"],
            )
            if asset and asset.get("sg_asset_type"):
                context["asset_type"] = str(asset["sg_asset_type"])
        return context

    def find_publishes(
        self,
        context: Any,
        output: str,
    ) -> List[Mapping[str, Any]]:
        """Query ShotGrid PublishedFiles for the supplied Task context."""

        sg, _ = self._connection()
        filters = [
            [
                "project",
                "is",
                {"type": "Project", "id": context.project.id},
            ],
            [
                "task",
                "is",
                {"type": "Task", "id": context.task.id},
            ],
        ]
        fields = [
            "code",
            "path",
            "path_cache",
            "sg_path_string",
            "version",
            "version_number",
            "published_file_type",
        ]
        rows = sg.find(
            "PublishedFile",
            filters,
            fields,
            order=[{"field_name": "version_number", "direction": "desc"}],
        ) or []
        publishes = [self._publish_row(row, output) for row in rows]
        version_rows = sg.find(
            "Version",
            [
                filters[0],
                [
                    "sg_task",
                    "is",
                    {"type": "Task", "id": context.task.id},
                ],
            ],
            ["code", "sg__publish_uuid", "created_at"],
            order=[{"field_name": "created_at", "direction": "desc"}],
        ) or []
        publishes.extend(
            {
                "id": row.get("id"),
                "code": row.get("code"),
                "publish_uuid": row.get("sg__publish_uuid"),
                "output": output,
                "source": self.name,
                "record_type": "Version",
            }
            for row in version_rows
        )
        matching = [
            row for row in publishes if self._matches_output(row, output)
        ]
        return matching or publishes

    def register_publish(
        self,
        context: Any,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Register a Version with classified output and source paths."""

        from .publisher import ShotgunPublish

        sg, user = self._connection()
        publisher = ShotgunPublish(sg=sg, user=user)
        # Core owns the durable outbox and its manifest write in this path.
        publisher.write_publish_manifest = False
        publish_uuid = str(request["publish_uuid"])
        publisher.publish_uuid = publish_uuid
        publisher.version["sg__publish_uuid"] = publish_uuid
        publisher.set_context(
            {
                "project": {
                    "type": "Project",
                    "id": context.project.id,
                },
                "entity": {
                    "type": context.entity.type,
                    "id": context.entity.id,
                },
                "task": {"type": "Task", "id": context.task.id},
            }
        )
        publisher.set_version_name(str(request["version_code"]))
        publisher.set_description(str(request.get("description") or ""))
        if request.get("metadata"):
            import json
            publisher.set_metadata(json.dumps(request["metadata"],
                                              sort_keys=True))
            vendor = request["metadata"].get("vendor")
            if vendor:
                publisher.set_vendor_by_data(vendor)
        publisher.version["sg_dependiencies"] = [
            dict(link) for link in request.get("dependencies", ())]
        first_frame = request.get("first_frame")
        last_frame = request.get("last_frame")
        if first_frame is not None and last_frame is not None:
            publisher.set_frame_range(int(first_frame), int(last_frame))
        for path in request.get("files", []):
            publisher.add_file(str(path))
        preview = request.get("preview")
        if preview:
            publisher.set_preview_file(str(preview))
        existing = self._find_existing_version(sg, context, request)
        if existing is not None:
            if preview:
                sg.upload(
                    "Version",
                    int(existing["id"]),
                    str(preview),
                    "sg_uploaded_movie",
                )
            if publisher.published_files_enabled():
                self._ensure_published_files(
                    sg,
                    publisher,
                    existing,
                    request,
                )
            result = dict(existing)
            result["sg__publish_uuid"] = publish_uuid
            result["reused"] = True
            result["publish_manifest"] = self._publish_manifest(sg, result, context)
            return result

        result = publisher.publish(
            validate=True,
            upload_preview=bool(preview),
            registration="tracker",
        )
        if publisher.published_files_enabled():
            self._ensure_published_files(sg, publisher, result, request)
        result["sg__publish_uuid"] = publish_uuid
        result["reused"] = False
        result["publish_manifest"] = self._publish_manifest(sg, result, context)
        return result

    def _publish_manifest(self, sg: Any, result: Mapping[str, Any],
                          context: Any) -> Mapping[str, Any]:
        from .publish_manifests import VERSION_FIELDS, version_snapshot
        row = sg.find_one("Version", [["id", "is", int(result["id"])],
                                     ["project", "is", {"type": "Project", "id": context.project.id}],
                                     ["sg_task", "is", {"type": "Task", "id": context.task.id}]],
                          VERSION_FIELDS)
        if not row:
            raise RuntimeError("Registered Version snapshot could not be retrieved")
        return version_snapshot(row, sg.base_url)

    def publish_manifest_snapshots(self, context: Any) -> Sequence[Mapping[str, Any]]:
        """Fetch exact Task Versions for an explicit local index refresh."""
        from .publish_manifests import VERSION_FIELDS, version_snapshot
        sg, _ = self._connection()
        rows = sg.find("Version", [
            ["project", "is", {"type": "Project", "id": context.project.id}],
            ["sg_task", "is", {"type": "Task", "id": context.task.id}],
        ], VERSION_FIELDS) or []
        return [version_snapshot(row, sg.base_url) for row in rows]

    def health(self) -> Mapping[str, Any]:
        """Report package, identity, and connection availability."""

        from .nl_sgtk import __version__

        try:
            _, user = self._connection()
        except Exception as exc:
            return {
                "available": False,
                "version": __version__,
                "error": str(exc),
            }
        return {
            "available": True,
            "version": __version__,
            "identity_type": user.get("type") if user else None,
        }

    def _connection(self) -> Tuple[Any, Dict[str, Any]]:
        # Explicit injected clients remain supported by existing adapters.
        if self._sg is not None:
            return self._sg, dict(self._user or {})
        connection = getattr(self._connections, "value", None)
        if connection is None:
            from .nl_sgtk import sgtk_login

            sg, user = sgtk_login(product="nl_core")
            if sg is None or user is None:
                raise ConnectionError("nl_sgtk could not authenticate")
            connection = (sg, dict(user))
            self._connections.value = connection
        return connection[0], dict(connection[1])

    def _publish_row(
        self,
        row: Mapping[str, Any],
        output: str,
    ) -> Mapping[str, Any]:
        path = row.get("sg_path_string")
        path_field = row.get("path")
        if not path and isinstance(path_field, Mapping):
            path = (
                path_field.get("local_path")
                or path_field.get("local_path_windows")
                or path_field.get("local_path_linux")
            )
        version = row.get("version_number")
        version_entity = row.get("version")
        return {
            "id": row.get("id"),
            "code": row.get("code") or row.get("name"),
            "path": path,
            "version": version,
            "version_entity": version_entity,
            "published_file_type": row.get("published_file_type"),
            "output": output,
            "source": self.name,
        }

    def _find_existing_version(
        self,
        sg: Any,
        context: Any,
        request: Mapping[str, Any],
    ) -> Optional[Dict[str, Any]]:
        project = {"type": "Project", "id": context.project.id}
        task = {"type": "Task", "id": context.task.id}
        fields = ["code", "project", "sg_task", "sg__publish_uuid"]
        publish_uuid = str(request["publish_uuid"])
        version = sg.find_one(
            "Version",
            [
                ["project", "is", project],
                ["sg_task", "is", task],
                ["sg__publish_uuid", "is", publish_uuid],
            ],
            fields,
        )
        if version:
            return dict(version)

        for path in request.get("files", []):
            published = sg.find_one(
                "PublishedFile",
                [
                    ["project", "is", project],
                    ["task", "is", task],
                    ["sg_path_string", "is", str(path)],
                ],
                ["version"],
            )
            version_link = (published or {}).get("version")
            if isinstance(version_link, Mapping) and version_link.get("id"):
                found = sg.find_one(
                    "Version",
                    [["id", "is", int(version_link["id"])]],
                    fields,
                )
                if found:
                    return dict(found)

        code = str(request.get("version_code") or "")
        if not code:
            return None
        version = sg.find_one(
            "Version",
            [
                ["project", "is", project],
                ["sg_task", "is", task],
                ["code", "is", code],
            ],
            fields,
        )
        return dict(version) if version else None

    def _ensure_published_files(
        self,
        sg: Any,
        publisher: Any,
        version: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> None:
        version_id = version.get("id")
        if not version_id:
            raise RuntimeError("ShotGrid Version registration returned no ID")
        requested = [str(path) for path in request.get("files", [])]
        existing = self._published_paths(sg, int(version_id))
        missing = [
            path
            for path in requested
            if _normalized_path(path) not in existing
        ]
        if missing:
            requests = [
                publisher.publish_request_from_file(path, dict(version))
                for path in missing
            ]
            sg.batch(requests)
            existing = self._published_paths(sg, int(version_id))
        unresolved = [
            path
            for path in requested
            if _normalized_path(path) not in existing
        ]
        if unresolved:
            raise RuntimeError(
                "PublishedFile registration is incomplete for Version %s"
                % version_id
            )

    def _published_paths(self, sg: Any, version_id: int) -> set[str]:
        rows = sg.find(
            "PublishedFile",
            [["version", "is", {"type": "Version", "id": version_id}]],
            ["path", "sg_path_string"],
        ) or []
        return {
            _normalized_path(path)
            for row in rows
            for path in [_published_path(row)]
            if path
        }

    def _matches_output(
        self,
        row: Mapping[str, Any],
        output: str,
    ) -> bool:
        name = output.strip().lower()
        if not name:
            return True
        path = str(row.get("path") or "").replace("\\", "/").lower()
        code = str(row.get("code") or "").lower()
        return "/%s/" % name in path or "_%s_" % name in code


def _launch_link(entity_type: str, entity_id: int) -> Dict[str, Any]:
    if not isinstance(entity_type, str) or not entity_type.strip():
        raise ValueError("Launch entity type must be a non-empty string")
    if (
        not isinstance(entity_id, int)
        or isinstance(entity_id, bool)
        or entity_id <= 0
    ):
        raise ValueError("Launch entity ID must be a positive integer")
    return {"type": entity_type, "id": entity_id}


def _launch_row_link(
    row: Mapping[str, Any],
    field: str,
    expected_type: str = "",
    optional: bool = False,
) -> Dict[str, Any]:
    value = row.get(field)
    if optional and (value is None or value == {}):
        return {}
    if not isinstance(value, Mapping) or (
        expected_type and value.get("type") != expected_type
    ):
        raise ValueError("Invalid or missing launch context link: %s" % field)
    try:
        return _launch_link(value.get("type"), value.get("id"))
    except ValueError as exc:
        raise ValueError("Invalid launch context link: %s" % field) from exc


def _launch_text(row: Mapping[str, Any], field: str) -> str:
    if field not in row:
        raise ValueError("Version launch field was not returned: %s" % field)
    value = row[field]
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Version launch field must contain text: %s" % field)
    return value


def _published_path(row: Mapping[str, Any]) -> Optional[str]:
    direct = row.get("sg_path_string")
    if direct:
        return str(direct)
    path = row.get("path")
    if isinstance(path, Mapping):
        for key in (
            "local_path",
            "local_path_windows",
            "local_path_linux",
            "local_path_mac",
        ):
            if path.get(key):
                return str(path[key])
    return None


def _normalized_path(value: str) -> str:
    return value.replace("\\", "/").rstrip("/").casefold()
