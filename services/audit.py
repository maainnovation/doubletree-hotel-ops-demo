"""Session-local audit trail."""
from datetime import datetime, timezone


def record_change(history, field, old_value, new_value, source, reason="Scenario applied"):
    history.append({"field": field, "old_value": old_value, "new_value": new_value, "timestamp": datetime.now(timezone.utc).isoformat(), "source": source, "reason": reason})
    return history
