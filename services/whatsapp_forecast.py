"""Fail-closed admission for the supported WhatsApp native-text forecast template.

This does not grant a document authority to change configuration or invoke tools.
The document supplies only hotel code, dates and numeric cells. Arbitrary hotel
reports and OCR are deliberately not eligible for unattended scheduling yet.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO
import re
from typing import Any

from services.forecast_import import (
    NUMERIC_FIELDS,
    extraction_requires_review_before_generation,
    normalize_forecast_rows,
    parse_forecast_text,
    validate_forecast,
)

MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 10
MAX_IMAGE_PIXELS = 40_000_000
MAX_EXTRACTED_TEXT = 100_000
SUPPORTED_REPORT_PROFILES = frozenset({"canonical_v1", "hotel_daily_table_v1"})
SUPPORTED_METRIC_LABELS = {
    "departures": "Departures",
    "stayovers": "Stayovers",
    "arrivals": "Arrivals",
    "rooms_on_books": "Rooms on books",
    "forecast_pickup": "Forecast pickup",
    "forecast_occupied": "Forecast occupied",
    "occupancy_pct": "Occupancy %",
}


class ForecastNeedsCorrection(ValueError):
    """Safe, actionable explanation suitable for a same-sender correction reply."""


def _native_pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise ForecastNeedsCorrection("Please resend an unencrypted native-text forecast PDF.")
        if not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
            raise ForecastNeedsCorrection("Please resend a forecast PDF with at most 10 pages.")
        root = reader.trailer["/Root"]
        names = root.get("/Names", {})
        if hasattr(names, "get_object"):
            names = names.get_object()
        if root.get("/OpenAction") or root.get("/AA") or names.get("/JavaScript") or names.get("/EmbeddedFiles"):
            raise ForecastNeedsCorrection("Please resend a plain forecast PDF without embedded files or actions.")
        texts = []
        for page in reader.pages:
            if page.get("/AA"):
                raise ForecastNeedsCorrection("Please resend a plain forecast PDF without embedded actions.")
            text = page.extract_text() or ""
            if not text.strip():
                raise ForecastNeedsCorrection("Scanned or unclear pages need review. Please resend the native-text forecast PDF, or review the file in the app.")
            texts.append(text)
            if sum(map(len, texts)) > MAX_EXTRACTED_TEXT:
                raise ForecastNeedsCorrection("The PDF is too complex for automatic processing. Please resend the standard forecast report only.")
        return "\n".join(texts)
    except ForecastNeedsCorrection:
        raise
    except Exception as exc:
        raise ForecastNeedsCorrection("The PDF could not be read safely. Please resend a valid, unencrypted native-text forecast PDF.") from exc


def _validate_image(content: bytes, mime_type: str) -> None:
    """Validate media before rejecting OCR; never run expensive OCR automatically."""
    from PIL import Image

    signatures = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}
    if not content.startswith(signatures[mime_type]):
        raise ForecastNeedsCorrection("The attachment does not match its image type. Please resend the original forecast PDF.")
    try:
        with Image.open(BytesIO(content)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ForecastNeedsCorrection("The photo is too large (40 megapixels maximum). Please resend the native-text forecast PDF.")
            if image.format != {"image/png": "PNG", "image/jpeg": "JPEG"}[mime_type]:
                raise ValueError("Mismatched image format")
            image.verify()
    except ForecastNeedsCorrection:
        raise
    except Exception as exc:
        raise ForecastNeedsCorrection("The image could not be read safely. Please resend the native-text forecast PDF.") from exc
    raise ForecastNeedsCorrection("Photos and scanned forecasts need review before automatic scheduling. Please resend the native-text forecast PDF, or review and correct the file in the app.")


def _validate_explicit_identifiers(text: str, hotel_code: str, hotel_name: str | None) -> None:
    """Reject extra contradictory identity, even in the canonical report header."""
    mentions = re.findall(r"\b(?:Hotel|Property)\s+(?:code|id|name)\s*:", text, re.I)
    headers = list(re.finditer(r"^[ \t]*(?:Hotel|Property)[ \t]+(code|id|name)[ \t]*:[ \t]*([^\r\n]+?)[ \t]*$", text, re.I | re.M))
    if len(mentions) != len(headers):
        raise ForecastNeedsCorrection("The report contains an unclear hotel identifier. Please resend the correct hotel's standard report.")
    seen = set()
    for header in headers:
        kind = "name" if header.group(1).casefold() == "name" else "code"
        expected = hotel_name if kind == "name" else hotel_code
        if kind in seen or expected is None or header.group(2) != expected:
            raise ForecastNeedsCorrection("The report has a conflicting or duplicate hotel identifier. Please resend the correct hotel's standard report; no hotel mapping was guessed.")
        seen.add(kind)


def _validate_template(text: str, hotel_code: str, hotel_name: str | None = None) -> None:
    """Admit exact rows rather than accepting the manual parser's best effort.

    Labels may be separated from their cells by newlines, as PDF table extraction
    often emits one cell per line. Each row must still contain exactly seven
    visible numbers. Signs, duplicated labels, extra cells and prose in the
    numeric table are rejected, never converted into guessed positive counts.
    """
    hotel_mentions = re.findall(r"hotel\s+code\s*:", text, re.I)
    headers = re.findall(r"^[ \t]*Hotel code[ \t]*:[ \t]*([^\r\n]+?)[ \t]*$", text, re.I | re.M)
    if len(hotel_mentions) != 1 or len(headers) != 1 or headers[0] != hotel_code:
        raise ForecastNeedsCorrection("The report must contain one exact enrolled 'Hotel code: ...' header. Please resend the correct hotel's standard forecast; no hotel mapping was guessed.")

    _validate_explicit_identifiers(text, hotel_code, hotel_name)
    matches = []
    for field, label in SUPPORTED_METRIC_LABELS.items():
        pattern = r"^[ \t]*" + re.escape(label) + r"[ \t]*:?[ \t]*(?=\r?$|\d)"
        occurrences = list(re.finditer(pattern, text, re.I | re.M))
        if len(occurrences) != 1:
            raise ForecastNeedsCorrection(f"Please resend the standard forecast with exactly one '{label}' row and seven values.")
        match = occurrences[0]
        matches.append((match.start(), match.end(), field))
    matches.sort()

    date_tokens = re.findall(r"(?<![\w])20\d{2}-\d{2}-\d{2}(?![\w])", text)
    if len(date_tokens) != 7 or any(value not in text[:matches[0][0]] for value in date_tokens):
        raise ForecastNeedsCorrection("Please resend seven full YYYY-MM-DD date columns above the forecast rows, for one Friday-Thursday week.")
    for index, (_, end, field) in enumerate(matches):
        next_start = matches[index + 1][0] if index + 1 < len(matches) else len(text)
        segment = text[end:next_start].strip()
        number = r"(?:0|[1-9]\d{0,5})(?:\.\d{1,2})?"
        if field == "occupancy_pct":
            number += r"%?"
        if not re.fullmatch(number + r"(?:\s+" + number + r"){6}", segment):
            raise ForecastNeedsCorrection(f"The '{SUPPORTED_METRIC_LABELS[field]}' row is unclear or has extra/missing cells. Please resend exactly seven nonnegative values in the standard report.")


def _validate_daily_table_template(text: str, hotel_code: str, hotel_name: str | None) -> str:
    """Validate an explicitly enrolled, identity-less daily-table report profile.

    This profile must NEVER be auto-detected. Only operator enrollment permits
    omission of hotel identity, binding the format to that sender's one tenant.
    If identity is present, it must match enrollment exactly. The complete report
    must be known headers plus the exact table; unknown prose and conflicting
    identifiers are rejected instead of treating a familiar table as authority.
    """
    weekdays = r"FRI\s+SAT\s+SUN\s+MON\s+TUE\s+WED\s+THU"
    start = re.search(r"^[ \t]*" + weekdays + r"(?=\s|$)", text, re.I | re.M)
    if not start:
        raise ForecastNeedsCorrection("The enrolled daily-table report needs the exact Friday-Thursday weekday columns. Please resend the complete standard PDF.")
    prefix = text[:start.start()]
    seen = set()
    for line in (line.strip() for line in prefix.splitlines() if line.strip()):
        identifier = re.fullmatch(r"(?:Hotel|Property)[ \t]+(code|id|name)[ \t]*:[ \t]*(.+)", line, re.I)
        if identifier:
            kind = "name" if identifier.group(1).casefold() == "name" else "code"
            value = identifier.group(2).strip()
        elif hotel_name is not None and line == hotel_name:
            kind, value = "name", line
        else:
            raise ForecastNeedsCorrection("The enrolled daily-table report has an unrecognized hotel header or extra text. Please resend the correct hotel's standard report for review.")
        expected = hotel_name if kind == "name" else hotel_code
        if kind in seen or expected is None or value != expected:
            raise ForecastNeedsCorrection("The report has a conflicting or duplicate hotel identifier. Please resend the correct hotel's standard report; no hotel mapping was guessed.")
        seen.add(kind)

    # Spaces/newlines between table cells are equivalent, but row labels/order,
    # weekday order, full date columns and every numeric cell are mandatory.
    full_date = r"(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/20\d{2}"
    integer = r"(?:0|[1-9]\d{0,5})"
    percentage = integer + r"(?:\.\d{1,2})?%?"
    count_row = integer + r"(?:\s+" + integer + r"){6}"
    percent_row = percentage + r"(?:\s+" + percentage + r"){6}"
    labels = (
        "Arrivals", "Departures", "Stayovers", "Rooms on the books",
        "Forecasted Pick up", "Forcasted occupied rooms", "Occupancy %",
    )
    pattern = weekdays + r"\s+(?P<dates>" + full_date + r"(?:\s+" + full_date + r"){6})\s+"
    pattern += r"(?:Prior\s+Day\s+Occupancy\s+)?"
    for index, label in enumerate(labels):
        pattern += r"\s+".join(re.escape(part) for part in label.split()) + r"\s+"
        pattern += percent_row if label == "Occupancy %" else count_row
        if index < len(labels) - 1:
            pattern += r"\s+"
    table = text[start.start():].strip()
    match = re.fullmatch(pattern, table, re.I)
    if not match:
        raise ForecastNeedsCorrection("The enrolled daily-table report is incomplete or has unexpected rows, values or text. Please resend its seven full dates and all seven numeric cells per metric; Prior Day Occupancy must remain blank.")
    try:
        dates = [datetime.strptime(value, "%m/%d/%Y").date() for value in match.group("dates").split()]
    except ValueError as exc:
        raise ForecastNeedsCorrection("The daily-table report contains an invalid full date. Please correct and resend it.") from exc
    if dates[0].weekday() != 4 or any(day != dates[0] + timedelta(days=index) for index, day in enumerate(dates)):
        raise ForecastNeedsCorrection("The report's printed date columns do not match Friday-Thursday order. Please correct and resend the complete report.")
    # Do not parse hotel header words/numbers as forecast data. The shared parser
    # still rejects ambiguous numeric dates; this profile does not invent a date
    # convention from a filename, sender or weekday expectation.
    return table


def validate_whatsapp_forecast(
    data: bytes,
    mime_type: str,
    hotel_code: str,
    room_capacity: int,
    now: datetime,
    max_week_age_days: int = 7,
    max_week_future_days: int = 56,
    *,
    report_profile: str = "canonical_v1",
    hotel_name: str | None = None,
) -> list[dict[str, Any]]:
    """Validate supported media, exact hotel, dates, cells, totals and capacity.

    Returns normalized rows suitable for the existing deterministic scheduler.
    All input/extraction rejection messages are safe to return to the enrolled
    sender. Configuration must come from trusted enrollment, never the PDF.
    The canonical profile remains the default. The identity-less daily-table
    profile requires explicit per-tenant operator enrollment, not auto-detection.
    """
    if not isinstance(report_profile, str) or report_profile not in SUPPORTED_REPORT_PROFILES:
        raise ForecastNeedsCorrection("The enrolled forecast report profile is not supported. Please contact the administrator.")
    if report_profile == "hotel_daily_table_v1" and hotel_name is not None and (not isinstance(hotel_name, str) or not 1 <= len(hotel_name.strip()) <= 160):
        raise ForecastNeedsCorrection("The enrolled hotel name is not configured correctly. Please contact the administrator.")
    if not isinstance(hotel_code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", hotel_code):
        raise ForecastNeedsCorrection("The enrolled hotel code is not configured correctly. Please contact the administrator.")
    if isinstance(room_capacity, bool) or not isinstance(room_capacity, int) or not 1 <= room_capacity <= 100_000:
        raise ForecastNeedsCorrection("The enrolled hotel room capacity is not configured correctly. Please contact the administrator.")
    if not isinstance(now, datetime) or min(max_week_age_days, max_week_future_days) < 0:
        raise ValueError("A valid current datetime and nonnegative week bounds are required.")
    if mime_type not in {"application/pdf", "image/png", "image/jpeg"}:
        raise ForecastNeedsCorrection("Please send a PDF, PNG or JPEG forecast attachment. Automatic scheduling currently requires the native-text PDF.")
    if not isinstance(data, bytes) or not data:
        raise ForecastNeedsCorrection("The attachment is empty. Please resend the native-text forecast PDF.")
    limit = MAX_PDF_BYTES if mime_type == "application/pdf" else MAX_IMAGE_BYTES
    if len(data) > limit:
        raise ForecastNeedsCorrection(f"The attachment is too large. Please resend a {'10 MB or smaller PDF' if mime_type == 'application/pdf' else '5 MB or smaller photo'}.")
    if mime_type != "application/pdf":
        _validate_image(data, mime_type)
    if not data.startswith(b"%PDF-"):
        raise ForecastNeedsCorrection("The attachment does not match its PDF type. Please resend the original native-text forecast PDF.")
    text = _native_pdf_text(data)
    if report_profile == "canonical_v1":
        _validate_template(text, hotel_code, hotel_name)
    else:
        text = _validate_daily_table_template(text, hotel_code, hotel_name)
    rows, notices = parse_forecast_text(text)
    errors = validate_forecast(rows)
    if errors or extraction_requires_review_before_generation("PDF text extraction", notices):
        # Existing validation errors mention dates/fields only, not arbitrary
        # extracted source prose. Keep the WhatsApp correction reply bounded.
        details = " ".join(errors[:3]) if errors else "The dates or numeric cells are ambiguous."
        raise ForecastNeedsCorrection("The forecast needs correction before scheduling. " + details + " Please resend the corrected standard PDF, or review it in the app.")
    normalized = normalize_forecast_rows(rows)
    from datetime import date

    start = date.fromisoformat(normalized[0]["date"])
    today = now.date()
    if not today - timedelta(days=max_week_age_days) <= start <= today + timedelta(days=max_week_future_days):
        raise ForecastNeedsCorrection("The forecast week is outside the permitted current/upcoming range. Please resend the correct Friday-Thursday forecast.")
    for row in normalized:
        if any(row[field] > room_capacity for field in NUMERIC_FIELDS if field != "occupancy_pct"):
            raise ForecastNeedsCorrection(f"{row['date']}: a room count exceeds the enrolled hotel's room capacity. Please correct and resend the forecast.")
        # The supported standard daily profile treats departing and staying
        # rooms as disjoint. Reject impossible inventory use; do not populate
        # the deliberately unknown prior_day_occupancy field.
        if row["departures"] + row["stayovers"] > room_capacity:
            raise ForecastNeedsCorrection(f"{row['date']}: departures plus stayovers exceed the enrolled room capacity for the supported daily report. Please correct the report or request manual review.")
        expected_pct = row["forecast_occupied"] / room_capacity * 100
        if abs(row["occupancy_pct"] - expected_pct) > 0.6000001:
            raise ForecastNeedsCorrection(f"{row['date']}: occupancy percent does not match forecast occupied rooms and the enrolled room capacity. Please correct and resend the forecast.")
    return normalized
