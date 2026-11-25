"""Versioned Hours Off rules shared by Hub and its authoritative worker.

This module performs no network requests and imports no Qt bindings.
Flow's signed production duration and artist day coverage are deliberately
separate. Callers must retain raw rows for project accounting.
"""

from __future__ import annotations

import calendar
import json
import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, List


VERSION = 1
LINK_FIELD = "sg_reedeemed_additional_timelogs"
ACCEPTED = {"ovract", "ovrfin", "ovrcca"}
FIELDS = [
    "id", "date", "created_at", "created_by", "updated_at", "updated_by", "user",
    "project", "entity", "duration", "sg_meta_duration", "description",
    "sg_day_rate_multilpier", "sg_overtime_info", "sg_overtime_status",
    "sg_payback_type", "sg_metadata", LINK_FIELD,
]


def metadata(row: dict) -> dict:
    """Copy metadata without discarding non-JSON legacy text on writes."""
    value = row.get("sg_metadata")
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        result = json.loads(value)
        if isinstance(result, dict):
            return result
    except (TypeError, ValueError):
        pass
    return {"previous_metadata_text": value}


def info(row: dict) -> dict:
    """Return recognized accounting metadata, or an empty mapping."""
    value = metadata(row).get("hours_off")
    if not isinstance(value, dict) or value.get("version") != VERSION:
        return {}
    return dict(value)


def patch_metadata(row: dict, value: dict) -> str:
    """Serialize an Hours Off patch while preserving other metadata."""
    result = metadata(row)
    result["hours_off"] = {"version": VERSION, **value}
    return json.dumps(result, sort_keys=True, default=str)


def day(value: Any) -> date:
    """Normalize a Flow date or raise a readable validation error."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("A valid Hours Off work date is required.") from exc


def month_start(value: date, offset: int = 0) -> date:
    """Move to the first day of a relative month, including year rollover."""
    index = value.year * 12 + value.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def expires_on(value: Any) -> date:
    """Return the last usable day after six complete following months."""
    last = month_start(day(value), 6)
    return last.replace(day=calendar.monthrange(last.year, last.month)[1])


def validate_usage(value: Any, today: date) -> date:
    """Enforce the inclusive Warsaw submission window and block the future."""
    value = day(value)
    earliest = month_start(today, -1 if today.day <= 5 else 0)
    if not earliest <= value <= today:
        raise ValueError(
            "Hours Off is allowed this month, or last month through the "
            "5th (Europe/Warsaw). Future dates are unavailable.")
    return value


def minutes(row: dict) -> int:
    """Read exact artist minutes, retaining legacy production-only rows."""
    raw = row.get("sg_meta_duration")
    if raw is None:
        raw = row.get("duration") or 0
    try:
        number = float(raw)
        if not math.isfinite(number) or int(number) != number:
            raise ValueError()
        return int(number)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("TimeLog has invalid whole-minute duration.") from exc


def is_accounting(row: dict) -> bool:
    """Identify generated records, including grouped Hub display rows."""
    return info(row).get("role") in {"request", "deduction", "display"}


def is_ad(row: dict) -> bool:
    """Identify earned AD records, excluding accounting control rows."""
    return (not is_accounting(row) and row.get("sg_overtime_info")
            not in (None, "", "None"))


def trusted_deduction(row: dict) -> bool:
    """Require immutable API creator/editor provenance before flipping signs.

    The coordinator additionally checks its exact configured ApiUser ID and
    permanent allocations. An artist-authored metadata label alone must never
    create positive coverage in a diary or missing-time report.
    """
    creator = row.get("created_by") or {}
    editor = row.get("updated_by") or {}
    return (info(row).get("role") == "deduction"
            and creator.get("type") == "ApiUser"
            and editor.get("type") == "ApiUser"
            and bool(creator.get("id"))
            and creator.get("id") == editor.get("id"))


def coverage(row: dict) -> int:
    """Return daily artist credit without changing signed project cost."""
    data = info(row)
    if data.get("role") == "display":
        return int(row.get("duration") or 0)
    if data.get("role") == "request":
        return 0
    if data.get("role") == "deduction":
        return (-minutes(row) if trusted_deduction(row)
                and data.get("state") == "active" else 0)
    return minutes(row)


def parse_negative_hours(value: str) -> int:
    """Parse negative decimal hours or -H:MM into positive whole minutes."""
    text = value.strip().replace("\u2212", "-")
    if not text.startswith("-"):
        raise ValueError("Enter a negative duration, for example -2:00.")
    try:
        if ":" in text:
            hours, remainder = text[1:].split(":")
            if not hours.isdigit() or not remainder.isdigit():
                raise ValueError()
            if len(remainder) != 2 or int(remainder) >= 60:
                raise ValueError()
            result = Decimal(hours) * 60 + Decimal(remainder)
        else:
            result = -Decimal(text.replace(",", ".")) * 60
        if not result.is_finite() or result <= 0 or result != int(result):
            raise ValueError()
        return int(result)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Use negative whole minutes, e.g. -2:30 or -2.5.") \
            from exc


def source_rows(rows: List[dict], usage: date) -> List[dict]:
    """Build unbounded FIFO credit lots and verify linked deductions.

    Args:
        rows: Complete raw artist TimeLogs, including all linked deductions.
        usage: Day off; expiry follows this date rather than submission.

    Raises:
        ValueError: On dangling links or inconsistent/overdrawn allocations.
    """
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError("Hours Off history contains duplicate TimeLog IDs.")
    result = []
    for row in rows:
        if (not is_ad(row) or row.get("sg_payback_type") != "Hours Off"
                or row.get("sg_overtime_status") not in ACCEPTED
                or minutes(row) <= 0 or (row.get("duration") or 0) <= 0):
            continue
        earned = minutes(row)
        used = 0
        production_used = 0
        for link in row.get(LINK_FIELD) or []:
            child = by_id.get(link["id"])
            data = info(child or {})
            if (child is None or not trusted_deduction(child)
                    or data.get("source_id") != row["id"]
                    or child.get("user") != row.get("user")):
                # Entity names can differ across different API projections.
                if (child is None or not trusted_deduction(child)
                        or data.get("source_id") != row["id"]
                        or (child.get("user") or {}).get("id")
                        != (row.get("user") or {}).get("id")):
                    raise ValueError("Unverified Hours Off source links.")
            if data.get("state") == "active":
                if minutes(child) >= 0 or (child.get("duration") or 0) > 0:
                    raise ValueError("Hours Off deduction was modified.")
                used -= minutes(child)
                production_used -= int(child.get("duration") or 0)
            elif data.get("state") not in {"cancelled", "prepared"}:
                raise ValueError("Unrecognized Hours Off deduction state.")
        if used > earned or production_used > int(row["duration"]):
            raise ValueError("Hours Off source is overdrawn.")
        result.append({
            "source": row, "earned": earned, "used": used,
            "production_used": production_used, "remaining": earned - used,
            "expires_on": expires_on(row["date"]).isoformat(),
            "eligible": month_start(usage, -6) <= day(row["date"]) <= usage,
        })
    return sorted(result, key=lambda lot: (
        day(lot["source"]["date"]),
        str(lot["source"].get("created_at") or ""), lot["source"]["id"]))


def allocate(lots: List[dict], amount: int) -> List[dict]:
    """Allocate integer minutes FIFO with conservative cumulative rounding."""
    if type(amount) is not int or amount <= 0:
        raise ValueError("Hours Off must contain positive whole minutes.")
    remaining = amount
    result = []
    for lot in lots:
        if not lot["eligible"] or lot["remaining"] <= 0:
            continue
        take = min(remaining, lot["remaining"])
        production = int(lot["source"]["duration"])
        # A cancelled older allocation can leave non-contiguous rounded parts.
        # Split the *remaining* production minutes; exhaustion is always exact.
        production_left = production - lot["production_used"]
        debit = (production_left if take == lot["remaining"] else
                 round(Fraction(production_left * take, lot["remaining"])))
        result.append({"source_id": lot["source"]["id"],
                       "minutes": take, "production_minutes": debit})
        remaining -= take
        if not remaining:
            return result
    raise ValueError("Not enough available Hours Off for this date.")


def ordinary_minutes(rows: List[dict], usage: date) -> int:
    """Sum ordinary worked time for the daily eight-hour allowance."""
    return sum(max(0, minutes(row)) for row in rows
               if day(row["date"]) == usage
               and not is_accounting(row) and not is_ad(row))


def available_for_day(rows: List[dict], usage: date) -> int:
    """Calculate the smaller of FIFO balance and remaining daily allowance."""
    balance = sum(lot["remaining"] for lot in source_rows(rows, usage)
                  if lot["eligible"])
    used = sum(coverage(row) for row in rows
               if day(row["date"]) == usage and is_accounting(row))
    return max(0, min(balance, 480 - ordinary_minutes(rows, usage) - used))


def daily_rows(rows: List[dict]) -> List[dict]:
    """Group completed deductions into one positive display row per usage."""
    result = []
    groups = {}
    for row in rows:
        data = info(row)
        if not is_accounting(row):
            result.append(row)
        elif data.get("role") == "deduction" and coverage(row) > 0:
            key = data["operation_id"]
            if key not in groups:
                groups[key] = {
                    **row, "id": "hours-off:" + key, "duration": 0,
                    "sg_meta_duration": None, "link": {},
                    "entity": {"type": "Task", "id": 0, "name": "Hours Off"},
                    "project": {"type": "Project", "id": 0,
                                "name": "Hours Off"},
                    "description": "Hours Off used",
                    "sg_metadata": patch_metadata({}, {
                        **data, "role": "display"}),
                }
            groups[key]["duration"] += coverage(row)
    return result + list(groups.values())
