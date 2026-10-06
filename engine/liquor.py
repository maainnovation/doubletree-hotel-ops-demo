"""Liquor demand, order quantity, and purchasing calculations."""
import math
import pandas as pd


def calculate_liquor(data):
    s = data.settings
    prior = float(s["prior_room_nights"])
    forecast = float(s["forecast_room_nights"])
    days = float(s["forecast_days"])
    safety_days = float(s["safety_days"])
    multiplier = forecast / prior
    incoming = {sku: 0.0 for sku in data.liquor.sku_id}
    for _, po in data.purchase_orders.iterrows():
        if str(po.status).strip().lower() == "confirmed":
            incoming[po.sku_id] = incoming.get(po.sku_id, 0.0) + float(po.outstanding_qty)
    rows = []
    for _, item in data.liquor.iterrows():
        demand = float(item.prior_consumption) * multiplier + float(item.incremental_event_demand)
        safety = demand / days * safety_days
        usable = float(item.storage_qty) + float(item.bar_qty) - float(item.unusable_qty)
        need = max(0.0, demand + safety - usable - incoming.get(item.sku_id, 0.0))
        pack = float(item.case_pack)
        order = math.ceil(need / pack) * pack if need else 0
        rows.append({"sku_id": item.sku_id, "product_name": item.product_name, "forecast_demand": demand, "safety_stock": safety, "usable_stock": usable, "incoming": incoming.get(item.sku_id, 0.0), "recommended_order": order, "unit_cost_usd": float(item.unit_cost_usd), "estimated_cost": order * float(item.unit_cost_usd)})
    result = pd.DataFrame(rows)
    budget = float(s["budget_usd"])
    return {"items": result, "forecast_demand": result.forecast_demand.sum(), "recommended_bottles": result.recommended_order.sum(), "estimated_cost": result.estimated_cost.sum(), "budget": budget, "remaining_budget": budget - result.estimated_cost.sum()}
