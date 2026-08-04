from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple


class NlSgtkProvider:
    """Adapt nl_sgtk to the tracker-neutral nl_core provider protocol."""

    name = "nl_sgtk"
    protocol_version = "1.0"

    def __init__(self) -> None:
        self._sg: Any = None
        self._user: Optional[Dict[str, Any]] = None

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
        """Register Version and PublishedFiles through ShotgunPublish."""

        from .publisher import ShotgunPublish

        sg, user = self._connection()
        publisher = ShotgunPublish(sg=sg, user=user)
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
            self._ensure_published_files(
                sg,
                publisher,
                existing,
                request,
            )
            result = dict(existing)
            result["sg__publish_uuid"] = publish_uuid
            result["reused"] = True
            return result

        result = publisher.publish(
            validate=True,
            upload_preview=bool(preview),
        )
        self._ensure_published_files(sg, publisher, result, request)
        result["sg__publish_uuid"] = publish_uuid
        result["reused"] = False
        return result

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
        from .nl_sgtk import sgtk_login

        if self._sg is None:
            sg, user = sgtk_login(product="nl_core")
            if sg is None or user is None:
                raise ConnectionError("nl_sgtk could not authenticate")
            self._sg = sg
            self._user = dict(user)
        return self._sg, dict(self._user or {})

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
