"""Day-by-day inventory flow for each SKU."""
import pandas as pd


def calculate_inventory_timing(data, liquor_result=None, receipt_day_overrides=None):
    receipt_day_overrides = receipt_day_overrides or {}
    horizon = int(data.settings.get("forecast_days", 1))
    result = liquor_result or __import__("engine.liquor", fromlist=["calculate_liquor"]).calculate_liquor(data)
    sku_results = []
    for _, item in data.liquor.iterrows():
        opening = float(item.storage_qty) + float(item.bar_qty) - float(item.unusable_qty)
        rate = float(item.prior_consumption) * float(data.settings["forecast_room_nights"]) / float(data.settings["prior_room_nights"]) / horizon + float(item.incremental_event_demand) / horizon
        po_rows = data.purchase_orders[(data.purchase_orders.sku_id == item.sku_id) & (data.purchase_orders.status == "Confirmed")]
        for day in range(1, horizon + 1):
            receipts = sum(float(po.outstanding_qty) for _, po in po_rows.iterrows() if int(receipt_day_overrides.get(item.sku_id, po.expected_receipt_day)) == day)
            demand = rate
            available = opening + receipts
            served = min(available, demand)
            closing = max(0.0, available-demand)
            sku_results.append({"sku_id": item.sku_id, "product_name": item.product_name, "day": day, "opening": opening, "receipts": receipts, "demand": demand, "served": served, "unmet": max(0.0, demand-available), "closing": closing})
            opening = closing
    return pd.DataFrame(sku_results)
