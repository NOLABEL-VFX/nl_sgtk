"""Verify time-bank arithmetic without live Flow requests."""

from datetime import date

import pytest

from nl_sgtk import hours_off as rules


def source(number=1, workday="2026-04-01", amount=180):
    """Build an approved source with the exact fields used for accounting."""
    return {"id": number, "date": workday, "duration": amount,
            "sg_meta_duration": amount, "user": {"id": 10},
            "sg_payback_type": "Hours Off", "sg_overtime_info": "Weekend",
            "sg_overtime_status": "ovract", rules.LINK_FIELD: []}


@pytest.mark.parametrize("text, amount", [
    ("-4", 240), ("-2:30", 150), ("−0:01", 1), ("-2.5", 150),
    ("-2,5", 150), ("-0.05", 3),
])
def test_signed_input(text, amount):
    """Accept negative hour quantities without losing minute precision."""
    assert rules.parse_negative_hours(text) == amount


@pytest.mark.parametrize("text", [
    "0", "-0", "2", "nan", "-NaN", "-Infinity", "-1:60", "-1:5",
    "-0.001", "--1", "-1:-5", "",
])
def test_invalid_input(text):
    """Reject positive, malformed, fractional-minute and nonfinite values."""
    with pytest.raises(ValueError):
        rules.parse_negative_hours(text)


def test_fifo_three_projects():
    """A ten-hour opening balance minus four leaves six, not one."""
    rows = [source(3, "2026-08-01", 300), source(2, "2026-05-01", 120),
            source()]
    lots = rules.source_rows(rows, date(2026, 10, 22))
    assert rules.allocate(lots, 240) == [
        {"source_id": 1, "minutes": 180, "production_minutes": 180},
        {"source_id": 2, "minutes": 60, "production_minutes": 60}]
    assert sum(lot["remaining"] for lot in lots) - 240 == 360


@pytest.mark.parametrize("usage, valid", [
    (date(2026, 10, 31), True), (date(2026, 11, 1), False),
    (date(2026, 3, 31), False),
])
def test_full_month_expiry(usage, valid):
    """April remains usable through the end of October."""
    assert rules.source_rows([source()], usage)[0]["eligible"] is valid
    assert rules.expires_on("2026-04-01") == date(2026, 10, 31)
    assert rules.expires_on("2026-10-31") == date(2027, 4, 30)


def test_backdating_grace_and_year_rollover():
    """The day off governs expiry while the submission window limits grace."""
    usage = rules.validate_usage("2026-10-31", date(2026, 11, 5))
    assert rules.source_rows([source()], usage)[0]["eligible"]
    with pytest.raises(ValueError):
        rules.validate_usage("2026-10-31", date(2026, 11, 6))
    assert rules.month_start(date(2027, 1, 5), -6) == date(2026, 7, 1)
    with pytest.raises(ValueError):
        rules.validate_usage("2026-11-6", date(2026, 11, 5))


@pytest.mark.parametrize("field,value", [
    ("sg_overtime_status", "ovrsub"), ("sg_overtime_status", "rjc"),
    ("sg_payback_type", "Normal"), ("sg_payback_type", "Exception"),
    ("sg_payback_type", "Internal"), ("sg_payback_type", None),
    ("sg_overtime_info", "None"),
])
def test_no_unaccepted_or_untyped_credit(field, value):
    """Never apply a UI default type to accounting eligibility."""
    row = source()
    row[field] = value
    assert rules.source_rows([row], date(2026, 10, 22)) == []


def test_signs_and_unknown_negatives():
    """Only recognized active deductions become positive artist coverage."""
    row = {"duration": -30, "sg_meta_duration": -60}
    assert rules.coverage(row) == -60
    row["sg_metadata"] = rules.patch_metadata({}, {
        "role": "deduction", "state": "active"})
    assert rules.coverage(row) == 0
    row["created_by"] = row["updated_by"] = {"type": "ApiUser", "id": 70}
    assert rules.coverage(row) == 60
    row["sg_metadata"] = rules.patch_metadata(row, {
        "role": "deduction", "state": "cancelled"})
    assert rules.coverage(row) == 0


def test_rounding_after_partial_cancellation():
    """Exhausting a fractional production conversion reverses it exactly."""
    row = source(amount=3)
    row["duration"] = 1
    lots = rules.source_rows([row], date(2026, 10, 22))
    first = rules.allocate(lots, 1)[0]
    lots[0].update(remaining=2, production_used=first["production_minutes"])
    second = rules.allocate(lots, 2)[0]
    assert first["production_minutes"] + second["production_minutes"] == 1


def test_metadata_preserved():
    """Keep original/current and other tools' metadata during bank writes."""
    row = {"sg_metadata": '{"original":{"duration":60},"other":true}'}
    changed = {"sg_metadata": rules.patch_metadata(row, {"role": "request"})}
    assert rules.metadata(changed)["original"] == {"duration": 60}
    assert rules.metadata(changed)["other"] is True
