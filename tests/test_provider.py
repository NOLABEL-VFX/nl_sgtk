from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

import pytest

from nl_sgtk.provider import NlSgtkProvider


def test_entity_launch_preserves_project_ocio(monkeypatch):
    import nl_sgtk.nl_sgtk as api

    config = "//server/show/OCIO/aces_1.2/config.ocio"
    row = {
        "project": {"type": "Project", "id": 1},
        "code": "sh010",
        "project.Project.sg_ocio_config_path": config,
        "env": {"SHOT_LUT_PRIMARY": "day.cube"},
    }
    monkeypatch.setattr(api, "get_entity_context", lambda *a, **k: row)
    provider = NlSgtkProvider()
    monkeypatch.setattr(provider, "_connection", lambda: (None, {"id": 2}))
    for entity_type in ("Shot", "Asset"):
        context = provider.resolve_launch_context(entity_type, 3)
        assert context["metadata"]["ocio_config"] == config
    assert context["metadata"]["SHOT_LUT_PRIMARY"] == "day.cube"
    row["project.Project.sg_ocio_config_path"] = None
    context = provider.resolve_launch_context("Shot", 3)
    assert context["metadata"]["ocio_config"] == ""


def test_connections_are_isolated_between_worker_threads(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import nl_sgtk.nl_sgtk as api

    monkeypatch.setattr(api, "sgtk_login",
                        lambda **kwargs: (object(), {"id": 1}))
    provider = NlSgtkProvider()
    barrier = Barrier(2)

    def connect():
        first = provider._connection()[0]
        barrier.wait(timeout=5)
        assert provider._connection()[0] is first
        return first

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: connect(), range(2)))
    assert first is not second


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
    base_url = "https://test.shotgrid.autodesk.com"
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
    FILE_FIELDS = ('sg_path_to_frames', 'sg_path_to_movie',
                   'sg_path_to_geometry', 'sg_path_to_script')
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

    def set_metadata(self, metadata: str) -> None:
        self.version["sg_metadata"] = metadata

    def movie_has_slate(self, value: bool) -> None:
        self.version["sg_movie_has_slate"] = value

    def set_frame_range(self, first_frame: int, last_frame: int) -> None:
        self.version["sg_first_frame"] = first_frame
        self.version["sg_last_frame"] = last_frame

    def add_file(self, path: str, force_version_field: Optional[str] = None
                 ) -> None:
        self.version.setdefault('classified_files', []).append(
            (path, force_version_field))

    def set_preview_file(self, path: str) -> None:
        del path

    def publish(
        self,
        validate: bool,
        upload_preview: bool,
        registration: str = "tracker",
    ) -> Dict[str, Any]:
        del validate, upload_preview
        type(self).publish_calls += 1
        self.sg.existing = dict(self.version, type="Version", id=99,
            project=self.context["project"], entity=self.context["entity"],
            sg_task=self.context["task"])
        return dict(self.sg.existing)

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
        "publish_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
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


def test_explicit_sidecar_role_is_forwarded_and_invalid_role_never_writes(
    monkeypatch: Any,
) -> None:
    from nl_sgtk import publisher as publisher_module
    monkeypatch.setattr(publisher_module, 'ShotgunPublish', FakePublisher)
    sg = FakeShotGrid()
    request = dict(_request(), files=['//rama/show/metadata.json'],
        metadata={'file_roles': {
            '//rama/show/metadata.json': 'sg_path_to_script'}})
    result = _provider(sg).register_publish(_context(), request)
    assert result['classified_files'] == [
        ('//rama/show/metadata.json', 'sg_path_to_script')]
    invalid = dict(request, metadata={'file_roles': {
        '//rama/show/metadata.json': 'arbitrary_field'}})
    before = FakePublisher.publish_calls
    with pytest.raises(ValueError, match='explicit publish file role'):
        _provider(FakeShotGrid()).register_publish(_context(), invalid)
    assert FakePublisher.publish_calls == before


@pytest.mark.parametrize("metadata,error", [
    ({"has_slate": "false"}, "has_slate must be a boolean"),
    ({"has_slate": 0}, "has_slate must be a boolean"),
    ({"has_slate": 1}, "has_slate must be a boolean"),
    ({"has_slate": None}, "has_slate must be a boolean"),
    ({"file_roles": []}, "path/field mapping"),
    ({"file_roles": None}, "path/field mapping"),
    ({"file_roles": {"sidecar": "arbitrary_field"}}, "file role"),
    ({"file_roles": {"//rama/sidecar": "sg_path_to_script",
                     "//RAMA/sidecar": "sg_path_to_frames"}},
     "Conflicting explicit publish file roles"),
    (["invalid"], "metadata must be a mapping"),
    ([], "metadata must be a mapping"),
    (False, "metadata must be a mapping"),
])
def test_invalid_publish_metadata_fails_before_authentication(
    monkeypatch, metadata, error,
):
    provider = NlSgtkProvider()

    def forbidden():
        raise AssertionError("Invalid metadata must not authenticate")

    monkeypatch.setattr(provider, "_connection", forbidden)
    with pytest.raises(ValueError, match=error):
        provider.register_publish(_context(),
                                  dict(_request(), metadata=metadata))


def test_explicit_roles_preserve_mixed_files_and_automatic_fallback(monkeypatch):
    from nl_sgtk import publisher as publisher_module

    monkeypatch.setattr(publisher_module, "ShotgunPublish", FakePublisher)
    files = ["//rama/show/%d.sidecar" % i for i in range(4)]
    roles = dict(zip(files, FakePublisher.FILE_FIELDS))
    roles = {path.replace("/", "\\"): field for path, field in roles.items()}
    files.append("//rama/show/automatic.exr")
    result = _provider(FakeShotGrid()).register_publish(
        _context(), dict(_request(), files=files, metadata={"file_roles": roles}))
    assert result["classified_files"] == [
        *zip(files[:4], FakePublisher.FILE_FIELDS), (files[4], None)]


def test_existing_uuid_reuses_version_without_published_file_by_default(
    monkeypatch: Any,
) -> None:
    from nl_sgtk import publisher as publisher_module

    existing = {
        "type": "Version",
        "id": 50,
        "code": "shot_main_v001",
        "sg__publish_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
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


@pytest.mark.parametrize("has_slate", [True, False])
def test_core_vendor_preserves_explicit_slate(monkeypatch, has_slate):
    """Persist both checked and unchecked slate flags through Core."""
    from nl_sgtk import publisher as publisher_module

    sg = FakeShotGrid()
    monkeypatch.setattr(publisher_module, "ShotgunPublish", FakePublisher)
    request = dict(_request(), metadata={"has_slate": has_slate})
    result = _provider(sg).register_publish(_context(), request)
    assert result["sg_movie_has_slate"] is has_slate
    repeated = _provider(sg).register_publish(_context(), request)
    assert repeated["sg_movie_has_slate"] is has_slate
    assert repeated["reused"]


def test_core_vendor_rejects_non_boolean_slate(monkeypatch):
    from nl_sgtk import publisher as publisher_module

    sg = FakeShotGrid()
    monkeypatch.setattr(publisher_module, "ShotgunPublish", FakePublisher)
    with pytest.raises(ValueError, match="must be a boolean"):
        _provider(sg).register_publish(_context(),
            dict(_request(), metadata={"has_slate": "false"}))
    assert sg.existing is None


def test_recovery_keeps_version_and_completed_upload(monkeypatch):
    """Reuse a Version and skip a durably acknowledged upload on replay."""
    from nl_sgtk import publisher as publisher_module
    from unittest.mock import Mock

    sg = FakeShotGrid()
    sg.upload = Mock()
    monkeypatch.setattr(publisher_module, "ShotgunPublish", FakePublisher)
    FakePublisher.publish_calls = 0
    receipt = {}
    provider = _provider(sg)
    request = dict(_request(), preview="movie.mov")
    provider.resume_publish(_context(), request, receipt, receipt.update)
    provider.resume_publish(_context(), request, receipt, receipt.update)
    assert FakePublisher.publish_calls == 1
    sg.upload.assert_called_once_with(
        "Version", 99, "movie.mov", "sg_uploaded_movie")
    assert receipt["preview_version_id"] == 99


def test_recovery_rejects_another_uuid(monkeypatch):
    """Never attach recovered media to another artist's logical publish."""
    from nl_sgtk import publisher as publisher_module

    sg = FakeShotGrid({"id": 50, "sg__publish_uuid": "someone-else"})
    monkeypatch.setattr(publisher_module, "ShotgunPublish", FakePublisher)
    with pytest.raises(ValueError, match="another UUID"):
        _provider(sg).resume_publish(_context(), _request(), {}, lambda x: None)
