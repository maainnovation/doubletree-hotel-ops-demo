"""Hermetic Twilio PDF round-trip using the real adapter with MockTransport.

Run: python -m scripts.demo_twilio_workflow --output /tmp/twilio-demo
Refuses existing directories. No live requests, accounts, credentials or sends.
The fictional 400-room fixture is unrelated to the user's 292-room app setting.
"""
from __future__ import annotations

import argparse
from email.utils import format_datetime
import json
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode

from fastapi.testclient import TestClient
import httpx
from twilio.request_validator import RequestValidator

from scripts.demo_whatsapp_workflow import DEMO_CODE, DEMO_NOW, DEMO_SENDER, FrozenDateTime, forecast_pdf
from services.hotel_parameters import apply_shared_parameters
from services.staff_settings import demo_defaults
from services.twilio_media import TwilioMediaPublisher
from services.twilio_provider import TwilioWhatsAppProvider, twilio_business_id
from services.weekly_roster import generate_weekly_roster
from services.whatsapp_forecast import validate_whatsapp_forecast
from services.whatsapp_store import PrivateFileStore, WorkflowStore
from services.whatsapp_workflow import ForecastWorker


def run_demo(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    store = WorkflowStore(output / "runtime" / "workflow.sqlite3")
    files = PrivateFileStore(output / "runtime" / "private")
    account, inbound, outbound = "AC" + "a" * 32, "SM" + "b" * 32, "SM" + "d" * 32
    business, secret = "15555550900", "synthetic-process-local-token"
    base = "https://synthetic-pilot.example"
    webhook, callback = base + "/webhooks/twilio/whatsapp", base + "/webhooks/twilio/status"
    metadata_path = f"/2010-04-01/Accounts/{account}/Messages/{inbound}.json"
    media_path = f"/2010-04-01/Accounts/{account}/Messages/{inbound}/Media/ME" + "c" * 32
    send_path = f"/2010-04-01/Accounts/{account}/Messages.json"
    store.enroll_tenant(tenant_id="synthetic-twilio", manager_id="synthetic-gm", hotel_code=DEMO_CODE, hotel_name="Synthetic Twilio Pilot Hotel")
    store.enroll_sender(tenant_id="synthetic-twilio", phone_number_id=twilio_business_id(account, business), sender=DEMO_SENDER)
    staff = apply_shared_parameters(demo_defaults(), {"room_capacity": 400})
    store.save_staff_settings("synthetic-gm", staff, dataset_mode="demo")
    baseline = generate_weekly_roster(validate_whatsapp_forecast(forecast_pdf(), "application/pdf", DEMO_CODE, 400, DEMO_NOW), staff)
    locked = next(row for row in baseline["schedule"] if row["role"] == "Housekeeper" and row["code"] == "SHIFT")
    locked.update(locked=True, notes="Manager locked this assignment before the synthetic Twilio upload.")
    store.save_weekly_roster("synthetic-gm", baseline["week_start"], baseline)
    source = forecast_pdf(departures=220, stayovers=180)
    (output / "synthetic-twilio-forecast.pdf").write_bytes(source)
    publisher = TwilioMediaPublisher(store, files, base, clock=lambda: DEMO_NOW.timestamp())
    recorded = {"provider_requests": 0, "pdf_replies": 0, "head_requests": 0, "get_requests": 0}
    returned_pdf = []
    # TestClient runs ASGI in process. No TCP server, tunnel or external hosting.
    import api.main as api
    with patch.object(api, "store", store), patch.object(api, "datetime", FrozenDateTime), patch.object(api, "_twilio_media_publisher", lambda: publisher), patch.dict("os.environ", {
            "WHATSAPP_FORECAST_ENABLED": "1", "TWILIO_WEBHOOK_ENABLED": "1", "TWILIO_DEV_MEDIA_ENABLED": "1",
            "TWILIO_ACCOUNT_SID": account, "TWILIO_AUTH_TOKEN": secret, "TWILIO_WHATSAPP_FROM": business,
            "TWILIO_WEBHOOK_URL": webhook, "TWILIO_STATUS_CALLBACK_URL": callback,
    }), TestClient(api.app, base_url=base, client=("127.0.0.1", 12345)) as client:
        def transport(request):
            recorded["provider_requests"] += 1
            assert request.url.host == "api.twilio.com"
            if request.method == "GET" and request.url.path == metadata_path:
                return httpx.Response(200, json={"sid": inbound, "account_sid": account,
                    "from": "whatsapp:+" + DEMO_SENDER, "to": "whatsapp:+" + business,
                    "direction": "inbound", "date_created": format_datetime(DEMO_NOW)})
            if request.method == "GET" and request.url.path == media_path:
                return httpx.Response(200, content=source, headers={"Content-Type": "application/pdf"})
            assert request.method == "POST" and request.url.path == send_path
            payload = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            assert payload["To"] == "whatsapp:+" + DEMO_SENDER and payload["From"] == "whatsapp:+" + business
            assert "Body" not in payload and payload["StatusCallback"] == callback
            # Simulate Twilio validating and retrieving MediaUrl, never log or
            # serialize that bearer capability into the user-facing summary.
            head, get = client.head(payload["MediaUrl"]), client.get(payload["MediaUrl"])
            assert head.status_code == get.status_code == 200
            assert head.headers["content-type"] == "application/pdf"
            assert head.content == b"" and int(head.headers["content-length"]) == len(get.content)
            assert "no-store" in get.headers["cache-control"]
            recorded["head_requests"] += 1
            recorded["get_requests"] += 1
            recorded["pdf_replies"] += 1
            returned_pdf.append(get.content)
            return httpx.Response(201, json={"sid": outbound})

        provider = TwilioWhatsAppProvider(account, secret, business, media_publisher=publisher,
            status_callback_url=callback, transport=httpx.MockTransport(transport))
        assert not provider.is_live
        worker = ForecastWorker(store, files, provider, clock=lambda: DEMO_NOW.timestamp())
        form = {"AccountSid": account, "MessageSid": inbound, "From": "whatsapp:+" + DEMO_SENDER,
            "To": "whatsapp:+" + business, "NumMedia": "1", "MediaContentType0": "application/pdf",
            "MediaUrl0": "https://api.twilio.com" + media_path, "Body": ""}
        signature = RequestValidator(secret).compute_signature(webhook, form)
        headers = {"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature}
        response = client.post(webhook, content=urlencode(form), headers=headers)
        assert response.status_code == 200 and recorded["provider_requests"] == 0
        result = worker.drain_ready()
        assert client.post(webhook, content=urlencode(form), headers=headers).status_code == 200
        assert worker.drain_ready() == {"processed": 0, "dispatched": 0}

    roster = store.get_weekly_roster("synthetic-gm", "2026-10-23")
    retained = next(row for row in roster["schedule"] if row["staff_id"] == locked["staff_id"] and row["date"] == locked["date"])
    assert retained["locked"] and all(retained[key] == locked[key] for key in ("code", "start", "end"))
    assert len(returned_pdf) == 1 and roster["workflow_status"] == "needs_attention"
    (output / "synthetic-twilio-weekly-roster.pdf").write_bytes(returned_pdf[0])
    summary = {"synthetic_only": True, "transport": "httpx.MockTransport (no external network)",
        "external_messages_sent": 0, "signed_webhook_http_status": response.status_code, "worker": result,
        "versions_preserved": sorted(item["version"] for item in store.list_weekly_roster_versions("synthetic-gm")),
        "latest_version": roster["version"], "status": roster["workflow_status"],
        "constraint_count": len(roster["constraints"]), "locked_assignment_preserved": True,
        "mock_pdf_replies": recorded["pdf_replies"], "duplicate_replies": 0, "same_authorized_sender": True,
        "mock_provider_requests": recorded["provider_requests"], "media_head_requests": recorded["head_requests"],
        "media_get_requests": recorded["get_requests"], "outbox_status": store.rows("wa_outbox")[0]["status"],
        "fixture_room_capacity": 400, "note": "Fictional 400-room fixture, not the user's 292-room hotel. Mock API acceptance is not real WhatsApp delivery or proof of trial PDF entitlement. Local development storage only."}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(run_demo(args.output), indent=2))
