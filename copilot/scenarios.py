"""Scenario state helpers. Applying a scenario never edits workbook inputs."""
def initial_scenarios():
    return {"attendant_adjustment": 0, "checkout_minutes": None, "late_release_minutes": 0, "receipt_day_overrides": {}, "budget_usd": None}


def reset_scenarios():
    return initial_scenarios()
