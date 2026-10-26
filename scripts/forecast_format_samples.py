"""Create fabricated forecast-format fixtures and a cell-level expectation manifest.

All generated values/dates are synthetic. This module is shared by automated
format-variation tests and the optional user-facing sample ZIP.
"""
from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
from typing import Any

import os

# This file also supports direct script execution outside package startup.
os.environ["ORT_DISABLE_TELEMETRY"] = "1"

from PIL import Image, ImageDraw, ImageFont
import pymupdf
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfgen import canvas

FIELDS = (
    "arrivals", "departures", "stayovers", "rooms_on_books",
    "forecast_pickup", "forecast_occupied", "occupancy_pct",
)
LABELS = {
    "arrivals": "Arrivals",
    "departures": "Departures",
    "stayovers": "Stayovers",
    "rooms_on_books": "Rooms on books",
    "forecast_pickup": "Forecast pickup",
    "forecast_occupied": "Forecast occupied",
    "occupancy_pct": "Occupancy %",
}

BASE = [
    {"date": date(2026, 9, 25), "arrivals": 0, "departures": 0, "stayovers": 0, "rooms_on_books": 0, "forecast_pickup": 0, "forecast_occupied": 0, "occupancy_pct": 0.0, "prior_day_occupancy": None},
    {"date": date(2026, 9, 26), "arrivals": 18, "departures": 12, "stayovers": 5, "rooms_on_books": 23, "forecast_pickup": 2, "forecast_occupied": 25, "occupancy_pct": 25.5, "prior_day_occupancy": None},
    {"date": date(2026, 9, 27), "arrivals": 14, "departures": 14, "stayovers": 8, "rooms_on_books": 22, "forecast_pickup": 3, "forecast_occupied": 25, "occupancy_pct": 24.75, "prior_day_occupancy": None},
    {"date": date(2026, 9, 28), "arrivals": 22, "departures": 10, "stayovers": 3, "rooms_on_books": 25, "forecast_pickup": 4, "forecast_occupied": 29, "occupancy_pct": 29.25, "prior_day_occupancy": None},
    {"date": date(2026, 9, 29), "arrivals": 17, "departures": 16, "stayovers": 7, "rooms_on_books": 24, "forecast_pickup": 1, "forecast_occupied": 25, "occupancy_pct": 25.0, "prior_day_occupancy": None},
    {"date": date(2026, 9, 30), "arrivals": 19, "departures": 9, "stayovers": 4, "rooms_on_books": 23, "forecast_pickup": 0, "forecast_occupied": 23, "occupancy_pct": 23.0, "prior_day_occupancy": None},
    {"date": date(2026, 10, 1), "arrivals": 21, "departures": 11, "stayovers": 6, "rooms_on_books": 27, "forecast_pickup": 5, "forecast_occupied": 32, "occupancy_pct": 32.0, "prior_day_occupancy": None},
]


def _date_label(day: date, style: str) -> str:
    if style == "us": return f"{day.month}/{day.day}/{day.year}"
    if style == "iso": return day.isoformat()
    if style == "month_day": return day.strftime("%b %d, %Y")
    if style == "day_month": return day.strftime("%d %b %Y")
    raise ValueError(style)


def _metric_label(field: str, style: str) -> str:
    if style == "pickup" and field == "forecast_pickup": return "Forecasted Pick-up"
    if style == "occupied_typo" and field == "forecast_occupied": return "Forcasted occupied rooms"
    if style == "punctuated":
        return {
            "arrivals": "Arrivals:", "departures": "Departure(s)", "stayovers": "Stay-over rooms",
            "rooms_on_books": "Rooms on the Books", "forecast_pickup": "Forecasted Pick up",
            "forecast_occupied": "Forecasted occupied rooms", "occupancy_pct": "Occupancy percent",
        }[field]
    return LABELS[field]


def _cell(value: Any, field: str, percent_style: str) -> str:
    if value is None: return "—"
    if field != "occupancy_pct": return str(int(value))
    if percent_style == "percent": return f"{value:g}%"
    if percent_style == "word": return f"{value:g} percent"
    return f"{value:g}"


def _render_image(rows, order, date_style, label_style, percent_style, metric_order=None, missing_field=None, invalid_total=False):
    width, height = 2800, 1260
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    regular = ImageFont.truetype(font_path, 40)
    bold = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 40)
    x0, label_width, cell_width, top, row_height = 80, 650, 290, 100, 145
    draw.text((x0, top), "Metric", fill="#111111", font=bold)
    for column, idx in enumerate(order):
        x = x0 + label_width + column * cell_width
        draw.text((x, top), _date_label(rows[idx]["date"], date_style), fill="#111111", font=bold)
    order_fields = metric_order or list(FIELDS)
    for row_idx, field in enumerate(order_fields, 1):
        y = top + row_idx * row_height
        draw.text((x0, y), _metric_label(field, label_style), fill="#111111", font=bold if field == "occupancy_pct" else regular)
        values = []
        for idx in order:
            value = rows[idx][field]
            if invalid_total and field == "rooms_on_books" and idx == order[0]: value += 1
            values.append(None if missing_field == field and idx == order[-1] else value)
        for column, value in enumerate(values):
            x = x0 + label_width + column * cell_width
            draw.text((x, y), _cell(value, field, percent_style), fill="#111111", font=regular)
    return image


def _native_text(rows, order, date_style, label_style, percent_style, metric_order=None, wrap=False, missing_field=None, invalid_total=False):
    lines = ["Forecast dates: " + " | ".join(_date_label(rows[index]["date"], date_style) for index in order)]
    for field in metric_order or list(FIELDS):
        label = _metric_label(field, label_style)
        if wrap and field in {"forecast_pickup", "forecast_occupied"}:
            label = label.replace(" ", "\n", 1)
        values = []
        for index in order:
            value = rows[index][field]
            if invalid_total and field == "rooms_on_books" and index == order[0]: value += 1
            values.append(None if missing_field == field and index == order[-1] else value)
        lines.append(label + ": " + " | ".join(_cell(value, field, percent_style) for value in values))
    return "\n".join(lines)


def _write_pdf_text(path: Path, text: str):
    output = canvas.Canvas(str(path), pagesize=landscape(letter))
    output.setFont("Helvetica", 10)
    y = 560
    for line in text.splitlines():
        output.drawString(28, y, line[:180])
        y -= 34
    output.save()


def _write_scanned_pdf(path: Path, image: Image.Image):
    temp = path.with_suffix(".png")
    image.save(temp, format="PNG")
    image_doc = pymupdf.open()
    page = image_doc.new_page(width=image.width, height=image.height)
    page.insert_image(page.rect, filename=str(temp))
    path.write_bytes(image_doc.tobytes())
    temp.unlink()


def _case(name, filename, rows, order, mode, expected_valid=True, date_style="us", label_style="standard", percent_style="percent", **kwargs):
    return {"name": name, "file": filename, "rows": rows, "source_order": order, "mode": mode,
            "expected_valid": expected_valid, "date_style": date_style, "label_style": label_style,
            "percent_style": percent_style, **kwargs}


def build_forecast_variants(destination: str | Path) -> dict[str, Any]:
    """Write six positive and five negative cases, returning their manifest."""
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=True)
    rows = [dict(row) for row in BASE]
    normal = list(range(7))
    reordered_a = [3, 0, 5, 1, 6, 2, 4]
    reordered_b = [2, 4, 6, 1, 3, 5, 0]
    reversed_metrics = list(reversed(FIELDS))
    cases = [
        _case("native text / US numeric dates / pickup spelling", "native_us_labels.pdf", rows, normal, "pdf_text", date_style="us", label_style="pickup", percent_style="percent"),
        _case("native text / ISO dates / shuffled date columns", "native_iso_reordered.pdf", rows, reordered_a, "pdf_text", date_style="iso", label_style="occupied_typo", percent_style="word"),
        _case("native text / month names / reordered metric rows / wrapped labels", "native_month_wrapped.pdf", rows, reordered_b, "pdf_text", date_style="month_day", label_style="punctuated", percent_style="plain", metric_order=reversed_metrics, wrap=True),
        _case("scanned PDF / shuffled date columns / fractional percentages", "scanned_reordered.pdf", rows, reordered_b, "ocr_pdf", date_style="us", label_style="pickup", percent_style="percent"),
        _case("PNG / US numeric dates / typo and label punctuation", "forecast_variant.png", rows, normal, "ocr_image", date_style="us", label_style="occupied_typo", percent_style="word", metric_order=reversed_metrics),
        _case("JPEG / ISO dates / pickup labels", "forecast_variant.jpg", rows, reordered_a, "ocr_image", date_style="iso", label_style="pickup", percent_style="percent"),
        _case("negative / ambiguous numeric dates", "negative_ambiguous_dates.pdf", rows, normal, "pdf_text", expected_valid=False, date_style="ambiguous"),
        _case("negative / duplicate date", "negative_duplicate_date.pdf", rows, normal, "pdf_text", expected_valid=False, date_style="duplicate"),
        _case("negative / missing date", "negative_missing_date.pdf", rows, normal, "pdf_text", expected_valid=False, date_style="missing_date"),
        _case("negative / invalid total", "negative_invalid_total.pdf", rows, normal, "pdf_text", expected_valid=False, date_style="us", invalid_total=True, expected_error="rooms on books"),
        _case("negative / missing metric cell", "negative_missing_cell.pdf", rows, normal, "pdf_text", expected_valid=False, date_style="us", missing_field="departures", expected_error="departures is missing"),
    ]
    manifest_cases = []
    for case in cases:
        source_order = case["source_order"]
        style = case["date_style"]
        actual_column_order = [rows[index]["date"].isoformat() for index in source_order]
        if style == "ambiguous":
            series_dates = [date(2026, 10, 2) + timedelta(days=i) for i in range(7)]
            actual_column_order = [day.isoformat() for day in series_dates]
            line = "Forecast dates: " + " | ".join(f"{d.month}/{d.day}/{d.year}" for d in series_dates)
            text = line + "\n" + "\n".join(
                _metric_label(field, "standard") + ": " + " | ".join(_cell(row[field], field, "percent") for row in rows)
                for field in FIELDS)
        elif style == "duplicate":
            local_order = source_order.copy()
            printed_dates = [rows[i]["date"] for i in local_order]
            printed_dates[-1] = printed_dates[-2]
            actual_column_order = [day.isoformat() for day in printed_dates]
            line = "Forecast dates: " + " | ".join(_date_label(d, "us") for d in printed_dates)
            text = line + "\n" + "\n".join(
                _metric_label(field, "standard") + ": " + " | ".join(_cell(rows[i][field], field, "percent") for i in local_order)
                for field in FIELDS)
        elif style == "missing_date":
            local_order = source_order[:-1]
            actual_column_order = [rows[index]["date"].isoformat() for index in local_order]
            line = "Forecast dates: " + " | ".join(_date_label(rows[i]["date"], "us") for i in local_order)
            text = line + "\n" + "\n".join(
                _metric_label(field, "standard") + ": " + " | ".join(_cell(rows[i][field], field, "percent") for i in source_order)
                for field in FIELDS)
        else:
            date_style = "us" if style in {"duplicate", "missing_date"} else style
            text = _native_text(rows, source_order, date_style, case["label_style"], case["percent_style"],
                                metric_order=case.get("metric_order"), wrap=case.get("wrap", False),
                                missing_field=case.get("missing_field"), invalid_total=case.get("invalid_total", False))
        target = dest / case["file"]
        if case["mode"] == "pdf_text":
            _write_pdf_text(target, text)
        elif case["mode"] == "ocr_pdf":
            image = _render_image(rows, source_order, case["date_style"], case["label_style"], case["percent_style"], case.get("metric_order"))
            _write_scanned_pdf(target, image)
        else:
            image = _render_image(rows, source_order, case["date_style"], case["label_style"], case["percent_style"], case.get("metric_order"))
            image.save(target, format="PNG" if target.suffix == ".png" else "JPEG", quality=95)

        sorted_rows = []
        for row in rows:
            expected = {key: (row["date"].isoformat() if key == "date" else row[key]) for key in ("date", *FIELDS)}
            if case.get("invalid_total") and row["date"] == rows[source_order[0]]["date"]:
                expected["rooms_on_books"] += 1
            if case.get("missing_field") == "departures" and row["date"] == rows[source_order[-1]]["date"]:
                expected["departures"] = None
            sorted_rows.append(expected)
        if style == "duplicate":
            for row in sorted_rows: row["expected_not_assigned_due_duplicate_dates"] = True
        if style == "missing_date":
            for row in sorted_rows: row["expected_not_assigned_due_missing_date"] = True
        if style == "ambiguous":
            sorted_rows = [{"date": (date(2026, 10, 2) + timedelta(days=i)).isoformat(), **{field: rows[i][field] for field in FIELDS}} for i in range(7)]
            for row in sorted_rows: row["expected_not_assigned_due_ambiguous_dates"] = True
        manifest_cases.append({
            "name": case["name"], "file": target.name, "file_type": target.suffix[1:],
            "extraction_mode": case["mode"], "expected_valid": case["expected_valid"],
            "source_column_order": actual_column_order,
            "expected_by_date": sorted_rows,
            **({"expected_error": case["expected_error"]} if case.get("expected_error") else {}),
        })
    manifest = {
        "title": "Synthetic forecast format robustness pack",
        "disclaimer": "All forecasts, dates, and values in this package are fabricated for software testing. Not hotel data or policy.",
        "dates": [row["date"].isoformat() for row in rows],
        "metrics": list(FIELDS),
        "metric_labels": LABELS,
        "positive_cases": sum(case["expected_valid"] for case in cases),
        "negative_cases": sum(not case["expected_valid"] for case in cases),
        "cases": manifest_cases,
    }
    (dest / "expected_results.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (dest / "README.txt").write_text(
        "Synthetic hotel-forecast test files\n\n"
        "All dates, counts, names, and layouts here are fabricated. These files are for local extraction/review testing only.\n"
        "See expected_results.json for exact field-by-date values and expected validation outcomes.\n"
        "Positive examples: native-text PDF, shuffled columns/rows, wrapped labels, scanned PDF, PNG, JPEG.\n"
        "Negative examples: ambiguous, duplicate, missing dates; invalid arithmetic total; missing metric cell.\n"
        "A pass on these samples does not establish accuracy on other hotel documents.\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    result = build_forecast_variants(args.destination)
    print(f"Generated {result['positive_cases']} positive and {result['negative_cases']} negative synthetic cases in {args.destination}")
