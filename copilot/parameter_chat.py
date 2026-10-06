"""Validated session edits and factual comparisons for the web copilot."""
import re

# label, lower bound, upper bound, unit, aliases
FIELDS = {
    "checkout_minutes": ("Checkout cleaning", 1, 240, "min", r"check[ -]?out|departure"),
    "stayover_minutes": ("Stayover cleaning", 1, 180, "min", r"stay[ -]?over|occupied room|not checked out"),
    "inspection_minutes": ("Final inspection", 0, 120, "min", r"inspection"),
    "late_release_minutes": ("Late release delay", 0, 240, "min", r"late (?:release|checkout)|release delay"),
    "paid_shift_hours": ("Paid shift", 1, 16, "hours", r"shift (?:length|hours)|paid shift"),
    "break_minutes": ("Break time", 0, 360, "min", r"break(?:s| time)?"),
    "other_duties_minutes": ("Other duties", 0, 360, "min", r"other duties"),
    "prior_room_nights": ("Prior room nights", 1, 100000, "room nights", r"prior room nights|historical room nights"),
    "forecast_room_nights": ("Forecast room nights", 0, 100000, "room nights", r"forecast room nights"),
    "forecast_days": ("Forecast horizon", 1, 90, "days", r"forecast (?:horizon|days)"),
    "safety_days": ("Safety stock", 0, 30, "days", r"safety (?:stock|days)"),
    "budget_usd": ("Purchasing budget", 0, 1000000, "USD", r"budget"),
}


def parse_change(message):
    """Parse explicit edits or previews. Never guess an unspecified room type."""
    q = message.lower().replace("’", "'")
    q = q.replace("not checked out", "stayover").replace("checked out", "checkout")
    words = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "ten": "10"}
    for word, number in words.items():
        q = re.sub(rf"\b{word}\b", number, q)
    action = re.search(r"\b(add|increase|raise|bump|decrease|reduce|subtract|lower|set|change|make)\b", q)
    if not action:
        return None
    if re.search(r"\b(don't|do not|never|don't actually)\b", q):
        return {"message": "No parameters changed. Ask ‘What if I increase checkout cleaning by 5 minutes?’ for a preview."}
    targets = [key for key, spec in FIELDS.items() if re.search(rf"\b(?:{spec[4]})\b", q)]
    if "late_release_minutes" in targets:
        targets = [key for key in targets if key != "checkout_minutes"]
    if "both" in q and re.search(r"clean", q):
        targets = ["checkout_minutes", "stayover_minutes"]
    if not targets:
        if re.search(r"clean|minutes?|mins?", q):
            return {"message": "Which cleaning time should I change: **checkout**, **stayover**, or **both**? Please include the amount, for example ‘Add 5 minutes to checkout cleaning’."}
        return None
    both = {"checkout_minutes", "stayover_minutes"}
    if len(targets) > 1 and set(targets) != both:
        return {"message": "Please change one parameter at a time so I can show its exact impact."}
    if len(re.findall(r"\b(add|increase|raise|bump|decrease|reduce|subtract|lower|set|change|make)\b", q)) > 1:
        return {"message": "Please send one change at a time so I can show its exact impact."}
    numbers = re.findall(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?(?![\w.])", q)
    if len(numbers) != 1:
        return {"message": "Please include one value, for example **‘Set checkout cleaning to 27 minutes’** or **‘Add 5 minutes to checkout cleaning’**."}
    amount = float(numbers[0].replace(",", ""))
    op = "set" if action.group() in {"set", "change", "make"} or re.search(r"\bto\s*\$?\s*[-+]?\d", q) else "add"
    if re.search(r"\b(by|longer|shorter|more|less)\b", q) and not re.search(r"\bto\s*\$?\s*[-+]?\d", q):
        op = "add"
    if (action.group() in {"decrease", "reduce", "subtract", "lower"} or re.search(r"\b(shorter|less)\b", q)) and op == "add":
        amount = -abs(amount)
    if "%" in q or "percent" in q:
        return {"message": "Please use minutes, hours, days, room nights, or a dollar amount rather than a percentage."}
    unit = FIELDS[targets[0]][3]
    if unit == "min" and re.search(r"\bhours?\b", q):
        amount *= 60
    elif unit == "hours" and re.search(r"\b(?:minutes?|mins?)\b", q):
        amount /= 60
    preview = bool(re.search(r"\b(if|preview|suppose|imagine|would|should i|what happens|what will|simulate)\b", q))
    return {"targets": targets, "amount": amount, "operation": op, "preview": preview}


def resolve_change(request, effective_parameters):
    updates = {}
    for key in request["targets"]:
        label, low, high, unit, _ = FIELDS[key]
        value = request["amount"] if request["operation"] == "set" else effective_parameters[key] + request["amount"]
        if key not in {"paid_shift_hours", "budget_usd"} and not float(value).is_integer():
            raise ValueError(f"{label} uses whole {unit}. Please enter a whole number.")
        if not low <= value <= high:
            raise ValueError(f"{label} must be between {low:g} and {high:g} {unit}. That change would make it {value:g}.")
        updates[key] = float(value) if key in {"paid_shift_hours", "budget_usd"} else int(value)
    merged = {**effective_parameters, **updates}
    if merged["paid_shift_hours"] * 60 <= merged["break_minutes"] + merged["other_duties_minutes"]:
        raise ValueError("Breaks and other duties must leave some productive time in the paid shift.")
    return updates


def change_reply(before, after, updates, preview=False):
    lines = ["**Scenario preview**" if preview else "**Updated your session plan**", ""]
    for key, value in updates.items():
        label, _, _, unit, _ = FIELDS[key]
        lines.append(f"{label}: **{before['parameters'][key]:g} → {value:g} {unit}**.")
    if any(key in updates for key in ("checkout_minutes", "stayover_minutes", "inspection_minutes", "late_release_minutes", "paid_shift_hours", "break_minutes", "other_duties_minutes")):
        old, new = before["housekeeping"], after["housekeeping"]
        lines += ["", "| Impact | Before | After |", "| --- | ---: | ---: |",
                  f"| Cleaning workload | {old['workload_minutes']:,.0f} min | {new['workload_minutes']:,.0f} min |",
                  f"| Attendants required | {old['required_attendants']} | {new['required_attendants']} |",
                  f"| Checkout ready | {old['checkout_ready_time'] or 'Unavailable'} | {new['checkout_ready_time'] or 'Unavailable'} |", "",
                  f"**Coverage:** {new['available_attendants']} attendants available; " + (f"{new['shortage_minutes']:,.0f} minutes of uncovered workload." if new['shortage_minutes'] else "the calculated workload is covered.")]
    else:
        old, new = before["liquor"], after["liquor"]
        lines += ["", "| Impact | Before | After |", "| --- | ---: | ---: |",
                  f"| Recommended bottles | {old['recommended_bottles']:,.0f} | {new['recommended_bottles']:,.0f} |",
                  f"| Estimated purchase | ${old['estimated_cost']:,.0f} | ${new['estimated_cost']:,.0f} |",
                  f"| Budget remaining | ${old['remaining_budget']:,.0f} | ${new['remaining_budget']:,.0f} |"]
    lines += ["", "Preview only; your current plan is unchanged. Repeat the request without ‘what if’ to apply it." if preview else "Saved for this session. Today, Parameters, and Operations now use these values."]
    return "\n".join(lines)
