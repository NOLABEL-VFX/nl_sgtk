"""Flow-backed Hours Off overlay API.

Source TimeLogs keep their original duration. Overlay allocations have zero
duration and carry positive redeemed minutes in the configured Flow field.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from . import hours_off as rules


REDEEMED_VALUE_FIELD = "sg_reedeemed_value"
REDEEMED_LINK_FIELD = "sg_reedeemed_additional_timelogs"
REDEEM_ID_FIELD = "sg_reedeemed_id"
METADATA_KEY = "redeem_timelog"


def _local_now() -> datetime:
    """Return local wall time; production machines use Europe/Warsaw."""
    return datetime.now().astimezone()


FIELDS = list(dict.fromkeys(rules.FIELDS + [
    REDEEMED_VALUE_FIELD, REDEEM_ID_FIELD,
]))
ROLE_ALLOCATION = "allocation"
STATE_PREPARED = "prepared"
STATE_APPLIED = "applied"
STATE_CANCELLED = "cancelled"


class BalanceChanged(ValueError):
    """Signal that the user's preview no longer matches Flow."""

    def __init__(self, snapshot: Dict[str, Any]) -> None:
        self.snapshot = snapshot
        super().__init__("Hours Off balance changed; review the refreshed preview.")


class ReconciliationRequired(RuntimeError):
    """Keep an incomplete or uncertain Flow operation for manual review."""


def _user_id(row: Dict[str, Any]) -> Optional[int]:
    user = row.get("user") or {}
    return user.get("id") if user.get("type") == "HumanUser" else None


def _redeem_data(row: Dict[str, Any]) -> Dict[str, Any]:
    value = rules.metadata(row).get(METADATA_KEY)
    return dict(value) if isinstance(value, dict) else {}


def _patch_row(row: Dict[str, Any], **changes: Any) -> str:
    metadata = rules.metadata(row)
    metadata[METADATA_KEY] = {**_redeem_data(row), **changes, "version": 1}
    return json.dumps(metadata, sort_keys=True, default=str)


def _row_value(row: Dict[str, Any]) -> int:
    """Read a nonnegative whole-minute overlay value."""
    value = row.get(REDEEMED_VALUE_FIELD)
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise ValueError("Redeemed values must be nonnegative whole minutes.")
    return value


def _eligible_source(row: Dict[str, Any]) -> bool:
    return (
        not rules.is_accounting(row)
        and row.get("sg_payback_type") == "Hours Off"
        and row.get("sg_overtime_status") in rules.ACCEPTED
        and row.get("sg_overtime_info") not in (None, "", "None")
        and type(row.get("duration")) is int
        and row["duration"] > 0
    )


def source_rows(rows: List[Dict[str, Any]], usage_date: Any) -> List[Dict[str, Any]]:
    """Return validated FIFO credit sources with remaining values.

    Missing source balances mean the original duration, as agreed for opening
    balances. Once allocation links exist, their applied values must reconcile
    exactly with the stored remaining balance.
    """
    usage = rules.day(usage_date)
    by_id = {row.get("id"): row for row in rows}
    if None in by_id or len(by_id) != len(rows):
        raise ValueError("TimeLog history contains missing or duplicate IDs.")

    linked_ids = set()
    sessions: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        info = _redeem_data(row)
        redeem_id = row.get(REDEEM_ID_FIELD)
        if info.get("role") == ROLE_ALLOCATION:
            if not isinstance(redeem_id, str) or not redeem_id:
                raise ReconciliationRequired(
                    "An Hours Off allocation has no redemption UUID.")
            sessions.setdefault(redeem_id, []).append(row)
    for redeem_id, children in sessions.items():
        infos = [_redeem_data(child) for child in children]
        states = {info.get("state") for info in infos}
        requested = {info.get("requested_minutes") for info in infos}
        dates = {child.get("date") for child in children}
        tasks = {(child.get("entity") or {}).get("id") for child in children}
        projects = {(child.get("project") or {}).get("id") for child in children}
        task_types = {(child.get("entity") or {}).get("type")
                      for child in children}
        project_types = {(child.get("project") or {}).get("type")
                         for child in children}
        users = {_user_id(child) for child in children}
        sources = {info.get("source_id") for info in infos}
        session_ids = {info.get("redeem_id") for info in infos}
        if (len(requested) != 1 or type(next(iter(requested))) is not int
                or next(iter(requested)) <= 0 or len(dates) != 1
                or not isinstance(next(iter(dates)), str)
                or len(tasks) != 1 or None in tasks or len(projects) != 1
                or None in projects or task_types != {"Task"}
                or project_types != {"Project"}
                or len(users) != 1 or None in users
                or len(sources) != len(children)
                or session_ids != {redeem_id}
                or states not in ({STATE_APPLIED}, {STATE_CANCELLED})):
            raise ReconciliationRequired(
                "Hours Off redemption session metadata does not reconcile.")
        if states == {STATE_APPLIED}:
            applied = sum(_row_value(child) for child in children)
            if applied != next(iter(requested)):
                raise ReconciliationRequired(
                    "Hours Off redemption session total does not reconcile.")
        elif any(_row_value(child) != 0 for child in children):
            raise ReconciliationRequired(
                "Cancelled Hours Off allocations must have zero value.")

    for source in rows:
        for link in source.get(REDEEMED_LINK_FIELD) or []:
            if isinstance(link, dict) and link.get("type") == "TimeLog":
                linked_ids.add(link.get("id"))
    for row in rows:
        info = _redeem_data(row)
        if info.get("role") != ROLE_ALLOCATION:
            continue
        if info.get("state") == STATE_PREPARED:
            raise ReconciliationRequired(
                "An unfinished Hours Off operation needs reconciliation.")
        if (info.get("state") in {STATE_APPLIED, STATE_CANCELLED}
                and row.get("id") not in linked_ids):
            raise ReconciliationRequired(
                "An Hours Off allocation is missing its source link.")
    for row in rows:
        info = _redeem_data(row)
        if info.get("role") != ROLE_ALLOCATION:
            continue
        source = by_id.get(info.get("source_id"))
        if (source is None or _user_id(source) != _user_id(row)
                or info.get("user_id") != _user_id(row)
                or row.get(REDEEM_ID_FIELD) != info.get("redeem_id")
                or not row.get(REDEEM_ID_FIELD)):
            raise ReconciliationRequired(
                "An Hours Off allocation has unverifiable ownership or source.")

    result = []
    for source in rows:
        if not _eligible_source(source):
            continue
        owner = _user_id(source)
        if not owner:
            raise ValueError("Hours Off source has no HumanUser owner.")
        earned = int(source["duration"])
        allocations = []
        used = 0
        for link in source.get(REDEEMED_LINK_FIELD) or []:
            if not isinstance(link, dict) or link.get("type") != "TimeLog":
                raise ValueError("Hours Off source has a malformed allocation link.")
            child = by_id.get(link.get("id"))
            info = _redeem_data(child or {})
            if (child is None or info.get("role") != ROLE_ALLOCATION
                    or info.get("source_id") != source["id"]
                    or _user_id(child) != owner
                    or child.get("duration") != 0):
                raise ValueError("Hours Off allocation link cannot be verified.")
            state = info.get("state")
            amount = _row_value(child)
            if ((state == STATE_APPLIED
                 and info.get("allocated_minutes") != amount)
                    or (state == STATE_CANCELLED
                        and info.get("allocated_minutes") <= 0)
                    or info.get("user_id") != owner
                    or info.get("usage_date") != child.get("date")
                    or child.get(REDEEM_ID_FIELD) != _redeem_data(child).get("redeem_id")
                    or child.get(REDEEM_ID_FIELD) is None):
                raise ReconciliationRequired(
                    "Hours Off allocation value or ownership changed.")
            if state == STATE_APPLIED:
                if amount <= 0:
                    raise ValueError("Applied Hours Off allocation has no value.")
                used += amount
            elif state == STATE_CANCELLED:
                if amount != 0:
                    raise ValueError("Cancelled allocation must have zero value.")
            elif state == STATE_PREPARED:
                raise ReconciliationRequired(
                    "An unfinished Hours Off operation needs reconciliation.")
            else:
                raise ValueError("Hours Off allocation has an unknown state.")
            allocations.append(child)

        stored = source.get(REDEEMED_VALUE_FIELD)
        remaining = earned if stored is None else _row_value(source)
        if remaining != earned - used:
            raise ReconciliationRequired(
                "Hours Off source balance does not match its allocations.")
        expiry = rules.expires_on(source["date"])
        result.append({
            "source": source,
            "earned": earned,
            "used": used,
            "remaining": remaining,
            "expires_on": expiry.isoformat(),
            "eligible": rules.day(source["date"]) <= usage <= expiry,
            "allocations": allocations,
        })
    return sorted(result, key=lambda item: (
        rules.day(item["source"]["date"]),
        str(item["source"].get("created_at") or ""),
        item["source"]["id"],
    ))


def allocate(lots: List[Dict[str, Any]], amount: int) -> List[Dict[str, int]]:
    """Allocate positive whole minutes over eligible sources, oldest first."""
    if type(amount) is not int or amount <= 0:
        raise ValueError("Enter a positive whole-minute redemption amount.")
    remaining = amount
    allocations = []
    for lot in lots:
        if not lot["eligible"] or lot["remaining"] <= 0:
            continue
        take = min(remaining, lot["remaining"])
        allocations.append({"source_id": lot["source"]["id"], "minutes": take})
        remaining -= take
        if remaining == 0:
            return allocations
    raise ValueError("Not enough unexpired Hours Off balance.")


def _fingerprint(lots: List[Dict[str, Any]], usage: date) -> str:
    payload = {
        "usage_date": usage.isoformat(),
        "lots": [{
            "id": lot["source"]["id"],
            "date": str(lot["source"].get("date")),
            "earned": lot["earned"],
            "remaining": lot["remaining"],
            "status": lot["source"].get("sg_overtime_status"),
            "payback": lot["source"].get("sg_payback_type"),
            "links": sorted(link["id"] for link in
                             lot["source"].get(REDEEMED_LINK_FIELD) or []),
        } for lot in lots],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _fingerprint_all(rows: List[Dict[str, Any]], usage: date) -> str:
    """Include current-day ordinary logs in the pre-submit safety check."""
    payload = {
        "sources": sorted(
            (row.get("id"), row.get("date"), row.get("duration"),
             row.get(REDEEMED_VALUE_FIELD), row.get("sg_overtime_status"),
             row.get("sg_payback_type"), sorted(
                 link.get("id") for link in row.get(REDEEMED_LINK_FIELD) or []
                 if isinstance(link, dict)))
            for row in rows if _eligible_source(row)
            and rules.day(row["date"]) <= usage
            <= rules.expires_on(row["date"])
        ),
        "ordinary_today": sorted(
            (row.get("id"), row.get("duration"), row.get("date"),
             row.get("sg_meta_duration"))
            for row in rows
            if rules.day(row["date"]) == usage and not rules.is_ad(row)
            and not _redeem_data(row)
        ),
        "active_overlay_today": sorted(
            (row.get("id"), _row_value(row))
            for row in rows if rules.day(row["date"]) == usage
            and _redeem_data(row).get("role") == ROLE_ALLOCATION
            and _redeem_data(row).get("state") == STATE_APPLIED
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def fetch_user_timelogs(sg: Any, user_id: int) -> List[Dict[str, Any]]:
    """Read the complete TimeLog history visible to the supplied Flow session."""
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("A positive HumanUser ID is required.")
    return sg.find("TimeLog", [["user", "is", {
        "type": "HumanUser", "id": user_id,
    }]], FIELDS)


def snapshot(sg: Any, user_id: int, usage_date: Any,
             today: Optional[date] = None) -> Dict[str, Any]:
    """Read and validate a user's available Hours Off for one date."""
    from .hours_off import validate_usage

    as_of = _local_now().date()
    if today is not None and today != as_of:
        raise ValueError("Hours Off snapshots use today's local date.")
    usage = validate_usage(usage_date, as_of)
    if usage != as_of:
        raise ValueError("Hours Off can only be redeemed for today.")
    rows = fetch_user_timelogs(sg, user_id)
    if any(_user_id(row) not in (None, user_id) for row in rows):
        raise ValueError("Flow returned a TimeLog owned by another user.")
    lots = []
    reconciliation_error = None
    try:
        lots = source_rows(rows, usage)
    except ReconciliationRequired as exc:
        reconciliation_error = str(exc)
        for source in rows:
            if _eligible_source(source):
                earned = int(source["duration"])
                stored = source.get(REDEEMED_VALUE_FIELD)
                remaining = earned if stored is None else _row_value(source)
                expiry = rules.expires_on(source["date"])
                lots.append({
                    "source": source, "earned": earned,
                    "used": earned - remaining, "remaining": remaining,
                    "expires_on": expiry.isoformat(),
                    "eligible": rules.day(source["date"]) <= usage <= expiry,
                    "allocations": [],
                })
    eligible = [lot for lot in lots if lot["eligible"]]
    balance = sum(lot["remaining"] for lot in eligible)
    ordinary_minutes = rules.ordinary_minutes(rows, usage)
    overlay_minutes = sum(
        _row_value(row) for row in rows
        if rules.day(row["date"]) == usage
        and _redeem_data(row).get("role") == ROLE_ALLOCATION
        and _redeem_data(row).get("state") == STATE_APPLIED
    )
    daily_capacity = max(0, 480 - ordinary_minutes - overlay_minutes)
    return {
        "user_id": user_id,
        "usage_date": usage.isoformat(),
        "as_of_date": as_of.isoformat(),
        "rows": rows,
        "lots": lots,
        "balance": balance,
        "daily_capacity": daily_capacity,
        "redeemed_today": overlay_minutes,
        "reconciliation_required": reconciliation_error,
        "fingerprint": _fingerprint_all(rows, usage),
    }


def _batch_update(row_id: int, data: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    return {"request_type": "update", "entity_type": "TimeLog",
            "entity_id": row_id, "data": data, **extra}


def _assert_target_context(sg: Any, task: Dict[str, Any],
                           project: Dict[str, Any]) -> None:
    """Reject task/project mismatches before allocating employee credit."""
    if not hasattr(sg, "find_one"):
        return
    resolved = sg.find_one("Task", [["id", "is", task["id"]]], ["project"])
    if not resolved or (resolved.get("project") or {}).get("id") != project["id"]:
        raise ValueError("The target Task does not belong to the selected Project.")


def redeem(sg: Any, *, user: Dict[str, Any], usage_date: Any, amount: int,
           target_task: Dict[str, Any], target_project: Dict[str, Any],
           expected_fingerprint: str, redeem_id: str) -> Dict[str, Any]:
    """Create zero-duration overlay allocations directly in Flow.

    Existing UUID rows are returned only when they describe the same user,
    date, amount and target context. Source balance changes, links and applied
    allocation values commit in one Flow batch after prepared rows exist.
    """
    from .hours_off import validate_usage

    user_id = user.get("id")
    if type(user_id) is not int or user.get("type") != "HumanUser":
        raise ValueError("Redeeming Hours Off requires the authenticated HumanUser.")
    try:
        canonical_id = str(UUID(redeem_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("A canonical redemption UUID is required.") from exc
    if canonical_id != redeem_id:
        raise ValueError("Redemption UUID must use canonical lowercase form.")
    usage = validate_usage(usage_date,
                           _local_now().date())
    if usage != _local_now().date():
        raise ValueError("Hours Off can only be redeemed for today.")
    if (target_task.get("type") != "Task" or not target_task.get("id")
            or target_project.get("type") != "Project"
            or not target_project.get("id")):
        raise ValueError("A valid target Task and Project are required.")
    _assert_target_context(sg, target_task, target_project)

    rows = fetch_user_timelogs(sg, user_id)
    existing = [row for row in rows if row.get(REDEEM_ID_FIELD) == canonical_id]
    if existing:
        infos = [_redeem_data(row) for row in existing]
        actual_minutes = sum(_row_value(row) for row in existing)
        if any(info.get("role") != ROLE_ALLOCATION
               or info.get("user_id") != user_id
               or info.get("usage_date") != usage.isoformat()
               or (row.get("entity") or {}).get("id") != target_task["id"]
               or (row.get("project") or {}).get("id") != target_project["id"]
               for row, info in zip(existing, infos)):
            raise ValueError("This redemption UUID already belongs to another request.")
        requested_values = {info.get("requested_minutes") for info in infos}
        if (len(requested_values) != 1
                or next(iter(requested_values)) != amount):
            raise ValueError("This redemption UUID already belongs to another request.")
        if all(info.get("state") == STATE_APPLIED for info in infos):
            if (actual_minutes != amount
                    or any(row.get("duration") != 0 for row in existing)):
                raise ReconciliationRequired(
                    "Existing redemption UUID has inconsistent values.")
            source_rows(rows, usage)
            return {"redeem_id": canonical_id, "timelogs": existing,
                    "replayed": True}
        if any(info.get("state") != STATE_PREPARED for info in infos):
            raise ReconciliationRequired("This redemption needs reconciliation.")

    prepared = [row for row in rows
                if _redeem_data(row).get("state") == STATE_PREPARED]
    if prepared:
        if not existing or any(row.get(REDEEM_ID_FIELD) != canonical_id
                               for row in prepared):
            raise ReconciliationRequired(
                "An unfinished Hours Off operation needs reconciliation.")
        requested = {info.get("requested_minutes") for info in infos}
        if len(requested) != 1 or next(iter(requested)) != amount:
            raise ReconciliationRequired(
                "Prepared redemption UUID has inconsistent request values.")
        if any(info.get("state") not in {STATE_PREPARED, STATE_APPLIED}
               for info in infos):
            raise ReconciliationRequired(
                "Prepared redemption UUID contains an invalid state.")

    if existing and any(info.get("state") == STATE_APPLIED for info in infos):
        raise ReconciliationRequired(
            "Redemption UUID is only partly committed; reconciliation is required.")

    if existing:
        parts = [{"source_id": info.get("source_id"),
                  "minutes": info.get("allocated_minutes")}
                 for info in infos]
        if (any(type(part["minutes"]) is not int or part["minutes"] <= 0
                for part in parts)
                or len({part["source_id"] for part in parts}) != len(parts)
                or sum(part["minutes"] for part in parts) != amount):
            raise ReconciliationRequired(
                "Prepared redemption UUID has inconsistent allocation values.")
        if any(row.get("duration") != 0
               or row.get(REDEEMED_VALUE_FIELD) != 0
               or row.get(REDEEMED_LINK_FIELD)
               for row in existing):
            raise ReconciliationRequired(
                "Prepared allocation rows were modified; reconcile manually.")
        lots = source_rows(rows, usage)
        lot_by_id = {lot["source"]["id"]: lot for lot in lots
                     if lot["eligible"]}
        if any(part["source_id"] not in lot_by_id for part in parts):
            raise ReconciliationRequired(
                "A prepared redemption source is missing or expired.")
        for part in parts:
            lot = lot_by_id[part["source_id"]]
            child = next(row for row in existing
                         if _redeem_data(row).get("source_id")
                         == part["source_id"])
            links = lot["source"].get(REDEEMED_LINK_FIELD) or []
            if any(link.get("id") == child["id"]
                   for link in links if isinstance(link, dict)):
                raise ReconciliationRequired(
                    "A prepared allocation is already linked; reconcile before retry.")
            if part["minutes"] > lot["remaining"]:
                raise ReconciliationRequired(
                    "Prepared allocation exceeds the current source balance.")
            source_links = lot["source"].get(REDEEMED_LINK_FIELD) or []
            if any(link.get("id") == child["id"]
                   for link in source_links if isinstance(link, dict)):
                raise ReconciliationRequired(
                    "A prepared allocation is already linked; reconcile before retry.")
        fingerprint = _fingerprint_all(rows, usage)
        if fingerprint != expected_fingerprint:
            raise BalanceChanged(snapshot(sg, user_id, usage))
        eligible = list(lot_by_id.values())
        source_by_id = lot_by_id
    else:
        current_fingerprint = _fingerprint_all(rows, usage)
        if current_fingerprint != expected_fingerprint:
            raise BalanceChanged(snapshot(sg, user_id, usage))
        lots = source_rows(rows, usage)
        eligible = [lot for lot in lots if lot["eligible"]]
        daily_capacity = rules.available_for_day(rows, usage)
        if amount > daily_capacity:
            if any(_redeem_data(row).get("role") == ROLE_ALLOCATION
                   and _redeem_data(row).get("state") == STATE_APPLIED
                   for row in rows):
                raise BalanceChanged(snapshot(sg, user_id, usage))
            raise ValueError("The request exceeds today's available time.")
        parts = allocate(eligible, amount)
        source_by_id = {lot["source"]["id"]: lot for lot in eligible}
    for part in parts:
        source = source_by_id[part["source_id"]]["source"]
        if ((source.get("project") or {}).get("id") is None
                or (source.get("entity") or {}).get("id") is None):
            raise ValueError("A source TimeLog is missing its project or task.")

    daily_capacity = rules.available_for_day(rows, usage)
    if amount > daily_capacity:
        raise ValueError("The request exceeds today's available time.")

    if not existing:
        create_calls = []
        for part in parts:
            source = source_by_id[part["source_id"]]["source"]
            child_metadata = _patch_row({},
                role=ROLE_ALLOCATION,
                state=STATE_PREPARED,
                user_id=user_id,
                source_id=source["id"],
                requested_minutes=amount,
                allocated_minutes=part["minutes"],
                usage_date=usage.isoformat(),
                redeem_id=canonical_id,
            )
            data = {
                "user": {"type": "HumanUser", "id": user_id},
                "project": target_project,
                "entity": target_task,
                "date": usage.isoformat(),
                "duration": 0,
                "sg_meta_duration": 0,
                REDEEMED_VALUE_FIELD: 0,
                REDEEM_ID_FIELD: canonical_id,
                "sg_overtime_info": None,
                "sg_overtime_status": "na",
                "description": "Hours Off redemption overlay",
                "sg_metadata": child_metadata,
            }
            create_calls.append({"request_type": "create",
                                 "entity_type": "TimeLog", "data": data})
        created = sg.batch(create_calls)
        existing = [row for row in created if isinstance(row, dict) and row.get("id")]
        if len(existing) != len(parts):
            raise ReconciliationRequired(
                "Prepared allocations could not be read back; do not retry with a new UUID.")

    by_source = {_redeem_data(row).get("source_id"): row for row in existing}
    if (len(by_source) != len(parts)
            or set(by_source) != {part["source_id"] for part in parts}):
        raise ReconciliationRequired("Prepared allocation set is incomplete or duplicated.")

    commit_calls = []
    for part in parts:
        source_id = part["source_id"]
        lot = source_by_id[source_id]
        child = by_source.get(source_id)
        if child is None:
            raise ReconciliationRequired("A prepared source allocation is missing.")
        commit_calls.append(_batch_update(child["id"], {
            REDEEMED_VALUE_FIELD: part["minutes"],
            "sg_metadata": _patch_row(child, state=STATE_APPLIED),
        }))
        commit_calls.append({
            "request_type": "update",
            "entity_type": "TimeLog",
            "entity_id": source_id,
            "data": {
                REDEEMED_VALUE_FIELD: lot["remaining"] - part["minutes"],
                REDEEMED_LINK_FIELD: [{"type": "TimeLog", "id": child["id"]}],
            },
            "multi_entity_update_modes": {
                REDEEMED_LINK_FIELD: "add",
            },
        })
    try:
        sg.batch(commit_calls)
    except Exception as exc:
        try:
            after_error = fetch_user_timelogs(sg, user_id)
            expected_children = {row["id"] for row in existing}
            after_by_id = {row.get("id"): row for row in after_error}
            related = [row for row in after_error
                       if row.get(REDEEM_ID_FIELD) == canonical_id]
            if (expected_children <= set(after_by_id)
                    and len(related) == len(expected_children)
                    and all(_redeem_data(after_by_id[row_id]).get("state")
                            == STATE_APPLIED
                            and _row_value(after_by_id[row_id])
                            == _redeem_data(after_by_id[row_id]).get(
                                "allocated_minutes")
                            for row_id in expected_children)):
                try:
                    source_rows(after_error, usage)
                except ReconciliationRequired as audit_error:
                    raise ReconciliationRequired(
                        "Flow returned an ambiguous complete write; review the existing UUID.") from audit_error
        except ReconciliationRequired:
            raise
        except Exception:
            pass
        raise ReconciliationRequired(
            "Redemption commit outcome is uncertain; refresh before retrying with the same UUID.") from exc
    persisted = fetch_user_timelogs(sg, user_id)
    by_id = {row["id"]: row for row in persisted}
    if any(row_id not in by_id for row_id in (item["id"] for item in existing)):
        raise ReconciliationRequired("Committed allocation readback is incomplete.")
    try:
        source_rows(persisted, usage)
    except (ValueError, ReconciliationRequired) as exc:
        raise ReconciliationRequired(
            "Committed Hours Off values failed readback reconciliation.") from exc
    return {"redeem_id": canonical_id,
            "timelogs": [by_id[row["id"]] for row in existing],
            "replayed": False}


def _created_local_date(row: Dict[str, Any]) -> date:
    value = row.get("created_at")
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ReconciliationRequired("Redemption creation date is unavailable.")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().date()


def withdraw(sg: Any, *, user: Dict[str, Any], redeem_id: str,
             today: Optional[date] = None) -> Dict[str, Any]:
    """Withdraw a whole redemption session on its creation day only."""
    user_id = user.get("id")
    if type(user_id) is not int or user.get("type") != "HumanUser":
        raise ValueError("Withdrawing Hours Off requires the authenticated HumanUser.")
    try:
        canonical_id = str(UUID(redeem_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("A valid redemption UUID is required.") from exc
    rows = fetch_user_timelogs(sg, user_id)
    session = [row for row in rows if row.get(REDEEM_ID_FIELD) == canonical_id]
    if not session:
        raise ValueError("Redemption session was not found for this user.")
    if any(_user_id(row) != user_id for row in session):
        raise ValueError("Redemption session contains another user's TimeLog.")
    current = today or _local_now().date()
    creation_dates = {_created_local_date(row) for row in session}
    if creation_dates != {current}:
        raise ValueError("A redemption can only be withdrawn on its creation day.")
    infos = [_redeem_data(row) for row in session]
    if any(info.get("role") != ROLE_ALLOCATION for info in infos):
        raise ValueError("Redemption session contains an unrecognized TimeLog.")
    if all(info.get("state") == STATE_CANCELLED for info in infos):
        return {"redeem_id": canonical_id, "withdrawn": True, "replayed": True}
    if any(info.get("state") != STATE_APPLIED for info in infos):
        raise ReconciliationRequired("Only a complete redemption can be withdrawn.")
    requested = {info.get("requested_minutes") for info in infos}
    if (len(requested) != 1
            or sum(_row_value(row) for row in session) != next(iter(requested))):
        raise ReconciliationRequired("Redemption session values do not reconcile.")

    by_id = {row["id"]: row for row in rows}
    calls = []
    for child, info in zip(session, infos):
        amount = _row_value(child)
        source = by_id.get(info.get("source_id"))
        if source is None or not _eligible_source(source):
            raise ReconciliationRequired("An Hours Off source is missing or changed.")
        current_value = source.get(REDEEMED_VALUE_FIELD)
        remaining = source["duration"] if current_value is None else _row_value(source)
        linked_ids = {link.get("id") for link in
                      source.get(REDEEMED_LINK_FIELD) or [] if isinstance(link, dict)}
        if child["id"] not in linked_ids:
            raise ReconciliationRequired(
                "The Hours Off source does not link back to this allocation.")
        if remaining + amount > source["duration"]:
            raise ReconciliationRequired(
                "Withdrawal would restore more than the earned source balance.")
        source_links = list(source.get(REDEEMED_LINK_FIELD) or [])
        new_remaining = remaining + amount
        calls.append(_batch_update(child["id"], {
            REDEEMED_VALUE_FIELD: 0,
            "sg_metadata": _patch_row(child, state=STATE_CANCELLED),
        }))
        calls.append(_batch_update(source["id"], {
            REDEEMED_VALUE_FIELD: new_remaining,
            REDEEMED_LINK_FIELD: source_links,
        }))
    try:
        sg.batch(calls)
    except Exception as exc:
        raise ReconciliationRequired(
            "Withdrawal outcome is uncertain; refresh before retrying.") from exc
    refreshed = fetch_user_timelogs(sg, user_id)
    refreshed_by_id = {row["id"]: row for row in refreshed}
    if any(_redeem_data(refreshed_by_id.get(child["id"], {})).get("state")
           != STATE_CANCELLED for child in session):
        raise ReconciliationRequired("Withdrawal readback is incomplete.")
    try:
        source_rows(refreshed, current)
    except (ValueError, ReconciliationRequired) as exc:
        raise ReconciliationRequired(
            "Withdrawal readback failed source balance reconciliation.") from exc
    return {"redeem_id": canonical_id, "withdrawn": True, "replayed": False}


def withdrawable_sessions(sg: Any, user_id: int,
                          today: Optional[date] = None) -> List[Dict[str, Any]]:
    """List complete applied UUID sessions created today for one artist."""
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("A positive HumanUser ID is required.")
    current = today or _local_now().date()
    rows = fetch_user_timelogs(sg, user_id)
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        redeem_id = row.get(REDEEM_ID_FIELD)
        info = _redeem_data(row)
        if (isinstance(redeem_id, str) and info.get("role") == ROLE_ALLOCATION
                and info.get("state") == STATE_APPLIED):
            groups.setdefault(redeem_id, []).append(row)
    result = []
    for redeem_id, session in groups.items():
        if {_created_local_date(row) for row in session} != {current}:
            continue
        infos = [_redeem_data(row) for row in session]
        requested = {info.get("requested_minutes") for info in infos}
        amount = sum(_row_value(row) for row in session)
        if len(requested) != 1 or next(iter(requested)) != amount:
            raise ReconciliationRequired(
                "A same-day redemption session does not reconcile.")
        first = session[0]
        project = first.get("project") or {}
        entity = first.get("entity") or {}
        result.append({
            "redeem_id": redeem_id,
            "minutes": amount,
            "target": " / ".join(filter(None, [
                project.get("name"), entity.get("name")])),
        })
    return sorted(result, key=lambda item: item["redeem_id"])


def coverage(row: Dict[str, Any]) -> int:
    """Return daily coverage from applied overlay rows, excluding controls."""
    info = _redeem_data(row)
    if info.get("role") != ROLE_ALLOCATION or info.get("state") != STATE_APPLIED:
        return 0
    if (row.get("duration") != 0
            or type(info.get("source_id")) is not int
            or info.get("user_id") != _user_id(row)
            or not row.get(REDEEM_ID_FIELD)):
        return 0
    return _row_value(row)


def validated_coverage(rows: List[Dict[str, Any]], usage_date: Any) -> Dict[int, int]:
    """Return overlay coverage only after validating complete user history."""
    source_rows(rows, usage_date)
    return {row["id"]: coverage(row) for row in rows
            if type(row.get("id")) is int}
