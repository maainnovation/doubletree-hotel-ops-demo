"""Pilot-only one-read diagnostic capabilities, with zero real provider calls."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from urllib.parse import urlsplit

import pytest

from services.twilio_media import MediaGrantError
from services.twilio_pilot import MANAGER
from services.whatsapp_workflow import ForecastWorker
from test_twilio_live_pilot import pilot, signed  # Reuse the exact synthetic pilot runtime.


@pytest.fixture
def probe(pilot):
    from services.twilio_probe import PilotDiagnosticMediaPublisher
    rt, client, state, clock, _ = pilot
    assert signed(client).status_code == 200
    worker = ForecastWorker(rt.store, rt.files, rt.provider, clock=lambda: clock[0])
    assert worker.process_one()
    item = rt.store.rows("wa_outbox")[0]
    assert item["kind"] == "document" and not state["sends"]
    with rt.store._connect() as db:
        db.execute("UPDATE wa_outbox SET status='failed',error_code='twilio_http_403_code_20008',attempts=3 WHERE id=?", (item["id"],))
        db.executemany("INSERT INTO pilot_send_attempts(outbox_id,attempt) VALUES(?,?)", [(item["id"], attempt) for attempt in (1, 2, 3)])
    rt.publisher = PilotDiagnosticMediaPublisher(rt.store, rt.files, rt.config.origin,
        approved_until=rt.config.approved_until, clock=lambda: clock[0])
    item = rt.store.rows("wa_outbox")[0]
    pdf = rt.files.read(item["tenant_id"], item["object_key"])
    return rt, client, state, clock, item, pdf


def mint(probe):
    rt, _, _, _, item, _ = probe
    url = rt.publisher.enable_diagnostic_probe(item["id"])
    *_, token, filename = urlsplit(url).path.split("/")
    return token, filename, url


def preserved_state(probe):
    rt, _, state, _, _, _ = probe
    with rt.store._connect() as db:
        budget = db.execute("SELECT outbox_id,attempt FROM pilot_send_attempts ORDER BY outbox_id,attempt").fetchall()
    return rt.store.rows("wa_outbox"), rt.store.rows("wa_jobs"), budget, list(state["requests"]), list(state["sends"])


def test_disabled_until_explicit_mint_and_normal_failed_grant_stays_denied(probe):
    rt, client, _, _, item, pdf = probe
    before = preserved_state(probe)
    with pytest.raises(MediaGrantError):
        rt.publisher.get("x" * 43, item["filename"])
    with pytest.raises(MediaGrantError):
        rt.publisher.publish(pdf, item["filename"], item["id"])
    assert client.get("/media/twilio/" + "x" * 43 + "/" + item["filename"]).status_code == 404
    with rt.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM pilot_media_probes").fetchone()[0] == 0
    assert preserved_state(probe) == before


def test_real_api_head_then_exactly_one_get_returns_exact_pdf_without_state_or_sends(probe):
    rt, client, _, _, item, pdf = probe
    before = preserved_state(probe)
    token, filename, url = mint(probe)
    assert urlsplit(url).netloc == urlsplit(rt.config.origin).netloc
    assert len(token) == 43 and filename == item["filename"]
    assert all(value not in url for value in (item["id"], item["recipient"], item["tenant_id"]))
    for _ in range(3):
        head = client.head(url)
        assert head.status_code == 200 and head.content == b""
        assert int(head.headers["content-length"]) == len(pdf)
        assert head.headers["content-type"] == "application/pdf"
    response = client.get(url)
    assert response.status_code == 200 and response.content == pdf
    assert response.headers["content-disposition"] == f'inline; filename="{filename}"'
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    for method in (client.get, client.head):
        denied = method(url)
        assert denied.status_code == 404
        assert token not in denied.text and item["id"] not in denied.text
    assert preserved_state(probe) == before


def test_probe_is_separate_from_normal_grants_and_only_token_hash_is_saved(probe):
    rt, _, _, _, _, _ = probe
    token, _, _ = mint(probe)
    with rt.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM wa_twilio_media_grants").fetchone()[0] == 0
        row = db.execute("SELECT token_hash,gets,revoked_at FROM pilot_media_probes").fetchone()
        dump = "\n".join(db.iterdump())
    assert row == (hashlib.sha256(token.encode()).hexdigest(), 0, None)
    assert token not in dump
    assert hashlib.sha256(token.encode()).hexdigest() in dump


def test_ten_minute_ttl_is_not_extended_by_head(probe):
    rt, client, _, clock, _, _ = probe
    _, _, url = mint(probe)
    clock[0] += 599
    assert client.head(url).status_code == 200
    clock[0] += 1
    assert client.get(url).status_code == 404


def test_explicit_revoke_is_idempotent_and_preserves_failed_outbox_budget(probe):
    rt, client, _, clock, _, _ = probe
    before = preserved_state(probe)
    token, _, url = mint(probe)
    assert rt.publisher.revoke_diagnostic_probe(token) is True
    assert rt.publisher.revoke_diagnostic_probe(token) is False
    assert client.get(url).status_code == 404
    assert client.head(url).status_code == 404
    with rt.store._connect() as db:
        assert db.execute("SELECT gets,revoked_at FROM pilot_media_probes").fetchone() == (0, clock[0])
    assert preserved_state(probe) == before


@pytest.mark.parametrize("state", ["fresh", "consumed", "revoked", "expired"])
def test_only_one_probe_can_ever_be_issued_for_original_outbox(probe, state):
    rt, _, _, clock, item, _ = probe
    token, filename, _ = mint(probe)
    if state == "consumed":
        rt.publisher.get(token, filename)
    elif state == "revoked":
        rt.publisher.revoke_diagnostic_probe(token)
    elif state == "expired":
        clock[0] += 600
    before = preserved_state(probe)
    with pytest.raises(MediaGrantError):
        rt.publisher.enable_diagnostic_probe(item["id"])
    with rt.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM pilot_media_probes").fetchone()[0] == 1
    assert preserved_state(probe) == before


@pytest.mark.parametrize("bound", ["approval", "customer_window"])
def test_probe_expiry_never_extends_approval_or_customer_service_window(probe, bound):
    rt, _, _, clock, _, pdf = probe
    if bound == "approval":
        clock[0] = rt.config.approved_until - 50
    else:
        with rt.store._connect() as db:
            db.execute("UPDATE wa_senders SET last_inbound=?", (clock[0] - 86350,))
    token, filename, _ = mint(probe)
    with rt.store._connect() as db:
        assert db.execute("SELECT expires_at FROM pilot_media_probes").fetchone()[0] == clock[0] + 50
    clock[0] += 49
    assert rt.publisher.get(token, filename, head=True) == pdf
    clock[0] += 1
    with pytest.raises(MediaGrantError):
        rt.publisher.get(token, filename)


def test_invalid_or_wrong_filename_is_generic_and_does_not_spend_the_get(probe):
    rt, _, _, _, _, pdf = probe
    token, filename, _ = mint(probe)
    for candidate_token, candidate_name in ((token, "other.pdf"), ("x" * 43, filename),
        ("../secret", filename), (None, filename), (token, "../secret.pdf"), (token, "secret.txt")):
        with pytest.raises(MediaGrantError, match="^PDF media grant is unavailable.$"):
            rt.publisher.get(candidate_token, candidate_name)
    assert rt.publisher.get(token, filename) == pdf


@pytest.mark.parametrize("status", ["pending", "retry", "outside_window", "cancelled", "sending", "accepted", "sent", "delivered", "read", "reconciliation_required", "delivery_failed"])
def test_only_failed_document_outbox_can_mint_probe(probe, status):
    rt, _, _, _, item, _ = probe
    with rt.store._connect() as db:
        db.execute("UPDATE wa_outbox SET status=? WHERE id=?", (status, item["id"]))
    before = preserved_state(probe)
    with pytest.raises(MediaGrantError):
        rt.publisher.enable_diagnostic_probe(item["id"])
    assert preserved_state(probe) == before


@pytest.mark.parametrize("mutation", [
    "UPDATE wa_outbox SET attempts=0",
    "UPDATE wa_outbox SET attempts=2",
    "UPDATE wa_outbox SET attempts=4",
    "UPDATE wa_outbox SET provider_message_id='SMffffffffffffffffffffffffffffffff'",
    "DELETE FROM pilot_send_attempts WHERE attempt=3",
    "INSERT INTO pilot_send_attempts(outbox_id,attempt) VALUES('another-outbox',1)",
    "UPDATE pilot_send_attempts SET outbox_id='another-outbox' WHERE attempt=3",
])
@pytest.mark.parametrize("phase", ["mint", "read"])
def test_exhausted_exact_original_send_budget_and_no_receipt_are_required(probe, mutation, phase):
    rt, _, _, _, item, _ = probe
    if phase == "read":
        token, filename, _ = mint(probe)
    with rt.store._connect() as db:
        db.execute(mutation)
    before = preserved_state(probe)
    with pytest.raises(MediaGrantError):
        if phase == "mint":
            rt.publisher.enable_diagnostic_probe(item["id"])
        else:
            rt.publisher.get(token, filename)
    assert preserved_state(probe) == before


@pytest.mark.parametrize("mutation", [
    "UPDATE wa_senders SET enabled=0",
    "UPDATE wa_tenants SET enabled=0",
    "UPDATE wa_tenants SET hotel_code='WRONG'",
    "UPDATE wa_tenants SET report_profile='hotel_daily_table_v1'",
    "UPDATE wa_jobs SET tenant_id='different'",
    "UPDATE wa_jobs SET sender='15555550999'",
    "UPDATE wa_jobs SET phone_number_id='99999999999'",
    "UPDATE wa_jobs SET status='failed'",
    "UPDATE wa_outbox SET tenant_id='different'",
    "UPDATE wa_outbox SET recipient='15555550999'",
    "UPDATE wa_outbox SET kind='text'",
    "UPDATE wa_outbox SET status='retry'",
    "UPDATE staff_settings SET dataset_mode='operational'",
    "DELETE FROM weekly_rosters",
    "DELETE FROM wa_jobs",
])
def test_identity_scope_and_current_roster_are_revalidated_on_read(probe, mutation):
    rt, _, _, _, _, _ = probe
    token, filename, _ = mint(probe)
    with rt.store._connect() as db:
        db.execute(mutation)
    before = preserved_state(probe)
    with pytest.raises(MediaGrantError):
        rt.publisher.get(token, filename)
    assert preserved_state(probe) == before


@pytest.mark.parametrize("change", ["settings", "version", "corruption", "missing", "substitution"])
def test_settings_version_or_private_object_change_revokes_probe(probe, change):
    rt, _, _, _, item, _ = probe
    token, filename, _ = mint(probe)
    if change == "settings":
        rt.store.save_hotel_parameters(MANAGER, {"checkout_minutes": 99})
    elif change == "version":
        roster = rt.store.get_weekly_roster(MANAGER, "2026-10-23")
        rt.store.save_weekly_roster(MANAGER, roster["week_start"], roster)
    elif change == "corruption":
        (rt.files.root / item["object_key"]).write_bytes(b"%PDF-tampered")
    elif change == "missing":
        (rt.files.root / item["object_key"]).unlink()
    else:
        substitute = rt.files.put(item["tenant_id"], b"%PDF-substitute", "pdf")
        with rt.store._connect() as db:
            db.execute("UPDATE wa_outbox SET object_key=?", (substitute,))
    before = preserved_state(probe)
    with pytest.raises(MediaGrantError):
        rt.publisher.get(token, filename)
    assert preserved_state(probe) == before


def test_concurrent_reads_cannot_spend_more_than_one_get(probe):
    rt, _, _, _, _, pdf = probe
    token, filename, _ = mint(probe)
    before = preserved_state(probe)
    def get_once(_):
        try:
            return rt.publisher.get(token, filename) == pdf
        except MediaGrantError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(get_once, range(12))) == 1
    assert preserved_state(probe) == before


def test_no_public_mint_revoke_or_listing_route(probe):
    rt, client, _, _, item, _ = probe
    before = preserved_state(probe)
    for path in ("/diagnostic/media/probe", "/admin/media/probe", "/media/twilio", "/media/twilio/probe"):
        assert client.get(path).status_code == 404
        assert client.post(path, json={"outbox_id": item["id"]}).status_code == 404
    _, _, url = mint(probe)
    assert client.post(url).status_code == 405
    assert client.delete(url).status_code == 405
    assert preserved_state(probe) == before


def test_normal_capability_fetch_behavior_is_unchanged(probe):
    rt, _, _, _, item, pdf = probe
    with rt.store._connect() as db:
        db.execute("UPDATE wa_outbox SET status='sending' WHERE id=?", (item["id"],))
    before = preserved_state(probe)
    url = rt.publisher.publish(pdf, item["filename"], item["id"])
    *_, token, filename = urlsplit(url).path.split("/")
    assert rt.publisher.get(token, filename, head=True) == pdf
    for _ in range(4):
        assert rt.publisher.get(token, filename) == pdf
    with pytest.raises(MediaGrantError):
        rt.publisher.get(token, filename)
    assert preserved_state(probe) == before
