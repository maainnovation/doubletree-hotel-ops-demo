def housekeeping_response(result):
    if result["shortage_minutes"]:
        return f"🔴 Coverage needs attention. Available staffing leaves a {result['shortage_minutes']:,.0f}-minute workload shortage."
    when = result["last_checkout_ready"].strftime("%-I:%M %p") if result["last_checkout_ready"] else "not yet available"
    return f"🟢 Yes. Current staffing can cover today’s room workload. Checkout rooms are projected inspected-ready around {when}."


def liquor_response(result):
    return f"Recommended order: {result['recommended_bottles']:,.0f} bottles, estimated at ${result['estimated_cost']:,.0f}."
