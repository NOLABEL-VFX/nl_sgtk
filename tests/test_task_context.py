from __future__ import annotations

from typing import Any, Dict, List


class FakeShotGrid:
    def __init__(self) -> None:
        self.task = {
            "id": 42,
            "type": "Task",
            "content": "Comp",
            "entity": {"id": 7, "type": "Asset", "name": "Robot"},
            "project": {"id": 3, "type": "Project", "name": "Demo"},
        }

    def find(self, entity_type: str, filters: Any, fields: Any) -> List[Dict[str, Any]]:
        del filters, fields
        if entity_type == "Task":
            return [dict(self.task)]
        if entity_type == "LocalStorage":
            return []
        raise AssertionError(entity_type)

    def find_one(self, entity_type: str, filters: Any, fields: Any) -> Dict[str, Any]:
        del fields
        if entity_type == "Task" and filters == [["id", "is", 42]]:
            return dict(self.task)
        raise AssertionError((entity_type, filters))


def test_compact_task_identity_can_resolve_context(monkeypatch: Any) -> None:
    from nl_sgtk import nl_sgtk as api

    sg = FakeShotGrid()
    monkeypatch.setattr(api, "_status_name", lambda value: value)

    rows = api.get_user_tasks({"id": 9, "type": "HumanUser"}, sg=sg)

    assert rows[0]["id"] == 42
    assert rows[0]["type"] == "Task"
    assert api.get_task_context(rows[0]["id"], sg=sg)["id"] == 42
