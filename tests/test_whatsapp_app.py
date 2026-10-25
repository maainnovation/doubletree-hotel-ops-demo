"""Local inbox safety and human-readable roster export regressions."""
from copy import deepcopy
from datetime import date
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from pypdf import PdfReader
from streamlit.testing.v1 import AppTest

from services.forecast_import import empty_forecast
from services.staff_settings import demo_defaults
from services.state_store import StateStore
from services.weekly_roster import build_exports, generate_weekly_roster


APP = Path(__file__).resolve().parents[1] / "app.py"


def example_roster():
    rows = empty_forecast(date(2026, 10, 23))
    for row in rows:
        row.update(departures=12, stayovers=40, arrivals=20, rooms_on_books=60,
                   forecast_pickup=0, forecast_occupied=60, occupancy_pct=20)
    roster = generate_weekly_roster(rows, demo_defaults())
    roster["version"] = 2
    roster["source"] = {"channel": "whatsapp", "dataset_mode": "demo", "hotel_name": "Fictional Harbor Hotel"}
    return roster


def configure_app(tmp_path, monkeypatch, *, mode="demo", ip="127.0.0.1", enabled="1", manager="local-manager"):
    monkeypatch.setenv("COPILOT_STATE_DB", str(tmp_path / "inbox.sqlite3"))
    monkeypatch.setenv("WHATSAPP_DEV_INBOX_ENABLED", enabled)
    if manager:
        monkeypatch.setenv("WEB_MANAGER_ID", manager)
    else:
        monkeypatch.delenv("WEB_MANAGER_ID", raising=False)
    monkeypatch.setattr(type(st.context), "ip_address", property(lambda self: ip))
    payload = demo_defaults()
    payload["dataset_mode"] = mode
    StateStore().save_staff_settings(manager or "unused", payload, dataset_mode=mode)


@pytest.mark.parametrize("ip", ["203.0.113.7", None, "localhost"])
@pytest.mark.parametrize("enabled", ["0", "1"])
@pytest.mark.parametrize("tenant_enabled", [True, False])
def test_linked_manager_is_blocked_before_private_state_reads_on_remote_or_unknown_context(tmp_path, monkeypatch, ip, enabled, tenant_enabled):
    from services.whatsapp_store import WorkflowStore
    configure_app(tmp_path, monkeypatch, ip=ip, enabled=enabled)
    WorkflowStore().enroll_tenant(tenant_id="synthetic-hotel", manager_id="local-manager", hotel_code="SYNTHETIC",
                                hotel_name="Fictional Harbor Hotel")
    if not tenant_enabled:
        with WorkflowStore()._connect() as db:
            db.execute("UPDATE wa_tenants SET enabled=0 WHERE manager_id=?", ("local-manager",))
    def forbidden_read(*args, **kwargs):
        raise AssertionError("An unauthenticated client reached enrolled manager state")
    monkeypatch.setattr(StateStore, "get_manager", forbidden_read)
    monkeypatch.setattr(StateStore, "get_staff_settings", forbidden_read)
    monkeypatch.setattr(StateStore, "list_weekly_roster_versions", forbidden_read)
    app = AppTest.from_file(APP).run(timeout=40)
    assert not app.exception
    assert any("WhatsApp-linked data is available only in the local development app" in item.value for item in app.error)
    assert not app.segmented_control
    assert "staff_saved" not in app.session_state
    assert not any(item.label == "WhatsApp forecast inbox (local demo)" for item in app.expander)


def test_linked_demo_manager_remains_available_on_actual_loopback(tmp_path, monkeypatch):
    from services.whatsapp_store import WorkflowStore
    configure_app(tmp_path, monkeypatch, ip="::1")
    WorkflowStore().enroll_tenant(tenant_id="synthetic-hotel", manager_id="local-manager", hotel_code="SYNTHETIC",
                                hotel_name="Fictional Harbor Hotel")
    app = AppTest.from_file(APP).run(timeout=40)
    assert not app.exception
    assert app.segmented_control
    assert app.session_state["staff_saved"]["dataset_mode"] == "demo"
    assert any(item.label == "WhatsApp forecast inbox (local demo)" for item in app.expander)


@pytest.mark.parametrize("changes", [
    {"enabled": "0"}, {"ip": "203.0.113.7"}, {"ip": None},
    {"ip": "localhost"}, {"manager": ""}, {"mode": "operational"},
])
def test_inbox_is_hidden_and_never_queries_versions_without_every_gate(tmp_path, monkeypatch, changes):
    configure_app(tmp_path, monkeypatch, **changes)
    def forbidden_lookup(*args, **kwargs):
        raise AssertionError("Private inbox queried before local synthetic gates passed")
    monkeypatch.setattr(StateStore, "list_weekly_roster_versions", forbidden_lookup, raising=False)
    app = AppTest.from_file(APP).run(timeout=40)
    assert not app.exception
    assert not any(item.label == "WhatsApp forecast inbox (local demo)" for item in app.expander)


def test_local_inbox_filters_private_versions_and_loads_only_on_click(tmp_path, monkeypatch):
    configure_app(tmp_path, monkeypatch)
    roster = example_roster()
    operational = deepcopy(roster)
    operational["version"] = 3
    operational["source"]["dataset_mode"] = "operational"
    manual = deepcopy(roster)
    manual["version"] = 4
    manual["source"]["channel"] = "web"
    calls = []
    def lookup(self, manager_id, week_start=None):
        calls.append(manager_id)
        return [manual, operational, deepcopy(roster)]
    monkeypatch.setattr(StateStore, "list_weekly_roster_versions", lookup, raising=False)
    app = AppTest.from_file(APP).run(timeout=40)
    assert not app.exception
    inbox = next(item for item in app.expander if item.label == "WhatsApp forecast inbox (local demo)")
    assert inbox.proto.expanded is False
    assert app.session_state["forecast_confirmed"] is None
    buttons = [button for button in app.button if button.label.startswith("Load WhatsApp roster")]
    assert len(buttons) == 1 and buttons[0].label.endswith("v2")
    buttons[0].click().run(timeout=40)
    assert not app.exception
    assert app.session_state["weekly_candidate"]["version"] == 2
    assert app.session_state["forecast_confirmed"] == roster["forecast"]
    assert all(manager == "local-manager" for manager in calls)
    assert next(button for button in app.button if button.label.startswith("Load WhatsApp roster")).disabled


def test_inbox_does_not_replace_an_unsaved_forecast_or_manual_lock(tmp_path, monkeypatch):
    configure_app(tmp_path, monkeypatch)
    roster = example_roster()
    monkeypatch.setattr(StateStore, "list_weekly_roster_versions", lambda *args: [deepcopy(roster)], raising=False)
    app = AppTest.from_file(APP).run(timeout=40)
    draft = deepcopy(roster)
    draft["schedule"][0]["locked"] = True
    draft["schedule"][0]["notes"] = "Unsaved manager edit"
    app.session_state["forecast_confirmed"] = draft["forecast"]
    app.session_state["weekly_candidate"] = draft
    app.run(timeout=40)
    assert not app.exception
    assert next(button for button in app.button if button.label.startswith("Load WhatsApp roster")).disabled
    assert app.session_state["weekly_candidate"]["schedule"][0]["notes"] == "Unsaved manager edit"
    assert app.session_state["weekly_candidate"]["schedule"][0]["locked"] is True


def test_inbox_reads_persisted_versions_only_for_the_mapped_demo_manager(tmp_path, monkeypatch):
    configure_app(tmp_path, monkeypatch)
    roster = example_roster()
    store = StateStore()
    store.save_weekly_roster("local-manager", roster["week_start"], roster)
    store.save_weekly_roster("local-manager", roster["week_start"], roster)
    other = deepcopy(roster)
    other["week_start"] = "2026-10-30"
    store.save_weekly_roster("another-hotel", "2026-10-30", other)
    app = AppTest.from_file(APP).run(timeout=40)
    assert not app.exception
    buttons = [button for button in app.button if button.label.startswith("Load WhatsApp roster")]
    assert [button.label for button in buttons] == [
        "Load WhatsApp roster 2026-10-23 v2", "Load WhatsApp roster 2026-10-23 v1"]
    buttons[0].click().run(timeout=40)
    assert not app.exception
    assert app.session_state["weekly_candidate"]["version"] == 2
    assert app.session_state["forecast_confirmed"] == roster["forecast"]


def test_pdf_contains_every_daily_calculation_version_locks_and_honest_constraints():
    roster = example_roster()
    roster["feasible"] = False
    roster["constraints"].append("2026-10-23 DEMO-HK-01: locked assignment conflicts with requested time off.")
    roster["schedule"][0]["locked"] = True
    xlsx, pdf = build_exports(roster)
    reader = PdfReader(BytesIO(pdf))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert len(reader.pages) >= 2
    assert "Version 2" in text and "Fictional Harbor Hotel" in text
    assert "Received via WhatsApp" in text
    assert "NEEDS ATTENTION" in text
    assert "Day-by-day calculation summary" in text
    assert text.count("checkouts x 22") == 7
    assert text.count("stayovers x 10") == 7
    assert "Preserved locked assignment" in text
    assert "locked assignment conflicts with requested time off" in text
    assert all(row["date"] in text for row in roster["forecast"])
    assert "Daily Calculations" in pd.ExcelFile(BytesIO(xlsx)).sheet_names


def test_pdf_treats_staff_names_hotel_and_constraints_as_plain_data():
    roster = example_roster()
    roster["source"]["hotel_name"] = "Harbor <b>not markup</b> & Hotel"
    roster["constraints"] = ["Missing <font color='red'>text</font> & capacity"]
    for row in roster["schedule"]:
        if row["staff_id"] == "DEMO-HK-01":
            row["display_name"] = "A <b>Literal</b> & B"
    _, pdf = build_exports(roster)
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)
    assert "<b>not markup</b> & Hotel" in text
    assert "<b>Literal</b> & B" in text
    assert "<font color='red'>text</font> & capacity" in text


def test_shared_capacity_starts_unknown_and_uses_only_saved_hotel_inventory():
    from services.hotel_parameters import apply_shared_parameters, saved_room_capacity, shared_parameters_from_staff
    staff = demo_defaults()
    staff["settings"]["checkout_rooms"] = 150
    staff["settings"]["stayover_rooms"] = 150
    assert saved_room_capacity(staff) is None
    with pytest.raises(ValueError, match="Parameters"):
        saved_room_capacity(staff, required=True)
    saved = apply_shared_parameters(staff, {"room_capacity": 292, "checkout_minutes": 25})
    assert shared_parameters_from_staff(saved) == {"room_capacity": 292, "checkout_minutes": 25, "stayover_minutes": 10}
    assert saved_room_capacity(staff) is None  # Pure merge; never mutate a snapshot.
    assert saved["settings"]["hotel_inventory"] == {"room_capacity": 292}
    assert "room_capacity" not in saved["settings"]["cleaning_standards"]
    cleared = apply_shared_parameters(saved, {"room_capacity": None})
    assert saved_room_capacity(cleared) is None
    with pytest.raises(ValueError, match="Parameters"):
        saved_room_capacity(cleared, required=True)


@pytest.mark.parametrize("capacity", [True, 0, -1, 292.5, "292", 10001])
def test_invalid_inventory_cannot_be_saved(capacity):
    from services.hotel_parameters import apply_shared_parameters
    with pytest.raises(ValueError, match="whole number"):
        apply_shared_parameters(demo_defaults(), {"room_capacity": capacity})


def test_parameters_shared_inventory_and_cleaning_survive_new_browser_session(tmp_path, monkeypatch):
    from services.hotel_parameters import saved_room_capacity
    configure_app(tmp_path, monkeypatch)
    app = AppTest.from_file(APP).run(timeout=40)
    app.segmented_control[0].set_value("PARAMETERS").run(timeout=40)
    assert app.number_input(key="param_room_capacity").value is None
    app.number_input(key="param_room_capacity").set_value(292).run(timeout=40)
    app.number_input(key="param_checkout_minutes").set_value(25).run(timeout=40)
    app.number_input(key="param_checkout_rooms").set_value(42).run(timeout=40)
    assert not app.exception
    saved = StateStore().get_staff_settings("local-manager")
    assert saved_room_capacity(saved, required=True) == 292
    assert saved["settings"]["cleaning_standards"]["checkout_minutes"] == 25
    assert "daily_parameters" not in saved["settings"]
    fresh = AppTest.from_file(APP).run(timeout=40)
    fresh.segmented_control[0].set_value("PARAMETERS").run(timeout=40)
    assert not fresh.exception
    assert fresh.number_input(key="param_room_capacity").value == 292
    assert fresh.number_input(key="param_checkout_minutes").value == 25
    assert fresh.number_input(key="param_checkout_rooms").value == 150  # Daily workload remains session-only.
    next(button for button in fresh.button if button.label == "Reset Parameters to Workbook Defaults").click().run(timeout=40)
    assert fresh.number_input(key="param_room_capacity").value == 292
    assert saved_room_capacity(StateStore().get_staff_settings("local-manager")) == 292


def test_latest_shared_settings_refresh_without_overwriting_newer_staff_or_locked_draft(tmp_path, monkeypatch):
    from services.hotel_parameters import saved_room_capacity
    from services.weekly_roster import is_roster_current
    configure_app(tmp_path, monkeypatch)
    app = AppTest.from_file(APP).run(timeout=40)
    original = example_roster()
    original["schedule"][0]["locked"] = True
    original["schedule"][0]["notes"] = "Unsaved locked assignment"
    app.session_state["forecast_confirmed"] = original["forecast"]
    app.session_state["weekly_candidate"] = deepcopy(original)
    store = StateStore()
    current = store.get_staff_settings("local-manager")
    current["staff"][0]["requested_time_off"] = "2026-10-23"
    store.save_staff_settings("local-manager", current, dataset_mode="demo", expected_revision=current["_settings_revision"])
    updated = store.save_hotel_parameters("local-manager", {"room_capacity": 292, "checkout_minutes": 25})
    app.run(timeout=40)
    assert not app.exception
    assert app.session_state["parameters"]["room_capacity"] == 292
    assert app.session_state["parameters"]["checkout_minutes"] == 25
    assert app.session_state["staff_saved"]["staff"][0]["requested_time_off"] == "2026-10-23"
    assert app.session_state["weekly_candidate"]["schedule"][0]["locked"] is True
    assert app.session_state["weekly_candidate"]["schedule"][0]["notes"] == "Unsaved locked assignment"
    assert not is_roster_current(original, original["forecast"], updated)
    assert any("ROSTER OUT OF DATE" in item.value for item in app.warning)
    app.segmented_control[0].set_value("PARAMETERS").run(timeout=40)
    app.number_input(key="param_room_capacity").set_value(293).run(timeout=40)
    saved = store.get_staff_settings("local-manager")
    assert saved_room_capacity(saved) == 293
    assert saved["staff"][0]["requested_time_off"] == "2026-10-23"


def test_open_staff_form_cannot_overwrite_newer_saved_parameters(tmp_path, monkeypatch):
    from services.hotel_parameters import saved_room_capacity
    configure_app(tmp_path, monkeypatch)
    app = AppTest.from_file(APP).run(timeout=40)
    app.segmented_control[0].set_value("STAFF & SETTINGS").run(timeout=40)
    next(item for item in app.number_input if item.label == "Checkout cleaning standard (minutes)").set_value(27)
    StateStore().save_hotel_parameters("local-manager", {"room_capacity": 292, "checkout_minutes": 25})
    next(button for button in app.button if button.label == "Save changes").click().run(timeout=40)
    assert not app.exception
    assert any("Your form was not saved" in item.value for item in app.error)
    saved = StateStore().get_staff_settings("local-manager")
    assert saved_room_capacity(saved) == 292
    assert saved["settings"]["cleaning_standards"]["checkout_minutes"] == 25
    next(button for button in app.button if button.label == "Cancel edits").click().run(timeout=40)
    assert not app.exception
    assert next(item for item in app.number_input if item.label == "Checkout cleaning standard (minutes)").value == 25
    assert app.session_state["staff_editor_base_revision"] == saved["_settings_revision"]


@pytest.mark.parametrize("kind,expected", [
    ("new_builtin", 292), ("existing_builtin_seed", 292),
    ("explicit_other_value", 300), ("explicit_unknown", None),
    ("unseeded_record", None), ("operational", None), ("linked", None),
    ("alternate_workbook", None),
])
def test_user_confirmed_total_rooms_seed_is_narrow_and_never_overwrites_other_settings(tmp_path, monkeypatch, kind, expected):
    import shutil
    from services.hotel_parameters import saved_room_capacity
    from services.staff_settings import DEMO_SEED_VERSION
    from services.whatsapp_store import WorkflowStore
    monkeypatch.setenv("COPILOT_STATE_DB", str(tmp_path / "confirmed-inventory.sqlite3"))
    monkeypatch.setenv("WEB_MANAGER_ID", "confirmed-demo")
    monkeypatch.setenv("WHATSAPP_DEV_INBOX_ENABLED", "0")
    monkeypatch.delenv("HOTEL_OPS_WORKBOOK", raising=False)
    monkeypatch.setattr(type(st.context), "ip_address", property(lambda self: "127.0.0.1"))
    if kind != "new_builtin":
        staff = demo_defaults()
        if kind == "explicit_other_value":
            staff["settings"]["hotel_inventory"] = {"room_capacity": 300}
        elif kind == "explicit_unknown":
            staff["settings"]["hotel_inventory"] = {"room_capacity": None}
        StateStore().save_staff_settings("confirmed-demo", staff,
                                        demo_seed_version=0 if kind == "unseeded_record" else DEMO_SEED_VERSION,
                                        dataset_mode="operational" if kind == "operational" else "demo")
    if kind == "linked":
        WorkflowStore().enroll_tenant(tenant_id="other-hotel", manager_id="confirmed-demo", hotel_code="OTHER",
                                    hotel_name="Another Fictional Hotel")
    if kind == "alternate_workbook":
        alternate = tmp_path / "another-hotel.xlsx"
        shutil.copyfile(APP.parent / "data" / "Hotel_Ops_Demo_Data.xlsx", alternate)
        monkeypatch.setenv("HOTEL_OPS_WORKBOOK", str(alternate))
    app = AppTest.from_file(APP).run(timeout=40)
    app.segmented_control[0].set_value("PARAMETERS").run(timeout=40)
    assert not app.exception
    assert app.number_input(key="param_room_capacity").label == "Total rooms"
    assert app.number_input(key="param_room_capacity").value == expected
    stored = StateStore().get_staff_settings("confirmed-demo")
    assert saved_room_capacity(stored) == expected
    revision = stored["_settings_revision"]
    fresh = AppTest.from_file(APP).run(timeout=40)
    assert not fresh.exception
    assert StateStore().get_staff_settings("confirmed-demo")["_settings_revision"] == revision
    assert fresh.session_state["parameters"]["room_capacity"] == expected


def test_clearing_user_confirmed_builtin_inventory_does_not_silently_reseed(tmp_path, monkeypatch):
    from services.hotel_parameters import saved_room_capacity
    monkeypatch.setenv("COPILOT_STATE_DB", str(tmp_path / "clear-confirmed.sqlite3"))
    monkeypatch.setenv("WEB_MANAGER_ID", "clear-demo")
    monkeypatch.delenv("HOTEL_OPS_WORKBOOK", raising=False)
    app = AppTest.from_file(APP).run(timeout=40)
    app.segmented_control[0].set_value("PARAMETERS").run(timeout=40)
    assert app.number_input(key="param_room_capacity").value == 292
    app.number_input(key="param_room_capacity").set_value(None).run(timeout=40)
    assert not app.exception
    assert saved_room_capacity(StateStore().get_staff_settings("clear-demo")) is None
    fresh = AppTest.from_file(APP).run(timeout=40)
    assert not fresh.exception
    assert fresh.session_state["parameters"]["room_capacity"] is None


@pytest.mark.parametrize("concurrent_capacity", [None, 300])
def test_confirmed_demo_seed_does_not_overwrite_a_concurrent_explicit_setting(tmp_path, monkeypatch, concurrent_capacity):
    from services.hotel_parameters import saved_room_capacity
    from services.staff_settings import DEMO_SEED_VERSION
    configure_app(tmp_path, monkeypatch)
    StateStore().save_staff_settings("local-manager", demo_defaults(), DEMO_SEED_VERSION, dataset_mode="demo")
    original = StateStore.save_hotel_parameters
    attempts = []
    def racing_seed(self, manager_id, values, expected_revision=None):
        if expected_revision is not None:
            attempts.append(values)
            original(self, manager_id, {"room_capacity": concurrent_capacity})
        return original(self, manager_id, values, expected_revision=expected_revision)
    monkeypatch.setattr(StateStore, "save_hotel_parameters", racing_seed)
    app = AppTest.from_file(APP).run(timeout=40)
    assert not app.exception
    assert len(attempts) == 1 and attempts[0] == {"room_capacity": 292}
    assert saved_room_capacity(StateStore().get_staff_settings("local-manager")) == concurrent_capacity
    assert app.session_state["parameters"]["room_capacity"] == concurrent_capacity
    app.run(timeout=40)
    assert len(attempts) == 1


@pytest.mark.parametrize("action", ["save", "reset"])
def test_staff_save_or_reset_keeps_payload_and_revision_from_the_same_snapshot(tmp_path, monkeypatch, action):
    configure_app(tmp_path, monkeypatch)
    app = AppTest.from_file(APP).run(timeout=40)
    app.segmented_control[0].set_value("STAFF & SETTINGS").run(timeout=40)
    original_save = StateStore.save_staff_settings
    calls = []
    def racing_save(self, manager_id, payload, demo_seed_version=0, dataset_mode="operational", expected_revision=None):
        revision = original_save(self, manager_id, payload, demo_seed_version, dataset_mode, expected_revision)
        if expected_revision is not None:
            calls.append(revision)
            self.save_hotel_parameters(manager_id, {"room_capacity": 294, "checkout_minutes": 29})
        return revision
    monkeypatch.setattr(StateStore, "save_staff_settings", racing_save)
    if action == "save":
        next(item for item in app.number_input if item.label == "Checkout cleaning standard (minutes)").set_value(27)
        next(button for button in app.button if button.label == "Save changes").click().run(timeout=40)
    else:
        next(item for item in app.checkbox if item.label == "I understand reset restores the fictional demo roster and demo assumptions.").check().run(timeout=40)
        next(button for button in app.button if button.label == "Reset demo staff & settings").click().run(timeout=40)
    assert not app.exception
    assert len(calls) == 1
    stored = StateStore().get_staff_settings("local-manager")
    assert stored["settings"]["cleaning_standards"]["checkout_minutes"] == 29
    assert app.session_state["staff_saved"]["settings"] == stored["settings"]
    assert app.session_state["staff_saved_revision"] == stored["_settings_revision"]
    assert app.session_state["staff_editor_base_revision"] == stored["_settings_revision"]
    assert app.session_state["parameters"]["room_capacity"] == 294
    assert app.session_state["parameters"]["checkout_minutes"] == 29


def test_partial_parameter_save_refreshes_other_concurrently_changed_shared_fields(tmp_path, monkeypatch):
    configure_app(tmp_path, monkeypatch)
    app = AppTest.from_file(APP).run(timeout=40)
    app.segmented_control[0].set_value("PARAMETERS").run(timeout=40)
    original_save = StateStore.save_hotel_parameters
    submitted = []
    def racing_parameters(self, manager_id, values, expected_revision=None):
        submitted.append(dict(values))
        original_save(self, manager_id, {"stayover_minutes": 12})
        return original_save(self, manager_id, values, expected_revision=expected_revision)
    monkeypatch.setattr(StateStore, "save_hotel_parameters", racing_parameters)
    app.number_input(key="param_room_capacity").set_value(292).run(timeout=40)
    assert not app.exception
    assert submitted == [{"room_capacity": 292}]
    stored = StateStore().get_staff_settings("local-manager")
    assert stored["settings"]["cleaning_standards"]["stayover_minutes"] == 12
    assert app.session_state["parameters"]["stayover_minutes"] == 12
    assert app.number_input(key="param_stayover_minutes").value == 12
    assert app.session_state["staff_saved_revision"] == stored["_settings_revision"]
