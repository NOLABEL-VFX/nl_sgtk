"""Regression tests for the reusable technical Ticket API."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from nl_sgtk.tickets import (
    PipelineGroup,
    TicketAttachmentError,
    TicketCreationError,
    TicketPriority,
    TicketReadbackError,
    TicketRoutingError,
    TicketSchemaError,
    TicketType,
    TicketValidationError,
    create_ticket,
    pipeline_group,
)


class FakeShotGrid:
    """Implement the Ticket subset of the ShotGrid API for deterministic tests."""

    def __init__(
        self,
        *,
        create_error: Optional[Exception] = None,
        upload_error_at: Optional[int] = None,
        readback: bool = True,
    ) -> None:
        self.create_error = create_error
        self.upload_error_at = upload_error_at
        self.readback = readback
        self.created: List[Dict[str, Any]] = []
        self.uploaded: List[str] = []

    def schema_field_read(self, entity_type: str) -> Dict[str, Any]:
        assert entity_type == "Ticket"
        fields = (
            "title",
            "description",
            "project",
            "sg_ticket_type",
            "sg_priority",
            "sg_status_list",
            "sg_was_error",
            "addressings_to",
            "addressings_cc",
        )
        schema = {
            field: {"editable": {"value": True}, "properties": {}}
            for field in fields
        }
        schema["sg_ticket_type"]["properties"] = {
            "valid_values": {
                "value": [
                    "Feature Request",
                    "Bug Report",
                    "Software Need",
                    "Data Wrangling",
                ],
            }
        }
        schema["sg_priority"]["properties"] = {
            "valid_values": {
                "value": [item.value for item in TicketPriority],
            }
        }
        schema["sg_status_list"]["properties"] = {
            "valid_values": {"value": ["wtg", "opn", "ip", "res"]}
        }
        return schema

    def find_one(self, entity_type: str, filters: Any, fields: Any) -> Optional[Dict[str, Any]]:
        del fields
        entity_id = filters[0][2]
        if entity_type == "Project" and entity_id == 750:
            return {"type": "Project", "id": 750, "name": "00_IN_HOUSE"}
        configured_groups = {
            302: "Pipeline Development",
            1523: "3DMax Development",
            1524: "Houdini Development",
            1525: "ComfyUI Development",
        }
        if entity_type == "Group" and entity_id in configured_groups:
            return {
                "type": "Group",
                "id": entity_id,
                "code": configured_groups[entity_id],
            }
        if entity_type == "Group" and entity_id == 500:
            return {"type": "Group", "id": 500, "code": "Custom Tech"}
        if entity_type == "HumanUser" and entity_id == 77:
            return {"type": "HumanUser", "id": 77, "name": "Tech Artist"}
        if entity_type == "Ticket" and entity_id == 9001 and self.readback:
            payload = dict(self.created[-1])
            payload.update({"type": "Ticket", "id": 9001})
            return payload
        return None

    def create(self, entity_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        assert entity_type == "Ticket"
        if self.create_error:
            raise self.create_error
        self.created.append(dict(payload))
        return {"type": "Ticket", "id": 9001}

    def upload(self, entity_type: str, entity_id: int, path: str) -> int:
        assert (entity_type, entity_id) == ("Ticket", 9001)
        if self.upload_error_at == len(self.uploaded):
            raise RuntimeError("upload unavailable")
        self.uploaded.append(path)
        return 7000 + len(self.uploaded)


def _user() -> Dict[str, Any]:
    return {"type": "HumanUser", "id": 484, "name": "Ada Artist", "_login": "ada"}


def test_ticket_api_is_exported_from_package_root() -> None:
    import nl_sgtk

    assert nl_sgtk.pipeline_group.PIPELINE is PipelineGroup.PIPELINE
    assert nl_sgtk.TicketType.BUG.value == "Bug Report"
    assert nl_sgtk.TicketPriority.MEDIUM.value == "Medium"
    assert nl_sgtk.create_ticket is create_ticket


def test_pipeline_enum_creates_verified_ticket_with_metadata_and_attachment(
    tmp_path: Path,
) -> None:
    attachment = tmp_path / "diagnostic.txt"
    attachment.write_text("trace", encoding="utf-8")
    sg = FakeShotGrid()

    result = create_ticket(
        "Render failed",
        "Worker stopped with token=secret-value",
        ticket_type=TicketType.ERROR,
        priority=TicketPriority.HIGH,
        user_group=pipeline_group.PIPELINE,
        attachments=[attachment],
        metadata={"file": "shot010.nk", "frames": [1001, 1002]},
        occurred_at=datetime(2026, 8, 29, 20, 0, tzinfo=timezone.utc),
        sg=sg,
        user=_user(),
    )

    assert result.ticket_id == 9001
    assert result.attachment_ids == (7001,)
    payload = sg.created[0]
    assert payload["project"]["id"] == 750
    assert payload["addressings_to"] == [
        {"type": "Group", "id": 302, "name": "Pipeline Development"}
    ]
    assert payload["sg_ticket_type"] == "Bug Report"
    assert payload["sg_priority"] == "High"
    assert payload["sg_status_list"] == "wtg"
    assert payload["sg_was_error"] is True
    assert payload["description"].startswith("Technical ticket metadata")
    assert "reporter: Ada Artist" in payload["description"]
    assert 'file: "shot010.nk"' in payload["description"]
    assert "secret-value" not in payload["description"]


@pytest.mark.parametrize(
    "recipient, expected_id, expected_name",
    [
        (PipelineGroup.PIPELINE, 302, "Pipeline Development"),
        (PipelineGroup.MAX, 1523, "3DMax Development"),
        (PipelineGroup.HOUDINI, 1524, "Houdini Development"),
        (PipelineGroup.COMFY, 1525, "ComfyUI Development"),
    ],
)
def test_enum_routes_create_for_the_expected_group(
    recipient: PipelineGroup,
    expected_id: int,
    expected_name: str,
) -> None:
    assert len({member.value for member in PipelineGroup}) == 4
    sg = FakeShotGrid()
    create_ticket("Topic", "Content", user_group=recipient, sg=sg, user=_user())
    assert sg.created[0]["addressings_to"] == [
        {"type": "Group", "id": expected_id, "name": expected_name}
    ]


def test_enum_route_accepts_a_group_whose_name_has_changed(
    monkeypatch: Any,
) -> None:
    sg = FakeShotGrid()
    original_find_one = sg.find_one

    def renamed_group(entity_type: str, filters: Any, fields: Any) -> Any:
        if entity_type == "Group" and filters[0][2] == 1523:
            return {"type": "Group", "id": 1523, "code": "Unexpected Group"}
        return original_find_one(entity_type, filters, fields)

    monkeypatch.setattr(sg, "find_one", renamed_group)
    create_ticket(
        "Topic",
        "Content",
        user_group=PipelineGroup.MAX,
        sg=sg,
        user=_user(),
    )
    assert sg.created[0]["addressings_to"] == [
        {"type": "Group", "id": 1523, "name": "Unexpected Group"}
    ]


def test_enum_route_rejects_a_missing_group_id(monkeypatch: Any) -> None:
    sg = FakeShotGrid()
    original_find_one = sg.find_one

    def missing_group(entity_type: str, filters: Any, fields: Any) -> Any:
        if entity_type == "Group" and filters[0][2] == 1525:
            return None
        return original_find_one(entity_type, filters, fields)

    monkeypatch.setattr(sg, "find_one", missing_group)
    with pytest.raises(TicketRoutingError, match="Group 1525"):
        create_ticket(
            "Topic",
            "Content",
            user_group=PipelineGroup.COMFY,
            sg=sg,
            user=_user(),
        )
    assert sg.created == []


def test_semantic_type_aliases_match_live_shotgrid_values() -> None:
    assert TicketType.ERROR is TicketType.BUG
    assert TicketType.REQUEST is TicketType.FEATURE
    assert TicketType.SOFTWARE_NEED.value == "Software Need"
    assert TicketType.DATA_WRANGLING.value == "Data Wrangling"
    assert TicketPriority.URGENT.value == "Urgent"


def test_manual_report_can_be_marked_as_not_originating_from_an_error() -> None:
    sg = FakeShotGrid()
    result = create_ticket(
        "Manual report",
        "The user noticed an issue.",
        was_error=False,
        sg=sg,
        user=_user(),
    )

    assert result.ticket["sg_was_error"] is False
    assert sg.created[0]["sg_was_error"] is False


@pytest.mark.parametrize(
    "recipient, expected_type, expected_id",
    [
        ({"type": "Group", "id": 500, "name": "Custom Tech"}, "Group", 500),
        ({"type": "HumanUser", "id": 77, "name": "Tech Artist"}, "HumanUser", 77),
    ],
)
def test_full_group_or_humanuser_link_is_validated_and_supported(
    recipient: Dict[str, Any],
    expected_type: str,
    expected_id: int,
) -> None:
    sg = FakeShotGrid()
    create_ticket("Topic", "Content", user_group=recipient, sg=sg, user=_user())
    assert sg.created[0]["addressings_to"][0]["type"] == expected_type
    assert sg.created[0]["addressings_to"][0]["id"] == expected_id


def test_invalid_input_and_attachment_fail_before_create(tmp_path: Path) -> None:
    sg = FakeShotGrid()
    with pytest.raises(TicketValidationError, match="topic"):
        create_ticket("", "Content", sg=sg, user=_user())
    with pytest.raises(TicketValidationError, match="attachment"):
        create_ticket(
            "Topic",
            "Content",
            attachments=[tmp_path / "missing.txt"],
            sg=sg,
            user=_user(),
        )
    with pytest.raises(TicketValidationError, match="TicketType"):
        create_ticket("Topic", "Content", ticket_type="Bug Report", sg=sg, user=_user())
    with pytest.raises(TicketValidationError, match="was_error"):
        create_ticket("Topic", "Content", was_error=1, sg=sg, user=_user())
    assert sg.created == []


def test_schema_mismatch_fails_before_create(monkeypatch: Any) -> None:
    sg = FakeShotGrid()
    schema = sg.schema_field_read("Ticket")
    schema["sg_priority"]["properties"]["valid_values"]["value"] = ["Low"]
    monkeypatch.setattr(sg, "schema_field_read", lambda entity_type: schema)
    with pytest.raises(TicketSchemaError, match="does not allow"):
        create_ticket(
            "Topic",
            "Content",
            priority=TicketPriority.HIGH,
            sg=sg,
            user=_user(),
        )
    assert sg.created == []


def test_creation_and_readback_errors_preserve_write_state() -> None:
    create_failure = FakeShotGrid(create_error=RuntimeError("offline"))
    with pytest.raises(TicketCreationError) as create_info:
        create_ticket("Topic", "Content", sg=create_failure, user=_user())
    assert isinstance(create_info.value.__cause__, RuntimeError)

    readback_failure = FakeShotGrid(readback=False)
    with pytest.raises(TicketReadbackError) as readback_info:
        create_ticket("Topic", "Content", sg=readback_failure, user=_user())
    assert readback_info.value.ticket_id == 9001
    assert len(readback_failure.created) == 1


def test_partial_attachment_failure_exposes_ticket_and_uploaded_paths(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    sg = FakeShotGrid(upload_error_at=1)

    with pytest.raises(TicketAttachmentError) as error_info:
        create_ticket(
            "Topic",
            "Content",
            attachments=[first, second],
            sg=sg,
            user=_user(),
        )

    assert error_info.value.ticket_id == 9001
    assert error_info.value.failed_path == str(second.resolve())
    assert error_info.value.uploaded_paths == (str(first.resolve()),)
    assert len(sg.created) == 1


def test_default_path_uses_current_user_login(monkeypatch: Any) -> None:
    from nl_sgtk import nl_sgtk as api

    sg = FakeShotGrid()
    calls: List[str] = []

    def login(*, product: str):
        calls.append(product)
        return sg, _user()

    monkeypatch.setattr(api, "sgtk_login", login)
    result = create_ticket("Topic", "Content")
    assert result.ticket_id == 9001
    assert calls == ["NL SGTK Technical Tickets"]
