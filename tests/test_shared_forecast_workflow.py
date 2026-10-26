"""Signed intake + separate worker + authenticated app on one synthetic store.

OCR assessments are injected here to test composition/state transitions. Real
local extraction/resource-limit tests live in the media-review test modules.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
import hashlib
from io import BytesIO
import json
import re
import sys
from urllib.parse import urlencode

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader
import pytest
from twilio.request_validator import RequestValidator

from api.private_hotel_app import HotelPrincipal, TokenMapAuthenticator
from api.shared_forecast_workflow import TwilioWebhookConfig, create_shared_workflow_app
from scripts.demo_whatsapp_workflow import DEMO_NOW
from services.hotel_parameters import apply_shared_parameters
from services.staff_settings import demo_defaults
from services.twilio_pilot import HOTEL_CODE, synthetic_forecast
from services.whatsapp_forecast import validate_whatsapp_forecast
from services.whatsapp_provider import FakeWhatsAppProvider, ProviderError
from services.whatsapp_store import PrivateFileStore, WorkflowStore
from services.whatsapp_workflow import UPLOAD_ACK_TEXT


ACCOUNT, TOKEN = "AC" + "a" * 32, "synthetic-webhook-auth-token"
BUSINESS, SENDER, OTHER = "15555550900", "15555550123", "15555550124"
ORIGIN = "https://workflow.synthetic.test"
ACCESS_A, ACCESS_B = "synthetic-private-access-hotel-a", "synthetic-private-access-hotel-b"
SOURCE = synthetic_forecast(date(2026, 10, 23))
STALE_SOURCE = synthetic_forecast(date(2025, 10, 24))


class BoundFakeProvider(FakeWhatsAppProvider):
    """Explicit fake immutable-provider metadata; no network implementation."""
    def __init__(self, phone_id):
        super().__init__()
        self.phone_number_id = phone_id
        self.metadata, self.metadata_reads, self.fetches = {}, [], []

    def verify_inbound_message(self, message_sid, sender, now):
        self.metadata_reads.append(message_sid)
        actual_sender, created = self.metadata[message_sid]
        if sender != actual_sender or not 0 <= now - created < 86400:
            raise ProviderError("Synthetic message identity or time mismatch")
        return created

    def fetch_media(self, media_id, expected_mime, max_bytes):
        self.fetches.append(media_id)
        return super().fetch_media(media_id, expected_mime, max_bytes)

    def _record(self, payload, key):
        super()._record(payload, key)
        sid = "SM" + hashlib.sha256(key.encode()).hexdigest()[:32]
        for sent in self.sent:
            if sent["idempotency_key"] == key:
                sent["provider_id"] = sid
        return sid


@pytest.fixture
def shared(tmp_path):
    config = TwilioWebhookConfig(ACCOUNT, TOKEN, BUSINESS, ORIGIN + "/webhooks/twilio/whatsapp", ORIGIN + "/webhooks/twilio/status")
    store, files = WorkflowStore(tmp_path / "shared.sqlite3"), PrivateFileStore(tmp_path / "private")
    for tenant, manager, hotel_code, sender in (("hotel-a", "manager-a", HOTEL_CODE, SENDER), ("hotel-b", "manager-b", "OTHER", OTHER)):
        store.enroll_tenant(tenant_id=tenant, manager_id=manager, hotel_code=hotel_code, hotel_name="Fictional " + tenant)
        store.enroll_sender(tenant_id=tenant, phone_number_id=config.phone_number_id, sender=sender)
        store.save_staff_settings(manager, apply_shared_parameters(demo_defaults(), {"room_capacity": 292}), dataset_mode="demo")
    auth = TokenMapAuthenticator({ACCESS_A: HotelPrincipal("hotel-a", "manager-a"), ACCESS_B: HotelPrincipal("hotel-b", "manager-b")})
    provider = BoundFakeProvider(config.phone_number_id)
    clock, evaluations = [DEMO_NOW.timestamp()], []
    rows = validate_whatsapp_forecast(SOURCE, "application/pdf", HOTEL_CODE, 292, DEMO_NOW)
    def evaluate(*args, **kwargs):
        evaluations.append((args, kwargs))
        candidate = deepcopy(rows)
        if args[0] in (SOURCE, STALE_SOURCE) and args[1] == "application/pdf":
            if args[0] == STALE_SOURCE:
                for index, row in enumerate(candidate):
                    row["date"] = (date(2025, 10, 24) + timedelta(days=index)).isoformat()
            # Mock extraction only. Even an eligible child assessment must pass
            # the parent's cheap current-date/count checks before scheduling.
            return {"method": "native_pdf", "automatic_roster_allowed": True,
                "requires_review": False, "rows": candidate}
        return {"method": "local_scanned_pdf_ocr" if args[1] == "application/pdf" else "local_image_ocr",
            "automatic_roster_allowed": False, "requires_review": True,
            "candidate_valid_for_confirmation": True, "rows": candidate}
    app = create_shared_workflow_app(store, files, provider, auth, config, clock=lambda: clock[0], media_evaluator=evaluate)
    with TestClient(app, base_url=ORIGIN) as client:
        yield {"store": store, "files": files, "provider": provider, "config": config, "clock": clock,
            "auth": auth, "app": app, "client": client, "worker": app.state.worker, "evaluations": evaluations, "rows": rows}


def private_headers(token=ACCESS_A):
    return {"Authorization": "Bearer " + token}


def signed(system, form, *, url=None, extra_headers=None):
    url = url or system["config"].webhook_url
    signature = RequestValidator(TOKEN).compute_signature(url, form)
    return system["client"].post(url, content=urlencode(form), headers={
        "Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature, **(extra_headers or {})})


def incoming(system, sid, *, data=SOURCE, mime="application/pdf", sender=SENDER, body=None, created=None):
    system["provider"].metadata[sid] = (sender, system["clock"][0] if created is None else created)
    form = {"AccountSid": ACCOUNT, "MessageSid": sid, "From": "whatsapp:+" + sender,
        "To": "whatsapp:+" + BUSINESS, "NumMedia": "0" if body is not None else "1", "Body": body or ""}
    if body is None:
        media_url = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages/{sid}/Media/ME" + "c" * 32
        form.update(MediaUrl0=media_url, MediaContentType0=mime)
        system["provider"].media[media_url] = (data, mime)
    return form


def image_bytes():
    stream = BytesIO()
    Image.new("RGB", (200, 80), "white").save(stream, format="PNG")
    return stream.getvalue()


def image_prompt(system, sid="SM" + "2" * 32, *, mime="image/png"):
    source = image_bytes()
    if mime == "application/pdf":
        output = BytesIO()
        Image.open(BytesIO(source)).save(output, format="PDF")
        source = output.getvalue()  # Synthetic raster PDF; extraction is mocked.
    response = signed(system, incoming(system, sid, data=source, mime=mime))
    assert response.status_code == 200
    assert system["worker"].drain_ready() == {"processed": 1, "dispatched": 1}
    prompt = system["provider"].sent[-1]
    assert prompt["type"] == "text" and "Check ALL 7 dates and 49 values" in prompt["text"]
    code = re.search(r"CONFIRM ([0-9a-f]{16})", prompt["text"]).group(1)
    return code


def test_native_signed_pdf_calculates_in_same_store_and_private_app_immediately_sees_version(shared):
    s = shared
    assert s["app"].state.workflow_store is s["store"] is s["worker"].store
    assert s["app"].state.private_files is s["files"] is s["worker"].files
    response = signed(s, incoming(s, "SM" + "1" * 32))
    assert response.status_code == 200 and response.headers["content-type"].startswith("application/xml")
    assert not s["provider"].metadata_reads and not s["provider"].fetches and not s["provider"].sent
    assert s["client"].get("/private/api/rosters", headers=private_headers()).json()["versions"] == []
    s["worker"].drain_ready()
    assert s["provider"].sent[0]["text"] == UPLOAD_ACK_TEXT
    assert not s["store"].list_weekly_roster_versions("manager-a")
    s["worker"].drain_ready()
    assert s["provider"].sent[-1]["type"] == "document" and s["provider"].sent[-1]["to"] == SENDER
    listed = s["client"].get("/private/api/rosters", headers=private_headers()).json()
    assert len(listed["versions"]) == 1 and listed["latest"][0]["version"] == 1
    pdf = s["client"].get("/private/api/rosters/2026-10-23/versions/1/pdf", headers=private_headers())
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    assert "Day-by-day calculation summary" in "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
    assert len(s["evaluations"]) == 2  # Initial admission and post-ACK revalidation.
    assert all(args[0] == SOURCE and args[1] == "application/pdf" for args, _ in s["evaluations"])


@pytest.mark.parametrize("native", [True, False], ids=["native-pdf", "raster-pdf"])
def test_review_enabled_pdf_never_calls_native_parser_in_parent(shared, monkeypatch, native):
    calls = []
    def forbidden_parser(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Untrusted PDF parsing must stay inside the evaluator")
    monkeypatch.setattr("services.whatsapp_workflow.validate_whatsapp_forecast", forbidden_parser)
    monkeypatch.setattr("services.whatsapp_forecast.validate_whatsapp_forecast", forbidden_parser)
    if native:
        assert signed(shared, incoming(shared, "SM" + "f" * 32)).status_code == 200
        shared["worker"].drain_ready()
        assert shared["provider"].sent[0]["text"] == UPLOAD_ACK_TEXT
        assert not shared["store"].list_weekly_roster_versions("manager-a")
    else:
        code = image_prompt(shared, mime="application/pdf")
        assert not shared["store"].list_weekly_roster_versions("manager-a")
        assert signed(shared, incoming(shared, "SM" + "f" * 32, body="CONFIRM " + code)).status_code == 200
    shared["worker"].drain_ready()
    assert shared["provider"].sent[-1]["type"] == "document"
    assert len(shared["store"].list_weekly_roster_versions("manager-a")) == 1
    assert shared["evaluations"] and calls == []


def test_isolated_assessment_failure_never_falls_back_to_parent_parser_or_retries(shared, monkeypatch):
    calls = []
    def fail_evaluation(*args, **kwargs):
        calls.append("evaluation")
        raise RuntimeError("synthetic-private-child-error")
    def forbidden_parser(*args, **kwargs):
        calls.append("parent-parser")
        raise AssertionError("No parent fallback")
    shared["worker"].media_evaluator = fail_evaluation
    monkeypatch.setattr("services.whatsapp_workflow.validate_whatsapp_forecast", forbidden_parser)
    assert signed(shared, incoming(shared, "SM" + "f" * 32)).status_code == 200
    shared["worker"].drain_ready(); shared["worker"].drain_ready()
    assert calls == ["evaluation"]
    assert not shared["store"].list_weekly_roster_versions("manager-a")
    assert len(shared["provider"].sent) == 1
    correction = shared["provider"].sent[0]
    assert correction["type"] == "text" and correction["text"] != UPLOAD_ACK_TEXT
    assert "synthetic-private-child-error" not in repr(shared["store"].rows("wa_jobs")) + correction["text"]


@pytest.mark.skipif(sys.platform != "linux", reason="Default isolated assessment requires Linux")
def test_real_default_isolated_native_pdf_reaches_shared_private_roster_without_parent_parsing(shared, monkeypatch):
    calls = []
    def forbidden_parser(*args, **kwargs):
        calls.append("parent-parser")
        raise AssertionError("The default evaluator must parse native PDF in its child")
    monkeypatch.setattr("services.whatsapp_workflow.validate_whatsapp_forecast", forbidden_parser)
    monkeypatch.setattr("services.whatsapp_forecast.validate_whatsapp_forecast", forbidden_parser)
    shared["worker"].media_evaluator = None  # Real default subprocess, not mocked extraction.
    assert signed(shared, incoming(shared, "SM" + "f" * 32)).status_code == 200
    assert shared["worker"].drain_ready() == {"processed": 1, "dispatched": 1}
    assert shared["provider"].sent[0]["text"] == UPLOAD_ACK_TEXT
    assert not shared["store"].list_weekly_roster_versions("manager-a")
    assert shared["worker"].drain_ready() == {"processed": 1, "dispatched": 1}
    assert [message["type"] for message in shared["provider"].sent] == ["text", "document"]
    assert shared["provider"].sent[-1]["to"] == SENDER
    listed = shared["client"].get("/private/api/rosters", headers=private_headers()).json()
    assert listed["versions"][0]["version"] == 1
    assert calls == [] and not shared["evaluations"]


@pytest.mark.skipif(sys.platform != "linux", reason="Default isolated OCR requires Linux")
def test_real_isolated_png_ocr_requires_exact_review_then_confirmation_saves_shared_private_roster(shared):
    # Explicitly synthetic typeset image; not a real photo or an accuracy claim
    # for arbitrary reports. No OCR/evaluator implementation is mocked here.
    image = Image.new("RGB", (2800, 1200), "white")
    draw, font = ImageDraw.Draw(image), ImageFont.truetype("DejaVuSans.ttf", 32)
    for index, line in enumerate(PdfReader(BytesIO(SOURCE)).pages[0].extract_text().splitlines()):
        draw.text((40, 40 + index * 95), line, fill="black", font=font)
    output = BytesIO()
    image.save(output, format="PNG")
    shared["worker"].media_evaluator = None
    assert signed(shared, incoming(shared, "SM" + "f" * 32, data=output.getvalue(), mime="image/png")).status_code == 200
    assert shared["worker"].drain_ready() == {"processed": 1, "dispatched": 1}
    assert not shared["store"].list_weekly_roster_versions("manager-a")
    assert len(shared["provider"].sent) == 1
    prompt = shared["provider"].sent[0]
    assert prompt["type"] == "text" and "Check ALL 7 dates and 49 values" in prompt["text"]
    assert prompt["text"] != UPLOAD_ACK_TEXT
    with shared["store"]._connect() as db:
        review = db.execute("SELECT candidate_json,status FROM wa_forecast_reviews").fetchone()
    assert review is not None and review[1] == "pending"
    assert json.loads(review[0]) == shared["rows"]  # Exact dates and every extracted cell.
    for row in shared["rows"]:
        expected = row["date"] + ": 200 / 70 / 40 / 110 / 10 / 120 / 41.1"
        assert expected in prompt["text"]
    code = re.search(r"CONFIRM ([0-9a-f]{16})", prompt["text"]).group(1)
    assert signed(shared, incoming(shared, "SM" + "e" * 32, body="CONFIRM " + code)).status_code == 200
    assert shared["worker"].drain_ready() == {"processed": 1, "dispatched": 1}
    assert [message["type"] for message in shared["provider"].sent] == ["text", "document"]
    assert shared["provider"].sent[-1]["to"] == SENDER
    listed = shared["client"].get("/private/api/rosters", headers=private_headers()).json()
    assert listed["versions"][0]["version"] == 1
    pdf = shared["client"].get("/private/api/rosters/2026-10-23/versions/1/pdf", headers=private_headers())
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    assert not shared["evaluations"]


@pytest.mark.parametrize("mime", ["image/png", "application/pdf"])
def test_signed_image_or_raster_pdf_confirmation_generates_pdf_visible_only_in_correct_private_app(shared, mime):
    s = shared
    code = image_prompt(s, mime=mime)
    assert len(s["evaluations"]) == 1 and not s["store"].list_weekly_roster_versions("manager-a")
    assert s["evaluations"][0][0][1] == mime
    assert all(sent.get("text") != UPLOAD_ACK_TEXT for sent in s["provider"].sent)
    form = incoming(s, "SM" + "3" * 32, body="CONFIRM " + code)
    assert signed(s, form).status_code == 200
    assert len(s["provider"].sent) == 1  # Webhook only queues the confirmation.
    s["worker"].drain_ready()
    assert s["provider"].sent[-1]["type"] == "document" and s["provider"].sent[-1]["to"] == SENDER
    assert len(s["store"].list_weekly_roster_versions("manager-a")) == 1
    listed = s["client"].get("/private/api/rosters", headers=private_headers()).json()
    assert listed["versions"][0]["version"] == 1
    assert s["client"].get("/private/api/rosters", headers=private_headers(ACCESS_B)).json()["versions"] == []
    path = "/private/api/rosters/2026-10-23/versions/1/pdf"
    assert s["client"].get(path).status_code == 401
    assert s["client"].get(path, headers=private_headers(ACCESS_B)).status_code == 404
    assert s["client"].get(path, headers=private_headers()).status_code == 200
    before = len(s["provider"].sent)
    assert signed(s, form).status_code == 200
    assert signed(s, incoming(s, "SM" + "4" * 32, body="CONFIRM " + code)).status_code == 200
    s["worker"].drain_ready()
    assert len(s["provider"].sent) == before and len(s["store"].list_weekly_roster_versions("manager-a")) == 1


def test_other_enrolled_sender_cannot_confirm_another_tenants_review(shared):
    code = image_prompt(shared)
    assert signed(shared, incoming(shared, "SM" + "5" * 32, sender=OTHER, body="CONFIRM " + code)).status_code == 200
    shared["worker"].drain_ready()
    assert not shared["store"].list_weekly_roster_versions("manager-a")
    assert not shared["store"].list_weekly_roster_versions("manager-b")
    assert not any(sent["type"] == "document" for sent in shared["provider"].sent)
    assert signed(shared, incoming(shared, "SM" + "6" * 32, body="CONFIRM " + code)).status_code == 200
    shared["worker"].drain_ready()
    assert shared["provider"].sent[-1]["type"] == "document" and shared["provider"].sent[-1]["to"] == SENDER


def test_private_settings_revision_change_invalidates_pending_confirmation(shared):
    code = image_prompt(shared)
    current = shared["client"].get("/private/api/parameters", headers=private_headers()).json()
    changed = shared["client"].patch("/private/api/parameters", headers=private_headers(),
        json={"values": {"checkout_minutes": 25}, "expected_revision": current["revision"]})
    assert changed.status_code == 200 and changed.json()["revision"] > current["revision"]
    signed(shared, incoming(shared, "SM" + "7" * 32, body="CONFIRM " + code))
    shared["worker"].drain_ready()
    assert not shared["store"].list_weekly_roster_versions("manager-a")
    assert not any(sent["type"] == "document" for sent in shared["provider"].sent)


def test_private_settings_edits_before_native_upload_are_used_without_duplicate_config(shared):
    current = shared["client"].get("/private/api/parameters", headers=private_headers()).json()
    response = shared["client"].patch("/private/api/parameters", headers=private_headers(),
        json={"values": {"checkout_minutes": 25}, "expected_revision": current["revision"]})
    assert response.status_code == 200
    signed(shared, incoming(shared, "SM" + "8" * 32))
    shared["worker"].drain_ready(); shared["worker"].drain_ready()
    roster = shared["store"].get_weekly_roster("manager-a", "2026-10-23")
    assert roster["source"]["settings_revision"] == response.json()["revision"]
    assert roster["input_snapshot"]["settings"]["cleaning_standards"]["checkout_minutes"] == 25
    assert roster["input_snapshot"]["settings"]["hotel_inventory"]["room_capacity"] == 292


@pytest.mark.parametrize("change", ["review_expired", "sender_revoked", "tenant_revoked"])
def test_expired_review_or_revoked_access_never_produces_roster(shared, change):
    code = image_prompt(shared)
    if change == "review_expired":
        shared["clock"][0] += 1801
    else:
        with shared["store"]._connect() as db:
            db.execute("UPDATE wa_senders SET enabled=0 WHERE sender=?" if change == "sender_revoked" else "UPDATE wa_tenants SET enabled=0 WHERE tenant_id=?",
                (SENDER if change == "sender_revoked" else "hotel-a",))
    signed(shared, incoming(shared, "SM" + "a" * 32, body="CONFIRM " + code))
    shared["worker"].drain_ready(); shared["worker"].drain_ready()
    assert not shared["store"].list_weekly_roster_versions("manager-a")
    assert not any(sent["type"] == "document" for sent in shared["provider"].sent)


def test_signed_status_callback_updates_same_store_but_never_runs_worker(shared):
    signed(shared, incoming(shared, "SM" + "b" * 32))
    shared["worker"].drain_ready()
    ack = shared["store"].rows("wa_outbox")[0]
    reads = list(shared["provider"].metadata_reads)
    form = {"AccountSid": ACCOUNT, "From": "whatsapp:+" + BUSINESS, "To": "whatsapp:+" + SENDER,
        "MessageSid": ack["provider_message_id"], "MessageStatus": "delivered", "UnknownNewField": " untouched "}
    assert signed(shared, form, url=shared["config"].status_url).status_code == 204
    assert shared["store"].rows("wa_outbox")[0]["status"] == "delivered"
    assert shared["provider"].metadata_reads == reads and not shared["store"].list_weekly_roster_versions("manager-a")


def test_signature_covers_all_fields_ignores_proxy_url_and_rejects_duplicate_headers(shared):
    form = incoming(shared, "SM" + "c" * 32)
    form["FutureTwilioField"] = " untouched + value "
    assert signed(shared, form, extra_headers={"X-Forwarded-Host": "attacker.test", "X-Forwarded-Proto": "http"}).status_code == 200
    changed = dict(form, MessageSid="SM" + "d" * 32, FutureTwilioField="tampered")
    signature = RequestValidator(TOKEN).compute_signature(shared["config"].webhook_url, form)
    headers = {"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature}
    assert shared["client"].post(shared["config"].webhook_url, content=urlencode(changed), headers=headers).status_code == 401
    duplicate = [("Content-Type", "application/x-www-form-urlencoded"), ("X-Twilio-Signature", signature), ("X-Twilio-Signature", signature)]
    assert shared["client"].post(shared["config"].webhook_url, content=urlencode(form), headers=duplicate).status_code == 401
    assert len(shared["store"].rows("wa_jobs")) == 1 and not shared["provider"].metadata_reads


@pytest.mark.parametrize("body,status", [(b"Body=a&Body=b", 400), (b"Body=%Q0", 400), (b"Body=%FF", 400),
    (b"Body=\xff", 400), (b"broken", 400), (b"x=" + b"a" * 65536, 413)])
def test_malformed_or_oversized_form_never_reaches_queue(shared, body, status):
    response = shared["client"].post(shared["config"].webhook_url, content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": "invalid"})
    assert response.status_code == status and not shared["store"].rows("wa_jobs")


def test_exact_configured_query_is_signed_without_reconstruction(shared):
    config = replace(shared["config"], webhook_url=shared["config"].webhook_url + "?route=a%2Fb&mode=1")
    app = create_shared_workflow_app(shared["store"], shared["files"], shared["provider"], shared["auth"], config, clock=lambda: shared["clock"][0])
    local = dict(shared, client=TestClient(app, base_url=ORIGIN), config=config)
    form = incoming(local, "SM" + "e" * 32)
    assert signed(local, form).status_code == 200
    assert signed(local, form, url=config.webhook_url.replace("route=a%2Fb&mode=1", "mode=1&route=a%2Fb")).status_code == 400
    assert signed(local, form, url=config.webhook_url.split("?")[0]).status_code == 400
    assert len(shared["store"].rows("wa_jobs")) == 1


def test_no_public_state_media_admin_or_legacy_chat_endpoints(shared):
    for path in ("/state", "/chat", "/admin", "/enroll", "/docs", "/openapi.json", "/media/twilio/token/file.pdf", "/private/api/settings"):
        response = shared["client"].get(path)
        assert response.status_code in {401, 404}
        assert TOKEN not in response.text and ACCESS_A not in response.text and "Fictional hotel-a" not in response.text


def test_config_token_is_hidden_and_factory_fails_closed_on_wrong_provider_or_live_dev(shared):
    assert TOKEN not in repr(shared["config"])
    provider = shared["provider"]
    provider.phone_number_id = "twilio:AC" + "f" * 32 + ":15555550901"
    with pytest.raises(ValueError, match="match"):
        create_shared_workflow_app(shared["store"], shared["files"], provider, shared["auth"], shared["config"])
    provider.phone_number_id = shared["config"].phone_number_id
    provider.is_live = True
    with pytest.raises(ValueError, match="local-only|Live WhatsApp is blocked"):
        create_shared_workflow_app(shared["store"], shared["files"], provider, shared["auth"], shared["config"])


@pytest.mark.parametrize("changes", [{"webhook_url": "http://workflow.synthetic.test/webhooks/twilio/whatsapp"},
    {"webhook_url": "https://workflow.synthetic.test/wrong"}, {"webhook_url": "https://127.0.0.1/webhooks/twilio/whatsapp"},
    {"status_url": "https://other.synthetic.test/webhooks/twilio/status"}, {"auth_token": ""},
    {"auth_token": "token with whitespace"}, {"max_jobs": True}, {"max_jobs": 101}])
def test_invalid_config_is_rejected_without_network_or_implicit_settings(shared, changes):
    with pytest.raises(ValueError):
        replace(shared["config"], **changes)
