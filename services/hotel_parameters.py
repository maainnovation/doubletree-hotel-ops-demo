"""One canonical source for Parameters values shared with the weekly worker.

Hotel inventory and cleaning times live in the existing saved Staff & Settings
payload. Daily service counts are workload, not hotel room inventory. Missing
inventory remains unknown; neither workbook task rows nor enrollment may fill it.
"""
from __future__ import annotations

from copy import deepcopy
import math

SHARED_PARAMETER_KEYS = ("room_capacity", "checkout_minutes", "stayover_minutes")


def saved_room_capacity(payload, *, required=False):
    inventory = payload.get("settings", {}).get("hotel_inventory", {})
    if not isinstance(inventory, dict):
        raise ValueError("Saved hotel room inventory needs correction in Parameters.")
    value = inventory.get("room_capacity")
    if value is None:
        if required:
            raise ValueError("Set the hotel room inventory in Parameters before automatic WhatsApp processing.")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10000:
        raise ValueError("Hotel room inventory must be a whole number from 1 to 10000.")
    return value


def shared_parameters_from_staff(payload):
    cleaning = payload.get("settings", {}).get("cleaning_standards", {})
    return {"room_capacity": saved_room_capacity(payload),
            "checkout_minutes": cleaning.get("checkout_minutes"),
            "stayover_minutes": cleaning.get("stayover_minutes")}


def apply_shared_parameters(payload, values):
    """Return a merged copy; callers persist it atomically against latest staff."""
    if not isinstance(values, dict) or not set(values).issubset(SHARED_PARAMETER_KEYS):
        raise ValueError("Only shared hotel inventory and cleaning parameters may be saved here.")
    saved = {"staff": deepcopy(payload["staff"]), "settings": deepcopy(payload["settings"]),
             "dataset_mode": payload.get("_dataset_mode", payload.get("dataset_mode", "operational"))}
    if "room_capacity" in values:
        saved["settings"].setdefault("hotel_inventory", {})["room_capacity"] = values["room_capacity"]
        saved_room_capacity(saved)
    for key, maximum in (("checkout_minutes", 240), ("stayover_minutes", 180)):
        if key not in values:
            continue
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError(f"{key.replace('_', ' ').capitalize()} must be positive and no more than {maximum}.")
        saved["settings"]["cleaning_standards"][key] = value
    return saved
