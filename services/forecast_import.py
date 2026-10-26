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
    """Produce a review-only candidate using at most four bounded local passes.

    Preprocessing changes pixels, never recognized letters/numbers/dates. Once
    a pass reads seven explicit dates, populated cells are not overwritten.
    Blank cells can be supplied by another pass only when source-column dates
    match exactly and all overlapping populated cells agree. Disagreement stops
    merging and is disclosed; this is not an OCR accuracy/confidence guarantee.
    """
    from PIL import Image, ImageOps

    if image.width * image.height > 40_000_000:
        raise ValueError("Image exceeds the 40 megapixel local OCR limit.")
    baseline = None
    original_rows = None
    fallback = None
    disagreement = False
    recovered = False
    additional_pass = False
    timeout_seen = False
    passes = (("original table", "--psm 6"), ("original page", "--psm 3"),
              ("rescaled grayscale", "--psm 6"), ("rescaled threshold", "--psm 6"))
    for index, (label, config) in enumerate(passes):
        current = image
        try:
            if index >= 2:
                # Resampling after a native PDF render avoids a second raster
                # interpolation by the PDF renderer. Bound every image to 40 MP.
                scale = min(1.5, math.sqrt(40_000_000 / (image.width * image.height)))
                size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
                with ImageOps.grayscale(image) as gray:
                    if index == 3:
                        with gray.point(lambda value: 255 if value > 170 else 0) as threshold:
                            current = threshold.resize(size, Image.Resampling.LANCZOS)
                    else:
                        current = gray.resize(size, Image.Resampling.LANCZOS)
            try:
                text = pytesseract.image_to_string(current, config=config, timeout=15)
            except pytesseract.TesseractNotFoundError as exc:
                raise ValueError("Local Tesseract OCR is not installed. Install tesseract-ocr and retry, or use manual entry.") from exc
            except pytesseract.TesseractError as exc:
                raise ValueError("Local Tesseract OCR could not process this image. Check the file or use manual entry.") from exc
            except RuntimeError:
                # pytesseract uses RuntimeError for its bounded-process timeout.
                timeout_seen = True
                continue
        finally:
            if current is not image:
                current.close()
        rows, notices = parse_forecast_text(text)
        source_dates, date_issues, _ = _date_tokens_from_text(text)
        date_count = sum(row.get("date") is not None for row in rows)
        cell_count = sum(row.get(field) is not None for row in rows for field in NUMERIC_FIELDS)
        candidate = {"rows": rows, "notices": notices, "source_dates": source_dates,
                     "score": (date_count, cell_count), "pass_index": index}
        if fallback is None or candidate["score"] > fallback["score"]:
            fallback = candidate
        if date_count != 7 or date_issues or len(set(source_dates)) != 7:
            continue
        if baseline is None:
            baseline = candidate
            baseline["rows"] = [dict(row) for row in rows]
            original_rows = [dict(row) for row in rows]
            additional_pass = index > 0
        else:
            if baseline["source_dates"] != source_dates:
                disagreement = True
                baseline["rows"] = [dict(row) for row in original_rows]
                recovered = False
                continue
            conflicts = any(
                left.get(field) is not None and right.get(field) is not None and left[field] != right[field]
                for left, right in zip(baseline["rows"], rows) for field in NUMERIC_FIELDS
            )
            if conflicts:
                disagreement = True
                # Withdraw any earlier supplemental cells once observed evidence
                # conflicts. Keep only the first explicitly dated pass for review.
                baseline["rows"] = [dict(row) for row in original_rows]
                recovered = False
                continue
            if not disagreement:
                for left, right in zip(baseline["rows"], rows):
                    for field in NUMERIC_FIELDS:
                        if left.get(field) is None and right.get(field) is not None:
                            left[field] = right[field]
                            recovered = True
                            additional_pass = True
        if all(row.get(field) is not None for row in baseline["rows"] for field in NUMERIC_FIELDS):
            break
    chosen = baseline or fallback
    if chosen is None:
        raise ValueError("Local OCR exceeded its processing time limit. Try a clearer file or use manual entry.")
    rows = chosen["rows"]
    notices = list(chosen["notices"])
    if recovered:
        # Missing-field notices refer to an earlier pass, not the displayed
        # multi-pass candidate. Keep the uncertainty explicit instead.
        for field in NUMERIC_FIELDS:
            if all(row.get(field) is not None for row in rows):
                notice = f"Could not confidently extract all seven {field.replace('_', ' ')} values; enter them manually."
                notices = [item for item in notices if item != notice]
        notices.append("Blank cells were recovered from additional local OCR passes with matching printed dates and agreeing populated cells. All values remain unconfirmed; verify every cell.")
    if disagreement:
        notices.append("Local OCR layout modes disagree. Conflicting values were not overwritten; supplemental cells were withdrawn and further blank-cell merging was stopped; verify every displayed field before confirming.")
    if additional_pass or chosen["pass_index"] > 0:
        notices.append("Additional bounded local OCR preprocessing recovered more fields; this is not a confidence guarantee. Review every date and value.")
    if timeout_seen:
        notices.append("A local OCR pass reached its time limit; the displayed candidate still requires complete manual review.")
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
                document.close()
                raise ValueError("For this demo, scanned PDFs are limited to 10 pages.")
            for page in document:
                if math.ceil(page.rect.width) * math.ceil(page.rect.height) > 40_000_000:
                    document.close()
                    raise ValueError("A scanned PDF page exceeds the 40 megapixel local OCR limit.")
        except pytesseract.TesseractNotFoundError as exc:
            raise ValueError("Local Tesseract OCR is not installed. Install tesseract-ocr and retry, or use manual entry.") from exc
        except pytesseract.TesseractError as exc:
            raise ValueError("Local Tesseract OCR could not process this scanned PDF. Check the file or use manual entry.") from exc
        # Render/process one native-resolution page at a time. The bounded
        # local fallback resamples with PIL only when the first read is incomplete.
        try:
            for index, page in enumerate(document):
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1, 1), colorspace=pymupdf.csRGB, alpha=False)
                with Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples) as page_image:
                    page_rows, page_notices = _extract_with_local_ocr(page_image, pytesseract)
                if index == 0:
                    rows, notices = page_rows, page_notices
                elif any(row.get("date") is not None for row in page_rows):
                    notices.append("More than one scanned page contains forecast-like dates; review the intended week manually.")
        finally:
            document.close()
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
