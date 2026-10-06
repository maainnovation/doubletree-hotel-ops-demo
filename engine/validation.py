"""Input validation with plain-language blocking errors and warnings."""
from dataclasses import dataclass
from datetime import time
from typing import Any

import pandas as pd


@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str]
    warnings: list[str]
    data_quality_status: str


def _columns(df: pd.DataFrame, required: list[str], label: str, errors: list[str]) -> None:
    missing = sorted(set(required) - set(df.columns))
    if missing:
        errors.append(f"{label} is missing required columns: {', '.join(missing)}.")


def _time_ok(value: Any) -> bool:
    if value is None or pd.isna(value):
        return False
    if isinstance(value, (time, pd.Timestamp)):
        return True
    try:
        pd.to_datetime(str(value), format="%H:%M", errors="raise")
        return True
    except (ValueError, TypeError):
        return False


def validate_data(data) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    schemas = {
        "Rooms": (data.rooms, ["room_id", "service_type", "release_time", "deadline", "access_status", "maintenance_hold", "room_type"]),
        "Staff": (data.staff, ["staff_id", "role", "available_today", "shift_start", "shift_end", "skills", "hourly_cost"]),
        "Liquor": (data.liquor, ["sku_id", "product_name", "prior_consumption", "incremental_event_demand", "storage_qty", "bar_qty", "unusable_qty", "case_pack", "unit_cost_usd", "count_status"]),
        "Purchase orders": (data.purchase_orders, ["po_line_id", "sku_id", "outstanding_qty", "expected_receipt_day", "status"]),
    }
    for label, (frame, required) in schemas.items():
        _columns(frame, required, label, errors)
    # Continue row checks only where schemas are present.
    def present(df, fields, label):
        if set(fields).issubset(df.columns):
            for index, row in df.iterrows():
                for field in fields:
                    if row[field] is None or pd.isna(row[field]) or str(row[field]).strip() == "":
                        errors.append(f"{label} row {index + 2} is missing {field}.")
    if "room_id" in data.rooms:
        present(data.rooms, ["room_id", "service_type", "release_time", "deadline", "access_status", "maintenance_hold"], "Room")
        if data.rooms.room_id.duplicated().any(): errors.append("Room IDs must be unique.")
        if "service_type" in data.rooms and not data.rooms.service_type.dropna().isin(["Checkout", "Stayover"]).all(): errors.append("Room service type must be Checkout or Stayover.")
        if {"release_time", "deadline"}.issubset(data.rooms.columns):
            for _, row in data.rooms.iterrows():
                if not _time_ok(row.release_time): errors.append(f"Room {row.room_id} has an invalid release time.")
                if not _time_ok(row.deadline): errors.append(f"Room {row.room_id} has an invalid deadline.")
    if "staff_id" in data.staff:
        present(data.staff, ["staff_id", "role", "available_today", "shift_start", "shift_end", "hourly_cost"], "Staff member")
        if data.staff.staff_id.duplicated().any(): errors.append("Staff IDs must be unique.")
        if not data.staff.role.dropna().isin(["Room Attendant", "Inspector"]).all(): errors.append("Staff role must be Room Attendant or Inspector.")
        if "hourly_cost" in data.staff:
            vals = pd.to_numeric(data.staff.hourly_cost, errors="coerce")
            if vals.isna().any() or (vals < 0).any(): errors.append("Staff hourly cost must be a nonnegative number.")
    if "sku_id" in data.liquor:
        if data.liquor.sku_id.duplicated().any(): errors.append("SKU IDs must be unique.")
        if "case_pack" in data.liquor:
            nums = pd.to_numeric(data.liquor.case_pack, errors="coerce")
            if nums.isna().any() or (nums <= 0).any(): errors.append("Every liquor item needs a case pack greater than zero.")
    if "po_line_id" in data.purchase_orders:
        if data.purchase_orders.po_line_id.duplicated().any(): errors.append("Duplicate purchase order line IDs block approval.")
        if "sku_id" in data.purchase_orders and "sku_id" in data.liquor:
            valid_skus = set(data.liquor.sku_id.dropna())
            if not set(data.purchase_orders.sku_id.dropna()).issubset(valid_skus): errors.append("A purchase order references an unknown liquor SKU.")
    if not data.settings:
        errors.append("Hotel settings are empty.")
    return ValidationResult(not errors, errors, warnings, "Ready" if not errors else "Needs attention")
