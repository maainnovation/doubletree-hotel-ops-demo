"""Regression coverage for editable inputs and the collapsed calculation summary."""
from copy import deepcopy
from datetime import date
import json

import pytest

from services.forecast_import import empty_forecast, normalize_forecast_rows
from services.staff_settings import demo_defaults
from services.weekly_roster import generate_weekly_roster, is_roster_current, roster_calculation_summary


def forecast():
    rows = empty_forecast(date(2026, 10, 23))
    for row in rows:
        row.update(departures=17, stayovers=1, arrivals=0, rooms_on_books=1,
                   forecast_pickup=0, forecast_occupied=1, occupancy_pct=1)
    return rows


def staff():
    payload = demo_defaults()
    payload["staff"] = payload["staff"][:2]
    for person in payload["staff"]:
        person["availability_json"] = json.dumps({day: "09:00-17:00" for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")})
        person["max_weekly_hours"] = 56
    payload["settings"]["minimum_role_coverage"] = {"Housekeeper": 1, "Houseman": 0, "Lobby Staff": 0, "Manager": 0}
    return payload


def test_checkout_minutes_change_demand_and_scheduled_staff_in_both_directions():
    source = forecast()
    original = deepcopy(source)
    payload = staff()
    first = generate_weekly_roster(source, payload)
    before = roster_calculation_summary(first)
    assert all(row["demand_minutes"] == 384 for row in before)
    assert all(row["required_housekeepers"] == row["scheduled_housekeepers"] == 1 for row in before)
    payload["settings"]["cleaning_standards"]["checkout_minutes"] = 25
    assert not is_roster_current(first, source, payload)
    second = generate_weekly_roster(source, payload, prior=first)
    after = roster_calculation_summary(second)
    assert all(row["demand_minutes"] == 435 for row in after)
    assert all(row["required_housekeepers"] == row["scheduled_housekeepers"] == 2 for row in after)
    assert all(row["productive_minutes_per_shift"] == 390 for row in after)
    assert all(row["status"] == "Covered" and row["uncovered_minutes"] == 0 for row in after)
    # The old summary stays tied to its own calculation inputs.
    assert roster_calculation_summary(first) == before
    payload["settings"]["cleaning_standards"]["checkout_minutes"] = 22
    third = generate_weekly_roster(source, payload, prior=second)
    assert roster_calculation_summary(third) == before
    assert third["forecast"] == second["forecast"] == first["forecast"] == normalize_forecast_rows(original)
    assert source == original
    assert [row["weekday"] for row in before] == ["Fri", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu"]


@pytest.mark.parametrize("change", ["checkout", "stayover", "capacity", "staff", "forecast"])
def test_input_signature_detects_changed_inputs_but_ignores_metadata(change):
    source, payload = forecast(), staff()
    roster = generate_weekly_roster(source, payload)
    assert is_roster_current(roster, source, payload)
    assert is_roster_current(roster, normalize_forecast_rows(source), payload)
    payload["dataset_mode"] = "operational"
    payload["updated_at"] = "later"
    assert is_roster_current(roster, source, payload)
    if change == "checkout":
        payload["settings"]["cleaning_standards"]["checkout_minutes"] = 25
    elif change == "stayover":
        payload["settings"]["cleaning_standards"]["stayover_minutes"] = 12
    elif change == "capacity":
        payload["settings"]["paid_productive_hours"]["other_duties_minutes"] = 45
    elif change == "staff":
        payload["staff"][0]["requested_time_off"] = "2026-10-23"
    else:
        source[0]["departures"] = 18
    assert not is_roster_current(roster, source, payload)
    assert not is_roster_current(None, source, payload)
    legacy = {key: value for key, value in roster.items() if key not in {"input_signature", "input_snapshot"}}
    assert not is_roster_current(legacy, source, payload)
    assert roster_calculation_summary(legacy) == []


def test_regeneration_preserves_unsaved_locked_edits_and_reports_constraint():
    source, payload = forecast(), staff()
    roster = generate_weekly_roster(source, payload)
    locked = next(row for row in roster["schedule"] if row["date"] == "2026-10-23" and row["code"] == "SHIFT")
    locked["locked"] = True
    locked["notes"] = "Manager edit"
    payload["staff"][0]["requested_time_off"] = "2026-10-23"
    payload["settings"]["cleaning_standards"]["checkout_minutes"] = 25
    updated = generate_weekly_roster(source, payload, prior=roster)
    kept = next(row for row in updated["schedule"] if row["date"] == locked["date"] and row["staff_id"] == locked["staff_id"])
    assert kept["locked"] is True and kept["start"] == locked["start"]
    assert "Manager edit" in kept["notes"]
    summary = roster_calculation_summary(updated)[0]
    assert summary["scheduled_housekeepers"] == summary["required_housekeepers"] == 2
    assert summary["uncovered_minutes"] == 0
    assert summary["status"] == "Review constraints"
    assert any("requested time off" in item for item in summary["constraints"])
    assert is_roster_current(updated, source, payload)


def test_summary_separates_uncovered_demand_from_scheduled_staff():
    source, payload = forecast(), staff()
    for person in payload["staff"]:
        person["requested_time_off"] = "2026-10-23"
    roster = generate_weekly_roster(source, payload)
    summary = roster_calculation_summary(roster)[0]
    assert summary["required_housekeepers"] == 1
    assert summary["scheduled_housekeepers"] == summary["scheduled_productive_minutes"] == 0
    assert summary["uncovered_minutes"] == 384
    assert summary["status"] == "Uncovered demand"
    assert any("uncovered" in item for item in summary["constraints"])


def test_summary_distinguishes_minimum_role_coverage_from_zero_cleaning_demand():
    source, payload = forecast(), staff()
    for row in source:
        row.update(departures=0, stayovers=0, rooms_on_books=0, forecast_occupied=0, occupancy_pct=0)
    summary = roster_calculation_summary(generate_weekly_roster(source, payload))[0]
    assert summary["demand_housekeepers"] == summary["demand_minutes"] == 0
    assert summary["minimum_housekeepers"] == summary["required_housekeepers"] == 1
    assert summary["scheduled_housekeepers"] == 1


def test_summary_does_not_count_inspection_or_unknown_locked_rows_as_cleaning():
    source, payload = forecast(), staff()
    roster = generate_weekly_roster(source, payload)
    locked = next(row for row in roster["schedule"] if row["date"] == "2026-10-23" and row["code"] == "SHIFT")
    locked.update(code="INSPECT", locked=True)
    unknown = deepcopy(locked)
    unknown.update(staff_id="removed-staff-id", code="SHIFT")
    roster["schedule"].append(unknown)
    updated = generate_weekly_roster(source, payload, prior=roster)
    summary = roster_calculation_summary(updated)[0]
    assert summary["scheduled_housekeepers"] == 1
    assert summary["scheduled_productive_minutes"] == 390
    assert summary["status"] == "Review constraints"
    assert any("no longer" in item for item in summary["constraints"])
