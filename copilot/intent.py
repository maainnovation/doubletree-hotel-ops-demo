"""Small deterministic fallback intent map for common hotel operations questions."""
import re


def parse_intent(text):
    q = text.lower().strip()
    if "approve" in q and "draft" in q: return {"action": "approve_draft"}
    if "reset demo" in q or "reset scenario" in q: return {"action": "reset_demo"}
    if any(x in q for x in ("called out", "calls out", "call out")):
        return {"action": "staff_scenario", "count": 2 if "two" in q or "2" in q else 1}
    if "tequila" in q and any(x in q for x in ("late", "day 10", "delivery", "arrive")):
        days = re.search(r"day\s*(\d+)", q)
        return {"action": "receipt_scenario", "sku": "tequila", "day": int(days.group(1)) if days else 10}
    budget = re.search(r"\$?\s*([0-9][0-9,]*)", q)
    if ("budget" in q or "spend" in q) and budget:
        return {"action": "budget_scenario", "budget": float(budget.group(1).replace(",", ""))}
    if "why" in q and ("13" in q or "attendant" in q): return {"action": "housekeeping_explanation"}
    if any(x in q for x in ("order", "bottles", "tequila")): return {"action": "liquor"}
    if any(x in q for x in ("room", "housekeeping", "finish")): return {"action": "housekeeping"}
    if any(x in q for x in ("attention", "okay today", "on track")): return {"action": "summary"}
    return {"action": "unsupported"}
