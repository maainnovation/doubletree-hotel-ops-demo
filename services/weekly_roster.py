"""Deterministic employee-shift planning from aggregate weekly room demand."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import math
import re
from typing import Any

from services.forecast_import import WEEKDAYS, validate_forecast, normalize_forecast_rows
from services.staff_settings import validate_staff_settings


def _clock_minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _span(span: Any) -> tuple[str, str] | None:
    if not isinstance(span, str):
        return None
    match = re.fullmatch(r"((?:[01]\d|2[0-3]):[0-5]\d)-((?:[01]\d|2[0-3]):[0-5]\d)", span.strip())
    if not match or _clock_minutes(match.group(2)) <= _clock_minutes(match.group(1)):
        return None
    return match.group(1), match.group(2)


def _time_off(person: dict[str, Any]) -> set[str]:
    return {item.strip() for item in str(person.get("requested_time_off", "")).split(",") if item.strip()}


def _productive_hours(paid_hours: float, settings: dict[str, Any]) -> float:
    cfg = settings["paid_productive_hours"]
    return max(0.0, paid_hours - float(cfg["break_minutes"]) / 60 - float(cfg["other_duties_minutes"]) / 60)


def _roster_inputs(forecast_rows: list[dict[str, Any]], staff_payload: dict[str, Any]) -> dict[str, Any]:
    """Capture calculation inputs, excluding persistence and dataset-mode metadata."""
    return {"forecast": normalize_forecast_rows(forecast_rows),
            "staff": deepcopy(staff_payload["staff"]), "settings": deepcopy(staff_payload["settings"])}


def _input_signature(inputs: dict[str, Any]) -> str:
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_roster_current(roster: dict[str, Any] | None, forecast_rows: list[dict[str, Any]], staff_payload: dict[str, Any]) -> bool:
    """Older saved rosters and changed inputs require explicit regeneration."""
    if not roster or not roster.get("input_signature") or not roster.get("input_snapshot"):
        return False
    try:
        return roster["input_signature"] == _input_signature(_roster_inputs(forecast_rows, staff_payload))
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def roster_calculation_summary(roster: dict[str, Any]) -> list[dict[str, Any]]:
    """Explain all seven days using this roster's inputs, not newer edited settings.

    Demand headcount is a rounded-up standard-shift estimate, not a promise that
    eligible employees can be assigned. Scheduled cleaning coverage and conflicts
    are reported separately; inspection/other-duty rows do not clean rooms.
    """
    inputs = roster.get("input_snapshot")
    if not inputs:
        return []
    settings = inputs["settings"]
    clean = settings["cleaning_standards"]
    productive = _productive_hours(float(settings["paid_productive_hours"]["paid_shift_hours"]), settings) * 60
    minimum = int(settings["minimum_role_coverage"].get("Housekeeper", 0))
    staff_by_id = {str(person["staff_id"]): person for person in inputs["staff"]}
    summary = []
    for forecast in inputs["forecast"]:
        dkey = forecast["date"]
        demand = forecast["departures"] * float(clean["checkout_minutes"]) + forecast["stayovers"] * float(clean["stayover_minutes"])
        cleaning_rows = [row for row in roster["schedule"] if row["date"] == dkey
                         and staff_by_id.get(str(row["staff_id"]), {}).get("role") == "Housekeeper"
                         and row.get("code") == "SHIFT" and float(row.get("productive_hours", 0)) > 0]
        scheduled = sum(float(row["productive_hours"]) * 60 for row in cleaning_rows)
        uncovered = max(0.0, demand - scheduled)
        constraints = [item for item in roster.get("constraints", [])
                       if item.startswith(dkey) or not re.match(r"\d{4}-\d{2}-\d{2}\b", item)]
        demand_housekeepers = math.ceil(demand / productive)
        summary.append({"date": dkey, "weekday": date.fromisoformat(dkey).strftime("%a"),
                        "checkout_rooms": forecast["departures"], "checkout_minutes": float(clean["checkout_minutes"]),
                        "stayover_rooms": forecast["stayovers"], "stayover_minutes": float(clean["stayover_minutes"]),
                        "demand_minutes": round(demand, 2), "productive_minutes_per_shift": round(productive, 2),
                        "demand_housekeepers": demand_housekeepers, "minimum_housekeepers": minimum,
                        "required_housekeepers": max(demand_housekeepers, minimum),
                        "scheduled_housekeepers": len({row["staff_id"] for row in cleaning_rows}),
                        "scheduled_productive_minutes": round(scheduled, 2), "uncovered_minutes": round(uncovered, 2),
                        "constraints": constraints,
                        "status": "Uncovered demand" if uncovered > 0.001 else "Review constraints" if constraints else "Covered"})
    return summary


def generate_weekly_roster(forecast_rows: list[dict[str, Any]], staff_payload: dict[str, Any], prior: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generate staff/day shifts. Aggregate room counts never become room assignments."""
    forecast_errors = validate_forecast(forecast_rows)
    staff_errors = validate_staff_settings(staff_payload)
    if forecast_errors:
        raise ValueError("Forecast must be valid before scheduling: " + " ".join(forecast_errors))
    if staff_errors:
        raise ValueError("Staff & Settings must be valid before scheduling: " + " ".join(staff_errors))

    forecast = normalize_forecast_rows(forecast_rows)
    staff = staff_payload["staff"]
    settings = staff_payload["settings"]
    clean = settings["cleaning_standards"]
    coverage = settings["minimum_role_coverage"]
    prior_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for item in (prior or {}).get("schedule", []):
        if item.get("locked"):
            prior_by_key[(str(item.get("staff_id", "")), str(item.get("date", "")))] = deepcopy(item)
    staff_by_id = {str(person["staff_id"]): person for person in staff}
    rows: list[dict[str, Any]] = []
    constraints: list[str] = []
    weekly_paid: dict[str, float] = {str(person["staff_id"]): 0.0 for person in staff}
    daily_by_staff: dict[tuple[str, str], float] = {}
    scheduled_by_day_role: dict[tuple[str, str], int] = {}
    hk_productive_by_date: dict[str, float] = {}
    dates = [date.fromisoformat(row["date"]) for row in forecast]
    forecast_by_date = {row["date"]: row for row in forecast}
    candidate_by_key: dict[tuple[str, str], tuple[str, str, float, str]] = {}

    def make_row(person: dict[str, Any], day: date, code: str, start: str = "", end: str = "", paid: float = 0.0, locked: bool = False, note: str = "") -> dict[str, Any]:
        productive = _productive_hours(paid, settings) if paid else 0.0
        return {"staff_id": str(person["staff_id"]), "display_name": str(person["display_name"]), "role": str(person["role"]),
                "date": day.isoformat(), "weekday": WEEKDAYS[(day - dates[0]).days], "code": code,
                "start": start, "end": end, "paid_hours": round(paid, 2), "productive_hours": round(productive, 2),
                "locked": bool(locked), "notes": note}

    # Seed explicit OFF/unavailable/locked rows first; assignments are made in a second deterministic pass.
    row_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for day in dates:
        wd = day.strftime("%a")
        dkey = day.isoformat()
        for person in staff:
            sid = str(person["staff_id"])
            old = prior_by_key.pop((sid, dkey), None)
            if old:
                row = deepcopy(old)
                row["locked"] = True
                row["notes"] = (str(row.get("notes", "")) + " Preserved locked manual edit.").strip()
                if person.get("status") != "Active": constraints.append(f"{dkey} {sid}: a locked assignment is retained although this employee is inactive.")
                if dkey in _time_off(person) and row.get("code") not in {"OFF", "R/OFF"}: constraints.append(f"{dkey} {sid}: locked assignment conflicts with requested time off.")
                availability = json.loads(person.get("availability_json", "{}"))
                available_span = _span(availability.get(wd, "OFF"))
                if row.get("code") not in {"OFF", "R/OFF"}:
                    actual_span = _span(f"{row.get('start','')}-{row.get('end','')}") if row.get("start") and row.get("end") else None
                    if available_span is None or actual_span is None or _clock_minutes(actual_span[0]) < _clock_minutes(available_span[0]) or _clock_minutes(actual_span[1]) > _clock_minutes(available_span[1]):
                        constraints.append(f"{dkey} {sid}: locked shift conflicts with recurring availability or has unresolved times.")
                span = _span(f"{row.get('start','')}-{row.get('end','')}") if row.get("start") and row.get("end") else None
                paid = float(row.get("paid_hours", 0) or 0)
                if span:
                    paid = (_clock_minutes(span[1]) - _clock_minutes(span[0])) / 60
                    row["paid_hours"] = round(paid, 2)
                    row["productive_hours"] = round(_productive_hours(paid, settings), 2)
                elif row.get("code") not in {"OFF", "R/OFF"}:
                    constraints.append(f"{dkey} {sid}: locked {row.get('code')} duty has no explicit start and end time; its hours are not inferred.")
                    row["paid_hours"] = 0.0; row["productive_hours"] = 0.0
                else:
                    row["paid_hours"] = 0.0; row["productive_hours"] = 0.0
                weekly_paid[sid] = weekly_paid.get(sid, 0.0) + paid
                daily_by_staff[(sid, dkey)] = paid
                if paid > 0 and row.get("code") not in {"OFF", "R/OFF", "BANQUET"}:
                    key = (dkey, str(person["role"]))
                    scheduled_by_day_role[key] = scheduled_by_day_role.get(key, 0) + 1
                    if person["role"] == "Housekeeper" and row.get("code") == "SHIFT":
                        hk_productive_by_date[dkey] = hk_productive_by_date.get(dkey, 0.0) + float(row["productive_hours"])
                row_by_key[(sid, dkey)] = row
                continue
            code = "OFF"
            start, end, paid, note = "", "", 0.0, ""
            availability = json.loads(person.get("availability_json", "{}"))
            requested = _time_off(person)
            if person.get("status") != "Active":
                note = "Inactive employee; not assigned."
            elif dkey in requested:
                code = "R/OFF"; note = "Requested time off; not assigned."
            else:
                availability_value = availability.get(wd, "OFF")
                if _span(availability_value):
                    start, end = _span(availability_value)  # type: ignore[misc]
                    paid = (_clock_minutes(end) - _clock_minutes(start)) / 60
                    if paid > float(person["max_daily_hours"]):
                        note = f"Unresolved: availability span is {paid:g}h, over the {person['max_daily_hours']}h daily limit."
                        constraints.append(f"{dkey} {sid}: availability span exceeds the daily-hour limit; not assigned.")
                        start = end = ""; paid = 0.0
                    else:
                        extra_note = ""
                        if paid > float(settings["paid_productive_hours"]["paid_shift_hours"]):
                            extra_note = "Availability exceeds the demo paid-shift assumption; full explicit span shown."
                        candidate_by_key[(sid, dkey)] = (start, end, paid, extra_note)
                        code = "OFF"
                        note = "Available shift; assigned only when role coverage or forecast demand requires it."
                        start = end = ""
                        paid = 0.0
                elif availability_value != "OFF":
                    code = str(availability_value)
                    note = "Duty code has no explicit times; hours are not computed."
                    constraints.append(f"{dkey} {sid}: {code} has no explicit start and end time; not assigned to coverage or cleaning capacity.")
            row_by_key[(sid, dkey)] = make_row(person, day, code, start, end, paid, False, note)

    # Preserve stale locked rows even if a staff ID was removed; do not discard manual work.
    for (_, dkey), old in prior_by_key.items():
        if dkey not in forecast_by_date:
            continue
        row = deepcopy(old)
        row["locked"] = True
        row["notes"] = (str(row.get("notes", "")) + " Preserved locked row for staff no longer in roster.").strip()
        row_by_key[(str(row.get("staff_id", "")), dkey)] = row
        constraints.append(f"{dkey} {row.get('staff_id')}: retained locked assignment references an ID no longer in the staff roster.")

    for day in dates:
        dkey = day.isoformat()
        # Existing manual locks are carried into hour and role counts before new assignments.
        for person in staff:
            sid = str(person["staff_id"])
            row = row_by_key[(sid, dkey)]
            if row.get("locked"):
                paid = float(row.get("paid_hours", 0) or 0)
                if paid > float(person["max_daily_hours"]): constraints.append(f"{dkey} {sid}: locked shift exceeds the daily-hour limit.")
                if weekly_paid.get(sid, 0.0) > float(person["max_weekly_hours"]): constraints.append(f"{dkey} {sid}: locked shifts exceed the weekly-hour limit.")
        # A locked employee/time-off/off row is not silently reassigned.
        for role, minimum in coverage.items():
            assigned = scheduled_by_day_role.get((dkey, role), 0)
            candidates = [p for p in staff if p["role"] == role and p.get("status") == "Active" and not row_by_key[(str(p["staff_id"]), dkey)].get("locked") and row_by_key[(str(p["staff_id"]), dkey)].get("code") == "OFF" and (str(p["staff_id"]), dkey) in candidate_by_key]
            candidates.sort(key=lambda p: (float(p.get("hourly_cost", 0)), str(p["staff_id"])))
            for person in candidates[:max(0, int(minimum) - assigned)]:
                sid = str(person["staff_id"]); row = row_by_key[(sid, dkey)]; start, end, paid, extra_note = candidate_by_key[(sid, dkey)]
                if weekly_paid[sid] + paid > float(person["max_weekly_hours"]):
                    constraints.append(f"{dkey} {sid}: not scheduled because weekly-hour limit would be exceeded.")
                    row["code"] = "OFF"; row["start"] = row["end"] = ""; row["paid_hours"] = row["productive_hours"] = 0.0
                    continue
                row.update({"code": "SHIFT", "start": start, "end": end, "paid_hours": round(paid, 2),
                            "productive_hours": round(_productive_hours(paid, settings), 2), "notes": extra_note})
                weekly_paid[sid] += paid
                scheduled_by_day_role[(dkey, role)] = scheduled_by_day_role.get((dkey, role), 0) + 1
                if role == "Housekeeper": hk_productive_by_date[dkey] = hk_productive_by_date.get(dkey, 0.0) + float(row["productive_hours"])
            assigned = scheduled_by_day_role.get((dkey, role), 0)
            if assigned < int(minimum):
                constraints.append(f"{dkey}: {role} minimum coverage {int(minimum)} not met ({assigned} assigned).")

        demand = forecast_by_date[dkey]["departures"] * float(clean["checkout_minutes"]) + forecast_by_date[dkey]["stayovers"] * float(clean["stayover_minutes"])
        current = hk_productive_by_date.get(dkey, 0.0) * 60
        remaining = max(0.0, demand - current)
        if remaining > 0:
            candidates = [p for p in staff if p["role"] == "Housekeeper" and p.get("status") == "Active" and not row_by_key[(str(p["staff_id"]), dkey)].get("locked") and row_by_key[(str(p["staff_id"]), dkey)].get("code") == "OFF" and (str(p["staff_id"]), dkey) in candidate_by_key]
            candidates.sort(key=lambda p: (-_productive_hours(candidate_by_key[(str(p["staff_id"]), dkey)][2], settings), float(p.get("hourly_cost", 0)), str(p["staff_id"])))
            for person in candidates:
                if remaining <= 0: break
                sid = str(person["staff_id"]); row = row_by_key[(sid, dkey)]; start, end, paid, extra_note = candidate_by_key[(sid, dkey)]
                if weekly_paid[sid] + paid > float(person["max_weekly_hours"]):
                    constraints.append(f"{dkey} {sid}: not scheduled for cleaning demand because weekly-hour limit would be exceeded.")
                    row["code"] = "OFF"; row["start"] = row["end"] = ""; row["paid_hours"] = row["productive_hours"] = 0.0
                    continue
                row.update({"code": "SHIFT", "start": start, "end": end, "paid_hours": round(paid, 2),
                            "productive_hours": round(_productive_hours(paid, settings), 2), "notes": extra_note})
                weekly_paid[sid] += paid
                weekly_paid[sid] = round(weekly_paid[sid], 4)
                scheduled_by_day_role[(dkey, "Housekeeper")] = scheduled_by_day_role.get((dkey, "Housekeeper"), 0) + 1
                productive = float(row["productive_hours"]) * 60
                hk_productive_by_date[dkey] = hk_productive_by_date.get(dkey, 0.0) + productive / 60
                remaining -= productive
            if remaining > 0:
                constraints.append(f"{dkey}: {remaining:,.0f} cleaning minutes remain uncovered after all eligible shifts and hour limits.")

    schedule = sorted(row_by_key.values(), key=lambda row: (row["date"], next((i for i, p in enumerate(staff) if str(p["staff_id"]) == row["staff_id"]), len(staff)), row["staff_id"]))
    # Re-evaluate any stale locked rows and all hard limits before labeling the result.
    paid_total: dict[str, float] = {}
    daily_total: dict[tuple[str, str], float] = {}
    for row in schedule:
        sid = str(row["staff_id"]); paid = float(row.get("paid_hours", 0) or 0)
        paid_total[sid] = paid_total.get(sid, 0.0) + paid
        daily_total[(sid, str(row["date"]))] = paid
        person = staff_by_id.get(sid)
        if person and paid > float(person["max_daily_hours"]): constraints.append(f"{row['date']} {sid}: shift exceeds the daily-hour limit.")
    for sid, paid in paid_total.items():
        person = staff_by_id.get(sid)
        if person and paid > float(person["max_weekly_hours"]): constraints.append(f"{sid}: weekly paid hours {paid:g} exceed the {person['max_weekly_hours']}h limit.")

    constraints = list(dict.fromkeys(constraints))
    infeasible = any("minimum coverage" in x or "uncovered" in x or "exceed" in x or "conflicts" in x or "no longer" in x or "no explicit" in x for x in constraints)
    inputs = _roster_inputs(forecast, staff_payload)
    return {"week_start": forecast[0]["date"], "week_end": forecast[-1]["date"], "forecast": forecast,
            "input_signature": _input_signature(inputs), "input_snapshot": inputs,
            "schedule": schedule, "constraints": constraints, "feasible": not infeasible,
            "demand_minutes": {row["date"]: int(row["departures"] * float(clean["checkout_minutes"]) + row["stayovers"] * float(clean["stayover_minutes"])) for row in forecast},
            "hours_by_staff": {sid: round(hours, 2) for sid, hours in paid_total.items()}, "version": int((prior or {}).get("version", 0)) + 1}


def schedule_changes(before: dict[str, Any] | None, after: dict[str, Any]) -> list[dict[str, Any]]:
    old = {(x["staff_id"], x["date"]): x for x in (before or {}).get("schedule", [])}
    new = {(x["staff_id"], x["date"]): x for x in after.get("schedule", [])}
    diffs = []
    fields = ("display_name", "role", "code", "start", "end", "paid_hours", "productive_hours", "locked")
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        changed = [f for f in fields if (a or {}).get(f) != (b or {}).get(f)]
        if changed:
            diffs.append({"staff_id": key[0], "date": key[1], "change": "Added" if a is None else "Removed" if b is None else ", ".join(changed),
                          "before": " / ".join(str(a.get(f, "")) for f in ("code", "start", "end", "paid_hours")) if a else "—",
                          "after": " / ".join(str(b.get(f, "")) for f in ("code", "start", "end", "paid_hours")) if b else "—"})
    return diffs


def weekly_grid(schedule: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group employee shifts into the target Fri–Thu roster sections."""
    role_order = ("Housekeeper", "Houseman", "Lobby Staff", "Manager")
    dates = sorted({str(row["date"]) for row in schedule})
    grouped: dict[str, list[dict[str, Any]]] = {role: [] for role in role_order}
    people: dict[tuple[str, str], dict[str, Any]] = {}
    for row in schedule:
        key = (str(row.get("role", "")), str(row.get("staff_id", "")))
        people.setdefault(key, {"Role": key[0], "Employee": row.get("display_name", ""), "Staff ID": key[1], **{d: "" for d in dates}, "Paid hours": 0.0, "Productive hours": 0.0})
        cell = str(row.get("code", "OFF"))
        if row.get("start") and row.get("end"):
            cell = f"{cell} {row['start']}–{row['end']}"
        people[key][str(row["date"])] = cell
        people[key]["Paid hours"] += float(row.get("paid_hours", 0) or 0)
        people[key]["Productive hours"] += float(row.get("productive_hours", 0) or 0)
    for (role, _), row in people.items():
        if role in grouped:
            grouped[role].append(row)
    for role in role_order:
        grouped[role].sort(key=lambda row: row["Staff ID"])
    return grouped


def build_exports(roster: dict[str, Any]) -> tuple[bytes, bytes]:
    """Export the captured roster, daily calculations, locks, and honest gaps."""
    from io import BytesIO
    from pathlib import Path
    from xml.sax.saxutils import escape
    import pandas as pd
    import reportlab
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    summary = roster_calculation_summary(roster)
    xlsx_out = BytesIO()
    with pd.ExcelWriter(xlsx_out, engine="openpyxl") as writer:
        pd.DataFrame(roster["forecast"]).to_excel(writer, sheet_name="Forecast", index=False)
        pd.DataFrame(roster["schedule"]).to_excel(writer, sheet_name="Weekly Shift Roster", index=False)
        grid_rows = [row for rows in weekly_grid(roster["schedule"]).values() for row in rows]
        pd.DataFrame(grid_rows).to_excel(writer, sheet_name="Fri-Thu Grid", index=False)
        pd.DataFrame(summary).to_excel(writer, sheet_name="Daily Calculations", index=False)
        pd.DataFrame({"Constraint": roster.get("constraints", []) or ["None identified"]}).to_excel(writer, sheet_name="Validation", index=False)
    pdf_out = BytesIO()
    doc = SimpleDocTemplate(pdf_out, pagesize=landscape(letter), rightMargin=24, leftMargin=24, topMargin=24, bottomMargin=30)
    # Embed ReportLab's bundled fonts so Linux/headless renderers do not rely on
    # platform Helvetica substitution (which can corrupt spacing in exports).
    font_dir = Path(reportlab.__file__).parent / "fonts"
    for name, filename in (("RosterSans", "Vera.ttf"), ("RosterSans-Bold", "VeraBd.ttf"),
                           ("RosterSans-Italic", "VeraIt.ttf"), ("RosterSans-BoldItalic", "VeraBI.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(font_dir / filename)))
    pdfmetrics.registerFontFamily("RosterSans", normal="RosterSans", bold="RosterSans-Bold",
                                italic="RosterSans-Italic", boldItalic="RosterSans-BoldItalic")
    styles = getSampleStyleSheet()
    for name in ("Normal", "BodyText", "Italic"):
        styles[name].fontName = "RosterSans"
    for name in ("Title", "Heading1", "Heading2"):
        styles[name].fontName = "RosterSans-Bold"
    styles.add(ParagraphStyle("RosterCell", parent=styles["Normal"], fontSize=7, leading=8.5))
    styles.add(ParagraphStyle("SummaryCell", parent=styles["Normal"], fontSize=8, leading=10))
    styles.add(ParagraphStyle("SmallNote", parent=styles["Normal"], fontSize=8, leading=10))

    def text(value):
        # Source documents and saved staff labels are data, never ReportLab XML.
        return escape(str(value)).replace("\n", "<br/>")

    def cell(value, style="RosterCell"):
        return Paragraph(text(value), styles[style])

    source = roster.get("source") if isinstance(roster.get("source"), dict) else {}
    metadata = []
    hotel_name = source.get("hotel_name") or roster.get("hotel_name")
    if hotel_name:
        metadata.append(str(hotel_name))
    if roster.get("version"):
        metadata.append(f"Version {roster['version']}")
    if source.get("channel") == "whatsapp":
        metadata.append("Received via WhatsApp")
    status = "Covered under saved assumptions" if roster["feasible"] else "NEEDS ATTENTION - INCOMPLETE / CONSTRAINTS UNMET"
    story = [Paragraph("Weekly Employee Shift Roster", styles["Title"]),
             cell(f"{roster['week_start']} to {roster['week_end']} | {status}", "SummaryCell")]
    if metadata:
        story.append(cell(" | ".join(metadata), "SummaryCell"))
    story.append(Spacer(1, 10))
    dates = [row["date"] for row in roster["forecast"]]
    date_headers = [datetime.strptime(d, "%Y-%m-%d").strftime("%a %m/%d") for d in dates]
    table_data = [["Role / employee", "Staff ID", *date_headers, "Paid h", "Prod h"]]
    role_row_indices = []
    locked = {(str(row["staff_id"]), row["date"]) for row in roster["schedule"] if row.get("locked")}
    for role, employees in weekly_grid(roster["schedule"]).items():
        role_row_indices.append(len(table_data))
        table_data.append([cell(role), *([""] * (len(date_headers) + 3))])
        for employee in employees:
            shifts = [str(employee[d]).replace("–", "-").removeprefix("SHIFT ") + (" *" if (employee["Staff ID"], d) in locked else "") for d in dates]
            table_data.append([cell(employee["Employee"]), cell(employee["Staff ID"]), *[cell(value) for value in shifts],
                               f"{employee['Paid hours']:.1f}", f"{employee['Productive hours']:.1f}"])
    table = Table(table_data, repeatRows=1, colWidths=[114, 74, *([65] * 7), 46, 46])
    styles_list = [("BACKGROUND", (0,0),(-1,0), colors.HexColor("#154734")), ("TEXTCOLOR",(0,0),(-1,0),colors.white), ("FONTNAME",(0,0),(-1,-1),"RosterSans"), ("FONTNAME",(0,0),(-1,0),"RosterSans-Bold"), ("FONTSIZE",(0,0),(-1,-1),7), ("GRID",(0,0),(-1,-1),0.25,colors.grey), ("VALIGN",(0,0),(-1,-1),"MIDDLE"), ("TOPPADDING",(0,0),(-1,-1),2), ("BOTTOMPADDING",(0,0),(-1,-1),2), ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F2F5F3")])]
    for index in role_row_indices:
        styles_list.extend([("SPAN", (0,index), (1,index)), ("SPAN", (2,index), (-1,index)), ("BACKGROUND", (0,index), (-1,index), colors.HexColor("#E8F0EC")), ("FONTNAME", (0,index), (-1,index), "RosterSans-Bold")])
    table.setStyle(TableStyle(styles_list))
    story.extend([table, Spacer(1, 8), cell("Times indicate scheduled SHIFT; * Preserved locked assignment. Paid hours and productive cleaning time differ; non-cleaning roles do not cover room-cleaning demand.", "SmallNote")])
    story.extend([PageBreak(), Paragraph("Day-by-day calculation summary", styles["Heading1"]),
                  cell("Calculated from this version's saved forecast, staff availability, and settings. Arrivals are not additional checkout cleans. Required headcount rounds up a standard productive shift; eligibility, hour limits, and locks still apply.", "SummaryCell"), Spacer(1, 10)])
    if summary:
        calculations = [["Day", "Cleaning workload (minutes)", "Required / scheduled", "Coverage / attention"]]
        for day in summary:
            calculations.append([
                cell(f"{day['weekday']} {day['date']}", "SummaryCell"),
                cell(f"{day['checkout_rooms']:g} checkouts x {day['checkout_minutes']:g} + {day['stayover_rooms']:g} stayovers x {day['stayover_minutes']:g} = {day['demand_minutes']:,.0f} min", "SummaryCell"),
                cell(f"{day['demand_minutes']:,.0f} / {day['productive_minutes_per_shift']:g} min per shift, round up; minimum {day['minimum_housekeepers']}. Needed {day['required_housekeepers']}; scheduled {day['scheduled_housekeepers']}.", "SummaryCell"),
                cell(f"Scheduled {day['scheduled_productive_minutes']:,.0f} min. Uncovered {day['uncovered_minutes']:,.0f} min. {day['status']}.", "SummaryCell"),
            ])
        summary_table = Table(calculations, repeatRows=1, colWidths=[98, 230, 220, 187])
        summary_table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#154734")), ("TEXTCOLOR",(0,0),(-1,0),colors.white), ("FONTNAME",(0,0),(-1,0),"RosterSans-Bold"), ("FONTSIZE",(0,0),(-1,0),8), ("GRID",(0,0),(-1,-1),0.25,colors.grey), ("VALIGN",(0,0),(-1,-1),"TOP"), ("TOPPADDING",(0,0),(-1,-1),6), ("BOTTOMPADDING",(0,0),(-1,-1),6), ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F2F5F3")])]))
        story.append(summary_table)
    else:
        story.append(cell("This older saved version has no captured calculation inputs. Regenerate it before relying on a daily explanation.", "SummaryCell"))
    story.extend([Spacer(1, 12), Paragraph("Constraints and uncovered demand", styles["Heading2"])])
    for constraint in roster.get("constraints", []) or ["None identified under the saved assumptions. This is not a guarantee of operational readiness."]:
        story.extend([cell(constraint, "SummaryCell"), Spacer(1, 3)])
    story.extend([Spacer(1, 10), cell("DEMO ASSUMPTIONS - Aggregate forecast counts are not room-level assignments. Review and confirm before operational use. Needs-attention rosters have not silently solved shortages or lock conflicts.", "SmallNote")])

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont("RosterSans", 7)
        canvas.setFillColor(colors.HexColor("#58655F"))
        canvas.drawString(24, 16, "Hotel Operations Copilot | Synthetic / development workflow")
        canvas.drawRightString(768, 16, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return xlsx_out.getvalue(), pdf_out.getvalue()
