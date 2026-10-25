"""Hermetic synthetic signed-webhook -> persisted roster -> fake PDF reply demo.

Run from repository root: python -m scripts.demo_whatsapp_workflow --output /tmp/whatsapp-demo
No external messages or network calls. Existing output directories are refused.
"""
from __future__ import annotations

import argparse
import base64
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import landscape, letter

from services.staff_settings import demo_defaults
from services.hotel_parameters import apply_shared_parameters
from services.whatsapp_forecast import validate_whatsapp_forecast
from services.whatsapp_provider import FakeWhatsAppProvider
from services.whatsapp_store import PrivateFileStore, WorkflowStore
from services.whatsapp_workflow import ForecastWorker
from services.weekly_roster import generate_weekly_roster

DEMO_NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
DEMO_PHONE_ID = "109876543210"
DEMO_SENDER = "15555550123"
DEMO_CODE = "SYNTHETIC-HOTEL"


def forecast_pdf(*, hotel_code=DEMO_CODE, start=date(2026, 10, 23), departures=30, stayovers=60):
    out = BytesIO()
    pdf = canvas.Canvas(out, pagesize=landscape(letter), invariant=1)
    pdf.setTitle("Synthetic hotel forecast - fictional data only")
    lines = ["Synthetic forecast - fictional data only", f"Hotel code: {hotel_code}",
             "Dates: " + " ".join((start + timedelta(days=i)).isoformat() for i in range(7))]
    values = {"Departures": departures, "Stayovers": stayovers, "Arrivals": 40,
              "Rooms on books": stayovers + 40, "Forecast pickup": 10,
              "Forecast occupied": stayovers + 50, "Occupancy %": (stayovers + 50) / 4}
    lines += [f"{label}: " + " ".join(str(value) for _ in range(7)) for label, value in values.items()]
    for i, line in enumerate(lines):
        pdf.drawString(30, 560 - 26 * i, line)
    pdf.save()
    return out.getvalue()


def webhook_payload(data, *, message_id="wamid.synthetic.forecast.1", media_id="synthetic-media-1", sender=DEMO_SENDER, phone_id=DEMO_PHONE_ID, timestamp=None, mime="application/pdf"):
    return {"object": "whatsapp_business_account", "entry": [{"id": "synthetic-waba", "changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": phone_id}, "messages": [{"id": message_id, "from": sender, "timestamp": str(int(DEMO_NOW.timestamp() if timestamp is None else timestamp)), "type": "document", "document": {"id": media_id, "mime_type": mime, "sha256": base64.b64encode(hashlib.sha256(data).digest()).decode(), "filename": "untrusted-name.pdf"}}]}}]}]}


def post_signed(client, payload, secret="synthetic-test-secret"):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"})


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return DEMO_NOW if tz else DEMO_NOW.replace(tzinfo=None)


def run_demo(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    store = WorkflowStore(output / "runtime" / "workflow.sqlite3")
    files = PrivateFileStore(output / "runtime" / "private")
    store.enroll_tenant(tenant_id="synthetic-pilot", manager_id="synthetic-gm", hotel_code=DEMO_CODE, hotel_name="Synthetic Pilot Hotel")
    store.enroll_sender(tenant_id="synthetic-pilot", phone_number_id=DEMO_PHONE_ID, sender=DEMO_SENDER)
    staff = apply_shared_parameters(demo_defaults(), {"room_capacity": 400})
    store.save_staff_settings("synthetic-gm", staff, dataset_mode="demo")
    baseline = generate_weekly_roster(validate_whatsapp_forecast(forecast_pdf(), "application/pdf", DEMO_CODE, 400, DEMO_NOW), staff)
    locked = next(row for row in baseline["schedule"] if row["role"] == "Housekeeper" and row["code"] == "SHIFT")
    locked["locked"] = True
    locked["notes"] = "Manager locked this assignment before WhatsApp upload."
    store.save_weekly_roster("synthetic-gm", baseline["week_start"], baseline)
    source = forecast_pdf(departures=220, stayovers=180)
    (output / "synthetic-forecast.pdf").write_bytes(source)
    provider = FakeWhatsAppProvider(media={"synthetic-media-1": source})
    worker = ForecastWorker(store, files, provider, clock=lambda: DEMO_NOW.timestamp())
    # This is a process-local test harness, not runtime credential configuration.
    import api.main as api
    with patch.object(api, "store", store), patch.object(api, "datetime", FrozenDateTime), patch.dict("os.environ", {"META_APP_SECRET": "synthetic-test-secret", "WHATSAPP_FORECAST_ENABLED": "1"}):
        client = TestClient(api.app)
        payload = webhook_payload(source)
        response = post_signed(client, payload)
        assert response.status_code == 200
        assert not provider.sent  # Webhook must only queue.
        result = worker.drain_ready()
        assert post_signed(client, payload).status_code == 200
        assert worker.drain_ready() == {"processed": 0, "dispatched": 0}
    versions = store.list_weekly_roster_versions("synthetic-gm")
    roster = store.get_weekly_roster("synthetic-gm", "2026-10-23")
    retained = next(row for row in roster["schedule"] if row["staff_id"] == locked["staff_id"] and row["date"] == locked["date"])
    assert retained["locked"] and retained["start"] == locked["start"] and retained["end"] == locked["end"]
    assert len(provider.sent) == 1 and provider.sent[0]["to"] == DEMO_SENDER
    (output / "synthetic-weekly-roster.pdf").write_bytes(provider.sent[0]["pdf_bytes"])
    summary = {"synthetic_only": True, "external_messages_sent": 0, "signed_webhook_http_status": response.status_code, "worker": result,
               "versions_preserved": sorted(item["version"] for item in versions), "latest_version": roster["version"],
               "status": roster["workflow_status"], "constraint_count": len(roster["constraints"]), "locked_assignment_preserved": True,
               "fake_pdf_replies": len(provider.sent), "duplicate_replies": 0, "same_authorized_sender": True,
               "outbox_status": store.rows("wa_outbox")[0]["status"], "note": "Fake provider acceptance is not real WhatsApp delivery. Development SQLite/private-file adapters only."}
    (output / "demo-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(run_demo(args.output), indent=2))
