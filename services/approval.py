"""Draft approval lifecycle; this never places an order or sends a schedule."""
from datetime import datetime, timezone


def approve(approval, manager, comments, version):
    if not manager or not manager.strip():
        raise ValueError("Enter a manager name to approve this draft.")
    history = approval.setdefault("history", [])
    if approval.get("status") == "APPROVED DRAFT":
        history.append(approval.copy())
    approval.update(status="APPROVED DRAFT", manager=manager.strip(), comments=comments, timestamp=datetime.now(timezone.utc).isoformat(), version=version)
    return approval


def invalidate(approval, reason):
    if approval.get("status") == "APPROVED DRAFT":
        approval.setdefault("history", []).append({**approval, "invalidated_reason": reason})
    approval.update(status="AWAITING APPROVAL", manager="", comments="", timestamp=None)
    return approval
