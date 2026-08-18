from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from nl_sgtk.provider import NlSgtkProvider


@dataclass
class Entity:
    id: int
    type: str


@dataclass
class Context:
    project: Entity
    entity: Entity
    task: Entity


class FakeShotGrid:
    def __init__(self, existing: Optional[Mapping[str, Any]] = None) -> None:
        self.existing = dict(existing) if existing else None
        self.files: List[Dict[str, Any]] = []
        self.versions: List[Dict[str, Any]] = []
        self.step: Optional[Dict[str, Any]] = None

    def find_one(
        self,
        entity_type: str,
        filters: Any,
        fields: Any,
    ) -> Optional[Dict[str, Any]]:
        del filters, fields
        if entity_type == "Version":
            return dict(self.existing) if self.existing else None
        if entity_type == "PublishedFile":
            return None
        if entity_type == "Step":
            return dict(self.step) if self.step else None
        raise AssertionError(entity_type)

    def find(
        self,
        entity_type: str,
        filters: Any,
        fields: Any,
        order: Any = None,
    ) -> List[Dict[str, Any]]:
        del filters, fields, order
        if entity_type == "PublishedFile":
            return list(self.files)
        if entity_type == "Version":
            return list(self.versions)
        raise AssertionError(entity_type)

    def batch(self, requests: List[Mapping[str, Any]]) -> None:
        for request in requests:
            data = dict(request["data"])
            self.files.append(data)


class FakePublisher:
    publish_calls = 0
    enable_published_files = False

    def __init__(self, sg: FakeShotGrid, user: Mapping[str, Any]) -> None:
        del user
        self.sg = sg
        self.publish_uuid = ""
        self.version: Dict[str, Any] = {"sg__publish_uuid": ""}

    def set_context(self, context: Mapping[str, Any]) -> None:
        self.context = context

    def set_version_name(self, name: str) -> None:
        self.version["code"] = name

    def set_description(self, description: str) -> None:
        self.version["description"] = description

    def set_frame_range(self, first_frame: int, last_frame: int) -> None:
        self.version["sg_first_frame"] = first_frame
        self.version["sg_last_frame"] = last_frame

    def add_file(self, path: str) -> None:
        del path

    def set_preview_file(self, path: str) -> None:
        del path

    def publish(
        self,
        validate: bool,
        upload_preview: bool,
    ) -> Dict[str, Any]:
        del validate, upload_preview
        type(self).publish_calls += 1
        return {"type": "Version", "id": 99, "code": self.version["code"]}

    def publish_request_from_file(
        self,
        path: str,
        version: Mapping[str, Any],
    ) -> Dict[str, Any]:
        return {
            "request_type": "create",
            "entity_type": "PublishedFile",
            "data": {
                "sg_path_string": path,
                "version": dict(version),
            },
        }

    def published_files_enabled(self) -> bool:
        return self.enable_published_files


def _request() -> Dict[str, Any]:
    return {
        "publish_uuid": "stable-uuid",
        "version_code": "shot_main_v001",
        "files": ["//rama/show/main/v001/render.exr"],
        "description": "Comp publish",
        "first_frame": 1001,
        "last_frame": 1010,
    }


def _provider(sg: FakeShotGrid) -> NlSgtkProvider:
    provider = NlSgtkProvider()
    provider._sg = sg
    provider._user = {"type": "HumanUser", "id": 1}
    return provider


def _context() -> Context:
    return Context(
        project=Entity(1, "Project"),
        entity=Entity(2, "Shot"),
        task=Entity(3, "Task"),
    )


def test_existing_uuid_reuses_version_without_published_file_by_default(
    monkeypatch: Any,
) -> None:
    from nl_sgtk import publisher as publisher_module

    existing = {
        "type": "Version",
        "id": 50,
        "code": "shot_main_v001",
        "sg__publish_uuid": "stable-uuid",
    }
    sg = FakeShotGrid(existing)
    FakePublisher.publish_calls = 0
    monkeypatch.setattr(publisher_module, "ShotgunPublish", FakePublisher)

    result = _provider(sg).register_publish(_context(), _request())

    assert result["id"] == 50
    assert result["reused"] is True
    assert FakePublisher.publish_calls == 0
    assert sg.files == []

    repeated = _provider(sg).register_publish(_context(), _request())
    assert repeated["id"] == 50
    assert sg.files == []


def test_new_version_skips_published_file_registration_by_default(
    monkeypatch: Any,
) -> None:
    from nl_sgtk import publisher as publisher_module

    sg = FakeShotGrid()
    FakePublisher.publish_calls = 0
    monkeypatch.setattr(publisher_module, "ShotgunPublish", FakePublisher)

    result = _provider(sg).register_publish(_context(), _request())

    assert result["id"] == 99
    assert result["reused"] is False
    assert FakePublisher.publish_calls == 1
    assert sg.files == []


def test_project_flag_enables_published_file_registration(
    monkeypatch: Any,
) -> None:
    from nl_sgtk import publisher as publisher_module

    sg = FakeShotGrid()
    FakePublisher.enable_published_files = True
    monkeypatch.setattr(publisher_module, "ShotgunPublish", FakePublisher)
    try:
        result = _provider(sg).register_publish(_context(), _request())
    finally:
        FakePublisher.enable_published_files = False

    assert result["id"] == 99
    assert len(sg.files) == 1


def test_publish_discovery_includes_partial_versions_by_output() -> None:
    sg = FakeShotGrid()
    sg.files = [
        {
            "id": 60,
            "code": "shot_preview_v002.mov",
            "sg_path_string": "//rama/show/preview/v002/movie.mov",
            "version_number": 2,
        }
    ]
    sg.versions = [
        {
            "id": 61,
            "code": "shot_main_v003",
            "sg__publish_uuid": "partial-uuid",
        }
    ]

    rows = _provider(sg).find_publishes(_context(), "main")

    assert [row["id"] for row in rows] == [61]
    assert rows[0]["record_type"] == "Version"


def test_task_fetch_hydrates_step_short_code(monkeypatch: Any) -> None:
    from nl_sgtk import nl_sgtk as api_module

    sg = FakeShotGrid()
    sg.step = {"id": 4, "code": "Compositing", "short_name": "comp"}
    monkeypatch.setattr(
        api_module,
        "get_task_context",
        lambda task_id, sg: {
            "id": task_id,
            "step": {"type": "Step", "id": 4, "name": "Compositing"},
        },
    )

    payload = _provider(sg).fetch_task(30)

    assert payload["step"]["code"] == "comp"
