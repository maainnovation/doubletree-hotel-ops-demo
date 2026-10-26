"""Synthetic private uploads never enter the WhatsApp job/outbox pipeline."""
from copy import deepcopy
from datetime import date
import hashlib
import json

import pytest

from scripts.demo_whatsapp_workflow import DEMO_NOW, DEMO_CODE, forecast_pdf
from services.forecast_import import empty_forecast
from services.private_forecast_workflow import PrivateForecastWorkflow, PrivateForecastError
from services.staff_settings import demo_defaults
from services.weekly_roster import generate_weekly_roster
from services.whatsapp_store import WorkflowStore, PrivateFileStore


@pytest.fixture
def private_system(tmp_path):
    store = WorkflowStore(tmp_path / "shared.sqlite3")
    files = PrivateFileStore(tmp_path / "private")
    for tenant, manager in [("hotel-a", "gm-a"), ("hotel-b", "gm-b")]:
        store.enroll_tenant(tenant_id=tenant, manager_id=manager, hotel_code=DEMO_CODE, hotel_name="Fictional " + tenant)
        store.save_staff_settings(manager, demo_defaults(), dataset_mode="demo")
        store.save_hotel_parameters(manager, {"room_capacity": 400})
    now = [DEMO_NOW.timestamp()]
    enabled = [True]
    rows = empty_forecast(date(2026, 10, 23))
    for row in rows:
        row.update(departures=12, stayovers=40, arrivals=20, rooms_on_books=60,
                   forecast_pickup=0, forecast_occupied=60, occupancy_pct=15)
    calls = []
    def evaluate(data, mime, hotel_code, capacity, at, **kwargs):
        calls.append((data, mime, hotel_code, capacity, at, kwargs))
        return {"method": "local_image_ocr", "candidate_valid_for_confirmation": True, "rows": deepcopy(rows)}
    service = PrivateForecastWorkflow(store, files, clock=lambda: now[0], admission_check=lambda tenant, manager, at: enabled[0], media_evaluator=evaluate)
    return store, files, service, now, enabled, rows, calls


def upload(service, key="synthetic-upload-1", tenant="hotel-a", manager="gm-a", data=b"synthetic-file", mime="image/png"):
    return service.enqueue(tenant, manager, data, mime, key)


def review(service):
    result = upload(service)
    assert service.process_one()
    return service.get("hotel-a", "gm-a", result["id"])


def confirm(service, item):
    return service.action("hotel-a", "gm-a", item["id"], "confirm", item["revision"])


def assert_no_whatsapp(store):
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM wa_jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM wa_outbox").fetchone()[0] == 0


def test_upload_only_queues_and_complete_review_calculates_exact_shared_scheduler(private_system):
    store, files, service, _, _, rows, calls = private_system
    item = upload(service)
    assert item["status"] == "queued" and not calls
    assert store.get_weekly_roster("gm-a", rows[0]["date"]) is None
    assert service.process_one()
    item = service.get("hotel-a", "gm-a", item["id"])
    assert item["status"] == "needs_review" and item["rows"] == json.loads(json.dumps(rows, default=str))
    assert len(calls) == 1 and calls[0][3] == 400
    queued = confirm(service, item)
    assert queued["status"] == "confirmed"
    assert store.get_weekly_roster("gm-a", rows[0]["date"]) is None
    assert service.process_one() and not service.process_one()
    saved = store.get_weekly_roster("gm-a", rows[0]["date"])
    expected = generate_weekly_roster(rows, store.get_staff_settings("gm-a"))
    assert saved["schedule"] == expected["schedule"]
    assert saved["demand_minutes"] == expected["demand_minutes"]
    assert saved["source"]["channel"] == "private_app" and saved["version"] == 1
    assert service.get("hotel-a", "gm-a", item["id"])["result_version"] == 1
    assert_no_whatsapp(store)
    assert "source_key" not in str(service.list_uploads("hotel-a", "gm-a"))


def test_default_denial_operational_unknown_mapping_and_revocation(private_system):
    store, files, service, _, enabled, _, _ = private_system
    with pytest.raises(PrivateForecastError, match="inactive"):
        upload(PrivateForecastWorkflow(store, files))
    with pytest.raises(PrivateForecastError):
        upload(service, manager="gm-b")
    store.save_staff_settings("gm-a", demo_defaults(), dataset_mode="operational")
    with pytest.raises(PrivateForecastError):
        upload(service)
    store.save_staff_settings("gm-a", demo_defaults(), dataset_mode="demo")
    with pytest.raises(PrivateForecastError, match="Total rooms"):
        upload(service)
    store.save_hotel_parameters("gm-a", {"room_capacity": 400})
    item = upload(service)
    enabled[0] = False
    assert service.process_one()
    enabled[0] = True
    assert service.get("hotel-a", "gm-a", item["id"])["status"] == "blocked"
    assert_no_whatsapp(store)


def test_cross_tenant_reads_actions_and_regenerate_fail_closed(private_system):
    service = private_system[2]
    item = review(service)
    for action in ["confirm", "cancel", "regenerate"]:
        with pytest.raises(PrivateForecastError) as exc:
            service.action("hotel-b", "gm-b", item["id"], action, item["revision"], "fresh-request")
        assert exc.value.status_code == 404
    with pytest.raises(PrivateForecastError) as exc:
        service.get("hotel-b", "gm-b", item["id"])
    assert exc.value.status_code == 404
    assert not service.list_uploads("hotel-b", "gm-b")


def test_idempotency_and_cancel_are_durable_across_instances(private_system):
    store, files, service, now, _, _, _ = private_system
    item = upload(service)
    assert upload(service) == item
    with pytest.raises(PrivateForecastError) as exc:
        upload(service, data=b"different")
    assert exc.value.status_code == 409
    restarted = PrivateForecastWorkflow(store, files, clock=lambda: now[0], admission_check=lambda *args: True)
    cancelled = restarted.action("hotel-a", "gm-a", item["id"], "cancel", item["revision"])
    assert cancelled["status"] == "cancelled"
    assert not service.process_one()
    assert_no_whatsapp(store)


@pytest.mark.parametrize("kind", ["incomplete", "invalid", "exception"])
def test_invalid_candidate_never_schedules_and_does_not_leak_errors(private_system, kind):
    store, _, service, _, _, rows, _ = private_system
    def bad(*args, **kwargs):
        if kind == "exception":
            raise RuntimeError("SENSITIVE RAW DOCUMENT")
        return {"candidate_valid_for_confirmation": kind != "incomplete", "rows": rows[:6]}
    service.media_evaluator = bad
    item = upload(service)
    assert service.process_one()
    item = service.get("hotel-a", "gm-a", item["id"])
    assert item["status"] == ("failed" if kind == "exception" else "needs_correction")
    assert "SENSITIVE" not in str(item)
    with pytest.raises(PrivateForecastError): confirm(service, item)
    assert not store.list_weekly_roster_versions("gm-a")
    assert_no_whatsapp(store)


@pytest.mark.parametrize("when", ["extraction", "review", "confirmed"])
def test_settings_change_requires_fresh_review_and_preserves_latest(private_system, when):
    store, _, service, _, _, _, _ = private_system
    if when == "extraction":
        evaluate = service.media_evaluator
        def race(*args, **kwargs):
            value = evaluate(*args, **kwargs)
            store.save_hotel_parameters("gm-a", {"checkout_minutes": 25})
            return value
        service.media_evaluator = race
    item = review(service)
    if when == "review":
        store.save_hotel_parameters("gm-a", {"checkout_minutes": 25})
        with pytest.raises(PrivateForecastError) as exc: confirm(service, item)
        assert exc.value.status_code == 409
    elif when == "confirmed":
        confirm(service, item)
        store.save_hotel_parameters("gm-a", {"checkout_minutes": 25})
        service.process_one()
    else:
        assert item["status"] == "stale"
    assert not store.list_weekly_roster_versions("gm-a")
    assert store.get_staff_settings("gm-a")["settings"]["cleaning_standards"]["checkout_minutes"] == 25


def test_regeneration_reads_latest_parameters_and_preserves_locks_and_versions(private_system):
    store, _, service, _, _, rows, _ = private_system
    prior = generate_weekly_roster(rows, store.get_staff_settings("gm-a"))
    assigned = next(row for row in prior["schedule"] if row["code"] not in {"OFF", "AL", "SICK"})
    assigned["manual"] = True
    prior = store.save_weekly_roster("gm-a", rows[0]["date"], prior)
    first = review(service)
    store.save_hotel_parameters("gm-a", {"checkout_minutes": 25})
    fresh = service.action("hotel-a", "gm-a", first["id"], "regenerate", first["revision"], "synthetic-fresh-1")
    service.process_one()
    fresh = service.get("hotel-a", "gm-a", fresh["id"])
    confirm(service, fresh)
    service.process_one()
    saved = store.get_weekly_roster("gm-a", rows[0]["date"])
    assert saved["version"] == 2
    locked = next(row for row in saved["schedule"] if row["staff_id"] == assigned["staff_id"] and row["date"] == assigned["date"])
    assert locked["locked"] and locked["code"] == assigned["code"]
    versions = store.list_weekly_roster_versions("gm-a")
    assert len(versions) == 2 and versions[1]["schedule"] == prior["schedule"]
    assert_no_whatsapp(store)


def test_roster_changed_after_review_requires_fresh_review(private_system):
    store, _, service, _, _, rows, _ = private_system
    item = review(service)
    store.save_weekly_roster("gm-a", rows[0]["date"], generate_weekly_roster(rows, store.get_staff_settings("gm-a")))
    with pytest.raises(PrivateForecastError) as exc: confirm(service, item)
    assert exc.value.status_code == 409
    assert len(store.list_weekly_roster_versions("gm-a")) == 1


@pytest.mark.parametrize("when", ["review", "source", "lease"])
def test_expiry_and_interrupted_processing_do_not_retry(private_system, when):
    store, _, service, now, _, _, _ = private_system
    item = review(service) if when == "review" else upload(service)
    if when == "lease":
        with store._connect() as db:
            db.execute("UPDATE private_forecast_uploads SET status='extracting',lease_until=?,claim_token='crashed'", (now[0] + 1,))
        now[0] += 2
    else:
        now[0] += 1801 if when == "review" else 86401
    assert not service.process_one()
    assert service.get("hotel-a", "gm-a", item["id"])["status"] == ("failed" if when == "lease" else "expired")
    assert not store.list_weekly_roster_versions("gm-a")


def test_cancellation_during_extraction_wins(private_system):
    store, _, service, _, _, _, _ = private_system
    evaluate = service.media_evaluator
    item = upload(service)
    def race(*args, **kwargs):
        active = service.get("hotel-a", "gm-a", item["id"])
        service.action("hotel-a", "gm-a", item["id"], "cancel", active["revision"])
        return evaluate(*args, **kwargs)
    service.media_evaluator = race
    assert service.process_one()
    assert service.get("hotel-a", "gm-a", item["id"])["status"] == "cancelled"
    assert not store.list_weekly_roster_versions("gm-a")


def test_source_and_candidate_hash_tampering_fail_closed(private_system):
    store, files, service, _, _, _, _ = private_system
    item = review(service)
    with store._connect() as db:
        db.execute("UPDATE private_forecast_uploads SET candidate_json='[]' WHERE id=?", (item["id"],))
    with pytest.raises(PrivateForecastError): confirm(service, item)
    other = upload(service, key="synthetic-upload-2")
    with store._connect() as db:
        key = db.execute("SELECT source_key FROM private_forecast_uploads WHERE id=?", (other["id"],)).fetchone()[0]
    (files.root / key).write_bytes(b"corrupted")
    service.process_one()
    assert service.get("hotel-a", "gm-a", other["id"])["status"] == "failed"
    assert not store.list_weekly_roster_versions("gm-a")


def test_source_limits_pending_rate_and_total_caps(private_system):
    store, files, service, now, _, _, _ = private_system
    for data, mime in [(b"", "image/png"), (b"x", "text/plain"), (b"x" * (5 * 1024 * 1024 + 1), "image/jpeg")]:
        with pytest.raises(PrivateForecastError): upload(service, data=data, mime=mime)
    bounded = PrivateForecastWorkflow(store, files, clock=lambda: now[0], admission_check=lambda *args: True, max_uploads=2, uploads_per_hour=1)
    first = upload(bounded)
    with pytest.raises(PrivateForecastError) as exc: upload(bounded, key="another-key-2")
    assert exc.value.status_code == 429
    bounded.action("hotel-a", "gm-a", first["id"], "cancel", first["revision"])
    now[0] += 3601
    second = upload(bounded, key="another-key-2")
    bounded.action("hotel-a", "gm-a", second["id"], "cancel", second["revision"])
    now[0] += 3601
    with pytest.raises(PrivateForecastError) as exc: upload(bounded, key="another-key-3")
    assert exc.value.status_code == 429


def test_real_native_pdf_isolated_evaluator_still_requires_review(private_system):
    store, _, service, _, _, _, _ = private_system
    service.media_evaluator = None
    item = upload(service, data=forecast_pdf(), mime="application/pdf")
    assert service.process_one()
    item = service.get("hotel-a", "gm-a", item["id"])
    assert item["status"] == "needs_review" and item["method"] == "native_pdf"
    assert len(item["rows"]) == 7 and not store.list_weekly_roster_versions("gm-a")
    confirm(service, item)
    assert service.process_one()
    assert store.list_weekly_roster_versions("gm-a")
    assert_no_whatsapp(store)


def test_regenerate_enqueues_reference_without_http_source_io(private_system, monkeypatch):
    _, files, service, _, _, _, _ = private_system
    item = review(service)
    monkeypatch.setattr(files, "read", lambda *args: pytest.fail("Regeneration must not read source bytes in HTTP"))
    monkeypatch.setattr(files, "put", lambda *args: pytest.fail("Regeneration must not copy source bytes in HTTP"))
    fresh = service.action("hotel-a", "gm-a", item["id"], "regenerate", item["revision"], "synthetic-regenerate-reference")
    assert fresh["status"] == "queued" and fresh["id"] != item["id"]


def test_pending_cap_and_approval_revoked_after_review_and_during_extraction(private_system):
    store, _, service, _, enabled, _, _ = private_system
    item = review(service)
    enabled[0] = False
    with pytest.raises(PrivateForecastError) as exc: confirm(service, item)
    assert exc.value.status_code == 403
    enabled[0] = True
    service.action("hotel-a", "gm-a", item["id"], "cancel", item["revision"])
    evaluate = service.media_evaluator
    def race(*args, **kwargs):
        result = evaluate(*args, **kwargs)
        enabled[0] = False
        return result
    service.media_evaluator = race
    item = upload(service, key="synthetic-revoked-during-extract")
    service.process_one()
    enabled[0] = True
    assert service.get("hotel-a", "gm-a", item["id"])["status"] == "blocked"
    for index in range(8): upload(service, key=f"synthetic-pending-{index}")
    with pytest.raises(PrivateForecastError) as exc: upload(service, key="synthetic-pending-overflow")
    assert exc.value.status_code == 429
    assert not store.list_weekly_roster_versions("gm-a")


def test_approval_and_review_window_rechecked_after_scheduler_before_commit(private_system, monkeypatch):
    import services.private_forecast_workflow as module
    store, _, service, _, enabled, _, _ = private_system
    item = review(service)
    confirm(service, item)
    generate = module.generate_weekly_roster
    def expire(*args, **kwargs):
        value = generate(*args, **kwargs)
        enabled[0] = False
        return value
    monkeypatch.setattr(module, "generate_weekly_roster", expire)
    assert service.process_one()
    enabled[0] = True
    assert service.get("hotel-a", "gm-a", item["id"])["status"] == "blocked"
    assert not store.list_weekly_roster_versions("gm-a")


def test_worker_does_not_commit_after_its_claim_lease_expires(private_system):
    store, _, service, now, _, _, _ = private_system
    evaluate = service.media_evaluator
    def delayed(*args, **kwargs):
        value = evaluate(*args, **kwargs)
        now[0] += 121
        return value
    service.media_evaluator = delayed
    item = upload(service)
    assert service.process_one()
    assert service.get("hotel-a", "gm-a", item["id"])["status"] == "failed"
    assert not store.list_weekly_roster_versions("gm-a")


def test_real_photo_isolated_ocr_review_then_shared_roster_without_whatsapp(private_system):
    import pymupdf
    store, _, service, _, _, _, _ = private_system
    with pymupdf.open(stream=forecast_pdf(), filetype="pdf") as document:
        photo = document[0].get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    service.media_evaluator = None
    item = upload(service, data=photo, mime="image/png")
    assert service.process_one()
    item = service.get("hotel-a", "gm-a", item["id"])
    assert item["status"] == "needs_review" and item["method"] == "local_image_ocr"
    assert len(item["rows"]) == 7 and "OCR can misread" in item["message"]
    assert not store.list_weekly_roster_versions("gm-a")
    confirm(service, item)
    assert service.process_one()
    assert len(store.list_weekly_roster_versions("gm-a")) == 1
    assert_no_whatsapp(store)


def test_private_worker_storage_safety_failure_needs_operator_not_repeated_resend(private_system, tmp_path):
    from services.private_storage_budget import QuotaPrivateFileStore
    store, _, service, now, _, _, _ = private_system
    files = QuotaPrivateFileStore(tmp_path / "bounded-private", free_reserve_bytes=0)
    bounded = PrivateForecastWorkflow(store, files, clock=lambda: now[0], admission_check=lambda *args: True, media_evaluator=service.media_evaluator)
    item = upload(bounded)
    with store._connect() as db:
        key = db.execute("SELECT source_key FROM private_forecast_uploads WHERE id=?", (item["id"],)).fetchone()[0]
    (files.root / key).write_bytes(b"corrupted")
    assert bounded.process_one()
    result = bounded.get("hotel-a", "gm-a", item["id"])
    assert result["status"] == "storage_blocked"
    assert "Contact the operator" in result["message"] and "do not repeatedly resend" in result["message"]
    assert not store.list_weekly_roster_versions("gm-a")
    assert_no_whatsapp(store)
