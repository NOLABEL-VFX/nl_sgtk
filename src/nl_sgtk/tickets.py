"""Create validated technical support Tickets in Flow Production Tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Optional, Sequence, Tuple, Union


DEFAULT_PROJECT_ID = 750
DEFAULT_PROJECT_NAME = "00_IN_HOUSE"
PIPELINE_GROUP_ID = 302
MAX_GROUP_ID = 1523
HOUDINI_GROUP_ID = 1524
COMFY_GROUP_ID = 1525
DEFAULT_TICKET_STATUS = "wtg"


class PipelineGroup(str, Enum):
    """Identify the technical discipline that should receive a Ticket."""

    PIPELINE = "pipeline"
    COMFY = "comfy"
    MAX = "max"
    HOUDINI = "houdini"


# Preserve the requested lower-case enum access style:
# ``pipeline_group.PIPELINE``.
pipeline_group = PipelineGroup


class TicketType(str, Enum):
    """Provide supported semantic Ticket categories."""

    BUG = "Bug Report"
    ERROR = "Bug Report"
    FEATURE = "Feature Request"
    REQUEST = "Feature Request"
    SOFTWARE_NEED = "Software Need"
    DATA_WRANGLING = "Data Wrangling"


class TicketPriority(str, Enum):
    """Provide supported semantic Ticket priorities."""

    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    URGENT = "Urgent"
    CRITICAL = "Critical"


class TicketError(RuntimeError):
    """Base exception for technical Ticket operations."""


class TicketValidationError(TicketError):
    """Report invalid caller input before any ShotGrid write occurs."""


class TicketAuthenticationError(TicketError):
    """Report failure to resolve the current authenticated user."""


class TicketSchemaError(TicketError):
    """Report an incompatible or inaccessible live Ticket schema."""


class TicketRoutingError(TicketError):
    """Report an unavailable or invalid Group/HumanUser destination."""


class TicketCreationError(TicketError):
    """Report a Ticket create failure before a stable Ticket ID exists."""


class TicketReadbackError(TicketError):
    """Report a created Ticket that could not be verified by readback."""

    def __init__(self, ticket_id: int, message: str) -> None:
        self.ticket_id = ticket_id
        super().__init__(message)


class TicketAttachmentError(TicketError):
    """Report attachment failure without hiding the created Ticket identity."""

    def __init__(
        self,
        ticket_id: int,
        failed_path: str,
        uploaded_paths: Sequence[str],
        message: str,
    ) -> None:
        self.ticket_id = ticket_id
        self.failed_path = failed_path
        self.uploaded_paths = tuple(uploaded_paths)
        super().__init__(message)


class TicketNoteError(TicketError):
    """Report a duplicate occurrence that could not be added as a Note."""

    def __init__(self, ticket_id: int, message: str) -> None:
        self.ticket_id = ticket_id
        super().__init__(message)


@dataclass(frozen=True)
class TicketResult:
    """Describe a verified Ticket and its successfully uploaded attachments."""

    ticket: Mapping[str, Any]
    attachment_ids: Tuple[int, ...] = ()
    attachment_paths: Tuple[str, ...] = ()
    note: Optional[Mapping[str, Any]] = None
    created: bool = True

    @property
    def ticket_id(self) -> int:
        """Return the verified numeric Ticket identifier."""

        return int(self.ticket["id"])

    @property
    def note_id(self) -> Optional[int]:
        """Return the Note ID when this occurrence was correlated."""

        return int(self.note["id"]) if self.note else None


Recipient = Union[PipelineGroup, Mapping[str, Any]]


def create_ticket(
    topic: str,
    content: str,
    *,
    ticket_type: TicketType = TicketType.BUG,
    priority: TicketPriority = TicketPriority.MEDIUM,
    user_group: Recipient = PipelineGroup.PIPELINE,
    was_error: bool = True,
    attachments: Sequence[Union[str, os.PathLike[str]]] = (),
    metadata: Optional[Mapping[str, Any]] = None,
    session_id: Optional[str] = None,
    deduplicate: bool = True,
    sg: Any = None,
    user: Optional[Mapping[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
) -> TicketResult:
    """Create and verify one technical Ticket for the current user.

    Args:
        topic: Short Ticket title.
        content: Detailed technical report body.
        ticket_type: Semantic type written to ``sg_ticket_type``.
        priority: Semantic priority written to ``sg_priority``.
        user_group: Routing enum or complete Group/HumanUser entity link.
        was_error: Whether the Ticket originated from a technical error. Pass
            ``False`` for a manual user report.
        attachments: Existing local files uploaded after Ticket creation.
        metadata: Structured diagnostic values rendered above ``content`` and
            stored as canonical JSON in ``sg_metadata_json``.
        session_id: Optional stable application-session identifier used for
            correlation. Secret session tokens must not be supplied.
        deduplicate: Add matching error occurrences as Notes instead of new
            Tickets. Matching uses session ID or a deterministic fingerprint.
        sg: Optional injected ShotGrid connection for tests or host reuse.
        user: Current user paired with an injected connection.
        occurred_at: Optional timezone-aware occurrence timestamp.

    Returns:
        Verified Ticket data and successful attachment identifiers.

    Raises:
        TicketValidationError: If inputs or attachment paths are invalid.
        TicketAuthenticationError: If current-user authentication fails.
        TicketSchemaError: If required fields or enum values are unsupported.
        TicketRoutingError: If the destination is unavailable or invalid.
        TicketCreationError: If ShotGrid rejects Ticket creation.
        TicketReadbackError: If a created Ticket cannot be read back.
        TicketAttachmentError: If an upload fails after Ticket creation. The
            exception retains ``ticket_id`` and already uploaded paths.

    Side Effects:
        - Authenticates through :func:`nl_sgtk.sgtk_login` when no connection
          is injected.
        - Creates one ShotGrid Ticket, or a Note on a matching open Ticket,
          and uploads zero or more attachments to the created entity.

    Notes:
        - Attachment uploads are non-atomic. Never blindly retry after
          ``TicketAttachmentError``; use its ``ticket_id`` first.
        - Enum routes are validated against their stable ShotGrid Group ID
          before the Ticket is created. Group names may change.
    """

    normalized_topic = _require_text(topic, "topic", maximum=255)
    normalized_content = _require_text(content, "content")
    normalized_type = _require_enum(ticket_type, TicketType, "ticket_type")
    normalized_priority = _require_enum(priority, TicketPriority, "priority")
    normalized_was_error = _require_bool(was_error, "was_error")
    normalized_deduplicate = _require_bool(deduplicate, "deduplicate")
    normalized_session_id = _normalize_session_id(session_id)
    attachment_paths = _validate_attachments(attachments)
    current = _normalize_datetime(occurred_at)
    client, current_user = _resolve_session(sg, user)
    project = _require_named_entity(
        client,
        "Project",
        DEFAULT_PROJECT_ID,
        DEFAULT_PROJECT_NAME,
        "name",
        TicketRoutingError,
    )
    recipient = _resolve_recipient(client, user_group)
    description = format_ticket_content(
        normalized_content,
        current_user,
        metadata=metadata,
        occurred_at=current,
    )
    metadata_json = _build_metadata_json(
        normalized_topic,
        normalized_content,
        current_user,
        metadata,
        normalized_session_id,
        current,
    )
    if normalized_was_error and normalized_deduplicate:
        match = _find_correlated_ticket(client, project, metadata_json)
        if match:
            return _append_occurrence(
                client,
                match,
                project,
                normalized_topic,
                description,
                metadata_json,
                attachment_paths,
            )
    payload = {
        "title": normalized_topic,
        "description": description,
        "project": project,
        "sg_ticket_type": normalized_type.value,
        "sg_priority": normalized_priority.value,
        "sg_status_list": DEFAULT_TICKET_STATUS,
        "sg_was_error": normalized_was_error,
        "sg_metadata_json": json.dumps(metadata_json, ensure_ascii=False, sort_keys=True),
        "addressings_to": [recipient],
        "addressings_cc": [],
    }
    _validate_schema(client, payload)

    try:
        created = client.create("Ticket", payload)
        ticket_id = int(created["id"])
    except Exception as exc:
        raise TicketCreationError(
            "ShotGrid rejected the Ticket creation; no attachment was attempted."
        ) from exc

    ticket = _readback_ticket(client, ticket_id)
    uploaded_ids = []
    uploaded_paths = []
    for attachment_path in attachment_paths:
        try:
            attachment_id = client.upload("Ticket", ticket_id, attachment_path)
            uploaded_ids.append(int(attachment_id))
            uploaded_paths.append(attachment_path)
        except Exception as exc:
            raise TicketAttachmentError(
                ticket_id,
                attachment_path,
                uploaded_paths,
                (
                    f"Ticket {ticket_id} exists, but attachment upload failed for "
                    f"{attachment_path!r}. Inspect the Ticket before retrying."
                ),
            ) from exc

    return TicketResult(
        ticket=ticket,
        attachment_ids=tuple(uploaded_ids),
        attachment_paths=tuple(uploaded_paths),
    )


def _build_metadata_json(
    topic: str,
    content: str,
    user: Mapping[str, Any],
    metadata: Optional[Mapping[str, Any]],
    session_id: Optional[str],
    occurred_at: datetime,
) -> dict[str, Any]:
    safe_metadata = _sanitize_metadata_value(metadata or {})
    fingerprint_source = {
        "topic": topic.casefold(),
        "content": _redact_secrets(content).casefold(),
        "metadata": safe_metadata,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_source, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    occurred = occurred_at.isoformat()
    return {
        "schema_version": 1,
        "session_id": session_id,
        "error_fingerprint": fingerprint,
        "reporter": {"type": user.get("type"), "id": user.get("id")},
        "first_occurred_at": occurred,
        "last_occurred_at": occurred,
        "occurrence_count": 1,
        "metadata": safe_metadata,
    }


def _sanitize_metadata_value(value: Any, key: str = "") -> Any:
    if re.search(r"(?i)(api[_-]?key|password|passwd|token|secret)", key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize_metadata_value(item_value, str(item_key))
            for item_key, item_value in sorted(
                value.items(), key=lambda item: str(item[0]).casefold()
            )
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_metadata_value(item) for item in value]
    if isinstance(value, str):
        return _redact_secrets(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_secrets(str(value))


def _find_correlated_ticket(
    client: Any,
    project: Mapping[str, Any],
    metadata_json: Mapping[str, Any],
) -> Optional[Mapping[str, Any]]:
    try:
        candidates = client.find(
            "Ticket",
            [
                ["project", "is", project],
                ["sg_was_error", "is", True],
                ["sg_status_list", "is_not", "res"],
                ["sg_metadata_json", "is_not", None],
            ],
            ["title", "sg_metadata_json", "sg_status_list"],
            order=[{"field_name": "created_at", "direction": "desc"}],
            limit=100,
        )
    except Exception as exc:
        raise TicketReadbackError(0, "Existing Tickets could not be checked for duplicates.") from exc
    wanted_session = metadata_json.get("session_id")
    wanted_fingerprint = metadata_json.get("error_fingerprint")
    for candidate in candidates or ():
        try:
            stored = json.loads(candidate.get("sg_metadata_json") or "{}")
        except (TypeError, ValueError):
            continue
        same_session = bool(wanted_session and stored.get("session_id") == wanted_session)
        same_error = bool(wanted_fingerprint and stored.get("error_fingerprint") == wanted_fingerprint)
        if same_session or same_error:
            return candidate
    return None


def _append_occurrence(
    client: Any,
    ticket: Mapping[str, Any],
    project: Mapping[str, Any],
    topic: str,
    description: str,
    occurrence: Mapping[str, Any],
    attachment_paths: Sequence[str],
) -> TicketResult:
    ticket_id = int(ticket["id"])
    note_payload = {
        "subject": f"Repeated occurrence: {topic}",
        "content": description,
        "note_links": [{"type": "Ticket", "id": ticket_id}],
        "project": project,
    }
    _validate_editable_fields(client, "Note", note_payload)
    try:
        note = client.create("Note", note_payload)
        note_id = int(note["id"])
    except Exception as exc:
        raise TicketNoteError(ticket_id, f"Ticket {ticket_id} matched, but its Note could not be created.") from exc

    stored = json.loads(ticket.get("sg_metadata_json") or "{}")
    stored["last_occurred_at"] = occurrence["last_occurred_at"]
    stored["occurrence_count"] = int(stored.get("occurrence_count") or 1) + 1
    try:
        client.update("Ticket", ticket_id, {
            "sg_metadata_json": json.dumps(stored, ensure_ascii=False, sort_keys=True)
        })
    except Exception as exc:
        raise TicketNoteError(ticket_id, f"Note {note_id} exists, but Ticket metadata could not be updated.") from exc

    uploaded_ids = []
    uploaded_paths = []
    for attachment_path in attachment_paths:
        try:
            uploaded_ids.append(int(client.upload("Note", note_id, attachment_path)))
            uploaded_paths.append(attachment_path)
        except Exception as exc:
            raise TicketAttachmentError(
                ticket_id, attachment_path, uploaded_paths,
                f"Note {note_id} exists, but attachment upload failed for {attachment_path!r}.",
            ) from exc
    refreshed = dict(ticket)
    refreshed["sg_metadata_json"] = json.dumps(stored, ensure_ascii=False, sort_keys=True)
    return TicketResult(
        ticket=refreshed,
        note={"type": "Note", "id": note_id},
        created=False,
        attachment_ids=tuple(uploaded_ids),
        attachment_paths=tuple(uploaded_paths),
    )


def format_ticket_content(
    content: str,
    user: Mapping[str, Any],
    *,
    metadata: Optional[Mapping[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
) -> str:
    """Render safe structured metadata above a technical Ticket body.

    Args:
        content: Detailed technical report body.
        user: Current HumanUser or ApiUser entity.
        metadata: Additional structured diagnostic values.
        occurred_at: Optional occurrence time; defaults to current UTC time.

    Returns:
        Plain text ready for the Ticket ``description`` field.

    Raises:
        TicketValidationError: If content or metadata is malformed.

    Notes:
        Common credential, token, password, and session URL patterns are
        redacted from both metadata and content.
    """

    body = _require_text(content, "content")
    if not isinstance(user, Mapping):
        raise TicketValidationError("user must be a ShotGrid entity mapping.")
    extra = metadata or {}
    if not isinstance(extra, Mapping):
        raise TicketValidationError("metadata must be a mapping when supplied.")
    current = _normalize_datetime(occurred_at)
    reporter_name = str(user.get("name") or user.get("_login") or "unknown")
    reporter_type = str(user.get("type") or "unknown")
    reporter_id = user.get("id")
    lines = [
        "Technical ticket metadata",
        f"reporter: {reporter_name}",
        f"reporter_entity: {reporter_type}:{reporter_id if reporter_id is not None else 'unknown'}",
        f"occurred_at: {current.isoformat()}",
    ]
    for key in sorted(extra, key=lambda item: str(item).casefold()):
        normalized_key = str(key).strip()
        if not normalized_key or "\n" in normalized_key or "\r" in normalized_key:
            raise TicketValidationError("metadata keys must be non-empty single-line values.")
        value = extra[key]
        try:
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError) as exc:
            raise TicketValidationError(
                f"metadata value for {normalized_key!r} cannot be serialized."
            ) from exc
        lines.append(f"{normalized_key}: {rendered}")
    lines.extend(("", "Ticket content", body))
    return _redact_secrets("\n".join(lines))


def _resolve_session(
    sg: Any,
    user: Optional[Mapping[str, Any]],
) -> Tuple[Any, Mapping[str, Any]]:
    if (sg is None) != (user is None):
        raise TicketValidationError("sg and user must be supplied together.")
    if sg is None:
        from .nl_sgtk import sgtk_login

        sg, user = sgtk_login(product="NL SGTK Technical Tickets")
    if sg is None or not isinstance(user, Mapping):
        raise TicketAuthenticationError("ShotGrid current-user authentication failed.")
    if user.get("type") not in {"HumanUser", "ApiUser"}:
        raise TicketAuthenticationError("Authenticated identity is not a HumanUser or ApiUser.")
    return sg, user


def _resolve_recipient(client: Any, recipient: Recipient) -> Mapping[str, Any]:
    if isinstance(recipient, PipelineGroup):
        if recipient is PipelineGroup.PIPELINE:
            group_id = PIPELINE_GROUP_ID
        elif recipient is PipelineGroup.COMFY:
            group_id = COMFY_GROUP_ID
        elif recipient is PipelineGroup.MAX:
            group_id = MAX_GROUP_ID
        elif recipient is PipelineGroup.HOUDINI:
            group_id = HOUDINI_GROUP_ID
        else:  # pragma: no cover - defensive against runtime enum mutation
            raise TicketRoutingError(f"Unsupported PipelineGroup: {recipient!r}.")
        entity = client.find_one("Group", [["id", "is", group_id]], ["code"])
        if not entity:
            raise TicketRoutingError(
                f"Configured Group {group_id} does not exist or is inaccessible."
            )
        return {
            "type": "Group",
            "id": group_id,
            "name": entity.get("code"),
        }
    if not isinstance(recipient, Mapping):
        raise TicketRoutingError(
            "user_group must be a PipelineGroup or Group/HumanUser entity mapping."
        )
    entity_type = recipient.get("type")
    entity_id = recipient.get("id")
    if entity_type not in {"Group", "HumanUser"}:
        raise TicketRoutingError("recipient type must be Group or HumanUser.")
    if not isinstance(entity_id, int) or isinstance(entity_id, bool) or entity_id <= 0:
        raise TicketRoutingError("recipient id must be a positive integer.")
    name_field = "code" if entity_type == "Group" else "name"
    entity = client.find_one(entity_type, [["id", "is", entity_id]], [name_field])
    if not entity:
        raise TicketRoutingError(f"{entity_type} {entity_id} does not exist or is inaccessible.")
    return {
        "type": entity_type,
        "id": entity_id,
        "name": entity.get(name_field) or recipient.get("name") or recipient.get("code"),
    }


def _require_named_entity(
    client: Any,
    entity_type: str,
    entity_id: int,
    expected_name: str,
    name_field: str,
    error_type: type[TicketError],
) -> Mapping[str, Any]:
    entity = client.find_one(entity_type, [["id", "is", entity_id]], [name_field])
    if not entity or entity.get(name_field) != expected_name:
        raise error_type(
            f"Configured {entity_type} {entity_id} is missing or is not {expected_name!r}."
        )
    return {"type": entity_type, "id": entity_id, "name": expected_name}


def _validate_schema(client: Any, payload: Mapping[str, Any]) -> None:
    schema = _validate_editable_fields(client, "Ticket", payload)
    for field in ("sg_ticket_type", "sg_priority", "sg_status_list"):
        valid_values = (
            schema[field].get("properties", {}).get("valid_values", {}).get("value")
        )
        if valid_values and payload[field] not in valid_values:
            raise TicketSchemaError(
                f"Ticket field {field!r} does not allow {payload[field]!r}."
            )


def _validate_editable_fields(
    client: Any,
    entity_type: str,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        schema = client.schema_field_read(entity_type)
    except Exception as exc:
        raise TicketSchemaError(
            f"{entity_type} schema could not be read before creation."
        ) from exc
    for field in payload:
        definition = schema.get(field)
        if not definition:
            raise TicketSchemaError(
                f"Required {entity_type} field {field!r} is unavailable."
            )
        editable = definition.get("editable", {}).get("value", True)
        if editable is False:
            raise TicketSchemaError(
                f"Required {entity_type} field {field!r} is not editable."
            )
    return schema


def _readback_ticket(client: Any, ticket_id: int) -> Mapping[str, Any]:
    try:
        ticket = client.find_one(
            "Ticket",
            [["id", "is", ticket_id]],
            [
                "title",
                "description",
                "project",
                "sg_ticket_type",
                "sg_priority",
                "sg_status_list",
                "sg_was_error",
                "sg_metadata_json",
                "addressings_to",
                "created_by",
                "created_at",
            ],
        )
    except Exception as exc:
        raise TicketReadbackError(
            ticket_id,
            f"Ticket {ticket_id} was created but readback failed.",
        ) from exc
    if not ticket or int(ticket.get("id") or 0) != ticket_id:
        raise TicketReadbackError(
            ticket_id,
            f"Ticket {ticket_id} was created but could not be verified by readback.",
        )
    return ticket


def _validate_attachments(
    attachments: Sequence[Union[str, os.PathLike[str]]],
) -> Tuple[str, ...]:
    if isinstance(attachments, (str, bytes, os.PathLike)):
        raise TicketValidationError("attachments must be a sequence of file paths.")
    normalized = []
    for raw_path in attachments:
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, TypeError) as exc:
            raise TicketValidationError(f"attachment does not exist: {raw_path!r}") from exc
        if not path.is_file():
            raise TicketValidationError(f"attachment is not a file: {str(path)!r}")
        rendered = str(path)
        if rendered not in normalized:
            normalized.append(rendered)
    return tuple(normalized)


def _require_text(value: Any, name: str, maximum: Optional[int] = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TicketValidationError(f"{name} must be a non-empty string.")
    normalized = value.strip()
    if maximum is not None and len(normalized) > maximum:
        raise TicketValidationError(f"{name} must not exceed {maximum} characters.")
    return normalized


def _require_enum(value: Any, enum_type: type[Enum], name: str) -> Any:
    if not isinstance(value, enum_type):
        raise TicketValidationError(f"{name} must be a {enum_type.__name__} value.")
    return value


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TicketValidationError(f"{name} must be a bool value.")
    return value


def _normalize_session_id(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = _require_text(value, "session_id", maximum=255)
    if re.search(r"(?i)(token|password|secret|bearer|https?://|[?&][^=]+=)", normalized):
        raise TicketValidationError("session_id must be an opaque non-secret identifier.")
    return normalized


def _normalize_datetime(value: Optional[datetime]) -> datetime:
    current = value or datetime.now(timezone.utc)
    if not isinstance(current, datetime):
        raise TicketValidationError("occurred_at must be a datetime value.")
    if current.tzinfo is None:
        raise TicketValidationError("occurred_at must include timezone information.")
    return current.astimezone(timezone.utc)


def _redact_secrets(value: str) -> str:
    secret_pattern = re.compile(
        r"(?i)\b(api[_-]?key|password|passwd|token|secret|session[_-]?token)"
        r"(\s*[=:]\s*)([^\s,;]+)"
    )
    text = secret_pattern.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        value,
    )
    text = re.sub(r"(?i)(https?://[^\s?]+)\?[^\s]+", r"\1?<redacted>", text)
    return re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer <redacted>",
        text,
    )
