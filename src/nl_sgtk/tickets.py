"""Create validated technical support Tickets in Flow Production Tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Optional, Sequence, Tuple, Union


DEFAULT_PROJECT_ID = 750
DEFAULT_PROJECT_NAME = "00_IN_HOUSE"
PIPELINE_GROUP_ID = 302
PIPELINE_GROUP_NAME = "Pipeline Development"
MAX_GROUP_ID = 1523
MAX_GROUP_NAME = "3DMax Development"
HOUDINI_GROUP_ID = 1524
HOUDINI_GROUP_NAME = "Houdini Development"
COMFY_GROUP_ID = 1525
COMFY_GROUP_NAME = "ComfyUI Development"
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


@dataclass(frozen=True)
class TicketResult:
    """Describe a verified Ticket and its successfully uploaded attachments."""

    ticket: Mapping[str, Any]
    attachment_ids: Tuple[int, ...] = ()
    attachment_paths: Tuple[str, ...] = ()

    @property
    def ticket_id(self) -> int:
        """Return the verified numeric Ticket identifier."""

        return int(self.ticket["id"])


Recipient = Union[PipelineGroup, Mapping[str, Any]]


def create_ticket(
    topic: str,
    content: str,
    *,
    ticket_type: TicketType = TicketType.BUG,
    priority: TicketPriority = TicketPriority.MEDIUM,
    user_group: Recipient = PipelineGroup.PIPELINE,
    attachments: Sequence[Union[str, os.PathLike[str]]] = (),
    metadata: Optional[Mapping[str, Any]] = None,
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
        attachments: Existing local files uploaded after Ticket creation.
        metadata: Structured diagnostic values rendered above ``content``.
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
        - Creates one ShotGrid Ticket and uploads zero or more attachments.

    Notes:
        - Attachment uploads are non-atomic. Never blindly retry after
          ``TicketAttachmentError``; use its ``ticket_id`` first.
        - Enum routes are validated against their expected ShotGrid Group ID
          and name before the Ticket is created.
    """

    normalized_topic = _require_text(topic, "topic", maximum=255)
    normalized_content = _require_text(content, "content")
    normalized_type = _require_enum(ticket_type, TicketType, "ticket_type")
    normalized_priority = _require_enum(priority, TicketPriority, "priority")
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
    payload = {
        "title": normalized_topic,
        "description": description,
        "project": project,
        "sg_ticket_type": normalized_type.value,
        "sg_priority": normalized_priority.value,
        "sg_status_list": DEFAULT_TICKET_STATUS,
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
            group_id, group_name = PIPELINE_GROUP_ID, PIPELINE_GROUP_NAME
        elif recipient is PipelineGroup.COMFY:
            group_id, group_name = COMFY_GROUP_ID, COMFY_GROUP_NAME
        elif recipient is PipelineGroup.MAX:
            group_id, group_name = MAX_GROUP_ID, MAX_GROUP_NAME
        elif recipient is PipelineGroup.HOUDINI:
            group_id, group_name = HOUDINI_GROUP_ID, HOUDINI_GROUP_NAME
        else:  # pragma: no cover - defensive against runtime enum mutation
            raise TicketRoutingError(f"Unsupported PipelineGroup: {recipient!r}.")
        return _require_named_entity(
            client,
            "Group",
            group_id,
            group_name,
            "code",
            TicketRoutingError,
        )
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
    try:
        schema = client.schema_field_read("Ticket")
    except Exception as exc:
        raise TicketSchemaError("Ticket schema could not be read before creation.") from exc
    for field in payload:
        definition = schema.get(field)
        if not definition:
            raise TicketSchemaError(f"Required Ticket field {field!r} is unavailable.")
        editable = definition.get("editable", {}).get("value", True)
        if editable is False:
            raise TicketSchemaError(f"Required Ticket field {field!r} is not editable.")
    for field in ("sg_ticket_type", "sg_priority", "sg_status_list"):
        valid_values = (
            schema[field].get("properties", {}).get("valid_values", {}).get("value")
        )
        if valid_values and payload[field] not in valid_values:
            raise TicketSchemaError(
                f"Ticket field {field!r} does not allow {payload[field]!r}."
            )


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
