from datetime import date
import json
from pathlib import Path
import shutil

import httpx
import pytest

from services.forecast_ai import (
    FORECAST_SCHEMA, ai_vision_available, compare_forecast_extractions, comparison_to_review_matrix,
    extract_forecast_with_ai, fill_missing_from_ai, is_loopback_address,
)
from services.forecast_import import empty_forecast, validate_forecast


def sample_rows():
    rows = empty_forecast(date(2026, 9, 25))
    values = {
        "departures": [110, 156, 165, 75, 31, 38, 82], "stayovers": [44, 126, 59, 39, 89, 75, 68],
        "arrivals": [218, 95, 56, 79, 30, 78, 138], "rooms_on_books": [262, 221, 115, 118, 119, 153, 206],
        "forecast_pickup": [30, 25, 20, 20, 25, 20, 20], "forecast_occupied": [292, 246, 135, 138, 144, 173, 226],
        "occupancy_pct": [100, 84, 46, 47, 49, 59, 77],
    }
    for i, row in enumerate(rows):
        for field, values_by_day in values.items(): row[field] = values_by_day[i]
    return rows


def structured(rows=None):
    rows = rows or sample_rows()
    payload = []
    for row in rows:
        payload.append({field: {"value": row[field].isoformat() if isinstance(row[field], date) else row[field],
                                "evidence": str(row[field]), "confidence": "clear"}
                        for field in row if field != "prior_day_occupancy"})
    return {"rows": payload}


class MockResponse:
    def __init__(self, body, status=200): self.body, self.status = body, status
    def raise_for_status(self):
        if self.status >= 400: raise httpx.HTTPStatusError("error", request=httpx.Request("POST", "https://example.test"), response=httpx.Response(self.status))
    def json(self): return self.body


class MockClient:
    def __init__(self, response=None, error=None): self.response, self.error, self.payload = response, error, None
    def post(self, url, *, headers, json):
        self.payload = json
        if self.error: raise self.error
        return MockResponse(self.response)


def test_private_ai_guard_is_explicitly_allowlisted(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("FORECAST_AI_PRIVATE_ENABLED", "1")
    monkeypatch.setenv("FORECAST_AI_ALLOWED_MANAGER_ID", "private-manager")
    assert not ai_vision_available("private-manager")
    assert ai_vision_available("private-manager", is_localhost=True)
    assert not ai_vision_available("public-session", is_localhost=True)
    assert not ai_vision_available("private-manager", is_localhost=False)
    assert is_loopback_address("127.0.0.1") and is_loopback_address("::1")
    assert not is_loopback_address("203.0.113.4") and not is_loopback_address(None)
    monkeypatch.setenv("FORECAST_AI_PRIVATE_ENABLED", "0")
    assert not ai_vision_available("private-manager", is_localhost=True)


def test_strict_ai_request_extracts_rows_with_per_cell_evidence(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from PIL import Image
    from io import BytesIO
    image = BytesIO(); Image.new("RGB", (100, 50), "white").save(image, "PNG")
    client = MockClient({"status": "completed", "output": [{"content": [{"type": "output_text", "text": json.dumps(structured())}]}], "usage": {"input_tokens": 10, "output_tokens": 20}})
    result = extract_forecast_with_ai("fake.png", image.getvalue(), client=client)
    assert result["rows"][0]["date"] == date(2026, 9, 25)
    assert result["rows"][0]["departures"] == 110
    assert result["evidence"][0]["departures"]["evidence"] == "110"
    assert result["validation_errors"] == []
    assert client.payload["store"] is False
    assert client.payload["text"]["format"]["schema"] == FORECAST_SCHEMA
    assert "data:image/jpeg;base64," in client.payload["input"][0]["content"][1]["image_url"]


def test_compare_surfaces_conflicts_agreement_missing_and_uncertainty(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    local = sample_rows()
    ai = structured()
    ai["rows"][0]["departures"]["value"] = 111
    ai["rows"][0]["departures"]["confidence"] = "uncertain"
    ai["rows"][0]["stayovers"]["confidence"] = "uncertain"
    from services.forecast_ai import extract_forecast_with_ai
    # The converter is exercised through the normal request with a lightweight client.
    from PIL import Image
    from io import BytesIO
    image = BytesIO(); Image.new("RGB", (20, 20), "white").save(image, "PNG")
    client = MockClient({"output": [{"content": [{"type": "output_text", "text": json.dumps(ai)}]}]})
    result = extract_forecast_with_ai("x.png", image.getvalue(), client=client)
    comparison = compare_forecast_extractions(local, result)
    statuses = {(r["field"], r["status"]) for r in comparison if r["day"] == 1}
    assert ("departures", "CONFLICT") in statuses
    assert ("stayovers", "AGREE · AI UNCERTAIN") in statuses
    assert ("arrivals", "AGREE") in statuses
    local[0]["arrivals"] = None
    assert any(r["field"] == "arrivals" and r["status"] == "AI ONLY" for r in compare_forecast_extractions(local, result))
    comparison_matrix = comparison_to_review_matrix(comparison)
    assert comparison_matrix[0]["date"] == "2026-09-25"
    assert comparison_matrix[0]["departures"] == "CONFLICT · 110 / 111"
    assert "uncertain" in comparison_matrix[0]["stayovers"]
    assert len(comparison_matrix) == 7


def test_fill_missing_only_never_overwrites_conflict_and_correction_validates():
    local, ai = sample_rows(), sample_rows()
    local[0]["departures"] = None
    ai[0]["departures"] = 110
    ai[0]["arrivals"] = 999
    merged = fill_missing_from_ai(local, ai)
    assert merged[0]["departures"] == 110
    assert merged[0]["arrivals"] == 218
    merged[0]["arrivals"] = 218  # user correction
    assert validate_forecast(merged) == []


def test_local_parser_does_not_guess_a_week_when_dates_are_unreadable():
    from services.forecast_import import parse_forecast_text
    rows, _ = parse_forecast_text("departures: 1 2 3 4 5 6 7")
    assert len(rows) == 7
    assert all(row["date"] is None for row in rows)


def test_tesseract_resolves_homebrew_binary_when_app_path_is_short(monkeypatch):
    import pytesseract
    import services.forecast_import as forecast_import
    monkeypatch.setattr(forecast_import.shutil, "which", lambda command: None if command == "tesseract" else command if command == "/opt/homebrew/bin/tesseract" else None)
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    forecast_import._configure_tesseract(pytesseract)
    assert pytesseract.pytesseract.tesseract_cmd == "/opt/homebrew/bin/tesseract"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is an optional system dependency")
def test_real_tesseract_extracts_synthetic_forecast_image_and_scanned_pdf():
    from datetime import timedelta
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont
    import pymupdf
    from services.forecast_import import extract_forecast_upload

    start = date(2026, 11, 6)
    data = [
        ("departures", [10, 11, 12, 13, 14, 15, 16]), ("stayovers", [5, 6, 7, 8, 9, 10, 11]),
        ("arrivals", [15, 16, 17, 18, 19, 20, 21]), ("rooms on books", [20, 22, 24, 26, 28, 30, 32]),
        ("forecast pickup", [2] * 7), ("forecast occupied", [22, 24, 26, 28, 30, 32, 34]),
        ("occupancy %", [50, 55, 60, 65, 70, 75, 80]),
    ]
    headers = ["Metric"] + [(start + timedelta(days=i)).strftime("%a %Y-%m-%d") for i in range(7)]
    image = Image.new("RGB", (2330, 600), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 26)
    metric_width, day_width, row_height = 255, 295, 71
    table_rows = [headers] + [[name.title()] + [str(value) for value in values] for name, values in data]
    for row_index, row in enumerate(table_rows):
        for col_index, value in enumerate(row):
            x = 15 if col_index == 0 else 15 + metric_width + (col_index - 1) * day_width
            width = metric_width if col_index == 0 else day_width
            y = 14 + row_index * row_height
            draw.rectangle((x, y, x + width, y + row_height), outline="black", width=2 if row_index == 0 else 1)
            draw.text((x + 8, y + 20), value, font=font, fill="black")
    png_buffer = BytesIO(); image.save(png_buffer, format="PNG"); png = png_buffer.getvalue()
    expected = {name.replace(" ", "_"): values for name, values in data if name != "occupancy %"}
    expected["occupancy_pct"] = dict(data)["occupancy %"]
    for name, content in [("synthetic.png", png)]:
        result = extract_forecast_upload(name, content)
        assert not validate_forecast(result["rows"])
        assert {field: [row[field] for row in result["rows"]] for field in expected} == expected
    scan = pymupdf.open()
    page = scan.new_page(width=image.width * 0.75, height=image.height * 0.75)
    page.insert_image(page.rect, stream=png)
    pdf_result = extract_forecast_upload("synthetic_scan.pdf", scan.tobytes())
    assert pdf_result["method"] == "Local OCR (Tesseract)"
    assert not validate_forecast(pdf_result["rows"])
    assert {field: [row[field] for row in pdf_result["rows"]] for field in expected} == expected


@pytest.mark.parametrize("body,match", [
    ({"output": [{"content": [{"type": "refusal", "refusal": "no"}]}]}, "refused"),
    ({"output": [{"content": [{"type": "output_text", "text": "not json"}]}]}, "malformed"),
    ({"status": "incomplete", "output": []}, "incomplete"),
    ({"output": []}, "no structured data"),
])
def test_refused_malformed_incomplete_and_empty_results_fall_back(monkeypatch, body, match):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from PIL import Image
    from io import BytesIO
    image = BytesIO(); Image.new("RGB", (10, 10), "white").save(image, "PNG")
    with pytest.raises(ValueError, match=match): extract_forecast_with_ai("x.png", image.getvalue(), client=MockClient(body))


def test_ai_timeout_and_missing_key_are_clear_and_do_not_retry(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="no server-side API key"): extract_forecast_with_ai("x.png", b"fake")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from PIL import Image
    from io import BytesIO
    image = BytesIO(); Image.new("RGB", (10, 10), "white").save(image, "PNG")
    client = MockClient(error=httpx.ReadTimeout("timeout"))
    with pytest.raises(ValueError, match="timed out"): extract_forecast_with_ai("x.png", image.getvalue(), client=client)
    assert client.payload is not None


def test_ai_dates_and_totals_are_still_validated(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from PIL import Image
    from io import BytesIO
    rows = sample_rows(); rows[0]["date"] = date(2026, 9, 26); rows[0]["rooms_on_books"] = 261
    image = BytesIO(); Image.new("RGB", (10, 10), "white").save(image, "PNG")
    client = MockClient({"output": [{"content": [{"type": "output_text", "text": json.dumps(structured(rows))}]}]})
    result = extract_forecast_with_ai("x.png", image.getvalue(), client=client)
    assert any("first date must be a Friday" in err for err in result["validation_errors"])
    assert any("arrivals + stayovers" in err for err in result["validation_errors"])


def test_ai_vision_ui_remains_hidden_from_public_sessions_and_needs_per_file_opt_in(tmp_path, monkeypatch):
    from io import BytesIO
    from PIL import Image
    from streamlit.testing.v1 import AppTest

    image = BytesIO(); Image.new("RGB", (80, 40), "white").save(image, "PNG")
    monkeypatch.setenv("COPILOT_STATE_DB", str(tmp_path / "ai-ui.sqlite3"))
    monkeypatch.setenv("WEB_MANAGER_ID", "public-manager-session")
    monkeypatch.setenv("OPENAI_API_KEY", "server-key-not-displayed")
    monkeypatch.delenv("FORECAST_AI_PRIVATE_ENABLED", raising=False)
    monkeypatch.delenv("FORECAST_AI_ALLOWED_MANAGER_ID", raising=False)
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run(timeout=40)
    app.segmented_control[0].set_value("WEEKLY FORECAST").run(timeout=40)
    app.file_uploader[0].upload("fake.png", image.getvalue(), "image/png").run(timeout=40)
    assert not any(button.label == "Compare with AI vision" for button in app.button)
    assert not any(box.label.startswith("I authorize sending") for box in app.checkbox)
    assert not app.exception
    # Even matching server-side IDs and flags do not expose paid vision to a
    # non-loopback requester such as a public shared demo visitor.
    monkeypatch.setenv("FORECAST_AI_PRIVATE_ENABLED", "1")
    monkeypatch.setenv("FORECAST_AI_ALLOWED_MANAGER_ID", "public-manager-session")
    app.run(timeout=40)
    assert not any(button.label == "Compare with AI vision" for button in app.button)
    assert not any(box.label.startswith("I authorize sending") for box in app.checkbox)
    assert not app.exception

    assert not app.exception
