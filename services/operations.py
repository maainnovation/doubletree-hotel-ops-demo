"""Shared hotel operations functions used by both the web app and API channels."""
from pathlib import Path
import os
from copy import copy

from engine.housekeeping import calculate_housekeeping
from engine.liquor import calculate_liquor
from engine.inventory_timing import calculate_inventory_timing
from engine.validation import validate_data
from services.data_loader import load_demo_data


def get_data(path=None):
    workbook = Path(path or os.getenv("HOTEL_OPS_WORKBOOK", Path(__file__).resolve().parents[1] / "data" / "Hotel_Ops_Demo_Data.xlsx"))
    data = load_demo_data(workbook)
    validation = validate_data(data)
    if not validation.is_valid:
        raise ValueError("; ".join(validation.errors))
    return data


def calculate_components(data, scenarios=None, parameters=None):
    scenarios = scenarios or {}
    parameters = parameters or {}
    calculation_data = copy(data)
    calculation_data.settings = {**data.settings, **parameters}
    scenario_checkout = scenarios.get("checkout_minutes")
    checkout_minutes = scenario_checkout if scenario_checkout is not None else None
    base_adjustment = int(parameters.get("attendant_adjustment", 0))
    scenario_adjustment = int(scenarios.get("attendant_adjustment", 0))
    housekeeping = calculate_housekeeping(
        calculation_data,
        attendant_adjustment=base_adjustment + scenario_adjustment,
        checkout_minutes=checkout_minutes,
        late_release_minutes=(int(parameters.get("late_release_minutes", 0))
                              + int(scenarios.get("late_release_minutes", 0))),
    )
    liquor = calculate_liquor(calculation_data)
    scenario_budget = scenarios.get("budget_usd")
    budget = (scenario_budget if scenario_budget is not None
              else parameters.get("budget_usd", liquor["budget"]))
    liquor["budget"] = budget
    liquor["remaining_budget"] = budget - liquor["estimated_cost"]
    timing = calculate_inventory_timing(calculation_data, liquor, scenarios.get("receipt_day_overrides", {}))
    return housekeeping, liquor, timing


def build_snapshot(scenarios=None, path=None, parameters=None):
    scenarios = scenarios or {}
    parameters = parameters or {}
    data = get_data(path)
    validation = validate_data(data)
    housekeeping, liquor, timing = calculate_components(data, scenarios, parameters)
    effective_settings = {**data.settings, **parameters}
    if scenarios.get("checkout_minutes") is not None:
        effective_settings["checkout_minutes"] = scenarios["checkout_minutes"]
    effective_settings["attendant_adjustment"] = int(parameters.get("attendant_adjustment", 0)) + int(scenarios.get("attendant_adjustment", 0))
    effective_settings["late_release_minutes"] = int(parameters.get("late_release_minutes", 0)) + int(scenarios.get("late_release_minutes", 0))
    effective_settings["budget_usd"] = liquor["budget"]
    ready = housekeeping["last_checkout_ready"]
    return {
        "hotel_name": data.settings.get("hotel_name", "Hotel"),
        "business_date": str(data.settings.get("business_date", "")),
        "housekeeping": {
            "total_rooms": housekeeping["total_rooms"],
            "checkout_rooms": housekeeping["checkout_rooms"],
            "stayover_rooms": housekeeping["stayover_rooms"],
            "workload_minutes": housekeeping["workload_minutes"],
            "required_attendants": housekeeping["required_attendants"],
            "available_attendants": housekeeping["available_attendants"],
            "shortage_minutes": housekeeping["shortage_minutes"],
            "available_minutes": housekeeping["available_minutes"],
            "late_room_tasks": housekeeping["exceptions"],
            "productive_minutes_per_attendant": housekeeping["productive_minutes_per_attendant"],
            "checkout_ready_time": ready.strftime("%I:%M %p").lstrip("0") if ready else None,
        },
        "parameters": {key: effective_settings.get(key) for key in (
            "checkout_minutes", "stayover_minutes", "inspection_minutes", "paid_shift_hours",
            "break_minutes", "other_duties_minutes", "late_release_minutes",
            "attendant_adjustment", "prior_room_nights", "forecast_room_nights",
            "forecast_days", "safety_days", "budget_usd",
        )},
        "liquor": {
            "forecast_demand": float(liquor["forecast_demand"]),
            "recommended_bottles": float(liquor["recommended_bottles"]),
            "estimated_cost": float(liquor["estimated_cost"]),
            "budget": float(liquor["budget"]),
            "remaining_budget": float(liquor["remaining_budget"]),
            "items": liquor["items"].to_dict(orient="records"),
        },
        "timing_risks": [
            {"sku_id": str(row.sku_id), "product_name": str(row.product_name), "day": int(row.day), "unmet": float(row.unmet)}
            for row in timing[timing.unmet > 0][["sku_id", "product_name", "day", "unmet"]].itertuples(index=False)
        ],
        "validation": {"status": "Ready", "warnings": validation.warnings},
        "model_notes": "Readiness uses a simplified sequential room assignment. Breaks and other duties reduce capacity but are not scheduled as timeline events. No live room status is connected.",
    }


def summarize_answer(intent, snapshot):
    hk = snapshot["housekeeping"]
    li = snapshot["liquor"]
    if intent["action"] == "housekeeping":
        if hk["shortage_minutes"]:
            return f"🔴 Coverage needs attention: available staffing leaves a {hk['shortage_minutes']:,.0f}-minute workload shortage. I can show options, but I won’t choose one for you."
        return f"🟢 Housekeeping is covered. {hk['available_attendants']} attendants are available; {hk['required_attendants']} are required. Checkout rooms are projected inspected-ready around {hk['checkout_ready_time'] or 'a time still being calculated'}."
    if intent["action"] == "housekeeping_explanation":
        return (f"Today has {hk['total_rooms']} room services ({hk['checkout_rooms']} checkouts and {hk['stayover_rooms']} stayovers). "
                f"The workload is {hk['workload_minutes']:,.0f} minutes, with {hk['available_attendants']} attendants available and {hk['required_attendants']} required.")
    if intent["action"] == "liquor":
        return (f"Recommended order: {li['recommended_bottles']:,.0f} bottles at an estimated ${li['estimated_cost']:,.0f}. "
                f"Budget: ${li['budget']:,.0f}; remaining: ${li['remaining_budget']:,.0f}.")
    if intent["action"] == "summary":
        return (f"Today: {hk['available_attendants']} of {hk['required_attendants']} attendants required are available; "
                f"checkout rooms are projected ready around {hk['checkout_ready_time'] or 'pending'}. "
                f"Bar order recommendation is {li['recommended_bottles']:,.0f} bottles for ${li['estimated_cost']:,.0f}.")
    return "I can help with today’s status, room readiness, staffing scenarios, inventory recommendations, delivery timing, and budget scenarios."
