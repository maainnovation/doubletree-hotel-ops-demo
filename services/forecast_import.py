"""Local-only forecast extraction and validation for the weekly roster workflow."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from io import BytesIO
import math
import re
import shutil
from typing import Any


FORECAST_FIELDS = (
    "date", "departures", "stayovers", "arrivals", "rooms_on_books",
    "forecast_pickup", "forecast_occupied", "occupancy_pct",
)
NUMERIC_FIELDS = FORECAST_FIELDS[1:]
REVIEW_FIELDS = (
    "arrivals", "departures", "stayovers", "rooms_on_books",
    "forecast_pickup", "forecast_occupied", "occupancy_pct",
)
REVIEW_LABELS = {
    "arrivals": "Arrivals",
    "departures": "Departures",
    "stayovers": "Stayovers",
    "rooms_on_books": "Rooms on books",
    "forecast_pickup": "Forecast pickup",
    "forecast_occupied": "Forecast occupied",
    "occupancy_pct": "Occupancy %",
}
REVIEW_LABEL_TO_FIELD = {label: field for field, label in REVIEW_LABELS.items()}
FIELD_LABELS = {
    "departures": ("departures", "departure", "check outs", "checkouts", "checkout rooms"),
    "stayovers": ("stayovers", "stay overs", "stayover rooms", "stay over rooms", "stayovers rooms"),
    "arrivals": ("arrivals", "arrival", "arrival rooms"),
    "rooms_on_books": ("rooms on books", "on the books", "rooms booked", "booked rooms"),
    # Preserve the source report's common split wording and its printed
    # "Forcasted" typo. Matching still extracts only visible numeric cells.
    "forecast_pickup": ("forecast pickup", "forecasted pickup", "forecast pick up", "forecasted pick up", "pick up", "pickup"),
    "forecast_occupied": ("forecasted occupied rooms", "forcasted occupied rooms", "forcasted occupied room", "forcasted occupied", "forecast occupied", "forecast occupancy rooms", "forecast rooms occupied", "occupied forecast"),
    "occupancy_pct": ("occupancy %", "occupancy percent", "occupancy", "occ %"),
}
WEEKDAYS = ("Fri", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu")


def forecast_rows_to_review_matrix(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build one review row per date with forecast metrics as columns.

    `_day_slot` remains a hidden stable key in the editor so row sorting or
    date corrections cannot detach values from their original date row.
    """
    matrix = []
    for idx in range(7):
        source = rows[idx] if idx < len(rows) else {}
        matrix.append({"_day_slot": idx, "date": source.get("date"), **{
            field: source.get(field) for field in REVIEW_FIELDS
        }})
    return matrix


def review_matrix_to_forecast_rows(
    matrix: list[dict[str, Any]], dates: list[Any] | None = None,
    original_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Read edited date rows back by stable day-slot identity.

    `dates` is retained as a compatibility fallback for callers using the old
    date-controls-plus-metric-grid form. New UI rows carry their editable date.
    """
    if dates is not None and len(dates) != 7:
        raise ValueError("A forecast review requires seven date inputs.")
    indexed = {int(row.get("_day_slot", idx)): row for idx, row in enumerate(matrix)}
    rows = []
    for day in range(7):
        original = dict(original_rows[day]) if original_rows and day < len(original_rows) else {}
        source = indexed.get(day, {})
        if "date" in source:
            original["date"] = source["date"]
        elif dates is not None:
            original["date"] = dates[day]
        for field in REVIEW_FIELDS:
            value = source.get(field)
            try:
                if value is not None and math.isnan(float(value)):
                    value = None
            except (TypeError, ValueError):
                pass
            original[field] = value
        rows.append(original)
    return rows


def _configure_tesseract(pytesseract) -> None:
    """Resolve the system binary even when a GUI-launched app has a short PATH."""
    configured = getattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    if shutil.which(configured):
        return
    for candidate in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/opt/local/bin/tesseract"):
        if shutil.which(candidate):
            pytesseract.pytesseract.tesseract_cmd = candidate
            return


def empty_forecast(start_date: date | None = None) -> list[dict[str, Any]]:
    """Return seven Friday-through-Thursday blank rows; no counts are invented."""
    if start_date is None:
        today = date.today()
        start_date = today - timedelta(days=(today.weekday() - 4) % 7)
    elif isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    return [{"date": start_date + timedelta(days=i), **{name: None for name in NUMERIC_FIELDS}, "prior_day_occupancy": None} for i in range(7)]


def validate_forecast(rows: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if not isinstance(rows, list) or len(rows) != 7:
        return ["Enter exactly seven daily rows for a Friday–Thursday week."]
    parsed_dates: list[date] = []
    for idx, row in enumerate(rows, 1):
        try:
            raw_date = row.get("date")
            if isinstance(raw_date, datetime):
                current = raw_date.date()
            elif isinstance(raw_date, date):
                current = raw_date
            else:
                current = date.fromisoformat(str(raw_date))
            parsed_dates.append(current)
        except (TypeError, ValueError):
            errors.append(f"Day {idx}: enter a valid date (YYYY-MM-DD).")
            continue
        for field in NUMERIC_FIELDS:
            raw = row.get(field)
            if raw is None or str(raw).strip() == "":
                errors.append(f"{current.isoformat()}: {field.replace('_', ' ')} is missing.")
                continue
            try:
                value = float(raw)
                if value < 0 or value != value or value in (float("inf"), float("-inf")):
                    raise ValueError
                if field != "occupancy_pct" and not value.is_integer():
                    raise ValueError
                if field == "occupancy_pct" and value > 100:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"{current.isoformat()}: {field.replace('_', ' ')} must be a nonnegative {'percentage up to 100' if field == 'occupancy_pct' else 'whole number'}.")
    if len(parsed_dates) == 7:
        if parsed_dates[0].weekday() != 4:
            errors.append("The first date must be a Friday.")
        if parsed_dates != sorted(parsed_dates) or len(set(parsed_dates)) != 7:
            errors.append("Dates must be unique and in Friday–Thursday order.")
        elif any((parsed_dates[i] - parsed_dates[0]).days != i for i in range(7)):
            errors.append("Dates must be seven consecutive days, Friday through Thursday.")
    # Validate arithmetic row-by-row even if a different date or row is invalid,
    # so the reviewer sees all resolvable issues in one pass.
    for row in rows:
        try:
            values = {field: float(row[field]) for field in NUMERIC_FIELDS}
        except (KeyError, TypeError, ValueError):
            continue
        d_raw = row.get("date")
        d = d_raw.isoformat() if isinstance(d_raw, (date, datetime)) else str(d_raw)
        if values["arrivals"] + values["stayovers"] != values["rooms_on_books"]:
            errors.append(f"{d}: arrivals + stayovers must equal rooms on books.")
        if values["rooms_on_books"] + values["forecast_pickup"] != values["forecast_occupied"]:
            errors.append(f"{d}: rooms on books + forecast pickup must equal forecast occupied.")
    return errors


def extraction_requires_review_before_generation(method: str | None, notices: list[str] | None = None) -> bool:
    """Block automatic roster drafts when extraction itself reports uncertainty.

    OCR is always treated as uncertain until a person reviews it. Native text
    extraction may produce a draft when dates, required values, and totals
    validate, but explicit ambiguity/coverage/layout notices still block it.
    """
    if "ocr" in (method or "").lower():
        return True
    blockers = (
        "ambiguous", "duplicate dates", "could not reliably identify",
        "could not confidently extract", "not a consecutive friday",
        "layout modes disagree", "second local ocr layout",
        "more than one scanned page",
    )
    return any(marker in notice.lower() for notice in (notices or []) for marker in blockers)


def normalize_forecast_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for row in rows:
        current = row["date"]
        normalized.append({"date": current.isoformat() if isinstance(current, (date, datetime)) else str(current),
                           **{field: int(float(row[field])) if field != "occupancy_pct" else float(row[field]) for field in NUMERIC_FIELDS},
                           "prior_day_occupancy": row.get("prior_day_occupancy")})
    return normalized


def _dates_from_text(text: str) -> list[date]:
    """Return full, unambiguous dates in the order printed in the source.

    Numeric month/day order is accepted only when it is unambiguous. The
    caller maps each metric cell to this source-column order before sorting
    output into the Friday–Thursday sequence.
    """
    dates, _, _ = _date_tokens_from_text(text)
    return dates


_MONTH = r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
_DATE_TOKEN = re.compile(
    rf"(?<![\w])(?:"
    rf"(?P<iso_year>20\d{{2}})[/-](?P<iso_month>\d{{1,2}})[/-](?P<iso_day>\d{{1,2}})"
    rf"|(?P<num_a>\d{{1,2}})[/-](?P<num_b>\d{{1,2}})[/-](?P<num_year>20\d{{2}})"
    rf"|(?P<month>{_MONTH})\s+(?P<month_day>\d{{1,2}}),?\s+(?P<month_year>20\d{{2}})"
    rf"|(?P<day_first>\d{{1,2}})\s+(?P<day_month>{_MONTH})\s+(?P<day_year>20\d{{2}})"
    rf")(?![\w])", re.I)


def _date_tokens_from_text(text: str) -> tuple[list[date], list[str], list[tuple[int, int]]]:
    dates: list[date] = []
    issues: list[str] = []
    spans: list[tuple[int, int]] = []
    matches = list(_DATE_TOKEN.finditer(text))
    # A sequence can establish numeric convention from dates that could not
    # themselves be reversed (e.g. 9/25/2026 disambiguates a neighboring
    # 10/1/2026). Never infer when the observed examples conflict or provide
    # no convention evidence at all.
    numeric_orders = set()
    for match in matches:
        groups = match.groupdict()
        if groups["num_year"]:
            a, b = int(groups["num_a"]), int(groups["num_b"])
            if a <= 12 < b: numeric_orders.add("month_day")
            elif b <= 12 < a: numeric_orders.add("day_month")
    inferred_numeric_order = next(iter(numeric_orders)) if len(numeric_orders) == 1 else None
    for match in matches:
        spans.append(match.span())
        groups = match.groupdict()
        try:
            if groups["iso_year"]:
                value = date(int(groups["iso_year"]), int(groups["iso_month"]), int(groups["iso_day"]))
            elif groups["num_year"]:
                a, b = int(groups["num_a"]), int(groups["num_b"])
                if a <= 12 and b <= 12:
                    if inferred_numeric_order is None:
                        issues.append(f"Ambiguous numeric date '{match.group()}'; use an unambiguous date such as YYYY-MM-DD or Sep 25, 2026.")
                        continue
                    if inferred_numeric_order == "month_day":
                        value = date(int(groups["num_year"]), a, b)
                    else:
                        value = date(int(groups["num_year"]), b, a)
                    dates.append(value)
                    continue
                if a > 12 and b <= 12:  # day/month/year
                    value = date(int(groups["num_year"]), b, a)
                elif a <= 12 and b > 12:  # month/day/year
                    value = date(int(groups["num_year"]), a, b)
                else:
                    issues.append(f"Invalid numeric date '{match.group()}'.")
                    continue
            elif groups["month"]:
                month = datetime.strptime(groups["month"][:3].title(), "%b").month
                value = date(int(groups["month_year"]), month, int(groups["month_day"]))
            else:
                month = datetime.strptime(groups["day_month"][:3].title(), "%b").month
                value = date(int(groups["day_year"]), month, int(groups["day_first"]))
        except ValueError:
            issues.append(f"Invalid date '{match.group()}'.")
            continue
        dates.append(value)
    return dates, issues, spans


def _label_pattern(alias: str) -> re.Pattern[str]:
    words = re.split(r"\s+", alias.strip())
    return re.compile(r"(?<![a-z])" + r"[\W_]+".join(map(re.escape, words)) + r"(?![a-z])", re.I)


def _field_label_matches(text: str) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for field, aliases in FIELD_LABELS.items():
        candidates = []
        for alias in aliases:
            for match in _label_pattern(alias).finditer(text):
                # Do not mistake the blank Prior Day Occupancy row for the
                # separate occupancy percentage field.
                line_start = text.rfind("\n", 0, match.start()) + 1
                prefix = text[line_start:match.start()].casefold()
                if field == "occupancy_pct" and "prior day" in prefix:
                    continue
                candidates.append(match)
        if candidates:
            match = min(candidates, key=lambda item: (item.start(), -(item.end() - item.start())))
            found.append((match.start(), match.end(), field))
    return sorted(found)


def parse_forecast_text(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Best-effort table-row extractor; extracted values are always review-only."""
    notices = ["Extracted values are unconfirmed. Review every date and count before generating a roster."]
    dates, date_issues, date_spans = _date_tokens_from_text(text)
    notices.extend(date_issues)
    if len(dates) != 7 or len(set(dates)) != 7:
        if len(dates) == 7 and len(set(dates)) != 7:
            notices.append("Duplicate dates were found. Correct the date columns manually; values were not reassigned.")
        elif len(dates) != 7:
            notices.append(f"Could not reliably identify seven distinct full dates (found {len(dates)}); enter or correct them manually.")
        rows = [{"date": None, **{name: None for name in NUMERIC_FIELDS}, "prior_day_occupancy": None} for _ in range(7)]
        return rows, notices
    sorted_dates = sorted(dates)
    if sorted_dates[0].weekday() != 4 or any((sorted_dates[index] - sorted_dates[0]).days != index for index in range(7)):
        notices.append("Dates are not a consecutive Friday–Thursday week; correct them manually before scheduling.")
        return [{"date": None, **{name: None for name in NUMERIC_FIELDS}, "prior_day_occupancy": None} for _ in range(7)], notices

    rows_by_date = {day: {"date": day, **{name: None for name in NUMERIC_FIELDS}, "prior_day_occupancy": None} for day in dates}
    label_matches = _field_label_matches(text)
    for index, (start, end, field) in enumerate(label_matches):
        next_start = label_matches[index + 1][0] if index + 1 < len(label_matches) else len(text)
        # A wrapped label can extend across line breaks. The segment stops at
        # the next known metric label so values from neighboring rows cannot leak.
        tail = text[end:next_start]
        if field == "occupancy_pct":
            tokens = re.findall(r"(?<![A-Za-z])(?:\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+\.\d+)\s*%?", tail)
        else:
            tokens = re.findall(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?", tail)
        numbers = []
        for token in tokens:
            try:
                numbers.append(float(token.replace(",", "").rstrip("%")))
            except ValueError:
                pass
        if len(numbers) == 7:
            # Cells are paired to dates in printed source-column order. Output
            # is sorted later for the Friday–Thursday scheduler.
            for day, value in zip(dates, numbers):
                rows_by_date[day][field] = int(value) if field != "occupancy_pct" and value.is_integer() else value
        else:
            notices.append(f"Could not confidently extract all seven {field.replace('_', ' ')} values; enter them manually.")
    return [rows_by_date[day] for day in sorted_dates], notices


def _extract_with_local_ocr(image, pytesseract) -> tuple[list[dict[str, Any]], list[str]]:
    """Try table-row OCR first, then page-layout OCR if the first parse is incomplete.

    OCR remains review-only. A fallback is used only when it yields more parsed
    dates or populated metric cells, and differing candidates are disclosed.
    """
    candidates = []
    for config in ("--psm 6", "--psm 3"):
        try:
            text = pytesseract.image_to_string(image, config=config)
        except pytesseract.TesseractNotFoundError as exc:
            raise ValueError("Local Tesseract OCR is not installed. Install tesseract-ocr and retry, or use manual entry.") from exc
        except pytesseract.TesseractError as exc:
            raise ValueError("Local Tesseract OCR could not process this image. Check the file or use manual entry.") from exc
        rows, notices = parse_forecast_text(text)
        date_count = sum(row.get("date") is not None for row in rows)
        cell_count = sum(row.get(field) is not None for row in rows for field in NUMERIC_FIELDS)
        candidates.append(((date_count, cell_count), rows, notices, config))
        # A full parse needs no alternate page segmentation pass.
        if date_count == 7 and cell_count == 7 * len(NUMERIC_FIELDS):
            return rows, notices

    candidates.sort(key=lambda item: item[0], reverse=True)
    _, rows, notices, chosen_config = candidates[0]
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        first_values = [(row.get("date"), *(row.get(field) for field in NUMERIC_FIELDS)) for row in candidates[0][1]]
        second_values = [(row.get("date"), *(row.get(field) for field in NUMERIC_FIELDS)) for row in candidates[1][1]]
        if first_values != second_values:
            notices.append("Local OCR layout modes disagree. Verify every displayed field before confirming.")
    if chosen_config != "--psm 6":
        notices.append("A second local OCR layout mode recovered more fields; verify every displayed value.")
    return rows, notices


def extract_forecast_upload(filename: str, content: bytes) -> dict[str, Any]:
    """Extract text locally from PDF or OCR supported image files; no upload leaves this process."""
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if not content:
        raise ValueError("The selected file is empty.")
    if len(content) > 25 * 1024 * 1024:
        raise ValueError("Forecast files are limited to 25 MB in this demo.")
    if suffix == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("PDF reading is unavailable. Install the listed pypdf dependency; you can still use manual entry.") from exc
        try:
            reader = PdfReader(BytesIO(content))
        except Exception as exc:
            raise ValueError("The PDF could not be read. Try a valid, unencrypted PDF or enter the forecast manually.") from exc
        if len(reader.pages) > 10:
            raise ValueError("For this demo, PDFs are limited to 10 pages.")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if text.strip():
            rows, notices = parse_forecast_text(text)
            notices.insert(0, "Read embedded PDF text locally. No file was sent to an external service.")
            return {"rows": rows, "notices": notices, "method": "PDF text extraction"}
        # Scanned PDF: render locally with PyMuPDF and OCR using the local Tesseract executable.
        try:
            import pymupdf
            import pytesseract
            from PIL import Image
        except ImportError as exc:
            raise ValueError("This PDF has no selectable text. Local scanned-PDF OCR needs PyMuPDF, Pillow, and pytesseract; manual entry remains available.") from exc
        _configure_tesseract(pytesseract)
        try:
            document = pymupdf.open(stream=content, filetype="pdf")
            if len(document) > 10:
                raise ValueError("For this demo, scanned PDFs are limited to 10 pages.")
            # A moderate render scale worked better than 2x on wide, compact
            # roster/forecast tables in local OCR checks while bounding memory.
            pages = [Image.open(BytesIO(page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).tobytes("png"))) for page in document]
        except pytesseract.TesseractNotFoundError as exc:
            raise ValueError("Local Tesseract OCR is not installed. Install tesseract-ocr and retry, or use manual entry.") from exc
        except pytesseract.TesseractError as exc:
            raise ValueError("Local Tesseract OCR could not process this scanned PDF. Check the file or use manual entry.") from exc
        rows, notices = _extract_with_local_ocr(pages[0], pytesseract)
        if len(pages) > 1:
            for page_image in pages[1:]:
                extra_rows, extra_notices = _extract_with_local_ocr(page_image, pytesseract)
                if any(row.get("date") is not None for row in extra_rows):
                    notices.append("More than one scanned page contains forecast-like dates; review the intended week manually.")
        notices.insert(0, "Scanned PDF was OCR-processed locally with Tesseract. OCR may misread figures; verify every cell.")
        return {"rows": rows, "notices": notices, "method": "Local OCR (Tesseract)"}
    if suffix not in {"png", "jpg", "jpeg", "tif", "tiff", "webp"}:
        raise ValueError("Upload a PDF or PNG, JPG, TIFF, or WEBP image.")
    try:
        from PIL import Image
        import pytesseract
    except ImportError as exc:
        raise ValueError("Local image OCR needs Pillow and pytesseract; manual entry remains available.") from exc
    _configure_tesseract(pytesseract)
    try:
        image = Image.open(BytesIO(content))
        if image.width * image.height > 40_000_000:
            raise ValueError("Image exceeds the 40 megapixel demo limit.")
        image = image.convert("RGB")
    except pytesseract.TesseractNotFoundError as exc:
        raise ValueError("Local Tesseract OCR is not installed. Install tesseract-ocr and retry, or use manual entry.") from exc
    except pytesseract.TesseractError as exc:
        raise ValueError("Local Tesseract OCR could not process this image. Check the file or use manual entry.") from exc
    rows, notices = _extract_with_local_ocr(image, pytesseract)
    notices.insert(0, "Image was OCR-processed locally with Tesseract. OCR may misread figures; verify every cell.")
    return {"rows": rows, "notices": notices, "method": "Local OCR (Tesseract)"}
