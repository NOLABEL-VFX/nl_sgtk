"""Offline launch contract tests, without package login/update side effects."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest


# Loading just the provider keeps these tests independent of SDK installation
# and the package's import-time remote version check.
_path = Path(__file__).resolve().parents[1] / "src/nl_sgtk/provider.py"
_spec = importlib.util.spec_from_file_location("launch_provider", _path)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
NlSgtkProvider = _module.NlSgtkProvider


PROJECT = {"type": "Project", "id": 1}
SHOT = {"type": "Shot", "id": 2}
TASK = {"type": "Task", "id": 3}


class FakeLaunchConnection:
    """Only read operations exist; record every query and requested field."""

    def __init__(self) -> None:
        self.version: Optional[Dict[str, Any]] = {
            "type": "Version",
            "id": 42,
            "project": dict(PROJECT, name="Show"),
            "entity": dict(SHOT, name="sh010"),
            "sg_task": dict(TASK, name="Comp"),
            "sg_path_to_script": "//server/show/comp_v007.nk",
            "code": "Nuke_15.1v4_comp_v999",
            "sg_path_to_movie": "//server/show/review.mov",
            "sg_path_to_frames": "//server/show/render.%04d.exr",
        }
        self.schema = {field: {} for field in self.version}
        self.tasks: Optional[List[Dict[str, Any]]] = []
        self.work_versions: Optional[List[Dict[str, Any]]] = []
        self.project: Optional[Dict[str, Any]] = {
            "type": "Project",
            "id": 1,
            "name": "Show",
            "code": "SHOW",
            "sg_project_path": "//server/show",
        }
        self.storages: List[Dict[str, Any]] = []
        self.calls: List[Any] = []
        self.schema_error = False

    def schema_field_read(self, entity_type: str) -> Mapping[str, Any]:
        self.calls.append(("schema_field_read", entity_type))
        assert entity_type == "Version"
        if self.schema_error:
            raise PermissionError("schema denied")
        return self.schema

    def find_one(
        self, entity_type: str, filters: Any, fields: Any,
    ) -> Optional[Dict[str, Any]]:
        self.calls.append(("find_one", entity_type, filters, fields))
        if entity_type == "Project":
            assert filters == [["id", "is", 1]]
            row = self.project
        else:
            assert entity_type == "Version"
            assert filters == [["id", "is", 42]]
            row = self.version
        if row is None:
            return None
        return {
            key: value for key, value in row.items()
            if key in fields or key in ("type", "id")
        }

    def find(
        self, entity_type: str, filters: Any, fields: Any, order: Any = None,
    ) -> Optional[List[Dict[str, Any]]]:
        self.calls.append(("find", entity_type, filters, fields, order))
        if entity_type == "LocalStorage":
            return self.storages
        if entity_type == "Project":
            return [self.project] if self.project else []
        if entity_type == "Task":
            return self.tasks
        assert entity_type == "Version"
        return self.work_versions


@pytest.fixture
def launch() -> Any:
    sg = FakeLaunchConnection()
    provider = NlSgtkProvider()
    provider._sg = sg
    provider._user = {
        "type": "HumanUser", "id": 10, "name": "Ada Artist"
    }
    return provider, sg


def test_exact_version_and_normalized_context(launch: Any) -> None:
    provider, sg = launch
    result = provider.resolve_launch_source("Version", 42)

    assert result == {
        "record_type": "Version",
        "record_id": 42,
        "project": PROJECT,
        "entity": SHOT,
        "task": TASK,
        "sources": [{
            "path": "//server/show/comp_v007.nk",
            "field": "sg_path_to_script",
        }],
        "application_version": "",
    }
    assert sg.calls == [
        ("schema_field_read", "Version"),
        ("find_one", "Version", [["id", "is", 42]],
         ["project", "entity", "sg_task", "sg_path_to_script"]),
    ]
    assert sg.version["entity"]["name"] == "sh010"


@pytest.mark.parametrize("value", [None, "", " ;  ; "])
def test_empty_sources_never_fall_back_to_media(
    launch: Any, value: Any,
) -> None:
    provider, sg = launch
    sg.version["sg_path_to_script"] = value
    sg.version["sg_task"] = None
    result = provider.resolve_launch_source("Version", 42)
    assert result["sources"] == []
    assert result["application_version"] == ""
    assert result["task"] == {}
    assert len(sg.calls) == 2


def test_custom_fields_split_paths_and_use_explicit_app_version(
    launch: Any,
) -> None:
    provider, sg = launch
    sg.schema.update({"sg_scene": {}, "sg_app_version": {}})
    sg.version.update({
        "entity": {"type": "Asset", "id": 20, "name": "Tree"},
        "sg_scene": "C:\\show\\scene; & $ data.hip",
        "sg_path_to_script": " /mnt/show/one.hip ; ;/mnt/show/two.hip;",
        "sg_app_version": " 20.5.487 ",
        "sg_task": {},
    })
    result = provider.resolve_launch_source(
        "Version", 42,
        source_fields=["sg_scene", "sg_path_to_script", "sg_scene"],
        application_version_field="sg_app_version",
    )
    assert result["sources"] == [
        {"path": "C:\\show\\scene; & $ data.hip", "field": "sg_scene"},
        {"path": "/mnt/show/one.hip", "field": "sg_path_to_script"},
        {"path": "/mnt/show/two.hip", "field": "sg_path_to_script"},
    ]
    assert result["entity"] == {"type": "Asset", "id": 20}
    assert result["task"] == {}
    assert result["application_version"] == "20.5.487"
    assert sg.calls[-1][3] == [
        "project", "entity", "sg_task", "sg_scene", "sg_path_to_script",
        "sg_app_version",
    ]


@pytest.mark.parametrize("path", [
    "C:\\show\\scene; & $ data.nk",
    "/mnt/show/scene; $(echo data) & 'quoted'.nk",
])
def test_custom_source_field_keeps_command_like_characters(
    launch: Any, path: str,
) -> None:
    provider, sg = launch
    sg.schema.update({"sg_scene": {}, "sg_other_scene": {}})
    sg.version.update({"sg_scene": path, "sg_other_scene": "/other/scene.nk"})
    result = provider.resolve_launch_source(
        "Version", 42, source_fields=("sg_scene", "sg_other_scene"),
    )
    assert result["sources"] == [
        {"path": path, "field": "sg_scene"},
        {"path": "/other/scene.nk", "field": "sg_other_scene"},
    ]


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_explicit_application_version(launch: Any, value: Any) -> None:
    provider, sg = launch
    sg.schema["sg_app_version"] = {}
    sg.version["sg_app_version"] = value
    result = provider.resolve_launch_source(
        "Version", 42, application_version_field="sg_app_version",
    )
    assert result["application_version"] == ""


@pytest.mark.parametrize("kwargs, field", [
    ({}, "sg_path_to_script"),
    ({"source_fields": ["sg_custom"]}, "sg_custom"),
    ({"application_version_field": "sg_app_version"}, "sg_app_version"),
])
def test_absent_schema_fields_fail_before_record_query(
    launch: Any, kwargs: Any, field: str,
) -> None:
    provider, sg = launch
    sg.schema.pop(field, None)
    with pytest.raises(ValueError, match=field):
        provider.resolve_launch_source("Version", 42, **kwargs)
    assert sg.calls == [("schema_field_read", "Version")]


def test_schema_failure_has_clear_error_and_cause(launch: Any) -> None:
    provider, sg = launch
    sg.schema_error = True
    with pytest.raises(RuntimeError, match="Version schema") as error:
        provider.resolve_launch_source("Version", 42)
    assert isinstance(error.value.__cause__, PermissionError)
    assert len(sg.calls) == 1


def test_missing_version_does_not_search_for_latest(launch: Any) -> None:
    provider, sg = launch
    sg.version = None
    with pytest.raises(LookupError, match="Version 42 was not found"):
        provider.resolve_launch_source("Version", 42)
    assert len(sg.calls) == 2


@pytest.mark.parametrize("field, value", [
    ("sg_path_to_script", ["scene.nk"]),
    ("sg_path_to_script", {"local_path": "scene.nk"}),
    ("sg_app_version", 15),
])
def test_nontext_configured_values_fail_clearly(
    launch: Any, field: str, value: Any,
) -> None:
    provider, sg = launch
    sg.schema["sg_app_version"] = {}
    sg.version["sg_app_version"] = "15.1v4"
    sg.version[field] = value
    with pytest.raises(ValueError, match=field):
        provider.resolve_launch_source(
            "Version", 42, application_version_field="sg_app_version",
        )


def test_configured_field_omitted_from_response(launch: Any) -> None:
    provider, sg = launch
    del sg.version["sg_path_to_script"]
    with pytest.raises(ValueError, match="not returned: sg_path_to_script"):
        provider.resolve_launch_source("Version", 42)


@pytest.mark.parametrize("field, value", [
    ("project", None),
    ("entity", {}),
    ("project", SHOT),
    ("entity", {"type": "Asset", "id": 0}),
    ("sg_task", SHOT),
])
def test_invalid_context_links(launch: Any, field: str, value: Any) -> None:
    provider, sg = launch
    sg.version[field] = value
    with pytest.raises(ValueError, match=field):
        provider.resolve_launch_source("Version", 42)


def test_context_only_and_unlinked_task(launch: Any) -> None:
    provider, sg = launch
    del sg.version["sg_task"]
    result = provider.resolve_launch_source("Version", 42, source_fields=())
    assert result["sources"] == []
    assert result["task"] == {}
    assert sg.calls == [
        ("find_one", "Version", [["id", "is", 42]],
         ["project", "entity", "sg_task"]),
    ]


@pytest.mark.parametrize("kwargs", [
    {"source_fields": "sg_path_to_script"},
    {"source_fields": None},
    {"source_fields": [""]},
    {"source_fields": [123]},
    {"application_version_field": None},
    {"application_version_field": " "},
])
def test_invalid_configuration_before_connection(
    launch: Any, kwargs: Any,
) -> None:
    provider, sg = launch
    with pytest.raises(ValueError):
        provider.resolve_launch_source("Version", 42, **kwargs)
    assert sg.calls == []


@pytest.mark.parametrize("entity_id", [0, -1, True, "42", None])
def test_invalid_ids_before_connection(launch: Any, entity_id: Any) -> None:
    provider, sg = launch
    with pytest.raises(ValueError, match="positive integer"):
        provider.resolve_launch_source("Version", entity_id)
    with pytest.raises(ValueError, match="positive integer"):
        provider.list_launch_tasks("Shot", entity_id)
    assert sg.calls == []


def test_unsupported_source_type_before_connection(launch: Any) -> None:
    provider, sg = launch
    with pytest.raises(ValueError, match="only Version"):
        provider.resolve_launch_source("Shot", 42)
    assert sg.calls == []


@pytest.mark.parametrize("entity_type", ["Shot", "Asset", "CustomEntity01"])
def test_tasks_respect_exact_link_and_normalize(
    launch: Any, entity_type: str,
) -> None:
    provider, sg = launch
    entity = {"type": entity_type, "id": 2}
    sg.tasks = [
        {"id": 3, "content": "Comp", "project": PROJECT,
         "entity": dict(entity, name="Linked entity")},
        {"id": 4, "content": "Other", "project": PROJECT,
         "entity": {"type": entity_type, "id": 99}},
        {"id": 5, "content": "Wrong type", "project": PROJECT,
         "entity": {"type": "Sequence", "id": 2}},
    ]
    assert provider.list_launch_tasks(entity_type, 2) == [{
        "id": 3, "name": "Comp", "project": PROJECT, "entity": entity,
    }]
    assert sg.calls == [
        ("find", "Task", [["entity", "is", entity]],
         ["content", "project", "entity", "task_assignees",
          "sg_status_list", "step"],
         [{"field_name": "content", "direction": "asc"},
          {"field_name": "id", "direction": "asc"}]),
    ]


def test_current_user_is_normalized(launch: Any) -> None:
    provider, _sg = launch
    assert provider.current_user() == {
        "type": "HumanUser", "id": 10, "name": "Ada Artist"
    }


def test_launch_project_is_lightweight(
    launch: Any,
) -> None:
    provider, sg = launch
    provider.localize_launch_path = lambda path: path

    first = provider.resolve_launch_project(1)
    second = provider.resolve_launch_project(1)

    assert first == second == {
        "project_id": 1,
        "project_name": "Show",
        "project_root": "//server/show",
    }
    project_calls = [call for call in sg.calls if call[1] == "Project"]
    assert len(project_calls) == 2


def test_list_launch_projects_returns_minimal_active_rows(
    launch: Any,
) -> None:
    provider, sg = launch
    provider.localize_launch_path = lambda path: path

    assert provider.list_launch_projects() == [{
        "project_id": 1,
        "project_name": "Show",
        "project_root": "//server/show",
    }]
    assert sg.calls == [(
        "find",
        "Project",
        [
            ["sg_status", "in", ["Active", "Pitch", "Inhouse", "AI"]],
            ["is_template", "is", False],
        ],
        ["name", "code", "sg_project_path"],
        None,
    )]


def test_task_workfiles_are_flattened_and_deduplicated(launch: Any) -> None:
    provider, sg = launch
    sg.work_versions = [
        {
            "id": 8,
            "code": "sh010_comp_v002",
            "sg_path_to_script": "//show/a.nk;//show/b.nk",
        },
        {
            "id": 7,
            "code": "sh010_comp_v001",
            "sg_path_to_script": "//show/a.nk",
        },
    ]
    assert provider.list_task_workfiles(3) == [
        {
            "path": "//show/a.nk",
            "field": "sg_path_to_script",
            "version_id": 8,
            "code": "sh010_comp_v002",
        },
        {
            "path": "//show/b.nk",
            "field": "sg_path_to_script",
            "version_id": 8,
            "code": "sh010_comp_v002",
        },
    ]


@pytest.mark.parametrize("rows", [None, []])
def test_no_linked_tasks(launch: Any, rows: Any) -> None:
    provider, sg = launch
    sg.tasks = rows
    assert provider.list_launch_tasks("Shot", 2) == []


def test_launch_source_entity_drives_task_lookup(launch: Any) -> None:
    provider, sg = launch
    sg.version["sg_task"] = None
    sg.tasks = [{
        "id": 3, "content": "Comp", "project": PROJECT, "entity": SHOT,
    }]
    source = provider.resolve_launch_source("Version", 42)
    tasks = provider.list_launch_tasks(
        source["entity"]["type"], source["entity"]["id"],
    )
    assert source["task"] == {}
    assert tasks == [{
        "id": 3, "name": "Comp", "project": PROJECT, "entity": SHOT,
    }]
