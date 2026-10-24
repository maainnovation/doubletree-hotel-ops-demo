"""Demo staff and editable operating assumptions stored separately from the workbook."""
from copy import deepcopy
import json
import math
import re
from datetime import date

DEMO_SEED_VERSION = 1
ROLE_COUNTS = {"Housekeeper": 16, "Houseman": 4, "Lobby Staff": 2, "Manager": 2}

def demo_defaults():
    names = {
        "Housekeeper": ["Avery Brooks", "Jordan Ellis", "Morgan Reed", "Casey Bennett", "Riley Parker", "Taylor Quinn", "Cameron Hayes", "Drew Sullivan", "Jamie Foster", "Alex Morgan", "Skyler James", "Peyton Clark", "Reese Bailey", "Kendall Price", "Rowan Kelly", "Emerson Cruz"],
        "Houseman": ["Logan Rivera", "Dakota Wells", "Finley Ross", "Harper Cole"],
        "Lobby Staff": ["Sage Turner", "Arden Lee"],
        "Manager": ["Blake Monroe", "Shiloh Grant"],
    }
    prefixes = {"Housekeeper": "DEMO-HK", "Houseman": "DEMO-HM", "Lobby Staff": "DEMO-LB", "Manager": "DEMO-MG"}
    skills = {"Housekeeper": "checkout cleaning; stayover service", "Houseman": "houseperson; linen; public areas", "Lobby Staff": "lobby; public areas", "Manager": "supervision; inspection"}
    staff = []
    for role, people in names.items():
        for i, person in enumerate(people, 1):
            staff.append({"staff_id": f"{prefixes[role]}-{i:02d}", "display_name": person, "role": role, "status": "Active", "skills": skills[role], "availability_json": json.dumps({"Mon":"09:00-17:00", "Tue":"09:00-17:00", "Wed":"09:00-17:00", "Thu":"09:00-17:00", "Fri":"09:00-17:00", "Sat":"OFF", "Sun":"OFF"}), "requested_time_off": "", "shift_pattern": "Demo day shift (09:00-17:00)", "max_weekly_hours": 40, "max_daily_hours": 8, "hourly_cost": 25.0})
    settings = {
        "demo_assumptions": True,
        "cleaning_standards": {"checkout_minutes": 22, "stayover_minutes": 10, "stayover_service_policy": "Service all stayovers (editable demo assumption)", "inspection_minutes": 3},
        "paid_productive_hours": {"paid_shift_hours": 8, "break_minutes": 60, "other_duties_minutes": 30},
        "minimum_role_coverage": {"Housekeeper": 1, "Houseman": 1, "Lobby Staff": 1, "Manager": 1},
        "break_policy": {"break_minutes": 60, "breaks_required": True, "notes": "Demo assumption; paid hours and productive hours are modeled separately."},
        "status_codes": {"OFF": "Not scheduled", "R/OFF": "Requested unavailable", "BANQUET": "Banquet duty; non-cleaning", "INSPECT": "Inspection duty"},
        "notes": "All values on this page are DEMO ASSUMPTIONS, not hotel policy. Add explicit end times before calculating shift hours.",
    }
    return {"staff": staff, "settings": settings, "dataset_mode": "demo"}

def validate_staff_settings(payload):
    errors = []
    staff = payload.get("staff", [])
    if not isinstance(staff, list) or not staff:
        return ["Add at least one staff row."]
    ids = [str(x.get("staff_id", "")).strip().upper() for x in staff]
    if any(not x for x in ids): errors.append("Every staff member needs a stable staff ID.")
    if len(ids) != len(set(ids)): errors.append("Staff IDs must be unique (case-insensitive).")
    roles = set(ROLE_COUNTS)
    for i, person in enumerate(staff, 1):
        label = f"Staff row {i}"
        if not str(person.get("display_name", "")).strip(): errors.append(f"{label}: display name is required.")
        if person.get("role") not in roles: errors.append(f"{label}: choose a supported role.")
        if person.get("status") not in {"Active", "Inactive"}: errors.append(f"{label}: status must be Active or Inactive.")
        try:
            requested = str(person.get("requested_time_off", "")).strip()
            if requested:
                for day in requested.split(","):
                    date.fromisoformat(day.strip())
        except ValueError:
            errors.append(f"{label}: requested time off must be comma-separated ISO dates (YYYY-MM-DD).")
        for field in ("max_weekly_hours", "max_daily_hours", "hourly_cost"):
            try:
                n = float(person.get(field))
                if not math.isfinite(n) or n < 0 or (field != "hourly_cost" and n > 168): raise ValueError
            except (TypeError, ValueError): errors.append(f"{label}: {field.replace('_', ' ')} must be a valid nonnegative number.")
        try:
            availability = json.loads(person.get("availability_json", "{}"))
            if not isinstance(availability, dict): raise ValueError
            total_hours = 0.0
            max_day_hours = 0.0
            for day, span in availability.items():
                if day not in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}: raise ValueError
                if span not in {"OFF", "R/OFF", "BANQUET", "INSPECT"} and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", str(span)):
                    raise ValueError
                if "-" in str(span) and span not in {"R/OFF", "BANQUET", "INSPECT"}:
                    start, end = str(span).split("-")
                    if end <= start: raise ValueError
                    start_minutes = int(start[:2]) * 60 + int(start[3:])
                    end_minutes = int(end[:2]) * 60 + int(end[3:])
                    shift_hours = (end_minutes - start_minutes) / 60
                    total_hours += shift_hours
                    max_day_hours = max(max_day_hours, shift_hours)
            if total_hours > float(person["max_weekly_hours"]):
                errors.append(f"{label}: recurring availability exceeds the weekly hour limit.")
            if max_day_hours > float(person["max_daily_hours"]):
                errors.append(f"{label}: a shift exceeds the daily hour limit.")
        except (ValueError, TypeError, json.JSONDecodeError):
            errors.append(f"{label}: availability must be valid JSON with weekday keys and explicit HH:MM-HH:MM spans; imported start-only shifts need an end time.")
    settings = payload.get("settings")
    if payload.get("dataset_mode", "demo") not in {"demo", "operational"}:
        errors.append("Choose either the explicitly confirmed demo dataset or operational dataset mode.")
    if not isinstance(settings, dict) or not settings.get("demo_assumptions"):
        errors.append("Settings must remain labeled as DEMO ASSUMPTIONS.")
    else:
        try:
            clean = settings["cleaning_standards"]
            if not 0 < float(clean["checkout_minutes"]) <= 240 or not 0 < float(clean["stayover_minutes"]) <= 180 or not 0 <= float(clean["inspection_minutes"]) <= 120: raise ValueError
            hours = settings["paid_productive_hours"]
            if not 0.5 <= float(hours["paid_shift_hours"]) <= 24 or not 0 <= float(hours["break_minutes"]) <= 720 or not 0 <= float(hours["other_duties_minutes"]) <= 720: raise ValueError
            if float(hours["paid_shift_hours"]) * 60 <= float(hours["break_minutes"]) + float(hours["other_duties_minutes"]): raise ValueError
            if not isinstance(settings["break_policy"]["breaks_required"], bool) or int(settings["break_policy"]["break_minutes"]) != int(hours["break_minutes"]): raise ValueError
            coverage = settings["minimum_role_coverage"]
            codes = settings["status_codes"]
            if not isinstance(coverage, dict) or set(coverage) != set(ROLE_COUNTS) or any(not math.isfinite(float(value)) or float(value) < 0 or float(value) != int(float(value)) for value in coverage.values()): raise ValueError
            if not isinstance(codes, dict) or any(not str(key).strip() or not str(value).strip() for key, value in codes.items()): raise ValueError
            if not {"OFF", "R/OFF", "BANQUET", "INSPECT"}.issubset(codes): raise ValueError
            if not clean.get("stayover_service_policy"): raise ValueError
        except (KeyError, TypeError, ValueError):
            errors.append("Cleaning times, paid hours, breaks, duties, coverage, stayover policy, and status definitions must be complete and valid; productive minutes must remain positive.")
    return errors

def clean_payload(payload):
    return {"staff": deepcopy(payload["staff"]), "settings": deepcopy(payload["settings"]), "dataset_mode": payload.get("dataset_mode", "demo")}
