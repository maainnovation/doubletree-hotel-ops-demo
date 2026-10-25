"""Synthetic-only media capabilities: no hosting, network or credentials."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from urllib.parse import urlsplit

import pytest

from scripts.demo_whatsapp_workflow import DEMO_CODE, DEMO_NOW, DEMO_PHONE_ID, DEMO_SENDER, forecast_pdf, webhook_payload
from services.staff_settings import demo_defaults
from services.twilio_media import MediaGrantError, TwilioMediaPublisher
from services.whatsapp_provider import FakeWhatsAppProvider
from services.whatsapp_store import PrivateFileStore, WorkflowStore
from services.whatsapp_workflow import ForecastWorker


TWILIO_BUSINESS = "twilio:AC" + "1" * 32 + ":15555550000"


@pytest.fixture
def media(tmp_path):
    store = WorkflowStore(tmp_path / "state.sqlite3")
    files = PrivateFileStore(tmp_path / "private")
    store.enroll_tenant(tenant_id="synthetic", manager_id="gm", hotel_code=DEMO_CODE, hotel_name="Fictional Hotel")
    store.enroll_sender(tenant_id="synthetic", phone_number_id=DEMO_PHONE_ID, sender=DEMO_SENDER)
    store.save_staff_settings("gm", demo_defaults(), dataset_mode="demo")
    store.save_hotel_parameters("gm", {"room_capacity": 400})
    now = [DEMO_NOW.timestamp()]
    data = forecast_pdf()
    store.ingest(webhook_payload(data), now[0])
    worker = ForecastWorker(store, files, FakeWhatsAppProvider(media={"synthetic-media-1": data}), clock=lambda: now[0])
    assert worker.process_one()
    # The fixture runs the already-tested scheduler independently of the new
    # Twilio intake parser, then explicitly binds this fictional result to it.
    with store._connect() as db:
        for table in ("wa_senders", "wa_jobs", "wa_outbox"):
            db.execute(f"UPDATE {table} SET phone_number_id=?", (TWILIO_BUSINESS,))
        db.execute("UPDATE wa_outbox SET status='sending'")
    item = store.rows("wa_outbox")[0]
    pdf = files.read(item["tenant_id"], item["object_key"])
    publisher = TwilioMediaPublisher(store, files, "https://media.example.test", clock=lambda: now[0])
    return store, files, publisher, item, pdf, now


def issued(media):
    store, files, publisher, item, pdf, now = media
    url = publisher.publish(pdf, item["filename"], item["id"])
    *_, token, filename = urlsplit(url).path.split("/")
    return token, filename, url


def test_pdf_is_private_and_capability_contains_no_identity(media):
    store, files, publisher, item, pdf, now = media
    token, filename, url = issued(media)
    assert url.startswith("https://media.example.test/media/twilio/")
    assert item["tenant_id"] not in url and item["recipient"] not in url and item["id"] not in url
    assert publisher.get(token, filename) == pdf
    with store._connect() as db:
        row = db.execute("SELECT token_hash,gets FROM wa_twilio_media_grants").fetchone()
        assert row == (hashlib.sha256(token.encode()).hexdigest(), 1)
        assert token not in str(db.execute("SELECT * FROM wa_twilio_media_grants").fetchall())


def test_head_does_not_spend_get_allowance_and_fetches_are_bounded(media):
    _, _, publisher, _, pdf, _ = media
    token, filename, _ = issued(media)
    for _ in range(6):
        assert publisher.get(token, filename, head=True) == pdf
    for _ in range(4):
        assert publisher.get(token, filename) == pdf
    with pytest.raises(MediaGrantError):
        publisher.get(token, filename)
    with pytest.raises(MediaGrantError):
        publisher.get(token, filename, head=True)


def test_expiration_at_boundary_and_not_extended_by_access(media):
    _, _, publisher, _, pdf, now = media
    token, filename, _ = issued(media)
    now[0] += 599
    assert publisher.get(token, filename) == pdf
    now[0] += 1
    with pytest.raises(MediaGrantError):
        publisher.get(token, filename)


def test_expiration_never_extends_customer_service_window(media):
    store, _, publisher, _, _, now = media
    with store._connect() as db:
        db.execute("UPDATE wa_senders SET last_inbound=?", (now[0] - 86390,))
    token, filename, _ = issued(media)
    now[0] += 10
    with pytest.raises(MediaGrantError):
        publisher.get(token, filename)


@pytest.mark.parametrize("token,filename", [
    ("unknown", "roster.pdf"), ("x" * 43, "roster.pdf"), ("../escape", "roster.pdf"),
    (None, "roster.pdf"), ("x" * 43, "../secret.pdf"), ("x" * 43, "name.txt"),
])
def test_invalid_capability_requests_are_generic(media, token, filename):
    with pytest.raises(MediaGrantError, match="^PDF media grant is unavailable.$"):
        media[2].get(token, filename)


def test_valid_token_wrong_filename_does_not_consume_or_disclose(media):
    token, filename, _ = issued(media)
    with pytest.raises(MediaGrantError):
        media[2].get(token, "other.pdf")
    assert media[2].get(token, filename) == media[4]


@pytest.mark.parametrize("status", ["pending", "retry", "cancelled", "failed", "outside_window", "delivery_failed"])
def test_revoked_or_nonfetchable_outbox_denies(media, status):
    token, filename, _ = issued(media)
    with media[0]._connect() as db:
        db.execute("UPDATE wa_outbox SET status=?", (status,))
    with pytest.raises(MediaGrantError):
        media[2].get(token, filename)


@pytest.mark.parametrize("status", ["sending", "accepted", "sent", "delivered", "read", "reconciliation_required"])
def test_provider_fetchable_states_keep_short_lived_capability(media, status):
    token, filename, _ = issued(media)
    with media[0]._connect() as db:
        db.execute("UPDATE wa_outbox SET status=?", (status,))
    assert media[2].get(token, filename) == media[4]


@pytest.mark.parametrize("mutation", [
    "UPDATE wa_senders SET enabled=0",
    "UPDATE wa_tenants SET enabled=0",
    "UPDATE wa_tenants SET hotel_code='WRONG'",
    "UPDATE wa_tenants SET report_profile='hotel_daily_table_v1'",
    "UPDATE wa_jobs SET tenant_id='different'",
    "UPDATE wa_jobs SET sender='15555550999'",
    "UPDATE wa_jobs SET phone_number_id='99999999999'",
    "UPDATE wa_outbox SET tenant_id='different'",
    "UPDATE wa_outbox SET kind='text'",
    "UPDATE staff_settings SET dataset_mode='operational'",
    "DELETE FROM weekly_rosters",
    "DELETE FROM wa_jobs",
    "UPDATE wa_jobs SET status='failed'",
])
def test_binding_revocation_real_data_and_cross_tenant_changes_fail_closed(media, mutation):
    token, filename, _ = issued(media)
    with media[0]._connect() as db:
        db.execute(mutation)
    with pytest.raises(MediaGrantError):
        media[2].get(token, filename)


def test_changed_parameters_or_manual_roster_version_revokes_pdf(media):
    store, _, publisher, _, _, _ = media
    token, filename, _ = issued(media)
    store.save_hotel_parameters("gm", {"checkout_minutes": 99})
    with pytest.raises(MediaGrantError):
        publisher.get(token, filename)


def test_new_roster_version_revokes_old_pdf(media):
    store, _, publisher, _, _, _ = media
    token, filename, _ = issued(media)
    roster = store.get_weekly_roster("gm", "2026-10-23")
    store.save_weekly_roster("gm", roster["week_start"], roster)
    with pytest.raises(MediaGrantError):
        publisher.get(token, filename)


def test_object_substitution_and_corruption_denied(media):
    store, files, publisher, item, _, _ = media
    token, filename, _ = issued(media)
    substitute = files.put(item["tenant_id"], b"%PDF-not-the-roster", "pdf")
    with store._connect() as db:
        db.execute("UPDATE wa_outbox SET object_key=?", (substitute,))
    with pytest.raises(MediaGrantError):
        publisher.get(token, filename)


def test_private_file_tampering_is_not_returned(media):
    _, files, publisher, item, _, _ = media
    token, filename, _ = issued(media)
    (files.root / item["object_key"]).write_bytes(b"%PDF-tampered")
    with pytest.raises(MediaGrantError):
        publisher.get(token, filename)


@pytest.mark.parametrize("change", ["bytes", "filename", "key", "not_sending", "meta_identity"])
def test_publish_only_exact_enrolled_inflight_roster(media, change):
    store, _, publisher, item, pdf, _ = media
    if change == "bytes":
        pdf = b"%PDF-other"
    elif change == "filename":
        item["filename"] = "different.pdf"
    elif change == "key":
        item["id"] = "unknown"
    elif change == "not_sending":
        with store._connect() as db:
            db.execute("UPDATE wa_outbox SET status='accepted'")
    else:
        with store._connect() as db:
            for table in ("wa_senders", "wa_jobs", "wa_outbox"):
                db.execute(f"UPDATE {table} SET phone_number_id=?", (DEMO_PHONE_ID,))
    with pytest.raises(MediaGrantError):
        publisher.publish(pdf, item["filename"], item["id"])


def test_safe_retry_rotates_token_and_revokes_previous_grant(media):
    token1, filename, _ = issued(media)
    token2, _, _ = issued(media)
    assert token1 != token2
    with pytest.raises(MediaGrantError):
        media[2].get(token1, filename)
    assert media[2].get(token2, filename) == media[4]
    with media[0]._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM wa_twilio_media_grants").fetchone()[0] == 1


def test_concurrent_gets_cannot_exceed_limit(media):
    token, filename, _ = issued(media)
    def attempt(_):
        try:
            media[2].get(token, filename)
            return True
        except MediaGrantError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(12))) == 4


@pytest.mark.parametrize("url", ["http://example.test", "https://user:pass@example.test", "https://example.test:123", "https://example.test/path", "https://example.test?token=x", "https://example.test#x", "https://example.test/\n", None])
def test_invalid_public_origin_denied(media, url):
    with pytest.raises(MediaGrantError):
        TwilioMediaPublisher(media[0], media[1], url)


@pytest.mark.parametrize("kwargs", [{"ttl_seconds": 0}, {"ttl_seconds": 601}, {"ttl_seconds": True}, {"max_gets": 0}, {"max_gets": 9}, {"max_gets": True}])
def test_unbounded_limits_denied(media, kwargs):
    with pytest.raises(MediaGrantError):
        TwilioMediaPublisher(media[0], media[1], "https://example.test", **kwargs)


def test_not_a_production_storage_adapter(media):
    media[0].development_only = False
    with pytest.raises(MediaGrantError, match="development adapters only"):
        TwilioMediaPublisher(media[0], media[1], "https://example.test")


def test_standalone_twilio_demo_writes_pdf_and_safe_summary_without_network(tmp_path):
    from pypdf import PdfReader
    from scripts.demo_twilio_workflow import run_demo
    output = tmp_path / "twilio-demo"
    summary = run_demo(output)
    assert summary["external_messages_sent"] == 0 and summary["mock_pdf_replies"] == 1
    assert summary["media_head_requests"] == summary["media_get_requests"] == 1
    assert summary["worker"] == {"processed": 1, "dispatched": 1}
    assert summary["versions_preserved"] == [1, 2] and summary["locked_assignment_preserved"]
    assert summary["status"] == "needs_attention" and summary["constraint_count"] > 0
    text = "\n".join(page.extract_text() for page in PdfReader(output / "synthetic-twilio-weekly-roster.pdf").pages)
    assert "Day-by-day calculation summary" in text and "NEEDS ATTENTION" in text
    raw = (output / "summary.json").read_text()
    assert all(secret not in raw for secret in ("MediaUrl", "Authorization", "synthetic-process-local-token", "/media/twilio/"))
    with pytest.raises(FileExistsError):
        run_demo(output)
