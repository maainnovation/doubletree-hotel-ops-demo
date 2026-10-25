"""Opt-in durable upload ACK before calculation; hermetic provider fixtures only."""
from copy import deepcopy
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.demo_whatsapp_workflow import DEMO_NOW, DEMO_PHONE_ID, DEMO_SENDER, forecast_pdf, post_signed
from services.whatsapp_provider import AmbiguousSendError, ProviderError, RetryableProviderError
from services.whatsapp_store import WorkflowStore
from services.whatsapp_workflow import ForecastWorker
from test_whatsapp_workflow import system, upload
from test_twilio_live_pilot import pilot, signed, MANAGER


ACK = "Got your PDF. I’m calculating the forecast and will send the report shortly."


@pytest.fixture
def acknowledged(system):
    store, files, provider, _, client, clock = system
    worker = ForecastWorker(store, files, provider, clock=lambda: clock[0], acknowledge_uploads=True)
    return store, files, provider, worker, client, clock


def outbox(store, purpose):
    return [row for row in store.rows("wa_outbox") if row["purpose"] == purpose]


def test_feature_is_off_by_default_and_keeps_original_one_reply(system):
    store, _, provider, worker, _, _ = system
    upload(system)
    assert worker.drain_ready() == {"processed": 1, "dispatched": 1}
    assert [sent["type"] for sent in provider.sent] == ["document"]
    assert not outbox(store, "ack") and len(outbox(store, "result")) == 1


def test_validated_ack_is_sent_before_any_calculation_and_pdf_on_next_drain(acknowledged, monkeypatch):
    import services.whatsapp_workflow as workflow
    store, _, provider, worker, _, _ = acknowledged
    events = []
    generate, send_text = workflow.generate_weekly_roster, provider.send_text
    def trace_generate(*args, **kwargs):
        assert events == ["ack"]
        events.append("calculate")
        return generate(*args, **kwargs)
    def trace_text(to, text, key):
        assert text == ACK and to == DEMO_SENDER
        assert not store.list_weekly_roster_versions("gm")
        events.append("ack")
        return send_text(to, text, key)
    monkeypatch.setattr(workflow, "generate_weekly_roster", trace_generate)
    monkeypatch.setattr(provider, "send_text", trace_text)
    assert upload(acknowledged).status_code == 200 and not provider.sent
    assert worker.drain_ready() == {"processed": 1, "dispatched": 1}
    assert events == ["ack"] and not store.list_weekly_roster_versions("gm")
    assert store.rows("wa_jobs")[0]["status"] == "awaiting_ack"
    ack = outbox(store, "ack")[0]
    assert ack["text"] == ACK and ack["kind"] == "text" and ack["status"] == "accepted"
    assert worker.drain_ready() == {"processed": 1, "dispatched": 1}
    assert events == ["ack", "calculate"]
    assert [sent["type"] for sent in provider.sent] == ["text", "document"]
    assert len(store.list_weekly_roster_versions("gm")) == 1
    result = outbox(store, "result")[0]
    assert result["id"] != ack["id"] and result["job_id"] == ack["job_id"]
    assert result["kind"] == "document" and result["status"] == "accepted"


@pytest.mark.parametrize("data", [b"not-pdf", forecast_pdf(hotel_code="WRONG-HOTEL"), forecast_pdf(departures=401)])
def test_invalid_pdf_gets_correction_only_never_acknowledgement(acknowledged, data):
    store, _, provider, worker, _, _ = acknowledged
    upload(acknowledged, data)
    worker.drain_ready()
    assert not outbox(store, "ack") and not store.list_weekly_roster_versions("gm")
    assert len(provider.sent) == 1 and provider.sent[0]["type"] == "text"
    assert provider.sent[0]["text"] != ACK and "No roster was changed" in provider.sent[0]["text"]


def test_integrity_mismatch_never_acknowledged(acknowledged):
    store, _, provider, worker, _, _ = acknowledged
    upload(acknowledged)
    provider.media["synthetic-media-1"] = forecast_pdf(departures=31)
    worker.drain_ready()
    assert not outbox(store, "ack") and not store.list_weekly_roster_versions("gm")
    assert "integrity check" in provider.sent[0]["text"]


def test_unauthorized_sender_never_gets_ack_or_result(acknowledged):
    store, _, provider, worker, _, _ = acknowledged
    upload(acknowledged, sender="15555550999")
    assert worker.drain_ready() == {"processed": 0, "dispatched": 0}
    assert not store.rows("wa_jobs") and not store.rows("wa_outbox") and not provider.sent


def test_same_message_redelivery_never_repeats_ack(acknowledged):
    store, _, provider, worker, _, _ = acknowledged
    upload(acknowledged); upload(acknowledged)
    worker.drain_ready()
    upload(acknowledged)
    worker.drain_ready(); worker.drain_ready()
    assert len(outbox(store, "ack")) == len(outbox(store, "result")) == 1
    assert [sent["type"] for sent in provider.sent] == ["text", "document"]


@pytest.mark.parametrize("phase", ["before_ack_dispatch", "after_ack", "after_result"])
def test_same_content_different_message_does_not_repeat_ack_or_result(acknowledged, phase):
    store, _, provider, worker, _, _ = acknowledged
    upload(acknowledged)
    if phase == "before_ack_dispatch":
        assert worker.process_one()
    else:
        worker.drain_ready()
        if phase == "after_result":
            worker.drain_ready()
    upload(acknowledged, message_id="wamid.same-content-another-message")
    for _ in range(4):
        worker.drain_ready()
    assert len(outbox(store, "ack")) == len(outbox(store, "result")) == 1
    assert [sent["type"] for sent in provider.sent] == ["text", "document"]
    assert len(store.list_weekly_roster_versions("gm")) == 1


def test_restart_after_accepted_ack_resumes_without_resending_ack(acknowledged):
    store, files, provider, worker, _, clock = acknowledged
    upload(acknowledged); worker.drain_ready()
    ack_id = outbox(store, "ack")[0]["id"]
    restarted = ForecastWorker(WorkflowStore(store.path), files, provider, clock=lambda: clock[0], acknowledge_uploads=True)
    assert restarted.drain_ready() == {"processed": 1, "dispatched": 1}
    assert outbox(store, "ack")[0]["id"] == ack_id
    assert [sent["type"] for sent in provider.sent] == ["text", "document"]
    assert len(store.list_weekly_roster_versions("gm")) == 1


def test_crash_after_ack_send_before_recording_receipt_never_blind_retries(acknowledged, monkeypatch):
    store, files, provider, worker, _, clock = acknowledged
    original = provider.send_text
    def crash_after_send(*args):
        original(*args)
        raise SystemExit("Synthetic process crash after send")
    monkeypatch.setattr(provider, "send_text", crash_after_send)
    upload(acknowledged)
    assert worker.process_one()
    with pytest.raises(SystemExit):
        worker.dispatch_one()
    assert outbox(store, "ack")[0]["status"] == "sending"
    monkeypatch.setattr(provider, "send_text", original)
    clock[0] += 301
    restarted = ForecastWorker(WorkflowStore(store.path), files, provider, clock=lambda: clock[0], acknowledge_uploads=True)
    restarted.drain_ready(); restarted.drain_ready()
    assert outbox(store, "ack")[0]["status"] == "reconciliation_required"
    assert len(provider.sent) == 1 and not outbox(store, "result")
    assert not store.list_weekly_roster_versions("gm")


def test_safe_ack_presend_retry_reuses_same_outbox_before_calculation(acknowledged, monkeypatch):
    store, _, provider, worker, _, clock = acknowledged
    send_text = provider.send_text
    calls = []
    def flaky(*args):
        calls.append(args[-1])
        if len(calls) == 1:
            raise RetryableProviderError("Synthetic pre-send failure")
        return send_text(*args)
    monkeypatch.setattr(provider, "send_text", flaky)
    upload(acknowledged); worker.drain_ready()
    ack = outbox(store, "ack")[0]
    assert ack["status"] == "retry" and not store.list_weekly_roster_versions("gm")
    clock[0] += 61
    worker.drain_ready()
    assert outbox(store, "ack")[0]["status"] == "accepted"
    assert not store.list_weekly_roster_versions("gm")
    worker.drain_ready()
    assert calls == [ack["id"], ack["id"]]
    assert len(outbox(store, "ack")) == len(store.list_weekly_roster_versions("gm")) == 1
    assert [sent["type"] for sent in provider.sent] == ["text", "document"]


def test_ack_on_fifth_validation_attempt_still_has_calculation_budget(acknowledged, monkeypatch):
    store, _, provider, worker, _, clock = acknowledged
    original = provider.fetch_media
    fetches = []
    def flaky(*args):
        fetches.append(args)
        if len(fetches) <= 4:
            raise RetryableProviderError("Synthetic pre-ACK media failure")
        return original(*args)
    monkeypatch.setattr(provider, "fetch_media", flaky)
    upload(acknowledged)
    for _ in range(4):
        worker.drain_ready()
        assert not provider.sent
        clock[0] += 1000
    worker.drain_ready()
    job = store.rows("wa_jobs")[0]
    assert job["status"] == "awaiting_ack" and job["attempts"] == job["ack_base_attempts"] == 5
    assert [sent["type"] for sent in provider.sent] == ["text"]
    worker.drain_ready()
    assert [sent["type"] for sent in provider.sent] == ["text", "document"]
    assert len(store.list_weekly_roster_versions("gm")) == 1
    job = store.rows("wa_jobs")[0]
    assert job["attempts"] == 6 and job["ack_base_attempts"] == 5


@pytest.mark.parametrize("failure", ["caught", "process_crash"])
def test_post_ack_five_calculation_failures_send_one_terminal_notice_not_infinite_wait(acknowledged, monkeypatch, failure):
    store, _, provider, worker, _, clock = acknowledged
    calls = []
    def fail(*args, **kwargs):
        calls.append(None)
        if failure == "process_crash":
            raise SystemExit("Synthetic calculation crash")
        raise RuntimeError("Synthetic calculation failure")
    monkeypatch.setattr("services.whatsapp_workflow.generate_weekly_roster", fail)
    upload(acknowledged); worker.drain_ready()
    for _ in range(5):
        if failure == "process_crash":
            with pytest.raises(SystemExit):
                worker.process_one()
        else:
            worker.drain_ready()
        clock[0] += 2000
    worker.drain_ready(); worker.drain_ready()
    assert len(calls) == 5 and not store.list_weekly_roster_versions("gm")
    assert [sent["type"] for sent in provider.sent] == ["text", "text"]
    assert provider.sent[0]["text"] == ACK and provider.sent[1]["text"] != ACK
    assert "could not be completed" in provider.sent[1]["text"]
    assert len(outbox(store, "ack")) == len(outbox(store, "result")) == 1
    job = store.rows("wa_jobs")[0]
    assert job["attempts"] == 6 and job["ack_base_attempts"] == 1


def test_fresh_upload_recovers_provably_unsent_expired_ack_without_changing_original_audit(acknowledged):
    store, _, provider, worker, _, clock = acknowledged
    upload(acknowledged, timestamp=clock[0] - 86399)
    assert worker.process_one()
    old = outbox(store, "ack")[0]
    assert old["attempts"] == 0 and old["status"] == "pending"
    clock[0] += 2
    assert worker.dispatch_one()
    assert outbox(store, "ack")[0]["status"] == "outside_window" and not provider.sent
    upload(acknowledged, message_id="wamid.fresh-after-ack-expiry", timestamp=clock[0])
    worker.drain_ready(); worker.drain_ready(); worker.drain_ready()
    rows = outbox(store, "ack")
    assert len(rows) == 2
    original = next(row for row in rows if row["id"] == old["id"])
    assert original["status"] == "cancelled" and original["attempts"] == 0
    assert original["job_id"] == old["job_id"] and original["provider_message_id"] is None
    assert original["error_code"]
    assert next(job for job in store.rows("wa_jobs") if job["id"] == old["job_id"])["status"] == "failed"
    assert [sent["type"] for sent in provider.sent] == ["text", "document"]
    assert len(store.list_weekly_roster_versions("gm")) == 1


@pytest.mark.parametrize("failure,expected", [(AmbiguousSendError, "reconciliation_required"), (ProviderError, "failed")])
def test_unconfirmed_or_rejected_ack_blocks_calculation_and_never_blind_retries(acknowledged, monkeypatch, failure, expected):
    store, _, provider, worker, _, clock = acknowledged
    calls = []
    def fail(*args):
        calls.append(args[-1])
        raise failure("Synthetic failure")
    monkeypatch.setattr(provider, "send_text", fail)
    upload(acknowledged); worker.drain_ready()
    assert outbox(store, "ack")[0]["status"] == expected
    clock[0] += 3600
    for _ in range(3):
        worker.drain_ready()
    assert len(calls) == 1 and not outbox(store, "result")
    assert not store.list_weekly_roster_versions("gm")


@pytest.mark.parametrize("status", ["accepted", "sent", "delivered", "read"])
def test_only_successful_ack_states_allow_calculation(acknowledged, status):
    store, _, _, worker, _, _ = acknowledged
    upload(acknowledged); worker.drain_ready()
    with store._connect() as db:
        db.execute("UPDATE wa_outbox SET status=? WHERE purpose='ack'", (status,))
    worker.drain_ready()
    assert len(store.list_weekly_roster_versions("gm")) == 1 and len(outbox(store, "result")) == 1


@pytest.mark.parametrize("status", ["sending", "reconciliation_required", "failed", "delivery_failed", "outside_window", "cancelled"])
def test_non_successful_ack_states_do_not_allow_calculation(acknowledged, status):
    store, _, _, worker, _, _ = acknowledged
    upload(acknowledged); worker.drain_ready()
    with store._connect() as db:
        db.execute("UPDATE wa_outbox SET status=? WHERE purpose='ack'", (status,))
    worker.drain_ready()
    assert not store.list_weekly_roster_versions("gm") and not outbox(store, "result")


def test_expired_before_ack_blocks_both_ack_send_and_calculation(acknowledged):
    store, _, provider, worker, _, _ = acknowledged
    upload(acknowledged, timestamp=DEMO_NOW.timestamp() - 86401)
    for _ in range(2):
        worker.drain_ready()
    assert not provider.sent and not store.list_weekly_roster_versions("gm")
    assert not outbox(store, "result")
    assert not outbox(store, "ack")
    assert store.rows("wa_jobs")[0]["status"] == "failed"
    assert store.rows("wa_jobs")[0]["error_code"] == "ack_window_expired"


@pytest.mark.parametrize("change", ["settings", "binding", "sender", "real_data"])
def test_ack_dispatch_revalidates_saved_settings_and_enrollment_before_send(acknowledged, change):
    store, _, provider, worker, _, _ = acknowledged
    upload(acknowledged)
    assert worker.process_one()
    assert outbox(store, "ack")[0]["status"] == "pending"
    if change == "settings":
        store.save_hotel_parameters("gm", {"checkout_minutes": 99})
    else:
        with store._connect() as db:
            if change == "binding":
                db.execute("UPDATE wa_tenants SET hotel_code='CHANGED'")
            elif change == "sender":
                db.execute("UPDATE wa_senders SET enabled=0")
            else:
                db.execute("UPDATE staff_settings SET dataset_mode='operational'")
    worker.drain_ready(); worker.drain_ready()
    assert not provider.sent and not store.list_weekly_roster_versions("gm")
    assert outbox(store, "ack")[0]["status"] not in {"pending", "retry", "sending", "accepted", "sent", "delivered", "read"}


@pytest.mark.parametrize("change", ["sender_revoked", "tenant_revoked", "binding_changed", "window_expired"])
def test_revocation_or_expiry_after_ack_prevents_calculation(acknowledged, change):
    store, _, provider, worker, _, clock = acknowledged
    upload(acknowledged); worker.drain_ready()
    with store._connect() as db:
        if change == "sender_revoked":
            db.execute("UPDATE wa_senders SET enabled=0")
        elif change == "tenant_revoked":
            db.execute("UPDATE wa_tenants SET enabled=0")
        elif change == "binding_changed":
            db.execute("UPDATE wa_tenants SET hotel_code='CHANGED'")
        else:
            clock[0] += 86400
    worker.drain_ready()
    assert not store.list_weekly_roster_versions("gm")
    assert not any(row["kind"] == "document" for row in outbox(store, "result"))
    assert not any(sent["type"] == "document" for sent in provider.sent)


def test_second_phase_revalidates_media_and_settings_not_cached_first_pass(acknowledged, monkeypatch):
    store, _, provider, worker, _, _ = acknowledged
    calls = []
    original = provider.fetch_media
    def tracked(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(provider, "fetch_media", tracked)
    upload(acknowledged); worker.drain_ready()
    store.save_hotel_parameters("gm", {"checkout_minutes": 99})
    revision = store.get_staff_settings("gm")["_settings_revision"]
    worker.drain_ready()
    assert len(calls) == 2
    assert store.get_weekly_roster("gm", "2026-10-23")["source"]["settings_revision"] == revision
    assert len(outbox(store, "ack")) == 1


def test_replayed_media_failure_sends_correction_not_second_ack_or_pdf(acknowledged):
    store, _, provider, worker, _, _ = acknowledged
    upload(acknowledged); worker.drain_ready()
    provider.media["synthetic-media-1"] = b"different bytes"
    worker.drain_ready()
    assert len(outbox(store, "ack")) == 1 and not store.list_weekly_roster_versions("gm")
    assert [sent["type"] for sent in provider.sent] == ["text", "text"]
    assert provider.sent[1]["text"] != ACK


def test_manual_lock_added_between_ack_and_calculation_is_preserved(acknowledged):
    store, files, provider, worker, _, clock = acknowledged
    upload(acknowledged)
    ForecastWorker(store, files, provider, clock=lambda: clock[0]).drain_ready()
    upload(acknowledged, forecast_pdf(departures=220, stayovers=180), message_id="wamid.changed-forecast")
    worker.drain_ready()
    prior = store.get_weekly_roster("gm", "2026-10-23")
    row = next(row for row in prior["schedule"] if row["code"] == "SHIFT")
    row.update(locked=True, notes="Fictional manager lock added after ACK")
    locked = deepcopy(row)
    store.save_weekly_roster("gm", prior["week_start"], prior)
    worker.drain_ready()
    roster = store.get_weekly_roster("gm", "2026-10-23")
    preserved = next(row for row in roster["schedule"] if row["staff_id"] == locked["staff_id"] and row["date"] == locked["date"])
    assert preserved["locked"] and all(preserved[key] == locked[key] for key in ("code", "start", "end"))
    assert roster["workflow_status"] == "needs_attention"


def test_same_file_resend_after_stale_result_correction_recalculates_and_preserves_manual_lock(acknowledged):
    store, _, provider, worker, _, _ = acknowledged
    upload(acknowledged); worker.drain_ready()
    assert worker.process_one()  # Calculate, but do not send the PDF yet.
    prior = store.get_weekly_roster("gm", "2026-10-23")
    row = next(row for row in prior["schedule"] if row["code"] == "SHIFT")
    row.update(locked=True, notes="Fictional manual edit before PDF dispatch")
    locked = deepcopy(row)
    store.save_weekly_roster("gm", prior["week_start"], prior)
    assert worker.dispatch_one()
    assert provider.sent[-1]["type"] == "text" and "No outdated PDF was sent" in provider.sent[-1]["text"]
    assert not any(sent["type"] == "document" for sent in provider.sent)
    upload(acknowledged, message_id="wamid.resend-after-manual-edit")
    worker.drain_ready(); worker.drain_ready(); worker.drain_ready()
    assert provider.sent[-1]["type"] == "document"
    assert sum(sent["type"] == "document" for sent in provider.sent) == 1
    latest = store.get_weekly_roster("gm", "2026-10-23")
    assert latest["version"] == 3 and len(store.list_weekly_roster_versions("gm")) == 3
    preserved = next(row for row in latest["schedule"] if row["staff_id"] == locked["staff_id"] and row["date"] == locked["date"])
    assert preserved["locked"] and all(preserved[key] == locked[key] for key in ("code", "start", "end"))


def test_failed_ack_callback_before_pending_result_does_not_swallow_fresh_same_file_upload(acknowledged):
    store, _, provider, worker, client, _ = acknowledged
    upload(acknowledged); worker.drain_ready()
    old_ack = outbox(store, "ack")[0]
    assert worker.process_one()
    old_result = outbox(store, "result")[0]
    assert old_result["status"] == "pending"
    callback = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": DEMO_PHONE_ID}, "statuses": [{"id": old_ack["provider_message_id"],
        "recipient_id": DEMO_SENDER, "status": "failed"}]}}]}]}
    assert post_signed(client, callback).status_code == 200
    assert outbox(store, "ack")[0]["status"] == "delivery_failed"
    upload(acknowledged, message_id="wamid.fresh-after-ack-delivery-failure")
    worker.drain_ready(); worker.drain_ready(); worker.drain_ready()
    prior = next(row for row in outbox(store, "result") if row["id"] == old_result["id"])
    assert prior["status"] == "cancelled" and prior["error_code"] == "ack_not_accepted"
    assert len(outbox(store, "ack")) == len(outbox(store, "result")) == 2
    assert provider.sent[-1]["type"] == "document"
    assert sum(sent["type"] == "document" for sent in provider.sent) == 1
    assert len(store.list_weekly_roster_versions("gm")) == 2


def test_twilio_signed_upload_uses_ack_then_pdf_same_sender_and_shared_saved_settings(pilot):
    rt, client, state, clock, _ = pilot
    worker = ForecastWorker(rt.store, rt.files, rt.provider, clock=lambda: clock[0], acknowledge_uploads=True)
    assert signed(client).status_code == 200
    worker.drain_ready()
    assert len(state["sends"]) == 1 and state["sends"][0]["Body"] == ACK
    assert "MediaUrl" not in state["sends"][0] and not rt.store.list_weekly_roster_versions(MANAGER)
    worker.drain_ready()
    assert len(state["sends"]) == 2 and "MediaUrl" in state["sends"][1]
    assert state["sends"][0]["To"] == state["sends"][1]["To"]
    assert len(state["pdfs"]) == 1 and len(rt.store.list_weekly_roster_versions(MANAGER)) == 1
    with rt.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM pilot_send_attempts").fetchone()[0] == 2


def test_twilio_ack_does_not_reset_or_bypass_existing_three_send_cap(pilot):
    rt, client, state, clock, _ = pilot
    with rt.store._connect() as db:
        db.executemany("INSERT INTO pilot_send_attempts(outbox_id,attempt) VALUES(?,1)", [("prior-synthetic-1",), ("prior-synthetic-2",)])
    worker = ForecastWorker(rt.store, rt.files, rt.provider, clock=lambda: clock[0], acknowledge_uploads=True)
    signed(client); worker.drain_ready(); worker.drain_ready()
    assert len(state["sends"]) == 1 and state["sends"][0]["Body"] == ACK
    assert not state["pdfs"] and outbox(rt.store, "result")[0]["status"] == "failed"
    with rt.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM pilot_send_attempts").fetchone()[0] == 3


@pytest.mark.parametrize("flag", [None, "0", "true", "1"])
def test_twilio_worker_script_ack_flag_is_explicit_opt_in(pilot, monkeypatch, flag):
    import scripts.twilio_pilot_worker as script
    rt, client, state, _, _ = pilot
    original = script.run_worker
    monkeypatch.setenv("PILOT_ENABLED", "1")
    monkeypatch.setattr(script, "PilotConfig", SimpleNamespace(from_env=lambda: rt.config))
    monkeypatch.setattr(script, "build_runtime", lambda config: rt)
    monkeypatch.setattr(script, "run_worker", lambda runtime, **kwargs: original(runtime, once=True, **kwargs))
    if flag is None:
        monkeypatch.delenv("WHATSAPP_UPLOAD_ACK_ENABLED", raising=False)
    else:
        monkeypatch.setenv("WHATSAPP_UPLOAD_ACK_ENABLED", flag)
    signed(client)
    script.main()
    assert len(state["sends"]) == 1
    assert (state["sends"][0].get("Body") == ACK) is (flag == "1")
    assert bool(rt.store.list_weekly_roster_versions(MANAGER)) is (flag != "1")


@pytest.mark.parametrize("flag", [None, "0", "true", "1"])
def test_default_worker_script_ack_flag_is_explicit_opt_in(system, monkeypatch, flag):
    import scripts.whatsapp_worker as script
    store, files, provider, _, _, _ = system
    upload(system)
    monkeypatch.setattr(script, "load_dotenv", lambda *_: None)
    monkeypatch.setattr(script, "WorkflowStore", lambda: store)
    monkeypatch.setattr(script, "PrivateFileStore", lambda *_: files)
    monkeypatch.setattr(script, "provider_from_env", lambda: provider)
    constructor = script.ForecastWorker
    monkeypatch.setattr(script, "ForecastWorker", lambda *args, **kwargs: constructor(*args, clock=DEMO_NOW.timestamp, **kwargs))
    monkeypatch.setattr("sys.argv", ["whatsapp_worker", "--once"])
    monkeypatch.setenv("WHATSAPP_FORECAST_ENABLED", "1")
    if flag is None:
        monkeypatch.delenv("WHATSAPP_UPLOAD_ACK_ENABLED", raising=False)
    else:
        monkeypatch.setenv("WHATSAPP_UPLOAD_ACK_ENABLED", flag)
    script.main()
    assert len(provider.sent) == 1
    assert (provider.sent[0].get("text") == ACK) is (flag == "1")
    assert bool(store.list_weekly_roster_versions("gm")) is (flag != "1")


def test_legacy_outbox_migration_preserves_ids_counters_and_allows_ack_result_pair(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE wa_outbox (
            id TEXT PRIMARY KEY,job_id TEXT NOT NULL UNIQUE,tenant_id TEXT NOT NULL,
            phone_number_id TEXT NOT NULL,recipient TEXT NOT NULL,kind TEXT NOT NULL,
            text TEXT NOT NULL,object_key TEXT,filename TEXT,status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,available_at REAL NOT NULL,
            provider_message_id TEXT,error_code TEXT,updated_at REAL NOT NULL)""")
        for n, status in enumerate(("failed", "accepted", "reconciliation_required"), 1):
            db.execute("INSERT INTO wa_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"old-{n}", f"job-{n}", "synthetic", "123456789", DEMO_SENDER,
                "document", "Historical result", "private.pdf", "report.pdf", status, n, 100, f"provider-{n}", "historical-error", 200))
        before = dict((row[0], row) for row in db.execute("SELECT * FROM wa_outbox"))
    store = WorkflowStore(path)
    rows = store.rows("wa_outbox")
    assert len(rows) == 3
    old_columns = ("id", "job_id", "tenant_id", "phone_number_id", "recipient", "kind", "text", "object_key", "filename", "status", "attempts", "available_at", "provider_message_id", "error_code", "updated_at")
    for row in rows:
        assert row["purpose"] == "result" and tuple(row[name] for name in old_columns) == before[row["id"]]
    with store._connect() as db:
        columns = ",".join(old_columns)
        values = [rows[0][name] for name in old_columns]
        values[0] = "new-ack"
        db.execute(f"INSERT INTO wa_outbox({columns},purpose) VALUES({','.join('?' for _ in old_columns)},'ack')", values)
        values[0] = "duplicate-result"
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f"INSERT INTO wa_outbox({columns},purpose) VALUES({','.join('?' for _ in old_columns)},'result')", values)
    restarted = WorkflowStore(path)
    assert sorted(restarted.rows("wa_outbox"), key=lambda r:r["id"]) == sorted(store.rows("wa_outbox"), key=lambda r:r["id"])


@pytest.mark.parametrize("phase", ["media_fetch", "before_persist"])
def test_ack_failure_callback_fences_active_second_phase_without_reviving_wait(acknowledged, monkeypatch, phase):
    store, _, provider, worker, client, _ = acknowledged
    upload(acknowledged)
    worker.drain_ready()
    ack = outbox(store, "ack")[0]
    assert ack["status"] == "accepted"

    def fail_ack():
        assert store.rows("wa_jobs")[0]["status"] == "processing"
        callback = {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": DEMO_PHONE_ID}, "statuses": [{"id": ack["provider_message_id"],
            "recipient_id": DEMO_SENDER, "status": "failed"}]}}]}]}
        assert post_signed(client, callback).status_code == 200
        assert store.rows("wa_jobs")[0]["status"] == "failed"

    if phase == "media_fetch":
        original = provider.fetch_media
        def intercepted(*args, **kwargs):
            fail_ack()
            return original(*args, **kwargs)
        monkeypatch.setattr(provider, "fetch_media", intercepted)
    else:
        original = worker._persist_roster
        def intercepted(*args, **kwargs):
            fail_ack()
            return original(*args, **kwargs)
        monkeypatch.setattr(worker, "_persist_roster", intercepted)

    assert worker.drain_ready() == {"processed": 1, "dispatched": 0}
    assert worker.drain_ready() == {"processed": 0, "dispatched": 0}
    job = store.rows("wa_jobs")[0]
    assert job["status"] == "failed" and job["error_code"] == "ack_delivery_failed"
    assert outbox(store, "ack")[0]["status"] == "delivery_failed"
    assert not outbox(store, "result") and not store.list_weekly_roster_versions("gm")
    assert [sent["type"] for sent in provider.sent] == ["text"]
