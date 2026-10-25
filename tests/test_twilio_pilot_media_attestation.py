"""Independent, hermetic checks for the isolated trial's Message-404 fallback.

Only a signed, enrolled, in-flight synthetic pilot job may use authenticated
Media metadata; the generic Twilio provider must never silently adopt this path.
"""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from io import BytesIO
import json
from urllib.parse import parse_qs, urlencode

from fastapi.testclient import TestClient
import httpx
from pypdf import PdfReader
import pytest
from twilio.request_validator import RequestValidator

from api.twilio_pilot import create_app
from scripts.demo_whatsapp_workflow import DEMO_NOW
from scripts.twilio_pilot_worker import run_worker
from services.twilio_pilot import PilotConfig, build_runtime, MANAGER, TENANT, MAX_SEND_ATTEMPTS
from services.twilio_provider import TwilioWhatsAppProvider
from services.whatsapp_provider import ProviderError

ACCOUNT, MESSAGE, MEDIA = "AC" + "a" * 32, "SM" + "b" * 32, "ME" + "c" * 32
SENDER, BUSINESS, TOKEN = "15555550123", "15555550900", "synthetic-only-attestation-token"
ORIGIN = "https://synthetic-pilot.onrender.com"
MEDIA_URL = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages/{MESSAGE}/Media/{MEDIA}"


@pytest.fixture
def attestation(tmp_path):
    clock = [DEMO_NOW.timestamp()]
    config = PilotConfig.from_env({"PILOT_ENABLED": "1", "PILOT_VOLUME_CONFIRMED": "1",
        "PILOT_DATA_ROOT": str(tmp_path / "pilot"), "PILOT_APPROVED_UNTIL": (DEMO_NOW + timedelta(hours=2)).isoformat(),
        "PILOT_WEEK_START": "2026-10-23", "PILOT_EXTERNAL_ORIGIN": ORIGIN, "PILOT_MANAGER_PHONE": SENDER,
        "TWILIO_ACCOUNT_SID": ACCOUNT, "TWILIO_AUTH_TOKEN": TOKEN, "TWILIO_WHATSAPP_FROM": BUSINESS}, now=clock[0])
    state = {"message_status": 404, "media_status": 200, "download_status": 200,
        "media_created": clock[0], "media_override": {}, "message_override": {}, "raw_media": None,
        "calls": [], "sends": [], "pdfs": [], "metadata_hook": None}
    runtime, client = None, None
    def transport(request):
        assert request.url.host == "api.twilio.com"
        assert request.headers.get("authorization", "").startswith("Basic ")
        state["calls"].append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith(f"/{MESSAGE}.json"):
            return httpx.Response(state["message_status"], json={"sid": MESSAGE, "account_sid": ACCOUNT,
                "from": "whatsapp:+" + SENDER, "to": "whatsapp:+" + BUSINESS, "direction": "inbound",
                "date_created": format_datetime(DEMO_NOW), **state["message_override"]})
        if request.method == "GET" and request.url.path.endswith(f"/Media/{MEDIA}.json"):
            if state["metadata_hook"]:
                state["metadata_hook"]()
            if state["raw_media"] is not None:
                return httpx.Response(state["media_status"], content=state["raw_media"])
            return httpx.Response(state["media_status"], json={"sid": MEDIA, "account_sid": ACCOUNT,
                "parent_sid": MESSAGE, "content_type": "application/pdf",
                "date_created": format_datetime(datetime.fromtimestamp(state["media_created"], timezone.utc)),
                **state["media_override"]}, headers={"Location": "https://evil.example/credential-leak"})
        if request.method == "GET" and str(request.url) == MEDIA_URL:
            return httpx.Response(state["download_status"], content=runtime.fixture, headers={"Content-Type": "application/pdf"})
        assert request.method == "POST" and request.url.path.endswith("/Messages.json")
        payload = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        state["sends"].append(payload)
        if "MediaUrl" in payload:
            response = client.get(payload["MediaUrl"])
            assert response.status_code == 200
            state["pdfs"].append(response.content)
        return httpx.Response(201, json={"sid": "SM" + "d" * 32})
    runtime = build_runtime(config, transport=httpx.MockTransport(transport), clock=lambda: clock[0])
    with TestClient(create_app(runtime), base_url=ORIGIN, client=("198.51.100.20", 12345)) as client:
        yield runtime, client, state, clock


def upload(system):
    _, client, _, _ = system
    data = {"AccountSid": ACCOUNT, "MessageSid": MESSAGE, "From": "whatsapp:+" + SENDER,
        "To": "whatsapp:+" + BUSINESS, "NumMedia": "1", "MediaContentType0": "application/pdf", "MediaUrl0": MEDIA_URL}
    signature = RequestValidator(TOKEN).compute_signature(ORIGIN + "/webhooks/twilio/whatsapp", data)
    return client.post("/webhooks/twilio/whatsapp", content=urlencode(data),
        headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature})


def media_metadata_calls(state):
    return [path for method, path in state["calls"] if method == "GET" and path.endswith(f"/Media/{MEDIA}.json")]


def test_fresh_authenticated_media_fallback_signed_roundtrip_preserves_all_gates(attestation):
    rt, _, state, clock = attestation
    assert upload(attestation).status_code == 200 and not state["calls"]
    assert run_worker(rt, once=True) == {"processed": 1, "dispatched": 1}
    assert len(state["sends"]) == len(state["pdfs"]) == 1
    assert state["sends"][0]["To"] == "whatsapp:+" + SENDER and "Body" not in state["sends"][0]
    assert "Day-by-day calculation summary" in "\n".join(page.extract_text() for page in PdfReader(BytesIO(state["pdfs"][0])).pages)
    job = rt.store.rows("wa_jobs")[0]
    expected_anchor = min(state["media_created"], clock[0], rt.config.approved_until - 86400) - 300
    assert job["inbound_at"] == expected_anchor
    assert rt.store.enrollment(rt.config.phone_id, SENDER)["last_inbound"] == expected_anchor
    audit = json.loads(job["media_json"])["_pilot_attestation"]
    assert audit["media_created_at"] == state["media_created"] and audit["window_anchor"] == expected_anchor
    assert upload(attestation).status_code == 200
    assert run_worker(rt, once=True) == {"processed": 0, "dispatched": 0}
    assert len(state["sends"]) == 1
    assert rt.store.get_staff_settings(MANAGER)["settings"]["hotel_inventory"]["room_capacity"] == 292
    with rt.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM pilot_send_attempts").fetchone()[0] == 1
    assert MAX_SEND_ATTEMPTS == 3


@pytest.mark.parametrize("status", [400, 401, 403, 408, 429, 500])
def test_non404_message_failure_never_uses_media_fallback(attestation, status):
    rt, _, state, _ = attestation
    state["message_status"] = status
    upload(attestation)
    run_worker(rt, once=True)
    assert not media_metadata_calls(state) and not state["sends"]
    assert not rt.store.list_weekly_roster_versions(MANAGER)


@pytest.mark.parametrize("override", [
    {"from": "whatsapp:+15555550999"}, {"to": "whatsapp:+15555550888"}, {"direction": "outbound-api"},
    {"account_sid": "AC" + "f" * 32}, {"sid": "SM" + "f" * 32},
    {"date_created": "not-a-date"}, {"date_created": format_datetime(DEMO_NOW - timedelta(days=2))},
])
def test_normal_message_identity_or_date_failure_never_falls_back(attestation, override):
    rt, _, state, _ = attestation
    state.update(message_status=200, message_override=override)
    upload(attestation)
    run_worker(rt, once=True)
    assert not media_metadata_calls(state) and not state["sends"]
    assert rt.store.rows("wa_jobs")[0]["status"] == "failed"


@pytest.mark.parametrize("override", [
    {"sid": "ME" + "f" * 32}, {"account_sid": "AC" + "f" * 32}, {"parent_sid": "SM" + "f" * 32},
    {"content_type": "text/html"}, {"content_type": "image/png"}, {"date_created": "no-date"},
    {"date_created": "Sun, 25 Oct 2026 12:00:00"}, {"date_created": None},
])
def test_media_metadata_identity_mime_and_date_fail_closed(attestation, override):
    rt, _, state, _ = attestation
    state["media_override"] = override
    upload(attestation)
    run_worker(rt, once=True)
    assert len(media_metadata_calls(state)) == 1 and not state["sends"]
    assert not rt.store.list_weekly_roster_versions(MANAGER)
    assert not list(rt.files.root.rglob("*.pdf"))


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 404, 500])
def test_media_metadata_denial_or_redirect_never_fetches_content_or_sends(attestation, status):
    rt, _, state, _ = attestation
    state["media_status"] = status
    upload(attestation)
    run_worker(rt, once=True)
    assert len(state["calls"]) == 2 and not state["sends"]
    assert not rt.store.list_weekly_roster_versions(MANAGER)


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b"x" * 65537])
def test_media_metadata_body_is_bounded_and_json_object_only(attestation, raw):
    rt, _, state, _ = attestation
    state["raw_media"] = raw
    upload(attestation)
    run_worker(rt, once=True)
    assert not state["sends"] and len(state["calls"]) == 2


@pytest.mark.parametrize("age,allowed", [(0, True), (1800, True), (1801, False), (-1, False)])
def test_fallback_exact_thirty_minute_age_boundary_and_no_future_dates(attestation, age, allowed):
    rt, _, state, clock = attestation
    now = clock[0]
    state["media_created"] = now - age
    clock[0] = now - max(age, 0)
    upload(attestation)
    clock[0] = now
    run_worker(rt, once=True)
    assert bool(state["sends"]) is allowed


@pytest.mark.parametrize("skew,allowed", [(-300, True), (300, True), (-301, False), (301, False)])
def test_media_time_must_match_first_receipt_within_five_minutes(attestation, skew, allowed):
    rt, _, state, clock = attestation
    now = clock[0]
    clock[0] = now - 600
    state["media_created"] = clock[0] + skew
    upload(attestation)
    clock[0] = now
    run_worker(rt, once=True)
    assert bool(state["sends"]) is allowed


def test_safe_media_retry_retains_first_receipt_not_rewritten_window_anchor(attestation):
    rt, _, state, clock = attestation
    state["download_status"] = 429
    upload(attestation)
    assert run_worker(rt, once=True) == {"processed": 1, "dispatched": 0}
    assert rt.store.rows("wa_jobs")[0]["status"] == "retry" and not state["sends"]
    state["download_status"] = 200
    clock[0] += 120
    assert run_worker(rt, once=True) == {"processed": 1, "dispatched": 1}
    assert len(state["sends"]) == 1


@pytest.mark.parametrize("mutation", [
    "UPDATE wa_jobs SET status='pending'", "UPDATE wa_jobs SET tenant_id='another-tenant'",
    "UPDATE wa_jobs SET sender='15555550999'", "UPDATE wa_jobs SET phone_number_id='9999999999'",
    "UPDATE wa_jobs SET message_id='SMffffffffffffffffffffffffffffffff'", "DELETE FROM wa_jobs",
])
def test_fallback_requires_exact_processing_job_binding(attestation, mutation):
    rt, _, state, clock = attestation
    upload(attestation)
    assert rt.store.claim_job(clock[0], rt.config.phone_id)
    with rt.store._connect() as db:
        db.execute(mutation)
    with pytest.raises(ProviderError):
        rt.provider.verify_inbound_message(MESSAGE, SENDER, clock[0])
    assert not media_metadata_calls(state) and not state["sends"]


@pytest.mark.parametrize("change", [
    {"_provider": "meta"}, {"type": "image"}, {"mime": "image/png"},
    {"id": "https://evil.example/pdf"}, {"_enrollment_binding": {"tenant_id": "other"}},
])
def test_fallback_requires_exact_frozen_media_and_enrollment(attestation, change):
    rt, _, state, clock = attestation
    upload(attestation)
    job = rt.store.claim_job(clock[0], rt.config.phone_id)
    media = json.loads(job["media_json"])
    media.update(change)
    with rt.store._connect() as db:
        db.execute("UPDATE wa_jobs SET media_json=? WHERE id=?", (json.dumps(media), job["id"]))
    with pytest.raises(ProviderError):
        rt.provider.verify_inbound_message(MESSAGE, SENDER, clock[0])
    assert not media_metadata_calls(state) and not state["sends"]


def test_no_job_generic_transport_does_not_enable_fallback(attestation):
    rt, _, state, clock = attestation
    with pytest.raises(ProviderError):
        rt.provider.transport_provider.verify_inbound_message(MESSAGE, SENDER, clock[0])
    assert not media_metadata_calls(state)


@pytest.mark.parametrize("url", [
    "https://evil.example/Media/ME" + "c" * 32, MEDIA_URL.replace(ACCOUNT, "AC" + "f" * 32),
    MEDIA_URL.replace(MESSAGE, "SM" + "f" * 32), MEDIA_URL + "?extra=1", MEDIA_URL.replace("https:", "http:"),
])
def test_metadata_helper_rejects_noncanonical_or_cross_identity_before_network(attestation, url):
    rt, _, state, clock = attestation
    with pytest.raises(ProviderError):
        rt.provider.transport_provider.verify_inbound_media_metadata(url, MESSAGE, clock[0])
    assert not state["calls"]


def test_resource_not_found_exception_only_nonsending_404():
    from services.twilio_provider import ProviderResourceNotFound
    provider = TwilioWhatsAppProvider(ACCOUNT, TOKEN, BUSINESS,
        transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    with pytest.raises(ProviderResourceNotFound):
        provider.verify_inbound_message(MESSAGE, SENDER, DEMO_NOW.timestamp())
    with pytest.raises(ProviderError) as error:
        provider.send_text(SENDER, "Synthetic", "test-send")
    assert not isinstance(error.value, ProviderResourceNotFound)


def test_successful_normal_message_path_remains_unchanged(attestation):
    rt, _, state, clock = attestation
    state["message_status"] = 200
    upload(attestation)
    assert run_worker(rt, once=True) == {"processed": 1, "dispatched": 1}
    job = rt.store.rows("wa_jobs")[0]
    assert job["inbound_at"] == clock[0]
    assert "_pilot_attestation" not in json.loads(job["media_json"])
    assert not media_metadata_calls(state)


def test_claim_change_during_metadata_request_denies_attestation_commit(attestation):
    rt, _, state, clock = attestation
    upload(attestation)
    rt.store.claim_job(clock[0], rt.config.phone_id)
    def change_claim():
        with rt.store._connect() as db:
            db.execute("UPDATE wa_jobs SET claim_token='reclaimed-by-another-worker'")
    state["metadata_hook"] = change_claim
    with pytest.raises(ProviderError, match="claim changed"):
        rt.provider.verify_inbound_message(MESSAGE, SENDER, clock[0])
    assert "_pilot_attestation" not in json.loads(rt.store.rows("wa_jobs")[0]["media_json"])
    assert not state["sends"]


@pytest.mark.parametrize("seconds_before_expiry,allowed", [(301, True), (300, False), (299, False)])
def test_conservative_window_ends_five_minutes_before_fixed_pilot_expiry(attestation, seconds_before_expiry, allowed):
    rt, _, state, clock = attestation
    clock[0] = rt.config.approved_until - seconds_before_expiry
    state["media_created"] = clock[0]
    upload(attestation)
    run_worker(rt, once=True)
    assert bool(state["sends"]) is allowed


@pytest.mark.parametrize("change", [{"first_received_at": True}, {"first_received_at": float("nan")},
    {"window_anchor": 0}, {"media_created_at": 0}, {"kind": "untrusted"}])
def test_retry_audit_cannot_forge_first_receipt_or_anchored_time(attestation, change):
    rt, _, state, clock = attestation
    state["download_status"] = 429
    upload(attestation)
    run_worker(rt, once=True)
    job = rt.store.rows("wa_jobs")[0]
    media = json.loads(job["media_json"])
    media["_pilot_attestation"].update(change)
    with rt.store._connect() as db:
        db.execute("UPDATE wa_jobs SET media_json=? WHERE id=?", (json.dumps(media), job["id"]))
    state["download_status"] = 200
    clock[0] += 120
    run_worker(rt, once=True)
    assert not state["sends"] and not rt.store.list_weekly_roster_versions(MANAGER)


def test_crash_after_attestation_before_worker_update_keeps_atomic_retry_state(attestation):
    rt, _, state, clock = attestation
    upload(attestation)
    rt.store.claim_job(clock[0], rt.config.phone_id)
    anchor = rt.provider.verify_inbound_message(MESSAGE, SENDER, clock[0])
    job = rt.store.rows("wa_jobs")[0]
    assert job["inbound_at"] == anchor
    assert json.loads(job["media_json"])["_pilot_attestation"]["window_anchor"] == anchor
    assert rt.provider.verify_inbound_message(MESSAGE, SENDER, clock[0]) == anchor
    assert not state["sends"]
