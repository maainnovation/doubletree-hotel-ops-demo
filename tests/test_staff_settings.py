import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

from services.staff_settings import demo_defaults, validate_staff_settings
from services.state_store import StateStore


def test_demo_roster_counts_ids_and_assumptions():
    payload = demo_defaults()
    assert len(payload["staff"]) == 24
    assert len({person["staff_id"] for person in payload["staff"]}) == 24
    assert {role: sum(p["role"] == role for p in payload["staff"]) for role in ("Housekeeper", "Houseman", "Lobby Staff", "Manager")} == {
        "Housekeeper": 16, "Houseman": 4, "Lobby Staff": 2, "Manager": 2
    }
    assert all(person["availability_json"] and person["max_weekly_hours"] and person["max_daily_hours"] for person in payload["staff"])
    assert set(payload["settings"]["minimum_role_coverage"]) == {"Housekeeper", "Houseman", "Lobby Staff", "Manager"}
    assert validate_staff_settings(payload) == []


def test_staff_validation_rejects_duplicate_id_and_bad_shift():
    payload = demo_defaults()
    payload["staff"][1]["staff_id"] = payload["staff"][0]["staff_id"]
    payload["staff"][2]["availability_json"] = json.dumps({"Mon": "09:00-T"})
    errors = validate_staff_settings(payload)
    assert any("unique" in error for error in errors)
    assert any("availability" in error for error in errors)


def test_staff_validation_checks_availability_limits_time_off_and_rules():
    payload = demo_defaults()
    row = payload["staff"][0]
    row["availability_json"] = json.dumps({"Mon": "07:00-17:00", "Tue": "07:00-17:00", "Wed": "07:00-17:00", "Thu": "07:00-17:00", "Fri": "07:00-17:00"})
    row["requested_time_off"] = "next Monday"
    payload["settings"]["minimum_role_coverage"].pop("Houseman")
    payload["settings"]["status_codes"].pop("BANQUET")
    errors = validate_staff_settings(payload)
    assert any("weekly hour limit" in error for error in errors)
    assert any("requested time off" in error for error in errors)
    assert any("complete and valid" in error for error in errors)


def test_staff_settings_save_survives_store_reopen(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    payload = demo_defaults()
    payload["staff"][0].update({
        "display_name": "Updated Demo Name",
        "availability_json": json.dumps({"Mon": "10:00-18:00", "Tue": "OFF", "Wed": "10:00-18:00", "Thu": "10:00-18:00", "Fri": "OFF", "Sat": "OFF", "Sun": "OFF"}),
        "requested_time_off": "2026-10-28, 2026-11-03",
        "max_weekly_hours": 24,
        "max_daily_hours": 8,
    })
    payload["settings"]["cleaning_standards"]["checkout_minutes"] = 24
    payload["settings"]["cleaning_standards"]["stayover_service_policy"] = "Service selected stayovers only (editable demo assumption)"
    payload["settings"]["minimum_role_coverage"]["Houseman"] = 2
    payload["settings"]["paid_productive_hours"]["break_minutes"] = 45
    payload["settings"]["break_policy"]["break_minutes"] = 45
    payload["dataset_mode"] = "demo"
    assert validate_staff_settings(payload) == []
    store.save_staff_settings("manager", payload, 1, dataset_mode="demo")
    reopened = StateStore(path).get_staff_settings("manager")
    assert reopened["staff"][0]["display_name"] == "Updated Demo Name"
    assert reopened["_demo_seed_version"] == 1
    assert reopened["_dataset_mode"] == "demo"
    for field in ("availability_json", "requested_time_off", "max_weekly_hours", "max_daily_hours"):
        assert reopened["staff"][0][field] == payload["staff"][0][field]
    for key in ("cleaning_standards", "minimum_role_coverage", "paid_productive_hours", "break_policy", "status_codes"):
        assert reopened["settings"][key] == payload["settings"][key]


def test_same_id_edit_defaults_to_operational_and_disables_reset(tmp_path, monkeypatch):
    database = tmp_path / "staff-state.sqlite3"
    monkeypatch.setenv("COPILOT_STATE_DB", str(database))
    monkeypatch.setenv("WEB_MANAGER_ID", "same-id-edit")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run(timeout=30)
    app.segmented_control[0].set_value("STAFF & SETTINGS").run(timeout=30)
    assert any("PUBLIC DEMO ONLY" in item.value for item in app.warning)
    unchanged_id = app.session_state["staff_saved"]["staff"][0]["staff_id"]
    app.session_state["staff_rows_0"] = {"edited_rows": {0: {"display_name": "Real employee"}}, "added_rows": [], "deleted_rows": []}
    next(button for button in app.button if button.label == "Save changes").click().run(timeout=30)
    saved = StateStore(database).get_staff_settings("same-id-edit")
    assert saved["staff"][0]["staff_id"] == unchanged_id
    assert saved["staff"][0]["display_name"] == "Real employee"
    assert saved["_dataset_mode"] == "operational"
    assert all(button.label != "Reset demo staff & settings" for button in app.button)
    assert not app.exception
