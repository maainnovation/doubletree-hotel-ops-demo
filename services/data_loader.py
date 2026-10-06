"""Load the editable hotel demo workbook into plain Python structures."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class DemoData:
    settings: dict[str, Any]
    rooms: pd.DataFrame
    staff: pd.DataFrame
    liquor: pd.DataFrame
    purchase_orders: pd.DataFrame


def _read_sheet(path: Path, sheet: str) -> pd.DataFrame:
    return pd.read_excel(path, sheet_name=sheet, dtype=object).where(pd.notna(pd.read_excel(path, sheet_name=sheet, dtype=object)), None)


def load_settings(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    # The maintained demo workbook has two title rows before its Setting/Value table.
    frame = pd.read_excel(path, sheet_name="Settings", dtype=object, header=2)
    if frame.empty:
        return {}
    # Supports sectioned Setting/Value tables and a single row of named columns.
    key_col = next((c for c in frame.columns if str(c).strip().lower() in {"setting", "key", "name"}), None)
    if key_col is not None:
        value_col = next((c for c in frame.columns if str(c).strip().lower() in {"value", "setting value"}), None)
        if value_col is None:
            raise ValueError("Settings sheet needs a Value column beside Setting.")
        return {str(row[key_col]).strip(): row[value_col] for _, row in frame.iterrows() if pd.notna(row[key_col])}
    return {str(k): v for k, v in frame.iloc[0].items() if pd.notna(v)}


def load_rooms(path: str | Path) -> pd.DataFrame:
    return pd.read_excel(path, sheet_name="Rooms", dtype=object)


def load_staff(path: str | Path) -> pd.DataFrame:
    return pd.read_excel(path, sheet_name="Staff", dtype=object)


def load_liquor(path: str | Path) -> pd.DataFrame:
    return pd.read_excel(path, sheet_name="Liquor", dtype=object)


def load_purchase_orders(path: str | Path) -> pd.DataFrame:
    return pd.read_excel(path, sheet_name="Purchase_Orders", dtype=object)


def load_demo_data(path: str | Path) -> DemoData:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Demo workbook not found: {path}")
    return DemoData(load_settings(path), load_rooms(path), load_staff(path), load_liquor(path), load_purchase_orders(path))
