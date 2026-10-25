from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path

import pytest
import pandas as pd
from streamlit.testing.v1 import AppTest

from services.forecast_import import (REVIEW_FIELDS, empty_forecast, extract_forecast_upload, normalize_forecast_rows,
                                      parse_forecast_text, validate_forecast,
                                      review_matrix_to_forecast_rows, forecast_rows_to_review_matrix)
from services.staff_settings import demo_defaults
from services.state_store import StateStore
from services.weekly_roster import build_exports, generate_weekly_roster, schedule_changes


def sample_forecast(start=date(2026, 9, 25)):
    rows = empty_forecast(start)
    values = {
        "departures": [110, 156, 165, 75, 31, 38, 82],
        "stayovers": [44, 126, 59, 39, 89, 75, 68],
        "arrivals": [218, 95, 56, 79, 30, 78, 138],
        "rooms_on_books": [262, 221, 115, 118, 119, 153, 206],
        "forecast_pickup": [30, 25, 20, 20, 25, 20, 20],
        "forecast_occupied": [292, 246, 135, 138, 144, 173, 226],
        "occupancy_pct": [100, 84, 46, 47, 49, 59, 77],
    }
    for index, row in enumerate(rows):
        for field, values_for_field in values.items():
            row[field] = values_for_field[index]
    return rows


def open_forecast_data_view(app):
    app.radio[0].set_value("Extracted forecast data").run(timeout=40)


def test_forecast_totals_and_dates_validate_and_missing_or_malformed_rows_fail():
    rows = sample_forecast()
    assert validate_forecast(rows) == []


def test_hilton_source_labels_and_transpose_keep_every_metric_and_unknown_prior_day():
    rows = sample_forecast()
    text = "FRI SAT SUN MON TUE WED THU\n9/25/2026 9/26/2026 9/27/2026 9/28/2026 9/29/2026 9/30/2026 10/1/2026\nPrior Day Occupancy\n"
    text += "\n".join((
        "Arrivals", "Departures", "Stayovers", "Rooms on the books",
        "Forecasted Pick up", "Forcasted occupied rooms", "Occupancy %",
    )[i] + ": " + " ".join(str(row[field]) for row in rows)
        for i, field in enumerate(("arrivals", "departures", "stayovers", "rooms_on_books", "forecast_pickup", "forecast_occupied", "occupancy_pct")))
    parsed, _ = parse_forecast_text(text)
    assert validate_forecast(parsed) == []
    assert [row["date"] for row in parsed] == [date(2026, 9, 25) + timedelta(days=i) for i in range(7)]
    for field in ("arrivals", "departures", "stayovers", "rooms_on_books", "forecast_pickup", "forecast_occupied", "occupancy_pct"):
        assert [row[field] for row in parsed] == [row[field] for row in rows]
    assert all(row["prior_day_occupancy"] is None for row in parsed)
    matrix = forecast_rows_to_review_matrix(parsed)
    assert [row["date"] for row in matrix] == [row["date"] for row in parsed]
    assert [key for key in matrix[0] if key in REVIEW_FIELDS] == list(REVIEW_FIELDS)
    matrix[2]["arrivals"] = 57
    selected_dates = [date(2026, 9, 25) + timedelta(days=i) for i in range(7)]
    round_trip = review_matrix_to_forecast_rows(matrix, selected_dates, parsed)
    assert round_trip[2]["arrivals"] == 57
    assert round_trip[1]["arrivals"] == 95
    assert round_trip[0]["forecast_pickup"] == 30
    assert round_trip[6]["forecast_occupied"] == 226
    assert round_trip[4]["occupancy_pct"] == 49
    assert round_trip[0]["prior_day_occupancy"] is None
    assert "arrivals + stayovers" in " ".join(validate_forecast(round_trip))
    bad = deepcopy(rows)
    bad[0]["arrivals"] += 1
    errors = validate_forecast(bad)
    assert any("arrivals + stayovers" in error for error in errors)
    bad = deepcopy(rows)
    bad[2]["departures"] = None
    assert any("departures is missing" in error for error in validate_forecast(bad))
    bad = deepcopy(rows)
    bad[0]["date"] = date(2026, 9, 26)
    assert any("first date must be a Friday" in error for error in validate_forecast(bad))


def test_transposed_review_keeps_zero_distinct_from_missing():
    rows = sample_forecast()
    matrix = forecast_rows_to_review_matrix(rows)
    # A genuine zero is a value, not an empty cell; keep the total consistent.
    matrix[0]["forecast_pickup"] = 0
    matrix[0]["forecast_occupied"] = rows[0]["rooms_on_books"]
    matrix[0]["occupancy_pct"] = 0
    dates = [date(2026, 9, 25) + timedelta(days=i) for i in range(7)]
    round_trip = review_matrix_to_forecast_rows(matrix, dates, rows)
    assert round_trip[0]["forecast_pickup"] == 0
    assert round_trip[0]["occupancy_pct"] == 0
    assert validate_forecast(round_trip) == []

    matrix[0]["departures"] = None
    missing = review_matrix_to_forecast_rows(matrix, dates, rows)
    assert missing[0]["departures"] is None
    assert any("departures is missing" in error for error in validate_forecast(missing))


def test_text_extraction_is_review_only_and_supports_source_forecast_shape():
    dates = "Dates: 2026-09-25 2026-09-26 2026-09-27 2026-09-28 2026-09-29 2026-09-30 2026-10-01"
    body = [dates]
    source = sample_forecast()
    for field in ("departures", "stayovers", "arrivals", "rooms_on_books", "forecast_pickup", "forecast_occupied", "occupancy_pct"):
        label = field.replace("_", " ")
        values = " ".join(str(row[field]) for row in source)
        body.append(f"{label}: {values}")
    rows, notices = parse_forecast_text("\n".join(body))
    assert len(rows) == 7
    assert rows[0]["date"] == date(2026, 9, 25)
    assert rows[6]["forecast_occupied"] == 226
    assert any("unconfirmed" in notice.lower() for notice in notices)
    assert validate_forecast(rows) == []


def test_default_demo_availability_generates_covered_fri_thu_roster():
    result = generate_weekly_roster(sample_forecast(), demo_defaults())
    assert result["feasible"] is True
    assert len(result["schedule"]) == 168
    assert result["constraints"] == []
    for forecast_row in result["forecast"]:
        day = [row for row in result["schedule"] if row["date"] == forecast_row["date"]]
        for role in ("Housekeeper", "Houseman", "Lobby Staff", "Manager"):
            assert sum(row["role"] == role and row["code"] == "SHIFT" for row in day) >= 1
        hk = [row for row in day if row["role"] == "Housekeeper" and row["code"] == "SHIFT"]
        demand = result["demand_minutes"][forecast_row["date"]]
        assert sum(row["productive_hours"] * 60 for row in hk) >= demand
    assert all(hours <= 40 for hours in result["hours_by_staff"].values())


def test_schedule_is_shift_roster_not_room_assignment_and_paid_productive_are_distinct():
    result = generate_weekly_roster(sample_forecast(), demo_defaults())
    row = next(item for item in result["schedule"] if item["code"] == "SHIFT")
    assert "room" not in row and "room_number" not in row
    assert row["paid_hours"] == 8
    assert row["productive_hours"] == 6.5


def test_locked_manual_rows_survive_regeneration_and_changes_are_previewable():
    payload = demo_defaults()
    forecast = sample_forecast()
    first = generate_weekly_roster(forecast, payload)
    locked = next(r for r in first["schedule"] if r["date"] == "2026-09-26" and r["role"] == "Housekeeper" and r["code"] == "SHIFT")
    edit = dict(locked, code="OFF", start="", end="", paid_hours=0, productive_hours=0, locked=True, notes="Manager locked day off")
    prior_schedule = [edit if r["staff_id"] == locked["staff_id"] and r["date"] == locked["date"] else r for r in first["schedule"]]
    updated = generate_weekly_roster(forecast, payload, prior={"schedule": prior_schedule, "version": 1})
    kept = next(r for r in updated["schedule"] if r["staff_id"] == edit["staff_id"] and r["date"] == edit["date"])
    assert kept["code"] == "OFF" and kept["locked"] is True
    assert "Manager locked day off" in kept["notes"]
    assert schedule_changes(first, updated)
    assert "uncovered" in " ".join(updated["constraints"])


def test_missing_availability_and_unavailable_staff_produce_uncovered_demand_not_false_feasibility():
    staff = demo_defaults()
    for person in staff["staff"]:
        if person["role"] == "Housekeeper":
            person["requested_time_off"] = ", ".join((date(2026, 9, 25) + timedelta(days=i)).isoformat() for i in range(7))
    result = generate_weekly_roster(sample_forecast(), staff)
    assert result["feasible"] is False
    assert any("uncovered" in item or "minimum coverage" in item for item in result["constraints"])


def test_weekly_roster_roundtrips_and_preserves_locked_rows_in_sqlite(tmp_path):
    db = tmp_path / "rosters.sqlite3"
    store = StateStore(db)
    result = generate_weekly_roster(sample_forecast(), demo_defaults())
    store.save_weekly_roster("manager", result["week_start"], result)
    reopened = StateStore(db).get_weekly_roster("manager", result["week_start"])
    assert reopened["version"] == 1
    assert reopened["forecast"] == result["forecast"]
    assert reopened["schedule"] == result["schedule"]
    assert reopened["feasible"] is True


def test_excel_and_pdf_exports_are_created_and_labeled_as_demo():
    roster = generate_weekly_roster(sample_forecast(), demo_defaults())
    excel, pdf = build_exports(roster)
    assert excel[:2] == b"PK"
    assert pdf.startswith(b"%PDF-")
    from io import BytesIO
    from pypdf import PdfReader
    extracted = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)
    assert "Weekly Employee Shift Roster" in extracted
    assert "DEMO ASSUMPTIONS" in extracted
    assert {"Forecast", "Weekly Shift Roster", "Fri-Thu Grid", "Validation"}.issubset(set(pd.ExcelFile(BytesIO(excel)).sheet_names))
    for role in ("Housekeeper", "Houseman", "Lobby Staff", "Manager"):
        assert role in extracted


def _forecast_pdf_bytes():
    from io import BytesIO
    from reportlab.pdfgen import canvas
    output = BytesIO()
    doc = canvas.Canvas(output)
    for line in [
        "Dates: 2026-09-25 2026-09-26 2026-09-27 2026-09-28 2026-09-29 2026-09-30 2026-10-01",
        *[f"{field.replace('_', ' ')}: " + " ".join(str(row[field]) for row in sample_forecast())
          for field in ("departures", "stayovers", "arrivals", "rooms_on_books", "forecast_pickup", "forecast_occupied", "occupancy_pct")],
    ]:
        doc.drawString(24, 760, line)
        doc.showPage()
    doc.save()
    return output.getvalue()


def test_native_text_pdf_upload_extracts_locally_for_review():
    result = extract_forecast_upload("forecast.pdf", _forecast_pdf_bytes())
    assert result["method"] == "PDF text extraction"
    assert result["rows"][0]["departures"] == 110
    assert result["rows"][6]["forecast_occupied"] == 226
    assert validate_forecast(result["rows"]) == []
    assert any("locally" in item for item in result["notices"])


def test_image_and_scanned_pdf_require_local_tesseract_not_an_external_ocr(monkeypatch):
    from io import BytesIO
    from PIL import Image
    from reportlab.pdfgen import canvas
    import pytesseract
    image_bytes = BytesIO()
    Image.new("RGB", (40, 20), "white").save(image_bytes, format="PNG")
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *_args, **_kwargs: (_ for _ in ()).throw(pytesseract.TesseractNotFoundError()))
    with pytest.raises(ValueError, match="Local Tesseract OCR is not installed"):
        extract_forecast_upload("forecast.png", image_bytes.getvalue())
    pdf_out = BytesIO(); pdf = canvas.Canvas(pdf_out)
    pdf.drawString(20, 760, "image-only scan placeholder"); pdf.showPage(); pdf.save()
    # Redact embedded text to exercise the scanned-PDF route (render one image page with no selectable layer).
    import pymupdf
    image_doc = pymupdf.open()
    page = image_doc.new_page(width=80, height=40)
    pix = page.get_pixmap()
    page.insert_image(page.rect, stream=pix.tobytes("png"))
    scanned = image_doc.tobytes()
    with pytest.raises(ValueError, match="Local Tesseract OCR is not installed"):
        extract_forecast_upload("scan.pdf", scanned)


def test_image_and_scanned_pdf_ocr_pipeline_parses_locally_with_tesseract_adapter(monkeypatch):
    from io import BytesIO
    from PIL import Image
    import pymupdf
    import pytesseract
    text = "Dates: 2026-09-25 2026-09-26 2026-09-27 2026-09-28 2026-09-29 2026-09-30 2026-10-01\n" + "\n".join(
        f"{field.replace('_',' ')}: " + " ".join(str(row[field]) for row in sample_forecast())
        for field in ("departures", "stayovers", "arrivals", "rooms_on_books", "forecast_pickup", "forecast_occupied", "occupancy_pct"))
    monkeypatch.setattr(pytesseract, "image_to_string", lambda *_args, **_kwargs: text)
    image_bytes = BytesIO(); Image.new("RGB", (40, 20), "white").save(image_bytes, format="PNG")
    image_result = extract_forecast_upload("forecast.png", image_bytes.getvalue())
    assert image_result["method"] == "Local OCR (Tesseract)"
    assert validate_forecast(image_result["rows"]) == []
    doc = pymupdf.open(); page = doc.new_page(width=80, height=40)
    pix = page.get_pixmap(); page.insert_image(page.rect, stream=pix.tobytes("png"))
    pdf_result = extract_forecast_upload("scan.pdf", doc.tobytes())
    assert pdf_result["method"] == "Local OCR (Tesseract)"
    assert pdf_result["rows"][6]["forecast_occupied"] == 226


def test_state_store_rejects_mismatched_roster_key(tmp_path):
    store = StateStore(tmp_path / "bad.sqlite3")
    result = generate_weekly_roster(sample_forecast(), demo_defaults())
    with pytest.raises(ValueError, match="week start"):
        store.save_weekly_roster("manager", "2026-09-26", result)


def test_upload_page_is_available_and_cancel_preserves_saved_roster(tmp_path, monkeypatch):
    db = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("COPILOT_STATE_DB", str(db))
    monkeypatch.setenv("WEB_MANAGER_ID", "weekly-ui")
    stored = generate_weekly_roster(sample_forecast(), demo_defaults())
    StateStore(db).save_weekly_roster("weekly-ui", stored["week_start"], stored)
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run(timeout=40)
    app.segmented_control[0].set_value("WEEKLY FORECAST").run(timeout=40)
    assert any("Weekly Forecast" in item.value for item in app.subheader)
    assert any("PUBLIC DEMO ONLY" in item.value for item in app.warning)
    assert not app.exception
    open_forecast_data_view(app)
    next(button for button in app.button if button.label == "Cancel / clear upload and review").click().run(timeout=40)
    reopened = StateStore(db).get_weekly_roster("weekly-ui", "2026-09-25")
    assert reopened["schedule"] == stored["schedule"]
    assert not app.exception


def test_ui_generate_and_save_weekly_roster_persists_confirmed_forecast(tmp_path, monkeypatch):
    db = tmp_path / "ui-generated.sqlite3"
    monkeypatch.setenv("COPILOT_STATE_DB", str(db))
    monkeypatch.setenv("WEB_MANAGER_ID", "weekly-ui-generation")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run(timeout=40)
    app.session_state["forecast_confirmed"] = normalize_forecast_rows(sample_forecast())
    app.segmented_control[0].set_value("WEEKLY FORECAST").run(timeout=40)
    next(button for button in app.button if button.label == "Generate / update weekly roster").click().run(timeout=40)
    assert not app.exception
    assert app.session_state["weekly_candidate"]["feasible"] is True
    next(item for item in app.checkbox if "reviewed the displayed changes" in item.label).check().run(timeout=40)
    next(button for button in app.button if button.label == "Save reviewed roster version").click().run(timeout=40)
    saved = StateStore(db).get_weekly_roster("weekly-ui-generation", "2026-09-25")
    assert saved["version"] == 1
    assert saved["forecast"][0]["departures"] == 110
    assert saved["forecast"][0]["forecast_pickup"] == 30
    assert saved["forecast"][0]["forecast_occupied"] == 292
    assert saved["forecast"][0]["occupancy_pct"] == 100
    assert saved["feasible"] is True
    assert not app.exception


def test_ui_roster_manual_edit_is_locked_when_rebuilding_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_STATE_DB", str(tmp_path / "ui-lock.sqlite3"))
    monkeypatch.setenv("WEB_MANAGER_ID", "weekly-ui-lock")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run(timeout=40)
    app.session_state["forecast_confirmed"] = normalize_forecast_rows(sample_forecast())
    app.segmented_control[0].set_value("WEEKLY FORECAST").run(timeout=40)
    next(button for button in app.button if button.label == "Generate / update weekly roster").click().run(timeout=40)
    before = app.session_state["weekly_candidate"]
    target = next(row for row in before["schedule"] if row["date"] == "2026-09-26" and row["role"] == "Housekeeper" and row["code"] == "SHIFT")
    row_index = before["schedule"].index(target)
    app.session_state["weekly_roster_table_1"] = {"edited_rows": {row_index: {"code": "OFF", "start": "", "end": ""}}, "added_rows": [], "deleted_rows": []}
    next(button for button in app.button if button.label == "Review roster edits and update preview").click().run(timeout=40)
    after = app.session_state["weekly_candidate"]
    preserved = next(row for row in after["schedule"] if row["staff_id"] == target["staff_id"] and row["date"] == target["date"])
    assert preserved["code"] == "OFF" and preserved["locked"] is True
    assert not app.exception
    assert after["feasible"] is False
    assert any("uncovered" in item for item in after["constraints"])


def test_ui_repeat_upload_after_cancel_reprocesses_same_native_pdf(tmp_path, monkeypatch):
    db = tmp_path / "upload-ui.sqlite3"
    monkeypatch.setenv("COPILOT_STATE_DB", str(db))
    monkeypatch.setenv("WEB_MANAGER_ID", "weekly-upload-repeat")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run(timeout=40)
    app.segmented_control[0].set_value("WEEKLY FORECAST").run(timeout=40)
    payload = _forecast_pdf_bytes()
    app.file_uploader[0].upload("forecast.pdf", payload, "application/pdf").run(timeout=40)
    assert app.session_state["forecast_review_rows"][0]["departures"] == 110
    open_forecast_data_view(app)
    next(button for button in app.button if button.label == "Cancel / clear upload and review").click().run(timeout=40)
    assert app.session_state["forecast_review_rows"] is None
    app.file_uploader[0].upload("forecast.pdf", payload, "application/pdf").run(timeout=40)
    assert app.session_state["forecast_review_rows"][0]["departures"] == 110
    assert not app.exception


def test_ui_invalid_blank_manual_forecast_is_rejected_before_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_STATE_DB", str(tmp_path / "invalid-ui.sqlite3"))
    monkeypatch.setenv("WEB_MANAGER_ID", "weekly-invalid-ui")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run(timeout=40)
    app.segmented_control[0].set_value("WEEKLY FORECAST").run(timeout=40)
    open_forecast_data_view(app)
    next(button for button in app.button if button.label == "Enter forecast manually").click().run(timeout=40)
    assert list(app.dataframe[0].value.columns) == ["_day_slot", "date", *REVIEW_FIELDS]
    assert app.session_state["weekly_candidate"] is None
    assert not app.exception
    next(button for button in app.button if button.label == "Validate and confirm forecast").click().run(timeout=40)
    assert app.session_state["forecast_confirmed"] is None
    assert any("is missing" in item.value for item in app.error)
    assert not app.exception


def test_existing_today_housekeeping_and_liquor_views_still_render(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_STATE_DB", str(tmp_path / "existing-views.sqlite3"))
    monkeypatch.setenv("WEB_MANAGER_ID", "existing-views-smoke")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run(timeout=40)
    assert app.segmented_control[0].value == "WEEKLY FORECAST"
    assert any("Weekly Forecast" in item.value for item in app.subheader)
    app.segmented_control[0].set_value("TODAY").run(timeout=40)
    assert any("Today's overview" in item.value for item in app.subheader)
    app.segmented_control[0].set_value("OPERATIONS").run(timeout=40)
    assert any("Operations" in item.value for item in app.subheader)
    assert any("Housekeeping" in item.label for item in app.tabs)
    assert any("Bar & inventory" in item.label for item in app.tabs)
    assert not app.exception


def test_uploaded_forecast_requires_and_passes_explicit_review_before_generation(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_STATE_DB", str(tmp_path / "confirmed-upload.sqlite3"))
    monkeypatch.setenv("WEB_MANAGER_ID", "weekly-confirm-upload")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run(timeout=40)
    app.segmented_control[0].set_value("WEEKLY FORECAST").run(timeout=40)
    app.file_uploader[0].upload("forecast.pdf", _forecast_pdf_bytes(), "application/pdf").run(timeout=40)
    assert app.session_state["forecast_confirmed"] is None
    assert app.session_state["weekly_candidate"] is not None
    assert app.session_state["weekly_candidate_is_draft"] is True
    assert not any("is missing" in error for error in app.error)
    save_button = next(button for button in app.button if button.label == "Save reviewed roster version")
    assert save_button.disabled is True
    open_forecast_data_view(app)
    editor_columns = list(app.dataframe[0].value.columns)
    assert editor_columns == ["_day_slot", "date", *REVIEW_FIELDS]
    next(button for button in app.button if button.label == "Validate and confirm forecast").click().run(timeout=40)
    assert app.session_state["forecast_confirmed"][0]["date"] == "2026-09-25"
    assert app.session_state["forecast_confirmed"][0]["departures"] == 110
    assert app.session_state["forecast_confirmed"][0]["forecast_pickup"] == 30
    assert app.session_state["forecast_confirmed"][0]["forecast_occupied"] == 292
    assert app.session_state["forecast_confirmed"][0]["occupancy_pct"] == 100
    assert any("Confirmed forecast" in item.value for item in app.success)
    app.radio[0].set_value("Weekly roster").run(timeout=40)
    next(button for button in app.button if button.label == "Generate / update weekly roster").click().run(timeout=40)
    assert app.session_state["weekly_candidate"]["feasible"] is True
    assert len(app.session_state["weekly_candidate"]["schedule"]) == 168
    assert not app.exception


def test_weekly_forecast_is_default_and_data_button_returns_to_roster(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_STATE_DB", str(tmp_path / "weekly-first.sqlite3"))
    monkeypatch.setenv("WEB_MANAGER_ID", "weekly-first")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run(timeout=40)
    assert app.segmented_control[0].value == "WEEKLY FORECAST"
    assert any("Weekly Forecast" in item.value for item in app.subheader)
    app.file_uploader[0].upload("forecast.pdf", _forecast_pdf_bytes(), "application/pdf").run(timeout=40)
    assert app.session_state["weekly_candidate_is_draft"] is True
    next(button for button in app.button if button.label == "View extracted forecast data").click().run(timeout=40)
    assert app.radio[0].value == "Extracted forecast data"
    assert list(app.dataframe[0].value.columns) == ["_day_slot", "date", *REVIEW_FIELDS]
    next(button for button in app.button if button.label == "Back to Weekly Forecast").click().run(timeout=40)
    assert app.radio[0].value == "Weekly roster"
    assert app.session_state["weekly_candidate_is_draft"] is True
    assert not app.exception


def test_uncertain_ocr_forecast_is_flagged_before_any_roster_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_STATE_DB", str(tmp_path / "uncertain-ocr.sqlite3"))
    monkeypatch.setenv("WEB_MANAGER_ID", "uncertain-ocr")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run(timeout=40)
    app.session_state["forecast_review_rows"] = sample_forecast()
    app.session_state["forecast_extraction_method"] = "Local OCR (Tesseract)"
    app.session_state["forecast_review_notices"] = ["OCR may misread figures; verify every cell."]
    app.run(timeout=40)
    assert app.session_state["weekly_candidate"] is None
    assert any("needs a person’s review" in item.value for item in app.warning)
    assert not app.exception


def test_ui_manual_entry_can_confirm_reviewed_values(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_STATE_DB", str(tmp_path / "manual-ui.sqlite3"))
    monkeypatch.setenv("WEB_MANAGER_ID", "weekly-manual-ui")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run(timeout=40)
    app.segmented_control[0].set_value("WEEKLY FORECAST").run(timeout=40)
    open_forecast_data_view(app)
    next(item for item in app.date_input if item.label.startswith("Manual forecast" )).set_value(date(2026, 9, 25)).run(timeout=40)
    next(button for button in app.button if button.label == "Enter forecast manually").click().run(timeout=40)
    values = {field: [row[field] for row in sample_forecast()] for field in sample_forecast()[0] if field not in {"date", "prior_day_occupancy"}}
    app.session_state["forecast_review_table_0"] = {"edited_rows": {
        i: {field: seq[i] for field, seq in values.items()} for i in range(7)
    }, "added_rows": [], "deleted_rows": []}
    next(button for button in app.button if button.label == "Validate and confirm forecast").click().run(timeout=40)
    assert app.session_state["forecast_confirmed"][0]["arrivals"] == 218
    assert not app.exception


def test_date_row_review_edits_follow_stable_slots_after_date_correction(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_STATE_DB", str(tmp_path / "transposed-review.sqlite3"))
    monkeypatch.setenv("WEB_MANAGER_ID", "transposed-review")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run(timeout=40)
    app.session_state["forecast_review_rows"] = sample_forecast()
    app.segmented_control[0].set_value("WEEKLY FORECAST").run(timeout=40)
    open_forecast_data_view(app)
    app.session_state["forecast_review_table_0"] = {
        "edited_rows": {0: {"date": date(2026, 9, 26), "arrivals": 219, "rooms_on_books": 263, "forecast_occupied": 293},
                         6: {"occupancy_pct": 77.5}},
        "added_rows": [], "deleted_rows": [],
    }
    next(button for button in app.button if button.label == "Validate and confirm forecast").click().run(timeout=40)
    assert app.session_state["forecast_confirmed"] is None
    assert app.session_state["forecast_review_rows"][0]["date"] == date(2026, 9, 26)
    assert app.session_state["forecast_review_rows"][0]["arrivals"] == 219
    assert app.session_state["forecast_review_rows"][0]["rooms_on_books"] == 263
    assert app.session_state["forecast_review_rows"][0]["forecast_occupied"] == 293
    assert app.session_state["forecast_review_rows"][6]["occupancy_pct"] == 77.5
    assert any("first date must be a Friday" in item.value for item in app.error)
    app.session_state["forecast_review_table_0"] = {
        "edited_rows": {0: {"date": date(2026, 9, 25)}}, "added_rows": [], "deleted_rows": [],
    }
    next(button for button in app.button if button.label == "Validate and confirm forecast").click().run(timeout=40)
    assert app.session_state["forecast_confirmed"][0]["arrivals"] == 219
    assert app.session_state["forecast_confirmed"][0]["rooms_on_books"] == 263
    assert app.session_state["forecast_confirmed"][0]["forecast_occupied"] == 293
    assert app.session_state["forecast_confirmed"][6]["occupancy_pct"] == 77.5
    assert not app.exception


def test_repeated_upload_parsing_returns_independent_reviewable_data():
    text = "Dates: 2026-09-25 2026-09-26 2026-09-27 2026-09-28 2026-09-29 2026-09-30 2026-10-01\n" + "\n".join(
        f"{field.replace('_',' ')}: " + " ".join(str(row[field]) for row in sample_forecast())
        for field in ("departures", "stayovers", "arrivals", "rooms_on_books", "forecast_pickup", "forecast_occupied", "occupancy_pct"))
    first = parse_forecast_text(text)
    second = parse_forecast_text(text)
    assert first == second
    first[0][0]["departures"] = 999
    assert second[0][0]["departures"] == 110
