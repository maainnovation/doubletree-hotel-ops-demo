"""Hermetic integration tests: no network, real accounts, or real hotel data."""
from copy import deepcopy
from datetime import timedelta
import hashlib
from io import BytesIO
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from pypdf import PdfReader
import pytest

from scripts.demo_whatsapp_workflow import (DEMO_CODE, DEMO_NOW, DEMO_PHONE_ID, DEMO_SENDER, FrozenDateTime,
    forecast_pdf, post_signed, run_demo, webhook_payload)
from services.staff_settings import demo_defaults
from services.whatsapp_provider import AmbiguousSendError, FakeWhatsAppProvider, RetryableProviderError
from services.whatsapp_store import PrivateFileStore, WorkflowStore
from services.whatsapp_workflow import ForecastWorker


@pytest.fixture
def system(tmp_path, monkeypatch):
    import api.main as api
    store = WorkflowStore(tmp_path / "db.sqlite3")
    store.enroll_tenant(tenant_id="pilot", manager_id="gm", hotel_code=DEMO_CODE, hotel_name="Fictional Pilot Hotel")
    store.enroll_sender(tenant_id="pilot", phone_number_id=DEMO_PHONE_ID, sender=DEMO_SENDER)
    store.save_staff_settings("gm", demo_defaults(), dataset_mode="demo")
    store.save_hotel_parameters("gm", {"room_capacity":400})
    files = PrivateFileStore(tmp_path / "private")
    provider = FakeWhatsAppProvider(media={"synthetic-media-1": forecast_pdf()})
    clock = [DEMO_NOW.timestamp()]
    worker = ForecastWorker(store, files, provider, clock=lambda: clock[0])
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api, "datetime", FrozenDateTime)
    monkeypatch.setenv("META_APP_SECRET", "synthetic-test-secret")
    monkeypatch.setenv("WHATSAPP_FORECAST_ENABLED", "1")
    return store, files, provider, worker, TestClient(api.app), clock


def upload(system, data=None, **kwargs):
    store, files, provider, worker, client, clock = system
    if data is None:
        data = forecast_pdf()
    media_id = kwargs.get("media_id", "synthetic-media-1")
    provider.media[media_id] = data
    return post_signed(client, webhook_payload(data, **kwargs))


def test_signed_webhook_only_queues_then_persists_pdf_same_sender(system):
    store, files, provider, worker, client, clock = system
    assert upload(system).status_code == 200
    assert len(store.rows("wa_jobs")) == 1 and not provider.sent
    assert store.list_weekly_roster_versions("gm") == []
    assert worker.drain_ready() == {"processed": 1, "dispatched": 1}
    roster = store.get_weekly_roster("gm", "2026-10-23")
    assert roster["source"]["channel"] == "whatsapp" and roster["source"]["dataset_mode"] == "demo"
    assert roster["version"] == 1
    assert len(provider.sent) == 1 and provider.sent[0]["to"] == DEMO_SENDER
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(provider.sent[0]["pdf_bytes"])).pages)
    assert "Day-by-day calculation summary" in text
    assert "2026-10-23" in text and "2026-10-29" in text
    assert store.rows("wa_outbox")[0]["status"] == "accepted"


def test_bad_signature_malformed_and_oversized_webhook(system):
    store, files, provider, worker, client, clock = system
    assert client.post("/webhooks/whatsapp", json=webhook_payload(forecast_pdf())).status_code == 401
    assert post_signed(client, {"object": "wrong"}).status_code == 400
    assert post_signed(client, {"object": "whatsapp_business_account", "entry": "not-list"}).status_code == 400
    assert client.post("/webhooks/whatsapp", content=b"x" * (1024 * 1024 + 1)).status_code == 413
    assert not store.rows("wa_jobs") and not provider.sent


@pytest.mark.parametrize("kwargs", [{"sender": "15555550999"}, {"phone_id": "999999999999"}, {"phone_id": ""}, {"sender": "+15555550123"}])
def test_unauthorized_sender_or_wrong_business_number_never_queues(system, kwargs):
    assert upload(system, **kwargs).status_code == 200
    store, files, provider, worker, client, clock = system
    assert not store.rows("wa_jobs") and not provider.sent


def test_no_implicit_settings_or_hotel_inference(system):
    store, files, provider, worker, client, clock = system
    with store._connect() as db:
        db.execute("DELETE FROM staff_settings WHERE manager_id='gm'")
    upload(system)
    worker.drain_ready()
    assert not store.list_weekly_roster_versions("gm")
    assert "No saved staff/settings" in provider.sent[0]["text"]


def test_operational_data_blocked_in_development(system):
    store, files, provider, worker, client, clock = system
    store.save_staff_settings("gm", demo_defaults(), dataset_mode="operational")
    upload(system)
    worker.drain_ready()
    assert not store.list_weekly_roster_versions("gm")
    assert "fictional demo staff only" in provider.sent[0]["text"]


@pytest.mark.parametrize("data", [b"not-pdf", forecast_pdf(hotel_code="WRONG-HOTEL"), forecast_pdf(departures=401)])
def test_invalid_file_sends_correction_not_roster(system, data):
    upload(system, data)
    store, files, provider, worker, client, clock = system
    worker.drain_ready()
    assert store.rows("wa_jobs")[0]["status"] == "needs_correction"
    assert not store.list_weekly_roster_versions("gm")
    assert len(provider.sent) == 1 and provider.sent[0]["type"] == "text"
    assert "No roster was changed" in provider.sent[0]["text"]


def test_integrity_hash_mismatch_no_roster(system):
    store, files, provider, worker, client, clock = system
    upload(system)
    provider.media["synthetic-media-1"] = forecast_pdf(departures=32)
    worker.drain_ready()
    assert not store.list_weekly_roster_versions("gm")
    assert "integrity check" in provider.sent[0]["text"]


def test_redelivery_and_same_content_hash_do_not_duplicate_version_or_send(system):
    store, files, provider, worker, client, clock = system
    upload(system)
    upload(system)
    worker.drain_ready()
    upload(system, message_id="wamid.duplicate-content")
    worker.drain_ready()
    assert len(store.list_weekly_roster_versions("gm")) == len(provider.sent) == 1
    assert sorted(row["status"] for row in store.rows("wa_jobs")) == ["calculated", "duplicate"]


def test_changed_forecast_preserves_versions_and_locked_manual_assignment(system):
    store, files, provider, worker, client, clock = system
    upload(system)
    worker.drain_ready()
    prior = store.get_weekly_roster("gm", "2026-10-23")
    row = next(row for row in prior["schedule"] if row["code"] == "SHIFT")
    row.update(locked=True, notes="Manager must preserve this assignment")
    locked = deepcopy(row)
    store.save_weekly_roster("gm", prior["week_start"], prior)
    upload(system, forecast_pdf(departures=220, stayovers=180), message_id="wamid.changed", timestamp=DEMO_NOW.timestamp()+1)
    worker.drain_ready()
    latest = store.get_weekly_roster("gm", "2026-10-23")
    retained = next(r for r in latest["schedule"] if r["staff_id"] == locked["staff_id"] and r["date"] == locked["date"])
    assert retained["locked"] and all(retained[k] == locked[k] for k in ("code", "start", "end"))
    assert len(store.list_weekly_roster_versions("gm")) == 3
    assert latest["workflow_status"] == "needs_attention"
    assert "NEEDS ATTENTION" in provider.sent[-1]["caption"]
    assert store.list_weekly_roster_versions("gm")[-1]["forecast"][0]["departures"] == 30


def test_cross_tenant_settings_rosters_and_objects_are_isolated(system):
    store, files, provider, worker, client, clock = system
    store.enroll_tenant(tenant_id="other", manager_id="other-gm", hotel_code="OTHER", hotel_name="Other fictional hotel")
    store.enroll_sender(tenant_id="other", phone_number_id=DEMO_PHONE_ID, sender="15555550124")
    upload(system, sender="15555550124")
    worker.drain_ready()
    assert not store.list_weekly_roster_versions("gm") and not store.list_weekly_roster_versions("other-gm")
    assert provider.sent[0]["to"] == "15555550124"
    key = files.put("pilot", b"%PDF-test", "pdf")
    with pytest.raises(ValueError, match="tenant"):
        files.read("other", key)
    with pytest.raises(ValueError):
        files.read("pilot", "../escape")


def test_outside_window_needs_template_no_freeform_or_document_send(system):
    store, files, provider, worker, client, clock = system
    upload(system, timestamp=DEMO_NOW.timestamp()-86401)
    worker.drain_ready()
    assert not provider.sent
    assert store.rows("wa_outbox")[0]["status"] == "outside_window"
    assert store.rows("wa_outbox")[0]["error_code"] == "approved_template_required"


def test_safe_fetch_retry_and_restart_complete_once(system, monkeypatch):
    store, files, provider, worker, client, clock = system
    upload(system)
    original = provider.fetch_media
    def fail(*args):
        raise RetryableProviderError("temporary")
    monkeypatch.setattr(provider, "fetch_media", fail)
    worker.drain_ready()
    assert store.rows("wa_jobs")[0]["status"] == "retry"
    monkeypatch.setattr(provider, "fetch_media", original)
    clock[0] += 61
    restarted = ForecastWorker(WorkflowStore(store.path), files, provider, clock=lambda: clock[0])
    restarted.drain_ready()
    assert len(provider.sent) == 1 and len(store.list_weekly_roster_versions("gm")) == 1


def test_safe_presend_retry_does_not_recalculate(system, monkeypatch):
    store, files, provider, worker, client, clock = system
    upload(system)
    original = provider.send_document
    monkeypatch.setattr(provider, "send_document", lambda *args: (_ for _ in ()).throw(RetryableProviderError("upload failed before send")))
    worker.drain_ready()
    assert store.rows("wa_outbox")[0]["status"] == "retry"
    assert len(store.list_weekly_roster_versions("gm")) == 1
    monkeypatch.setattr(provider, "send_document", original)
    clock[0] += 61
    worker.drain_ready()
    assert len(provider.sent) == 1 and len(store.list_weekly_roster_versions("gm")) == 1


def test_ambiguous_send_never_auto_retries(system, monkeypatch):
    store, files, provider, worker, client, clock = system
    upload(system)
    original = provider.send_document
    def lost_response(*args):
        original(*args)
        raise AmbiguousSendError("response lost")
    monkeypatch.setattr(provider, "send_document", lost_response)
    worker.drain_ready()
    assert store.rows("wa_outbox")[0]["status"] == "reconciliation_required"
    clock[0] += 600
    worker.drain_ready()
    assert len(provider.sent) == 1


def test_crashed_send_is_reconciliation_not_retry(system):
    store, files, provider, worker, client, clock = system
    upload(system)
    worker.process_one()
    with store._connect() as db:
        db.execute("UPDATE wa_outbox SET status='sending',attempts=1,updated_at=?", (clock[0]-301,))
    assert worker.dispatch_one() is False
    assert store.rows("wa_outbox")[0]["status"] == "reconciliation_required" and not provider.sent


def test_expired_processing_lease_recovers_but_final_attempt_terminates(system):
    store, files, provider, worker, client, clock = system
    upload(system)
    with store._connect() as db:
        db.execute("UPDATE wa_jobs SET status='processing',attempts=1,lease_until=?", (clock[0]-1,))
    worker.drain_ready()
    assert len(provider.sent) == 1
    upload(system, forecast_pdf(departures=32), message_id="wamid.lastattempt")
    with store._connect() as db:
        db.execute("UPDATE wa_jobs SET status='processing',attempts=5,lease_until=? WHERE message_id='wamid.lastattempt'", (clock[0]-1,))
    worker.drain_ready()
    assert store.rows("wa_jobs")[-1]["status"] == "failed"


def test_old_retry_cannot_replace_newer_forecast(system):
    store, files, provider, worker, client, clock = system
    upload(system, forecast_pdf(departures=32), message_id="wamid.older", timestamp=clock[0]-100)
    with store._connect() as db:
        db.execute("UPDATE wa_jobs SET available_at=?", (clock[0]+60,))
    upload(system, forecast_pdf(departures=40), message_id="wamid.newer")
    worker.drain_ready()
    clock[0] += 61
    # Register older media bytes again (same test ID intentionally updated).
    provider.media["synthetic-media-1"] = forecast_pdf(departures=32)
    worker.drain_ready()
    assert store.get_weekly_roster("gm", "2026-10-23")["forecast"][0]["departures"] == 40
    assert len(provider.sent) == 1
    assert store.rows("wa_jobs")[0]["status"] == "superseded"


def test_revoked_sender_blocked_before_processing_and_dispatch(system):
    store, files, provider, worker, client, clock = system
    upload(system)
    worker.process_one()
    with store._connect() as db:
        db.execute("UPDATE wa_senders SET enabled=0")
    worker.dispatch_one()
    assert not provider.sent and store.rows("wa_outbox")[0]["status"] == "cancelled"


def test_status_callback_is_scoped_and_distinguishes_delivery(system):
    store, files, provider, worker, client, clock = system
    upload(system)
    worker.drain_ready()
    outbox = store.rows("wa_outbox")[0]
    status = {"id": outbox["provider_message_id"], "recipient_id": DEMO_SENDER, "status": "delivered"}
    payload = {"object":"whatsapp_business_account", "entry":[{"changes":[{"field":"messages", "value":{"metadata":{"phone_number_id":DEMO_PHONE_ID}, "statuses":[status]}}]}]}
    status["recipient_id"] = "15555550999"
    post_signed(client, payload)
    assert store.rows("wa_outbox")[0]["status"] == "accepted"
    status["recipient_id"] = DEMO_SENDER
    post_signed(client, payload)
    assert store.rows("wa_outbox")[0]["status"] == "delivered"
    status["status"] = "sent"
    post_signed(client, payload)
    assert store.rows("wa_outbox")[0]["status"] == "delivered"


def test_atomic_file_store_permissions_and_repeat_put(system):
    store, files, provider, worker, client, clock = system
    key = files.put("pilot", b"%PDF-safe", "pdf")
    assert files.put("pilot", b"%PDF-safe", "pdf") == key
    assert files.read("pilot", key) == b"%PDF-safe"
    assert os.stat(files.root / key).st_mode & 0o777 == 0o600
    assert not list(files.root.rglob(".pending-*"))


def test_concurrent_claims_and_version_appends_are_unique(system):
    store, files, provider, worker, client, clock = system
    upload(system)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: store.claim_job(clock[0]), range(2)))
    assert sum(item is not None for item in claims) == 1
    # Restore job then create a baseline roster for concurrent manual saves.
    with store._connect() as db:
        db.execute("UPDATE wa_jobs SET status='pending'")
    worker.process_one()
    roster = store.get_weekly_roster("gm", "2026-10-23")
    with ThreadPoolExecutor(max_workers=2) as pool:
        versions = list(pool.map(lambda _: store.save_weekly_roster("gm", "2026-10-23", roster)["version"], range(2)))
    assert sorted(versions) == [2, 3]
    assert len(store.list_weekly_roster_versions("gm")) == 3


def test_live_provider_cannot_use_dev_storage(system):
    store, files, provider, worker, client, clock = system
    class Live:
        is_live = True
    with pytest.raises(ValueError, match="production adapters"):
        ForecastWorker(store, files, Live())


def test_demo_script_proves_end_to_end_and_preserves_lock(tmp_path):
    result = run_demo(tmp_path / "demo")
    assert result["external_messages_sent"] == result["duplicate_replies"] == 0
    assert result["versions_preserved"] == [1, 2]
    assert result["same_authorized_sender"] and result["locked_assignment_preserved"]
    assert result["status"] == "needs_attention"


def test_fresh_same_file_resend_reopens_only_expired_unsent_reply(system):
    store, files, provider, worker, client, clock = system
    upload(system, timestamp=DEMO_NOW.timestamp()-86401)
    worker.drain_ready()
    assert not provider.sent and store.rows("wa_outbox")[0]["status"] == "outside_window"
    upload(system, message_id="wamid.fresh-resend")
    worker.drain_ready()
    assert len(provider.sent) == 1 and len(store.list_weekly_roster_versions("gm")) == 1
    assert store.rows("wa_outbox")[0]["status"] == "accepted"
    upload(system, message_id="wamid.again")
    worker.drain_ready()
    assert len(provider.sent) == 1


def test_sqlite_workflow_file_is_private_and_manager_guard_is_explicit(system):
    store, files, provider, worker, client, clock = system
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    assert store.is_whatsapp_manager("gm") is True
    assert store.is_whatsapp_manager("unknown") is False


def test_webhook_is_default_off(system, monkeypatch):
    store, files, provider, worker, client, clock = system
    monkeypatch.delenv("WHATSAPP_FORECAST_ENABLED", raising=False)
    assert upload(system).status_code == 503
    assert not store.rows("wa_jobs")


def test_failed_object_publish_leaves_no_partial_final_and_can_retry(system, monkeypatch):
    store, files, provider, worker, client, clock = system
    link = os.link
    def fail_before_publish(*args, **kwargs):
        raise OSError("synthetic pre-publication failure")
    monkeypatch.setattr(os, "link", fail_before_publish)
    with pytest.raises(OSError):
        files.put("pilot", b"%PDF-crash-safety", "pdf")
    assert not list(files.root.rglob("*.pdf"))
    monkeypatch.setattr(os, "link", link)
    key = files.put("pilot", b"%PDF-crash-safety", "pdf")
    assert files.read("pilot", key) == b"%PDF-crash-safety"


def test_generation_failure_rolls_back_version_and_outbox_then_retry_completes(system, monkeypatch):
    import services.whatsapp_workflow as workflow
    store, files, provider, worker, client, clock = system
    upload(system)
    original = workflow.build_exports
    monkeypatch.setattr(workflow, "build_exports", lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic renderer failure")))
    worker.drain_ready()
    assert store.rows("wa_jobs")[0]["status"] == "retry"
    assert not store.list_weekly_roster_versions("gm")
    assert not store.rows("wa_outbox") and not provider.sent
    monkeypatch.setattr(workflow, "build_exports", original)
    clock[0] += 61
    worker.drain_ready()
    assert len(store.list_weekly_roster_versions("gm")) == len(provider.sent) == 1


def test_state_store_upgrade_keeps_existing_latest_as_history(tmp_path):
    from services.state_store import StateStore
    path = tmp_path / "old.sqlite3"
    payload = {"week_start":"2026-10-23", "forecast":[{}]*7, "schedule":[], "constraints":[]}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE weekly_rosters(manager_id TEXT, week_start TEXT, payload TEXT, version INTEGER, updated_at TEXT, PRIMARY KEY(manager_id,week_start))")
        db.execute("INSERT INTO weekly_rosters VALUES(?,?,?,?,?)", ("gm", "2026-10-23", json.dumps(payload), 7, "2026-10-23"))
    store = StateStore(path)
    assert store.list_weekly_roster_versions("gm")[0]["version"] == 7
    store.save_weekly_roster("gm", "2026-10-23", payload)
    assert sorted(v["version"] for v in store.list_weekly_roster_versions("gm")) == [7, 8]


def observed_layout_synthetic_pdf(identity=None):
    """Structure-equivalent only: all counts are fictional, not original data."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import landscape, letter
    out = BytesIO()
    pdf = canvas.Canvas(out, pagesize=landscape(letter), invariant=1)
    lines = ([identity] if identity else []) + [
        "FRI SAT SUN MON TUE WED THU",
        "10/23/2026 10/24/2026 10/25/2026 10/26/2026 10/27/2026 10/28/2026 10/29/2026",
        "Prior Day Occupancy",
        "Arrivals 40 40 40 40 40 40 40",
        "Departures 30 30 30 30 30 30 30",
        "Stayovers 60 60 60 60 60 60 60",
        "Rooms on the books 100 100 100 100 100 100 100",
        "Forecasted Pick up 10 10 10 10 10 10 10",
        "Forcasted occupied rooms 110 110 110 110 110 110 110",
        "Occupancy % 27.5% 27.5% 27.5% 27.5% 27.5% 27.5% 27.5%",
    ]
    for i, line in enumerate(lines):
        pdf.drawString(25, 570 - i * 25, line)
    pdf.save()
    return out.getvalue()


def enroll_observed_profile(store):
    store.enroll_tenant(tenant_id="format-pilot", manager_id="format-gm", hotel_code="FORMAT-PILOT",
                       hotel_name="Fictional Format Pilot",
                       report_profile="hotel_daily_table_v1")
    store.enroll_sender(tenant_id="format-pilot", phone_number_id=DEMO_PHONE_ID, sender="15555550125")
    store.save_staff_settings("format-gm", demo_defaults(), dataset_mode="demo")
    store.save_hotel_parameters("format-gm", {"room_capacity":400})


def test_headerless_observed_layout_requires_explicit_profile_enrollment(system):
    store, files, provider, worker, client, clock = system
    data = observed_layout_synthetic_pdf()
    upload(system, data)
    worker.drain_ready()
    assert not store.list_weekly_roster_versions("gm")
    assert store.rows("wa_jobs")[0]["status"] == "needs_correction"
    enroll_observed_profile(store)
    upload(system, data, message_id="wamid.profile-opt-in", sender="15555550125")
    worker.drain_ready()
    roster = store.get_weekly_roster("format-gm", "2026-10-23")
    assert roster["source"]["report_profile"] == "hotel_daily_table_v1"
    assert roster["source"]["hotel_code"] == "FORMAT-PILOT"
    assert roster["forecast"][0]["arrivals"] == 40
    assert roster["forecast"][0]["prior_day_occupancy"] is None
    assert provider.sent[-1]["to"] == "15555550125"
    assert not store.list_weekly_roster_versions("gm")


def test_explicit_profile_never_overrides_conflicting_document_identity(system):
    store, files, provider, worker, client, clock = system
    enroll_observed_profile(store)
    upload(system, observed_layout_synthetic_pdf("Hotel code: DIFFERENT-HOTEL"), sender="15555550125")
    worker.drain_ready()
    assert not store.list_weekly_roster_versions("format-gm")
    assert store.rows("wa_jobs")[0]["status"] == "needs_correction"


def test_profile_change_after_intake_requires_resend_not_reinterpretation(system):
    store, files, provider, worker, client, clock = system
    upload(system, observed_layout_synthetic_pdf())
    with store._connect() as db:
        db.execute("UPDATE wa_tenants SET report_profile='hotel_daily_table_v1' WHERE tenant_id='pilot'")
    worker.drain_ready()
    assert not store.list_weekly_roster_versions("gm")
    assert "format changed" in provider.sent[0]["text"]


def test_profile_change_during_processing_cannot_commit(system, monkeypatch):
    import services.whatsapp_workflow as workflow
    store, files, provider, worker, client, clock = system
    enroll_observed_profile(store)
    upload(system, observed_layout_synthetic_pdf(), sender="15555550125")
    original = workflow.validate_whatsapp_forecast
    def change_after_validation(*args, **kwargs):
        result = original(*args, **kwargs)
        with store._connect() as db:
            db.execute("UPDATE wa_tenants SET report_profile='canonical_v1' WHERE tenant_id='format-pilot'")
        return result
    monkeypatch.setattr(workflow, "validate_whatsapp_forecast", change_after_validation)
    worker.drain_ready()
    assert not store.list_weekly_roster_versions("format-gm")
    assert not store.rows("wa_outbox")
    assert store.rows("wa_jobs")[0]["status"] == "retry"
    monkeypatch.setattr(workflow, "validate_whatsapp_forecast", original)
    clock[0] += 61
    worker.drain_ready()
    assert "format changed" in provider.sent[0]["text"]


def test_profile_cannot_be_unknown_or_auto_detected_and_sender_cannot_map_twice(system):
    store, files, provider, worker, client, clock = system
    with pytest.raises(ValueError, match="report profile"):
        store.enroll_tenant(tenant_id="bad",manager_id="bad",hotel_code="BAD",hotel_name="Fictional",report_profile="auto")
    enroll_observed_profile(store)
    with pytest.raises(sqlite3.IntegrityError):
        store.enroll_sender(tenant_id="format-pilot", phone_number_id=DEMO_PHONE_ID, sender=DEMO_SENDER)
    assert store.enrollment(DEMO_PHONE_ID,DEMO_SENDER)["tenant_id"] == "pilot"


def test_old_tenant_schema_migrates_to_canonical_not_headerless(tmp_path):
    path = tmp_path / "old-enrollment.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE wa_tenants(tenant_id TEXT PRIMARY KEY,manager_id TEXT UNIQUE,hotel_code TEXT,hotel_name TEXT,room_capacity INTEGER,enabled INTEGER DEFAULT 1)")
        db.execute("INSERT INTO wa_tenants(tenant_id,manager_id,hotel_code,hotel_name,room_capacity) VALUES('old','old-gm','OLD','Fictional',400)")
    store = WorkflowStore(path)
    store.enroll_sender(tenant_id="old",phone_number_id=DEMO_PHONE_ID,sender=DEMO_SENDER)
    assert store.enrollment(DEMO_PHONE_ID,DEMO_SENDER)["report_profile"] == "canonical_v1"


def test_unset_app_inventory_blocks_auto_and_legacy_enrollment_capacity_is_ignored(system):
    store, files, provider, worker, client, clock = system
    store.save_hotel_parameters("gm", {"room_capacity":None})
    with store._connect() as db:
        db.execute("UPDATE wa_tenants SET room_capacity=400 WHERE tenant_id='pilot'")
    upload(system)
    worker.drain_ready()
    assert not store.list_weekly_roster_versions("gm")
    assert "saved Parameters" in provider.sent[0]["text"]
    assert "no capacity is inferred" in provider.sent[0]["text"]


def test_worker_uses_latest_shared_minutes_and_capacity_after_intake(system):
    store, files, provider, worker, client, clock = system
    upload(system)
    saved = store.save_hotel_parameters("gm", {"checkout_minutes":25, "room_capacity":400})
    with store._connect() as db:
        db.execute("UPDATE wa_tenants SET room_capacity=1 WHERE tenant_id='pilot'")
    worker.drain_ready()
    roster = store.get_weekly_roster("gm", "2026-10-23")
    assert roster["source"]["settings_revision"] == saved["_settings_revision"]
    assert roster["input_snapshot"]["settings"]["hotel_inventory"]["room_capacity"] == 400
    assert roster["input_snapshot"]["settings"]["cleaning_standards"]["checkout_minutes"] == 25
    assert roster["demand_minutes"]["2026-10-23"] == 30*25 + 60*10


def test_capacity_changed_in_saved_parameters_is_revalidated_not_cached(system):
    store, files, provider, worker, client, clock = system
    upload(system)
    store.save_hotel_parameters("gm", {"room_capacity":200})
    worker.drain_ready()
    assert not store.list_weekly_roster_versions("gm")
    assert "occupancy percent" in provider.sent[0]["text"]


def test_settings_change_during_admission_retries_latest_revision(system, monkeypatch):
    import services.whatsapp_workflow as workflow
    store, files, provider, worker, client, clock = system
    upload(system)
    original = workflow.validate_whatsapp_forecast
    changed = [False]
    def change_settings(*args, **kwargs):
        rows = original(*args, **kwargs)
        if not changed[0]:
            changed[0] = True
            store.save_hotel_parameters("gm", {"checkout_minutes":25})
        return rows
    monkeypatch.setattr(workflow,"validate_whatsapp_forecast",change_settings)
    worker.drain_ready()
    assert store.rows("wa_jobs")[0]["status"] == "retry"
    assert not store.list_weekly_roster_versions("gm") and not provider.sent
    clock[0] += 61
    worker.drain_ready()
    roster = store.get_weekly_roster("gm", "2026-10-23")
    assert roster["input_snapshot"]["settings"]["cleaning_standards"]["checkout_minutes"] == 25
    assert len(provider.sent) == 1


def test_same_file_with_new_saved_parameters_creates_new_version(system):
    store, files, provider, worker, client, clock = system
    upload(system)
    worker.drain_ready()
    store.save_hotel_parameters("gm", {"checkout_minutes":25})
    upload(system, message_id="wamid.new-settings")
    worker.drain_ready()
    roster = store.get_weekly_roster("gm", "2026-10-23")
    assert roster["version"] == 2
    assert roster["input_snapshot"]["settings"]["cleaning_standards"]["checkout_minutes"] == 25
    assert len(provider.sent) == 2
    upload(system, message_id="wamid.duplicate-new-settings")
    worker.drain_ready()
    assert len(provider.sent) == 2


def test_changed_parameters_before_send_block_outdated_pdf_then_resend_recalculates(system):
    store, files, provider, worker, client, clock = system
    upload(system)
    worker.process_one()
    store.save_hotel_parameters("gm", {"checkout_minutes":25})
    worker.dispatch_one()
    assert provider.sent[0]["type"] == "text"
    assert "No outdated PDF was sent" in provider.sent[0]["text"]
    upload(system, message_id="wamid.resend-current-settings")
    worker.drain_ready()
    assert provider.sent[-1]["type"] == "document"
    assert store.get_weekly_roster("gm", "2026-10-23")["version"] == 2


def test_changed_identity_before_dispatch_cancels_document(system):
    store, files, provider, worker, client, clock = system
    upload(system)
    worker.process_one()
    with store._connect() as db:
        db.execute("UPDATE wa_tenants SET hotel_code='CHANGED' WHERE tenant_id='pilot'")
    worker.dispatch_one()
    assert not provider.sent
    assert store.rows("wa_outbox")[0]["error_code"] == "enrollment_changed"


def test_shared_parameter_merge_preserves_newer_staff_and_stale_form_is_rejected(system):
    store, files, provider, worker, client, clock = system
    old = store.get_staff_settings("gm")
    newer = deepcopy(old)
    newer["staff"][0]["display_name"] = "Fictional Updated Employee"
    store.save_staff_settings("gm", newer, dataset_mode="demo", expected_revision=old["_settings_revision"])
    latest = store.save_hotel_parameters("gm", {"checkout_minutes":25})
    assert latest["staff"][0]["display_name"] == "Fictional Updated Employee"
    assert latest["settings"]["hotel_inventory"]["room_capacity"] == 400
    with pytest.raises(ValueError,match="another session"):
        store.save_staff_settings("gm",old,dataset_mode="demo",expected_revision=old["_settings_revision"])


def test_provider_business_id_must_match_job_without_consuming_another_providers_work(system):
    store, files, provider, worker, client, clock = system
    provider.phone_number_id = "999999999999"
    upload(system)
    worker.drain_ready()
    assert not provider.sent and not store.list_weekly_roster_versions("gm")
    assert store.rows("wa_jobs")[0]["status"] == "pending"
    assert store.rows("wa_jobs")[0]["error_code"] is None


def test_settings_revert_after_stale_text_does_not_suppress_requested_pdf(system):
    store, files, provider, worker, client, clock = system
    upload(system)
    worker.process_one()
    store.save_hotel_parameters("gm", {"checkout_minutes":25})
    worker.dispatch_one()
    assert provider.sent[0]["type"] == "text"
    store.save_hotel_parameters("gm", {"checkout_minutes":22})
    upload(system, message_id="wamid.reverted-settings-resend")
    worker.drain_ready()
    assert provider.sent[-1]["type"] == "document"
    assert store.get_weekly_roster("gm", "2026-10-23")["version"] == 2


def test_conditional_capacity_initialization_never_overwrites_concurrent_user_value(system):
    store, files, provider, worker, client, clock = system
    store.save_hotel_parameters("gm", {"room_capacity":None})
    old = store.get_staff_settings("gm")
    store.save_hotel_parameters("gm", {"room_capacity":350})
    with pytest.raises(ValueError,match="without overwriting"):
        store.save_hotel_parameters("gm", {"room_capacity":300}, expected_revision=old["_settings_revision"])
    assert store.get_staff_settings("gm")["settings"]["hotel_inventory"]["room_capacity"] == 350
