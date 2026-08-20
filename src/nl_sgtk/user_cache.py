"""Persist non-secret ShotGrid user metadata in nl_core's local cache."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Dict, Mapping, Optional, Sequence


log = logging.getLogger(__name__)

REFRESH_INTERVAL = timedelta(hours=24)
ARCHIVE_AFTER = timedelta(days=90)
REFRESH_LEASE = timedelta(minutes=5)
_NEVER_REFRESHED = "1970-01-01T00:00:00Z"

USER_FIELDS: Sequence[str] = (
    "name",
    "login",
    "email",
    "sg_status_list",
    "department",
    "permission_rule_set",
    "groups",
    "projects",
)


def default_user_cache_path() -> Path:
    """Return the shared per-user nl_core SQLite path."""

    return (
        Path.home()
        / ".nolabel"
        / "local"
        / "nl_core"
        / "nl_core.sqlite3"
    )


def update_user_cache(
    sg: Any,
    user: Mapping[str, Any],
    *,
    base_url: str,
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> None:
    """Record access and refresh a user's non-secret profile when stale."""

    cache_path = path or default_user_cache_path()
    current = _as_utc(now or datetime.now(timezone.utc))
    current_text = _timestamp(current)
    identity_key = _identity_key(base_url, user)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    with _connect(cache_path) as connection:
        _initialize(connection)
        connection.execute("BEGIN IMMEDIATE")
        _archive_stale(connection, current, current_text)
        row = connection.execute(
            """
            SELECT updated_at, update_info, refresh_started_at
            FROM nl_sgtk_user_data
            WHERE identity_key = ?
            """,
            (identity_key,),
        ).fetchone()
        refresh = row is None or _is_stale(row["updated_at"], current)
        refresh_in_progress = (
            row is not None
            and _refresh_lease_active(row["refresh_started_at"], current)
        )
        if row is not None:
            connection.execute(
                """
                UPDATE nl_sgtk_user_data SET last_accessed = ?
                WHERE identity_key = ?
                """,
                (current_text, identity_key),
            )
        if not refresh or refresh_in_progress:
            return

        pending_info = {
            "last_attempted_at": current_text,
            "last_successful_at": _last_successful_at(row),
            "refresh_interval_hours": int(REFRESH_INTERVAL.total_seconds() / 3600),
            "source": "shotgrid" if user.get("type") == "HumanUser" else "login",
            "status": "refreshing",
        }
        if row is None:
            connection.execute(
                """
                INSERT INTO nl_sgtk_user_data(
                    identity_key, entity_type, entity_id, login, payload,
                    update_info, updated_at, last_accessed, refresh_started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity_key,
                    str(user.get("type") or "HumanUser"),
                    user.get("id"),
                    str(user.get("_login") or user.get("login") or ""),
                    json.dumps(_safe_login_payload(user), sort_keys=True, default=str),
                    json.dumps(pending_info, sort_keys=True),
                    _NEVER_REFRESHED,
                    current_text,
                    current_text,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE nl_sgtk_user_data
                SET refresh_started_at = ?, update_info = ?
                WHERE identity_key = ?
                """,
                (current_text, json.dumps(pending_info, sort_keys=True), identity_key),
            )

    try:
        payload = _fetch_user_payload(sg, user)
    except Exception as exc:
        failure_info = dict(pending_info)
        failure_info.update(
            {
                "error_type": type(exc).__name__,
                "status": "failed",
            }
        )
        with _connect(cache_path) as connection:
            _initialize(connection)
            connection.execute(
                """
                UPDATE nl_sgtk_user_data
                SET update_info = ?, last_accessed = ?, refresh_started_at = NULL
                WHERE identity_key = ?
                """,
                (json.dumps(failure_info, sort_keys=True), current_text, identity_key),
            )
        raise

    update_info = {
        "last_attempted_at": current_text,
        "last_successful_at": current_text,
        "refresh_interval_hours": int(REFRESH_INTERVAL.total_seconds() / 3600),
        "source": "shotgrid" if user.get("type") == "HumanUser" else "login",
        "status": "updated",
    }
    with _connect(cache_path) as connection:
        _initialize(connection)
        connection.execute(
            """
            INSERT INTO nl_sgtk_user_data(
                identity_key, entity_type, entity_id, login, payload,
                update_info, updated_at, last_accessed, refresh_started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(identity_key) DO UPDATE SET
                entity_type = excluded.entity_type,
                entity_id = excluded.entity_id,
                login = excluded.login,
                payload = excluded.payload,
                update_info = excluded.update_info,
                updated_at = excluded.updated_at,
                last_accessed = excluded.last_accessed,
                refresh_started_at = NULL
            """,
            (
                identity_key,
                str(user.get("type") or "HumanUser"),
                user.get("id"),
                str(user.get("_login") or user.get("login") or ""),
                json.dumps(payload, sort_keys=True, default=str),
                json.dumps(update_info, sort_keys=True),
                current_text,
                current_text,
            ),
        )
        connection.execute(
            "DELETE FROM nl_sgtk_user_data_old WHERE identity_key = ?",
            (identity_key,),
        )


def _fetch_user_payload(sg: Any, user: Mapping[str, Any]) -> Dict[str, Any]:
    payload = _safe_login_payload(user)
    if user.get("type") != "HumanUser" or not user.get("id"):
        return payload
    record = sg.find_one(
        "HumanUser",
        [["id", "is", int(user["id"])]],
        list(USER_FIELDS),
    )
    if record:
        allowed_record_fields = set(USER_FIELDS) | {"type", "id"}
        payload.update(
            {
                key: value
                for key, value in record.items()
                if key in allowed_record_fields
            }
        )
    login = payload.pop("login", None)
    if login and not payload.get("_login"):
        payload["_login"] = str(login).split("@", 1)[0]
    return payload


def _safe_login_payload(user: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy only non-secret identity fields supplied by the login flow."""

    allowed = ("type", "id", "name", "_login")
    return {key: user[key] for key in allowed if key in user}


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=10.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as exc:
            # Concurrent first-time connections can race while one changes the
            # journal mode. The winner completes WAL setup; this connection's
            # later transaction still observes busy_timeout and can continue.
            if "locked" not in str(exc).casefold():
                raise
        return connection
    except Exception:
        connection.close()
        raise


def _initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS nl_sgtk_user_data(
            identity_key TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            login TEXT NOT NULL,
            payload TEXT NOT NULL,
            update_info TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_accessed TEXT NOT NULL,
            refresh_started_at TEXT
        );
        CREATE INDEX IF NOT EXISTS nl_sgtk_user_data_last_accessed
        ON nl_sgtk_user_data(last_accessed);
        CREATE INDEX IF NOT EXISTS nl_sgtk_user_data_entity
        ON nl_sgtk_user_data(entity_type, entity_id);
        CREATE TABLE IF NOT EXISTS nl_sgtk_user_data_old(
            identity_key TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            login TEXT NOT NULL,
            payload TEXT NOT NULL,
            update_info TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_accessed TEXT NOT NULL,
            archived_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS nl_sgtk_user_data_old_last_accessed
        ON nl_sgtk_user_data_old(last_accessed);
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(nl_sgtk_user_data)")
    }
    if "refresh_started_at" not in columns:
        connection.execute(
            "ALTER TABLE nl_sgtk_user_data ADD COLUMN refresh_started_at TEXT"
        )


def _archive_stale(
    connection: sqlite3.Connection,
    now: datetime,
    archived_at: str,
) -> None:
    cutoff = _timestamp(now - ARCHIVE_AFTER)
    connection.execute(
        """
        INSERT INTO nl_sgtk_user_data_old(
            identity_key, entity_type, entity_id, login, payload,
            update_info, updated_at, last_accessed, archived_at
        )
        SELECT identity_key, entity_type, entity_id, login, payload,
               update_info, updated_at, last_accessed, ?
        FROM nl_sgtk_user_data WHERE last_accessed < ?
        ON CONFLICT(identity_key) DO UPDATE SET
            entity_type = excluded.entity_type,
            entity_id = excluded.entity_id,
            login = excluded.login,
            payload = excluded.payload,
            update_info = excluded.update_info,
            updated_at = excluded.updated_at,
            last_accessed = excluded.last_accessed,
            archived_at = excluded.archived_at
        """,
        (archived_at, cutoff),
    )
    connection.execute(
        "DELETE FROM nl_sgtk_user_data WHERE last_accessed < ?",
        (cutoff,),
    )


def _identity_key(base_url: str, user: Mapping[str, Any]) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url must be a non-empty string")
    entity_type = str(user.get("type") or "HumanUser")
    identity = user.get("id") or user.get("_login") or user.get("login")
    if identity is None or str(identity).strip() == "":
        raise ValueError("user must contain an id or login identity")
    return "%s|%s|%s" % (
        base_url.strip().rstrip("/").casefold(),
        entity_type,
        identity,
    )


def _is_stale(value: str, now: datetime) -> bool:
    try:
        updated = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    age = now - _as_utc(updated)
    return age < timedelta(0) or age >= REFRESH_INTERVAL


def _refresh_lease_active(value: Optional[str], now: datetime) -> bool:
    if not value:
        return False
    try:
        started = _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return False
    age = now - started
    return timedelta(0) <= age < REFRESH_LEASE


def _last_successful_at(row: Optional[sqlite3.Row]) -> Optional[str]:
    if row is None:
        return None
    try:
        value = json.loads(row["update_info"]).get("last_successful_at")
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, str) and value else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")
