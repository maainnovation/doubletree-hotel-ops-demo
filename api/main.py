"""FastAPI adapter for the web channel and Meta WhatsApp Cloud API."""
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from copilot.llm import classify_message, compose_reply
from services.approval import invalidate
from services.audit import record_change
from services.operations import build_snapshot, get_data, summarize_answer
from services.state_store import StateStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
app = FastAPI(title="Hotel Operations Copilot API", version="0.1.0")
store = StateStore()


class ChatRequest(BaseModel):
    manager_id: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=4000)


def _api_key_check(key):
    expected = os.getenv("COPILOT_API_KEY", "").strip()
    if not expected:
        raise HTTPException(503, "API is not enabled. Set COPILOT_API_KEY first.")
    if not key or not hmac.compare_digest(key, expected):
        raise HTTPException(401, "Unauthorized")


def _manager_hash(phone):
    secret = os.getenv("COPILOT_STATE_KEY", "development-only-change-me").encode()
    return hmac.new(secret, phone.encode(), hashlib.sha256).hexdigest()


def _manager_id_for_phone(phone):
    normalized = re.sub(r"\D", "", phone)
    for pair in os.getenv("WHATSAPP_MANAGER_MAP", "").split(","):
        if "=" not in pair:
            continue
        allowed_phone, manager_id = pair.split("=", 1)
        if re.sub(r"\D", "", allowed_phone) == normalized and manager_id.strip():
            return manager_id.strip()[:160]
    return _manager_hash(phone)


def _allowed_managers():
    return {re.sub(r"\D", "", item) for item in os.getenv("WHATSAPP_ALLOWED_NUMBERS", "").split(",") if item.strip()}


def _event(manager_id, field, old, new, reason):
    return {"field": field, "old_value": old, "new_value": new, "timestamp": datetime.now(timezone.utc).isoformat(), "source": "WhatsApp Manager", "reason": reason, "manager_id": manager_id}


def _set_pending(manager_id, state, proposal):
    proposal["expires_at"] = datetime.now(timezone.utc).timestamp() + 15 * 60
    state["pending"] = proposal
    store.save_manager(manager_id, state)


def _apply_pending(manager_id, state):
    proposal = state.get("pending")
    if not proposal:
        return "There isn’t a scenario waiting for confirmation."
    scenarios = state["scenarios"]
    field = proposal["field"]
    old = scenarios.get(field)
    if proposal["kind"] == "attendant_adjustment":
        scenarios[field] = int(old or 0) - int(proposal["value"])
    elif proposal["kind"] == "receipt_day":
        scenarios[field][proposal["sku_id"]] = proposal["value"]
    else:
        scenarios[field] = proposal["value"]
    event = _event(manager_id, proposal.get("audit_field", field), proposal.get("old_value", old), proposal.get("new_value", scenarios[field]), "Manager confirmed proposed scenario")
    store.record_audit(manager_id, event)
    invalidate(state["approval"], "Confirmed scenario changed")
    state["pending"] = None
    store.save_manager(manager_id, state)
    snapshot = build_snapshot(scenarios)
    draft = "Scenario applied to your session. I recalculated the outlook; workbook inputs were not changed.\n\n" + summarize_answer({"action": "summary"}, snapshot)
    return compose_reply("Run scenario", snapshot, draft)


def _process_message(manager_id, message):
    state = store.get_manager(manager_id)
    text = message.strip()
    lowered = text.lower().strip()
    if lowered in {"cancel", "no", "never mind", "nevermind", "cancel scenario"}:
        if state.get("pending"):
            state["pending"] = None
            store.save_manager(manager_id, state)
            return "Cancelled. No scenario was applied."
        return "There isn’t a pending action to cancel."
    if state.get("pending") and datetime.now(timezone.utc).timestamp() > state["pending"].get("expires_at", 0):
        state["pending"] = None
        store.save_manager(manager_id, state)
    if lowered in {"run scenario", "apply scenario", "yes", "confirm"} and state.get("pending"):
        return _apply_pending(manager_id, state)
    if lowered in {"reset demo", "reset scenario"}:
        state["scenarios"] = {"attendant_adjustment": 0, "checkout_minutes": None, "late_release_minutes": 0, "receipt_day_overrides": {}, "budget_usd": None}
        state["pending"] = None
        invalidate(state["approval"], "Demo reset")
        store.save_manager(manager_id, state)
        return "Demo scenarios reset for your session. Workbook data is unchanged."

    intent = classify_message(text)
    action = intent.get("action", "unknown")
    snapshot = build_snapshot(state["scenarios"])
    if action == "staff_scenario":
        count = max(1, min(5, int(intent.get("count") or 1)))
        current = snapshot["housekeeping"]["available_attendants"]
        _set_pending(manager_id, state, {"kind": "attendant_adjustment", "field": "attendant_adjustment", "value": count, "audit_field": "available_attendants", "old_value": current, "new_value": max(0, current-count)})
        draft = f"I understood: available attendants {current} → {max(0,current-count)}. Run this scenario? Reply RUN SCENARIO or CANCEL."
        return compose_reply(text, snapshot, draft)
    if action == "budget_scenario":
        current = snapshot["liquor"]["budget"]
        value = float(intent.get("budget") or 0)
        _set_pending(manager_id, state, {"kind": "budget", "field": "budget_usd", "value": value, "audit_field": "budget_usd", "old_value": current, "new_value": value})
        draft = f"I understood: purchasing budget ${current:,.0f} → ${value:,.0f}. Run this scenario? Reply RUN SCENARIO or CANCEL."
        return compose_reply(text, snapshot, draft)
    if action == "receipt_scenario":
        data = get_data()
        matching = data.liquor[data.liquor.product_name.astype(str).str.contains("tequila", case=False)]
        if matching.empty:
            return "I can’t find a tequila item in the inventory workbook."
        sku_id = str(matching.iloc[0].sku_id)
        day = int(intent.get("receipt_day") or intent.get("day") or 10)
        state["scenarios"].setdefault("receipt_day_overrides", {})
        base_day = int(data.purchase_orders.loc[data.purchase_orders.sku_id == sku_id, "expected_receipt_day"].iloc[0])
        old_day = int(state["scenarios"].get("receipt_day_overrides", {}).get(sku_id, base_day))
        _set_pending(manager_id, state, {"kind": "receipt_day", "field": "receipt_day_overrides", "sku_id": sku_id, "value": day, "audit_field": "expected_receipt_day", "old_value": old_day, "new_value": day})
        draft = f"I understood: tequila receipt Day {old_day} → Day {day}. Run this scenario? Reply RUN SCENARIO or CANCEL."
        return compose_reply(text, snapshot, draft)
    if action == "approve_draft" or ("approve" in lowered and "draft" in lowered):
        if state.get("pending"):
            return "Please confirm or cancel the pending scenario before approving the draft."
        previous_status = state["approval"].get("status", "AWAITING APPROVAL")
        state["approval"].update({"status": "APPROVED DRAFT", "manager_id": manager_id, "timestamp": datetime.now(timezone.utc).isoformat(), "version": "demo-1"})
        store.save_manager(manager_id, state)
        store.record_audit(manager_id, _event(manager_id, "approval_status", previous_status, "APPROVED DRAFT", "Manager approved draft; no order or schedule sent"))
        return "Draft approved and recorded. No order was placed and no schedule was sent."
    if action == "reset_demo":
        state["scenarios"] = {"attendant_adjustment": 0, "checkout_minutes": None, "late_release_minutes": 0, "receipt_day_overrides": {}, "budget_usd": None}
        state["pending"] = None
        invalidate(state["approval"], "Demo reset")
        store.save_manager(manager_id, state)
        return "Demo scenarios reset for your session. Workbook data is unchanged."
    return compose_reply(text, snapshot, summarize_answer(intent, snapshot))


@app.get("/health")
def health():
    return {"status": "ok", "whatsapp_configured": bool(os.getenv("WHATSAPP_ACCESS_TOKEN") and os.getenv("WHATSAPP_PHONE_NUMBER_ID")), "llm_configured": bool(os.getenv("OPENAI_API_KEY"))}


@app.get("/api/today")
def today(x_api_key: str | None = Header(default=None)):
    _api_key_check(x_api_key)
    try:
        return build_snapshot()
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/chat")
def web_chat(body: ChatRequest, x_api_key: str | None = Header(default=None)):
    _api_key_check(x_api_key)
    try:
        return {"reply": _process_message(body.manager_id, body.message)}
    except Exception as exc:
        raise HTTPException(422, f"Could not answer from current hotel data: {exc}") from exc


@app.get("/webhooks/whatsapp")
def verify_whatsapp_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    expected = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    if hub_mode == "subscribe" and expected and hmac.compare_digest(hub_verify_token or "", expected):
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(403, "Webhook verification failed")


def _verify_meta_signature(raw_body, signature):
    app_secret = os.getenv("META_APP_SECRET", "")
    if not app_secret or not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.removeprefix("sha256="), digest)


def _extract_text(message):
    if message.get("type") == "text":
        return message.get("text", {}).get("body", "")
    if message.get("type") == "interactive":
        interactive = message.get("interactive", {})
        return interactive.get("button_reply", {}).get("title") or interactive.get("list_reply", {}).get("title") or ""
    return ""


async def _send_whatsapp_message(to, text):
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    version = os.getenv("META_GRAPH_API_VERSION", "")
    if not token or not phone_number_id or not version:
        raise RuntimeError("WhatsApp send credentials are not configured")
    url = f"https://graph.facebook.com/{version}/{phone_number_id}/messages"
    async with httpx.AsyncClient(timeout=15) as client:
        result = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4000]}})
    result.raise_for_status()


@app.post("/webhooks/whatsapp")
async def receive_whatsapp_webhook(request: Request, x_hub_signature_256: str | None = Header(default=None)):
    raw = await request.body()
    if not _verify_meta_signature(raw, x_hub_signature_256):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Invalid JSON") from exc
    allowed = _allowed_managers()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            expected_phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
            actual_phone_id = value.get("metadata", {}).get("phone_number_id", "")
            if expected_phone_id and actual_phone_id and actual_phone_id != expected_phone_id:
                continue
            for message in value.get("messages", []):
                message_id = message.get("id")
                phone = re.sub(r"\D", "", message.get("from", ""))
                text = _extract_text(message)
                if not message_id or not phone or not text or phone not in allowed:
                    continue
                if store.has_seen_message(message_id):
                    continue
                manager_id = _manager_id_for_phone(phone)
                try:
                    reply = _process_message(manager_id, text)
                    await _send_whatsapp_message(phone, reply)
                except Exception:
                    # Avoid leaking tracebacks, workbook contents, or secrets to WhatsApp.
                    reply = "I couldn’t complete that from the current demo data. Please check the web app or contact your administrator."
                    try:
                        await _send_whatsapp_message(phone, reply)
                    except Exception:
                        raise HTTPException(502, "Could not send WhatsApp reply")
                store.mark_message_seen(message_id)
    return {"status": "accepted"}
