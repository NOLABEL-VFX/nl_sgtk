from types import SimpleNamespace
from unittest.mock import Mock

from nl_sgtk.provider import NlSgtkProvider


def test_sequence_membership_queries_direct_shots_only():
    provider = NlSgtkProvider()
    client = Mock()
    client.find_one.return_value = {
        "shots": [{"type": "Shot", "id": 10, "name": "A"}],
        "sg_scenes": [{"type": "Scene", "id": 11, "name": "Scene"}],
    }
    provider._sg = client
    rows = provider.find_entity_relationships(
        SimpleNamespace(type="Sequence", id=2),
        ("sequence_shot_membership",))
    client.find_one.assert_called_once_with(
        "Sequence", [["id", "is", 2]], ["shots"])
    assert [row["entity"]["id"] for row in rows] == [10]
    assert rows[0]["direction"] == "downstream"


def test_task_dependency_keeps_both_directions_explicit():
    provider = NlSgtkProvider()
    provider._sg = Mock()
    provider._sg.find_one.return_value = {
        "upstream_tasks": [{"type": "Task", "id": 10}],
        "downstream_tasks": [{"type": "Task", "id": 11}],
    }
    rows = provider.find_entity_relationships(
        SimpleNamespace(type="Task", id=2), ("task_dependency",))
    assert [(row["entity"]["id"], row["direction"]) for row in rows] == [
        (10, "upstream"), (11, "downstream")]
