"""Verify Flow Hours Off overlays and UUID-session lifecycle."""

from copy import deepcopy
from datetime import date, datetime

import pytest
from dateutil.tz import gettz

from nl_sgtk import redeem_timelog as api


USER = {"type": "HumanUser", "id": 12, "name": "Artist"}
TASK = {"type": "Task", "id": 80, "name": "Production"}
TARGET_PROJECT = {"type": "Project", "id": 90, "name": "00_IN_HOUSE"}


class FakeShotgun:
    """Apply small Flow batches atomically to an in-memory TimeLog table."""

    def __init__(self, rows):
        self.rows = {row["id"]: deepcopy(row) for row in rows}
        self.next_id = max(self.rows, default=0) + 1

    def find(self, entity, filters, fields):
        assert entity == "TimeLog"
        user_id = filters[0][2]["id"]
        return [deepcopy(row) for row in self.rows.values()
                if (row.get("user") or {}).get("id") == user_id]

    def find_one(self, entity, filters, fields):
        assert entity == "Task"
        task_id = filters[0][2]
        return {"id": task_id, "project": TARGET_PROJECT}

    def batch(self, calls):
        working = deepcopy(self.rows)
        result = []
        created = datetime.now(gettz("UTC")).isoformat()
        for call in calls:
            if call["request_type"] == "create":
                row_id = self.next_id
                self.next_id += 1
                row = {"id": row_id, "type": "TimeLog",
                       "created_at": created, "created_by": USER,
                       "updated_by": USER, **deepcopy(call["data"])}
                working[row_id] = row
                result.append(deepcopy(row))
                continue
            row = working[call["entity_id"]]
            row.update(deepcopy(call["data"]))
            modes = call.get("multi_entity_update_modes", {})
            for field, mode in modes.items():
                if mode == "add":
                    old = row.get(field) or []
                    for link in call["data"].get(field) or []:
                        if link not in old:
                            old.append(link)
                    row[field] = old
            result.append({"type": "TimeLog", "id": row["id"]})
        self.rows = working
        return result


def _source(row_id, work_date, amount, project_id):
    return {
        "id": row_id, "type": "TimeLog", "user": USER,
        "date": work_date.isoformat(), "duration": amount,
        "project": {"type": "Project", "id": project_id},
        "entity": {"type": "Task", "id": project_id + 1000},
        "sg_overtime_info": "Weekend", "sg_overtime_status": "ovract",
        "sg_payback_type": "Hours Off", api.REDEEMED_VALUE_FIELD: amount,
        api.REDEEMED_LINK_FIELD: [],
    }


def _today():
    return datetime.now(gettz("Europe/Warsaw")).date()


def _rows():
    today = _today()
    first = today.replace(day=1)
    source_date = first.replace(month=first.month - 1) if first.month > 1 else date(first.year - 1, 12, 1)
    return [
        _source(1, source_date, 120, 20),
        _source(2, source_date, 60, 21),
        _source(3, source_date, 180, 22),
    ]


def test_fifo_preview_splits_five_hours_across_sources():
    """Allocate two, one, and two hours from a 2+1+3 hour balance."""
    usage = _today()
    lots = api.source_rows(_rows(), usage)
    assert api.allocate(lots, 300) == [
        {"source_id": 1, "minutes": 120},
        {"source_id": 2, "minutes": 60},
        {"source_id": 3, "minutes": 120},
    ]


def test_unlinked_overlay_row_requires_reconciliation():
    """Metadata alone never proves that a source balance was debited."""
    today = _today()
    rows = _rows()
    child = {
        "id": 4, "type": "TimeLog", "user": USER,
        "date": today.isoformat(), "duration": 0,
        "project": TARGET_PROJECT, "entity": TASK,
        api.REDEEMED_VALUE_FIELD: 60, api.REDEEM_ID_FIELD:
            "550e8400-e29b-41d4-a716-446655440008",
        "sg_metadata": api._patch_row({}, role=api.ROLE_ALLOCATION,
            state=api.STATE_APPLIED, user_id=USER["id"], source_id=1,
            requested_minutes=60, allocated_minutes=60,
            usage_date=today.isoformat()),
    }
    rows.append(child)
    with pytest.raises(api.ReconciliationRequired, match="session metadata"):
        api.source_rows(rows, today)


def test_uuid_session_cannot_mix_targets_or_users():
    """One redemption UUID must identify one user and target task/project."""
    today = _today()
    sg = FakeShotgun(_rows())
    preview = api.snapshot(sg, USER["id"], today)
    redeem_id = "550e8400-e29b-41d4-a716-446655440011"
    api.redeem(
        sg, user=USER, usage_date=today, amount=180,
        target_task=TASK, target_project=TARGET_PROJECT,
        expected_fingerprint=preview["fingerprint"], redeem_id=redeem_id,
    )
    child_id = max(sg.rows)
    child = sg.rows[child_id]
    sg.rows[child_id]["entity"] = {"type": "Task", "id": 999}
    with pytest.raises(api.ReconciliationRequired, match="session metadata"):
        api.source_rows(list(sg.rows.values()), today)


def test_same_uuid_retry_refuses_if_backlinks_do_not_reconcile():
    """Idempotent retries must audit sources before returning success."""
    today = _today()
    sg = FakeShotgun(_rows())
    preview = api.snapshot(sg, USER["id"], today)
    redeem_id = "550e8400-e29b-41d4-a716-446655440012"
    api.redeem(
        sg, user=USER, usage_date=today, amount=60,
        target_task=TASK, target_project=TARGET_PROJECT,
        expected_fingerprint=preview["fingerprint"], redeem_id=redeem_id,
    )
    sg.rows[1][api.REDEEMED_LINK_FIELD] = []
    with pytest.raises(api.ReconciliationRequired):
        api.redeem(
            sg, user=USER, usage_date=today, amount=60,
            target_task=TASK, target_project=TARGET_PROJECT,
            expected_fingerprint=preview["fingerprint"], redeem_id=redeem_id,
        )


def test_redeem_writes_zero_duration_positive_overlay_and_source_links():
    """Commit one UUID session without changing task-cost duration."""
    today = _today()
    sg = FakeShotgun(_rows())
    preview = api.snapshot(sg, USER["id"], today)
    TASK_PROJECT = {"type": "Project", "id": 90, "name": "00_IN_HOUSE"}
    result = api.redeem(
        sg, user=USER, usage_date=today, amount=300,
        target_task=TASK, target_project=TASK_PROJECT,
        expected_fingerprint=preview["fingerprint"],
        redeem_id="550e8400-e29b-41d4-a716-446655440000",
    )
    assert len(result["timelogs"]) == 3
    assert all(row["duration"] == 0 for row in result["timelogs"])
    assert sorted(row[api.REDEEMED_VALUE_FIELD] for row in result["timelogs"]) == [60, 120, 120]
    assert {row[api.REDEEM_ID_FIELD] for row in result["timelogs"]} == {
        "550e8400-e29b-41d4-a716-446655440000"}
    assert [sg.rows[row_id][api.REDEEMED_VALUE_FIELD] for row_id in (1, 2, 3)] == [0, 0, 60]
    assert [len(sg.rows[row_id][api.REDEEMED_LINK_FIELD]) for row_id in (1, 2, 3)] == [1, 1, 1]


def test_submit_rechecks_snapshot_and_refuses_stale_balance():
    """Require renewed user review when any source balance changed."""
    today = _today()
    sg = FakeShotgun(_rows())
    preview = api.snapshot(sg, USER["id"], today)
    TASK_PROJECT = {"type": "Project", "id": 90, "name": "00_IN_HOUSE"}
    sg.rows[1][api.REDEEMED_VALUE_FIELD] -= 1
    with pytest.raises(api.BalanceChanged):
        api.redeem(
            sg, user=USER, usage_date=today, amount=60,
            target_task=TASK, target_project=TASK_PROJECT,
            expected_fingerprint=preview["fingerprint"],
            redeem_id="550e8400-e29b-41d4-a716-446655440001",
        )


def test_new_overlay_changes_snapshot_and_blocks_overlapping_submit():
    """Any intervening submission must force a fresh artist confirmation."""
    today = _today()
    sg = FakeShotgun(_rows())
    initial = api.snapshot(sg, USER["id"], today)
    api.redeem(
        sg, user=USER, usage_date=today, amount=60,
        target_task=TASK, target_project=TARGET_PROJECT,
        expected_fingerprint=initial["fingerprint"],
        redeem_id="550e8400-e29b-41d4-a716-446655440003",
    )
    assert api.snapshot(sg, USER["id"], today)["fingerprint"] != initial["fingerprint"]
    with pytest.raises(api.BalanceChanged):
        api.redeem(
            sg, user=USER, usage_date=today, amount=60,
            target_task=TASK, target_project=TARGET_PROJECT,
            expected_fingerprint=initial["fingerprint"],
            redeem_id="550e8400-e29b-41d4-a716-446655440004",
        )


def test_unfinished_prepared_uuid_is_blocked_until_explicit_recovery():
    """A partial session cannot be silently recomputed or committed."""
    today = _today()
    sg = FakeShotgun(_rows())
    preview = api.snapshot(sg, USER["id"], today)
    redeem_id = "550e8400-e29b-41d4-a716-446655440005"
    original_batch = sg.batch

    def fail_commit(calls):
        if calls and calls[0]["request_type"] == "update":
            raise RuntimeError("simulated unavailable Flow write")
        return original_batch(calls)

    sg.batch = fail_commit
    with pytest.raises(api.ReconciliationRequired):
        api.redeem(
            sg, user=USER, usage_date=today, amount=60,
            target_task=TASK, target_project=TARGET_PROJECT,
            expected_fingerprint=preview["fingerprint"],
            redeem_id=redeem_id,
        )
    sg.batch = original_batch


def test_overlay_must_be_written_for_today_even_with_a_fresh_snapshot():
    """A selected historical diary date cannot bypass same-day policy."""
    today = _today()
    yesterday = today.fromordinal(today.toordinal() - 1)
    sg = FakeShotgun(_rows())
    with pytest.raises(ValueError, match="today"):
        api.snapshot(sg, USER["id"], yesterday)
def test_withdraw_is_same_day_only_and_restores_source_balance():
    """Withdraw an entire UUID session only on its Flow creation date."""
    today = _today()
    sg = FakeShotgun(_rows())
    preview = api.snapshot(sg, USER["id"], today)
    TASK_PROJECT = {"type": "Project", "id": 90, "name": "00_IN_HOUSE"}
    redeem_id = "550e8400-e29b-41d4-a716-446655440002"
    result = api.redeem(
        sg, user=USER, usage_date=today, amount=60,
        target_task=TASK, target_project=TASK_PROJECT,
        expected_fingerprint=preview["fingerprint"], redeem_id=redeem_id,
    )
    outcome = api.withdraw(sg, user=USER, redeem_id=redeem_id, today=today)
    assert outcome["withdrawn"] is True
    assert [sg.rows[row_id][api.REDEEMED_VALUE_FIELD] for row_id in (1, 2, 3)] == [120, 60, 180]
    assert all(sg.rows[row["id"]][api.REDEEMED_VALUE_FIELD] == 0
               for row in result["timelogs"])
    with pytest.raises(ValueError, match="creation day"):
        api.withdraw(sg, user=USER, redeem_id=redeem_id,
                     today=today.replace(day=today.day + 1))


def test_withdrawable_sessions_lists_complete_today_sessions():
    """The UI can discover only complete sessions eligible for withdrawal."""
    today = _today()
    sg = FakeShotgun(_rows())
    preview = api.snapshot(sg, USER["id"], today)
    redeem_id = "550e8400-e29b-41d4-a716-446655440007"
    api.redeem(
        sg, user=USER, usage_date=today, amount=60,
        target_task=TASK, target_project=TARGET_PROJECT,
        expected_fingerprint=preview["fingerprint"], redeem_id=redeem_id,
    )
    assert api.withdrawable_sessions(sg, USER["id"], today) == [{
        "redeem_id": redeem_id, "minutes": 60,
        "target": "00_IN_HOUSE / Production",
    }]
