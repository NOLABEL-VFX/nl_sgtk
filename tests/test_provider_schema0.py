"""Exact legacy evidence, storage mapping and ambiguous path ownership."""
from types import SimpleNamespace

from nl_sgtk.provider import NlSgtkProvider


def provider(rows):
    calls = []
    sg = SimpleNamespace(find=lambda *a, **k: calls.append((a, k)) or rows)
    result = NlSgtkProvider(sg=sg)
    result._storages = []
    return result, calls


def test_history_is_scoped_to_task_and_preserves_all_channels():
    row = {"sg_task": {"type": "Task", "id": 30},
           "sg_path_to_script": "C:/show/scene.max;C:/show/scene.nk",
           "sg_path_to_frames": "C:/show/output/main.%04d.exr",
           "user": {"type": "Group", "id": 4},
           "user.Group.tags": [{"name": "Vendor"}]}
    service, calls = provider([row])
    context = SimpleNamespace(project=SimpleNamespace(id=10),
                              task=SimpleNamespace(id=30))
    assert service.find_legacy_versions(context) == [row]
    args = calls[0][0]
    assert ["sg_task", "is", {"type": "Task", "id": 30}] in args[1]
    assert "sg_path_to_movie" in args[2]
    assert "sg_path_to_geometry" in args[2]


def test_exact_paths_preserve_ambiguity_and_ignore_filename_only_matches():
    service, calls = provider([
        {"sg_task": {"type": "Task", "id": 30},
         "sg_path_to_script": "C:/show/scene.max;C:/show/other.max"},
        {"sg_task": {"type": "Task", "id": 31},
         "sg_path_to_script": "C:/show/scene.max"},
        {"sg_task": {"type": "Task", "id": 32},
         "sg_path_to_script": "C:/unrelated/scene.max"},
    ])
    assert {row["id"] for row in service.find_tasks_for_path(
        "C:/show/scene.max")} == {30, 31}
    assert service.find_tasks_for_path("C:/show") == []
    assert len(calls) == 1
