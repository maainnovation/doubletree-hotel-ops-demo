"""Local manual-OCR recovery checks: no character/date repair or auto approval."""
from types import SimpleNamespace

import pytest
from PIL import Image

from scripts.forecast_format_samples import BASE, _native_text
from services.forecast_import import (
    NUMERIC_FIELDS,
    _extract_with_local_ocr,
    extract_forecast_upload,
    extraction_requires_review_before_generation,
    normalize_forecast_rows,
)

FULL = _native_text(BASE, list(range(7)), "iso", "standard", "percent")
PARTIAL = FULL.replace("Occupancy %: 0%", "Occupancy %: O%")


def mocked_ocr(texts):
    calls = []

    def image_to_string(image, *, config, timeout):
        calls.append((image.size, config, timeout))
        result = texts[min(len(calls) - 1, len(texts) - 1)]
        if isinstance(result, Exception):
            raise result
        return result

    return SimpleNamespace(image_to_string=image_to_string,
                           TesseractNotFoundError=type("MissingOCR", (Exception,), {}),
                           TesseractError=type("BrokenOCR", (Exception,), {})), calls


def run(texts):
    adapter, calls = mocked_ocr(texts)
    with Image.new("RGB", (40, 30), "white") as image:
        rows, notices = _extract_with_local_ocr(image, adapter)
    return rows, notices, calls


def test_agreeing_same_source_dates_only_fill_missing_cells_and_stay_review_only():
    rows, notices, calls = run([PARTIAL, FULL])
    assert normalize_forecast_rows(rows) == normalize_forecast_rows(BASE)
    assert len(calls) == 2
    assert all(call[2] == 15 for call in calls)
    assert any("Blank cells were recovered" in notice for notice in notices)
    assert extraction_requires_review_before_generation("Local OCR (Tesseract)", notices)
    assert all(row["prior_day_occupancy"] is None for row in rows)


@pytest.mark.parametrize("conflicting", [
    FULL.replace("Arrivals: 0 |", "Arrivals: 99 |"),
    PARTIAL.replace("Departures: 0 | 12 |", "Departures: 0 | 99 |"),
])
def test_conflicting_full_or_partial_pass_never_overwrites_or_fills_by_confidence(conflicting):
    rows, notices, calls = run([PARTIAL, conflicting, FULL, FULL])
    assert len(calls) == 4
    assert rows[0]["arrivals"] == 0
    assert rows[1]["departures"] == 12
    assert all(row["occupancy_pct"] is None for row in rows)
    assert any("layout modes disagree" in notice for notice in notices)
    assert not any("Blank cells were recovered" in notice for notice in notices)
    assert extraction_requires_review_before_generation("Local OCR (Tesseract)", notices)


def test_different_printed_date_order_cannot_supply_missing_cells_even_if_sorted_values_agree():
    reordered = _native_text(BASE, [1, 0, 2, 3, 4, 5, 6], "iso", "standard", "percent")
    rows, notices, calls = run([PARTIAL, reordered, FULL, FULL])
    assert all(row["occupancy_pct"] is None for row in rows)
    assert len(calls) == 4
    assert any("layout modes disagree" in notice for notice in notices)


def test_missing_ocr_zero_is_not_repaired_or_guessed_from_other_counts():
    rows, notices, calls = run([PARTIAL])
    assert len(calls) == 4
    assert all(row["occupancy_pct"] is None for row in rows)
    assert rows[0]["forecast_occupied"] == 0
    assert any("occupancy pct" in notice for notice in notices)
    assert calls[2][0] == calls[3][0] == (60, 45)


def test_bad_ocr_date_is_not_repaired_or_inferred_from_the_week():
    bad = FULL.replace("2026-09-27", "2026-09-2T")
    rows, notices, calls = run([bad])
    assert len(calls) == 4
    assert all(row["date"] is None for row in rows)
    assert all(row[field] is None for row in rows for field in NUMERIC_FIELDS)
    assert any("seven distinct full dates" in notice for notice in notices)


def test_all_timeouts_have_a_fixed_call_bound_and_no_candidate():
    adapter, calls = mocked_ocr([RuntimeError("Tesseract process timeout")])
    with Image.new("RGB", (40, 30), "white") as image:
        with pytest.raises(ValueError, match="time limit"):
            _extract_with_local_ocr(image, adapter)
    assert len(calls) == 4
    assert all(call[2] == 15 for call in calls)


def test_complete_first_read_does_not_run_unnecessary_fallbacks_but_is_still_ocr():
    rows, notices, calls = run([FULL])
    assert len(calls) == 1
    assert normalize_forecast_rows(rows) == normalize_forecast_rows(BASE)
    assert extraction_requires_review_before_generation("Local OCR (Tesseract)", notices)


def test_oversized_scanned_pdf_page_is_rejected_before_rendering(monkeypatch):
    import pymupdf
    with pymupdf.open() as document:
        document.new_page(width=10000, height=10000)
        content = document.tobytes()
    monkeypatch.setattr(pymupdf.Page, "get_pixmap", lambda *args, **kwargs: pytest.fail("Oversized page must not be rendered"))
    with pytest.raises(ValueError, match="40 megapixel"):
        extract_forecast_upload("synthetic-large-page.pdf", content)


def test_later_disagreement_withdraws_earlier_supplemental_cells_instead_of_choosing():
    original = PARTIAL.replace("Departures: 0 |", "Departures: O |")
    conflicting = FULL.replace("Departures: 0 | 12 |", "Departures: 0 | 99 |")
    rows, notices, calls = run([original, PARTIAL, conflicting, FULL])
    assert len(calls) == 4
    assert all(row["departures"] is None for row in rows)
    assert all(row["occupancy_pct"] is None for row in rows)
    assert any("supplemental cells were withdrawn" in notice for notice in notices)
    assert not any("Blank cells were recovered" in notice for notice in notices)
