from __future__ import annotations

from pathlib import Path

import pytest

from scripts.forecast_format_samples import build_forecast_variants
from services.forecast_import import (
    extract_forecast_upload,
    extraction_requires_review_before_generation,
    forecast_rows_to_review_matrix,
    normalize_forecast_rows,
    review_matrix_to_forecast_rows,
    validate_forecast,
)
from services.staff_settings import demo_defaults
from services.state_store import StateStore
from services.weekly_roster import generate_weekly_roster


@pytest.fixture(scope="module")
def variant_pack(tmp_path_factory):
    root = tmp_path_factory.mktemp("synthetic-forecast-variants")
    manifest = build_forecast_variants(root)
    return root, manifest


@pytest.mark.parametrize("case_index", range(6))
def test_positive_file_variants_match_every_metric_by_date_through_schedule_and_storage(variant_pack, tmp_path, case_index):
    root, manifest = variant_pack
    case = manifest["cases"][case_index]
    result = extract_forecast_upload(case["file"], (root / case["file"]).read_bytes())
    expected = {row["date"]: row for row in case["expected_by_date"]}
    rows = normalize_forecast_rows(result["rows"])
    assert validate_forecast(rows) == [], (case["file"], result["notices"])
    assert [row["date"] for row in rows] == manifest["dates"]

    # Compare every source cell to its expected date + field, including zeros
    # and fractional percentage points; do not compare only flat value lists.
    for row in rows:
        for field in manifest["metrics"]:
            assert row[field] == expected[row["date"]][field], (case["file"], row["date"], field, row[field], expected[row["date"]][field])

    matrix = forecast_rows_to_review_matrix(rows)
    dates = [row["date"] for row in rows]
    # Date and metrics stay on the same row through sorting/reordering.
    # Adjust dependent totals together; retain the source occupancy value.
    matrix[2]["arrivals"] += 1  # Sunday
    matrix[2]["rooms_on_books"] += 1
    matrix[2]["forecast_occupied"] += 1
    matrix = list(reversed(matrix))
    round_trip = normalize_forecast_rows(review_matrix_to_forecast_rows(matrix, dates, rows))
    assert round_trip[2]["date"] == "2026-09-27"
    assert round_trip[2]["arrivals"] == expected["2026-09-27"]["arrivals"] + 1
    assert round_trip[2]["rooms_on_books"] == expected["2026-09-27"]["rooms_on_books"] + 1
    assert round_trip[2]["forecast_occupied"] == expected["2026-09-27"]["forecast_occupied"] + 1
    assert round_trip[2]["occupancy_pct"] == expected["2026-09-27"]["occupancy_pct"]
    assert validate_forecast(round_trip) == []

    roster = generate_weekly_roster(round_trip, demo_defaults())
    assert roster["forecast"] == round_trip
    store = StateStore(tmp_path / f"{case_index}.sqlite3")
    store.save_weekly_roster("synthetic-review", roster["week_start"], roster)
    reopened = StateStore(tmp_path / f"{case_index}.sqlite3").get_weekly_roster("synthetic-review", roster["week_start"])
    assert reopened["forecast"] == round_trip
    assert len(reopened["schedule"]) == 24 * 7


@pytest.mark.parametrize("case_index", range(6, 11))
def test_negative_file_variants_are_flagged_without_guessing(variant_pack, case_index):
    root, manifest = variant_pack
    case = manifest["cases"][case_index]
    result = extract_forecast_upload(case["file"], (root / case["file"]).read_bytes())
    errors = validate_forecast(result["rows"])
    notices = " ".join(result["notices"]).casefold()
    assert errors or case["name"].endswith("invalid total"), (case["file"], result)

    if case["name"].endswith("ambiguous numeric dates"):
        assert "ambiguous numeric date" in notices
        assert all(row["date"] is None for row in result["rows"])
    elif case["name"].endswith("duplicate date"):
        assert "duplicate dates" in notices
        assert all(row["date"] is None for row in result["rows"])
    elif case["name"].endswith("missing date"):
        assert "found 6" in notices
        assert all(row["date"] is None for row in result["rows"])
    elif case["name"].endswith("invalid total"):
        assert any("arrivals + stayovers must equal rooms on books" in error for error in errors)
        assert any("rooms on books + forecast pickup" in error for error in errors)
    elif case["name"].endswith("missing metric cell"):
        assert any("departures is missing" in error for error in errors)
        assert "could not confidently extract all seven departures values" in notices


def test_manifest_maps_every_positive_value_and_explicitly_marks_negative_expectations(variant_pack):
    _, manifest = variant_pack
    assert manifest["positive_cases"] == 6
    assert manifest["negative_cases"] == 5
    positive = manifest["cases"][:6]
    assert all(len(case["expected_by_date"]) == 7 for case in positive)
    assert all(set(manifest["metrics"]).issubset(case["expected_by_date"][0]) for case in positive)
    assert any(row["forecast_pickup"] == 0 for row in positive[0]["expected_by_date"])
    assert any(row["occupancy_pct"] == 0 for row in positive[0]["expected_by_date"])
    assert any(row.get("expected_not_assigned_due_ambiguous_dates") for row in manifest["cases"][6]["expected_by_date"])


def test_uncertain_ocr_and_native_extraction_notices_block_drafts_but_clear_native_text_can_draft():
    assert extraction_requires_review_before_generation("Local OCR (Tesseract)", [])
    assert extraction_requires_review_before_generation("PDF text extraction", ["Ambiguous numeric date found."])
    assert extraction_requires_review_before_generation("PDF text extraction", ["Local OCR layout modes disagree."])
    assert not extraction_requires_review_before_generation(
        "PDF text extraction", ["Extracted values are unconfirmed. Review every date and count before generating a roster."]
    )
