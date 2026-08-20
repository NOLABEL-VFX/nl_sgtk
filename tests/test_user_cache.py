from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List

import pytest

from nl_sgtk.user_cache import update_user_cache


class FakeShotGrid:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def find_one(self, entity_type, filters, fields):
        self.calls.append(
            {"entity_type": entity_type, "filters": filters, "fields": fields}
        )
        return {
            "type": "HumanUser",
            "id": 42,
            "name": "Ada Artist",
            "login": "ada@example.com",
            "email": "ada@example.com",
            "sg_status_list": "act",
            "department": {"type": "Department", "id": 3, "name": "FX"},
            "permission_rule_set": {
                "type": "PermissionRuleSet",
                "id": 7,
                "name": "Artist",
            },
            "groups": [{"type": "Group", "id": 9, "name": "FX Leads"}],
            "projects": [{"type": "Project", "id": 11, "name": "Demo"}],
        }


def _user() -> Dict[str, Any]:
    return {
        "type": "HumanUser",
        "id": 42,
        "name": "Ada Artist",
        "_login": "ada",
    }


def _row(path: Path, table: str = "nl_sgtk_user_data") -> sqlite3.Row:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute("SELECT * FROM %s" % table).fetchone()
    finally:
        connection.close()


def test_user_cache_refreshes_profile_and_records_json(tmp_path: Path) -> None:
    path = tmp_path / "nl_core.sqlite3"
    sg = FakeShotGrid()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    update_user_cache(sg, _user(), base_url="https://sg", path=path, now=now)

    row = _row(path)
    payload = json.loads(row["payload"])
    update_info = json.loads(row["update_info"])
    assert payload["department"]["name"] == "FX"
    assert payload["permission_rule_set"]["name"] == "Artist"
    assert payload["_login"] == "ada"
    assert update_info["status"] == "updated"
    assert row["updated_at"] == "2026-01-01T00:00:00Z"
    assert row["last_accessed"] == "2026-01-01T00:00:00Z"
    assert len(sg.calls) == 1


def test_user_cache_only_refreshes_after_24_hours(tmp_path: Path) -> None:
    path = tmp_path / "nl_core.sqlite3"
    sg = FakeShotGrid()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    update_user_cache(sg, _user(), base_url="https://sg", path=path, now=now)

    update_user_cache(
        sg,
        _user(),
        base_url="https://sg",
        path=path,
        now=now + timedelta(hours=23),
    )
    assert len(sg.calls) == 1
    assert _row(path)["last_accessed"] == "2026-01-01T23:00:00Z"

    update_user_cache(
        sg,
        _user(),
        base_url="https://sg",
        path=path,
        now=now + timedelta(hours=25),
    )
    assert len(sg.calls) == 2
    assert _row(path)["updated_at"] == "2026-01-02T01:00:00Z"


def test_user_cache_archives_profiles_unused_for_90_days(tmp_path: Path) -> None:
    path = tmp_path / "nl_core.sqlite3"
    sg = FakeShotGrid()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    update_user_cache(sg, _user(), base_url="https://sg", path=path, now=now)

    other = {"type": "ApiUser", "id": None, "_login": "automation"}
    update_user_cache(
        sg,
        other,
        base_url="https://sg",
        path=path,
        now=now + timedelta(days=91),
    )

    active = _row(path)
    archived = _row(path, "nl_sgtk_user_data_old")
    assert active["login"] == "automation"
    assert archived["login"] == "ada"
    assert archived["archived_at"] == "2026-04-02T00:00:00Z"


def test_user_cache_never_persists_unexpected_login_secrets(tmp_path: Path) -> None:
    path = tmp_path / "nl_core.sqlite3"

    class UnexpectedFieldsShotGrid(FakeShotGrid):
        def find_one(self, entity_type, filters, fields):
            record = super().find_one(entity_type, filters, fields)
            record["session_token"] = "server-side-secret"
            return record

    user = dict(_user())
    user.update(
        {
            "session_token": "do-not-store",
            "script_key": "also-do-not-store",
            "credentials": {"password": "secret"},
        }
    )

    update_user_cache(
        UnexpectedFieldsShotGrid(),
        user,
        base_url="https://sg",
        path=path,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    serialized = _row(path)["payload"]
    assert "do-not-store" not in serialized
    assert "also-do-not-store" not in serialized
    assert "secret" not in serialized
    assert "server-side-secret" not in serialized


def test_failed_refresh_records_access_without_destroying_cached_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nl_core.sqlite3"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    update_user_cache(FakeShotGrid(), _user(), base_url="https://sg", path=path, now=now)
    original_payload = _row(path)["payload"]

    class FailingShotGrid:
        def find_one(self, entity_type, filters, fields):
            del entity_type, filters, fields
            raise RuntimeError("ShotGrid unavailable")

    failed_at = now + timedelta(days=2)
    with pytest.raises(RuntimeError, match="ShotGrid unavailable"):
        update_user_cache(
            FailingShotGrid(),
            _user(),
            base_url="https://sg",
            path=path,
            now=failed_at,
        )

    row = _row(path)
    update_info = json.loads(row["update_info"])
    assert row["payload"] == original_payload
    assert row["updated_at"] == "2026-01-01T00:00:00Z"
    assert row["last_accessed"] == "2026-01-03T00:00:00Z"
    assert row["refresh_started_at"] is None
    assert update_info["status"] == "failed"
    assert update_info["error_type"] == "RuntimeError"
    assert update_info["last_successful_at"] == "2026-01-01T00:00:00Z"
    assert "ShotGrid unavailable" not in row["update_info"]


def test_concurrent_logins_share_one_profile_refresh(tmp_path: Path) -> None:
    path = tmp_path / "nl_core.sqlite3"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class SlowShotGrid(FakeShotGrid):
        def find_one(self, entity_type, filters, fields):
            time.sleep(0.1)
            return super().find_one(entity_type, filters, fields)

    sg = SlowShotGrid()
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(
                update_user_cache,
                sg,
                _user(),
                base_url="https://sg",
                path=path,
                now=now,
            )
            for _ in range(8)
        ]
        for future in futures:
            future.result()

    assert len(sg.calls) == 1
    assert _row(path)["refresh_started_at"] is None


def test_existing_080_table_is_migrated_in_place(tmp_path: Path) -> None:
    path = tmp_path / "nl_core.sqlite3"
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            """
            CREATE TABLE nl_sgtk_user_data(
                identity_key TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                entity_id INTEGER,
                login TEXT NOT NULL,
                payload TEXT NOT NULL,
                update_info TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_accessed TEXT NOT NULL
            )
            """
        )

    update_user_cache(
        FakeShotGrid(),
        _user(),
        base_url="https://sg",
        path=path,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    with sqlite3.connect(str(path)) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(nl_sgtk_user_data)")
        }
    assert "refresh_started_at" in columns
    assert _row(path)["login"] == "ada"


def test_missing_identity_is_rejected_before_creating_database(tmp_path: Path) -> None:
    path = tmp_path / "nl_core.sqlite3"
    with pytest.raises(ValueError, match="id or login"):
        update_user_cache(
            FakeShotGrid(),
            {"type": "HumanUser"},
            base_url="https://sg",
            path=path,
        )
    assert not path.exists()
