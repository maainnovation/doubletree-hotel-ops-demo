"""Fresh-run authorization is local/operator-only; every transport is mocked."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

import pytest

from services.twilio_pilot import build_runtime
from services.twilio_run_budget import FreshRunBudget, create_run_approval, RUN_SECONDS
from services.whatsapp_provider import ProviderError, RetryableProviderError
from services.whatsapp_workflow import ForecastWorker, UPLOAD_ACK_TEXT
from test_twilio_live_pilot import pilot, signed, form


@pytest.fixture
def fresh(pilot):
    rt, client, state, clock, mock = pilot
    with rt.store._connect() as db:
        db.executemany("INSERT INTO pilot_send_attempts(outbox_id,attempt) VALUES('original',?)", [(1,), (2,), (3,)])
        db.execute("CREATE TABLE pilot_text_diagnostics(audit_id TEXT PRIMARY KEY,status TEXT)")
        db.execute("INSERT INTO pilot_text_diagnostics VALUES('previous-text','confirmed_rejected_no_retry')")
    approval = create_run_approval(rt.store, rt.config, "fresh-one", clock[0])
    rt.provider.fresh_run_budget = FreshRunBudget(rt.store, rt.config, "fresh-one", clock=lambda: clock[0])
    worker = ForecastWorker(rt.store, rt.files, rt.provider, clock=lambda: clock[0], acknowledge_uploads=True)
    return rt, client, state, clock, mock, approval, worker


def attempts(rt):
    with rt.store._connect() as db:
        return db.execute("SELECT purpose,outbox_id,job_id FROM pilot_run_attempts ORDER BY purpose").fetchall()


def ack_staged(fresh):
    rt, client, _, _, _, _, worker = fresh
    assert signed(client).status_code == 200
    assert worker.process_one()
    return next(row for row in rt.store.rows("wa_outbox") if row["purpose"] == "ack")


def test_fresh_ack_then_pdf_uses_two_new_reservations_without_resetting_old_audits(fresh):
    rt, _, state, _, _, approval, worker = fresh
    ack_staged(fresh)
    assert worker.dispatch_one() and state["sends"][0]["Body"] == UPLOAD_ACK_TEXT
    assert not rt.store.list_weekly_roster_versions("isolated-synthetic-manager")
    assert worker.process_one() and worker.dispatch_one()
    assert len(state["sends"]) == 2 and "MediaUrl" in state["sends"][1]
    assert [row[0] for row in attempts(rt)] == ["ack", "result"]
    assert len(state["pdfs"]) == 1
    with rt.store._connect() as db:
        assert db.execute("SELECT outbox_id,attempt FROM pilot_send_attempts ORDER BY attempt").fetchall() == [("original", 1), ("original", 2), ("original", 3)]
        assert db.execute("SELECT * FROM pilot_text_diagnostics").fetchall() == [("previous-text", "confirmed_rejected_no_retry")]
        assert db.execute("SELECT bound_job_id FROM pilot_run_authorizations").fetchone()[0] == rt.store.rows("wa_jobs")[0]["id"]
    assert approval["max_attempts"] == 2


def test_approval_is_operator_only_unique_bounded_and_does_not_change_pilot_identity(fresh, monkeypatch):
    rt, _, state, clock, mock, approval, _ = fresh
    assert approval["expires_at"] == min(clock[0] + RUN_SECONDS, rt.config.approved_until)
    with pytest.raises(ProviderError):
        create_run_approval(rt.store, rt.config, "another-active", clock[0])
    monkeypatch.setenv("PILOT_FRESH_RUN_ID", "fresh-one")
    rebuilt = build_runtime(rt.config, transport=mock, clock=lambda: clock[0])
    assert rebuilt.provider.fresh_run_budget.run_id == "fresh-one" and not state["sends"]
    assert rebuilt.config.public_identity() == rt.config.public_identity()
    monkeypatch.setenv("PILOT_FRESH_RUN_ID", "unapproved")
    with pytest.raises(ProviderError):
        build_runtime(rt.config, transport=mock, clock=lambda: clock[0])


def test_unselected_base_runtime_does_not_bypass_exhausted_original_budget(fresh, monkeypatch):
    rt, _, state, clock, mock, _, _ = fresh
    monkeypatch.delenv("PILOT_FRESH_RUN_ID", raising=False)
    rebuilt = build_runtime(rt.config, transport=mock, clock=lambda: clock[0])
    assert rebuilt.provider.fresh_run_budget is None
    ack = ack_staged(fresh)
    with rt.store._connect() as db:
        db.execute("UPDATE wa_outbox SET status='sending',attempts=1 WHERE id=?", (ack["id"],))
    with pytest.raises(ProviderError):
        rebuilt.provider.send_text(rt.config.manager_phone, UPLOAD_ACK_TEXT, ack["id"])
    assert not state["sends"] and not attempts(rt)


@pytest.mark.parametrize("mutation", [
    "UPDATE wa_jobs SET source_hash='wrong'",
    "UPDATE wa_jobs SET source_key='wrong'",
    "UPDATE wa_jobs SET sender='15555550999'",
    "UPDATE wa_jobs SET inbound_at=inbound_at-1",
    "UPDATE wa_jobs SET status='calculated'",
    "UPDATE wa_outbox SET text='Not the approved acknowledgement'",
    "UPDATE wa_outbox SET purpose='result'",
    "UPDATE wa_outbox SET attempts=2",
    "UPDATE wa_tenants SET enabled=0",
    "UPDATE pilot_run_authorizations SET minimum_job_rowid=999",
    "UPDATE pilot_run_authorizations SET identity_hash='changed'",
    "DELETE FROM pilot_send_attempts WHERE attempt=3",
])
def test_old_unvalidated_mismatched_or_retried_ack_cannot_consume_fresh_budget(fresh, mutation):
    rt, _, state, clock, _, _, _ = fresh
    ack = ack_staged(fresh)
    with rt.store._connect() as db:
        db.execute("UPDATE wa_outbox SET status='sending',attempts=1 WHERE id=?", (ack["id"],))
        db.execute(mutation)
    with pytest.raises(ProviderError):
        rt.provider.send_text(rt.config.manager_phone, UPLOAD_ACK_TEXT, ack["id"])
    assert not state["sends"] and not attempts(rt)


def test_missing_approval_expired_run_changed_identity_and_reused_id_fail_closed(fresh):
    rt, _, _, clock, _, _, _ = fresh
    with pytest.raises(ProviderError):
        FreshRunBudget(rt.store, rt.config, "missing", clock=lambda: clock[0])
    with pytest.raises(ProviderError):
        FreshRunBudget(rt.store, replace(rt.config, origin="https://other.onrender.com"), "fresh-one", clock=lambda: clock[0])
    clock[0] += RUN_SECONDS
    with pytest.raises(ProviderError):
        FreshRunBudget(rt.store, rt.config, "fresh-one", clock=lambda: clock[0])
    with pytest.raises(ProviderError):
        create_run_approval(rt.store, rt.config, "fresh-one", clock[0])


def test_concurrent_ack_reservations_allow_only_one_transport_attempt(fresh):
    rt, _, state, clock, _, _, _ = fresh
    ack = ack_staged(fresh)
    with rt.store._connect() as db:
        db.execute("UPDATE wa_outbox SET status='sending',attempts=1 WHERE id=?", (ack["id"],))
    def reserve(_):
        try:
            rt.provider.fresh_run_budget.reserve(ack["id"], clock[0], kind="text", text=UPLOAD_ACK_TEXT)
            return True
        except ProviderError:
            return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(reserve, range(10))) == 1
    assert len(attempts(rt)) == 1 and not state["sends"]
    assert not rt.provider.allows_automatic_retry(ack["id"])


def test_retryable_transport_failure_is_reserved_and_never_repeated(fresh, monkeypatch):
    rt, _, state, clock, _, _, worker = fresh
    ack = ack_staged(fresh)
    calls = []
    def unavailable(*args):
        calls.append(args)
        raise RetryableProviderError("Synthetic connection failure")
    monkeypatch.setattr(rt.provider.transport_provider, "send_text", unavailable)
    assert worker.dispatch_one()
    assert len(calls) == len(attempts(rt)) == 1
    assert not rt.provider.allows_automatic_retry(ack["id"])
    clock[0] += 120
    worker.dispatch_one()
    assert len(calls) == 1 and not state["sends"]


def test_fallback_fresh_media_timestamp_not_conservative_window_anchor_controls_admission(fresh):
    rt, _, state, clock, _, _, worker = fresh
    ack = ack_staged(fresh)
    with rt.store._connect() as db:
        row = db.execute("SELECT media_json FROM wa_jobs WHERE id=?", (ack["job_id"],)).fetchone()
        metadata = json.loads(row[0])
        anchor = rt.config.approved_until - 86400 - 300
        metadata["_pilot_attestation"] = {"kind": "authenticated_media_after_message_404",
            "window_anchor": anchor, "media_created_at": clock[0], "first_received_at": clock[0]}
        db.execute("UPDATE wa_jobs SET inbound_at=?,media_json=? WHERE id=?", (anchor, json.dumps(metadata), ack["job_id"]))
    assert worker.dispatch_one()
    assert len(state["sends"]) == len(attempts(rt)) == 1


def test_one_fresh_run_cannot_bind_a_second_validated_job(fresh):
    rt, client, state, clock, _, _, worker = fresh
    ack_staged(fresh)
    assert worker.dispatch_one()
    # Explicitly stage a second otherwise valid record to exercise the budget,
    # independently of the workflow's normal content-idempotency suppression.
    original = rt.store.rows("wa_jobs")[0]
    assert signed(client, form("SM" + "e" * 32)).status_code == 200
    second = [job for job in rt.store.rows("wa_jobs") if job["id"] != original["id"]][0]
    with rt.store._connect() as db:
        db.execute("UPDATE wa_jobs SET status='awaiting_ack',source_hash=?,source_key=?,media_json=? WHERE id=?",
                   (original["source_hash"], original["source_key"], original["media_json"], second["id"]))
        key = rt.store.add_outbox(db, second, clock[0], UPLOAD_ACK_TEXT, purpose="ack")
        db.execute("UPDATE wa_outbox SET status='sending',attempts=1 WHERE id=?", (key,))
    with pytest.raises(ProviderError):
        rt.provider.send_text(rt.config.manager_phone, UPLOAD_ACK_TEXT, key)
    assert len(state["sends"]) == len(attempts(rt)) == 1


@pytest.mark.parametrize("status", ["pending", "sending", "failed", "delivery_failed", "reconciliation_required"])
def test_result_requires_a_successfully_accepted_ack(fresh, status):
    rt, _, state, clock, _, _, worker = fresh
    ack = ack_staged(fresh)
    assert worker.dispatch_one() and worker.process_one()
    result = next(row for row in rt.store.rows("wa_outbox") if row["purpose"] == "result")
    with rt.store._connect() as db:
        db.execute("UPDATE wa_outbox SET status=? WHERE id=?", (status, ack["id"]))
        db.execute("UPDATE wa_outbox SET status='sending',attempts=1 WHERE id=?", (result["id"],))
    with pytest.raises(ProviderError):
        rt.provider.fresh_run_budget.reserve(result["id"], clock[0], kind="document")
    assert len(state["sends"]) == len(attempts(rt)) == 1


def test_result_reservation_cannot_be_repeated_even_if_outbox_state_is_restored(fresh):
    rt, _, state, clock, _, _, worker = fresh
    ack_staged(fresh)
    assert worker.dispatch_one() and worker.process_one() and worker.dispatch_one()
    result = next(row for row in rt.store.rows("wa_outbox") if row["purpose"] == "result")
    with rt.store._connect() as db:
        db.execute("UPDATE wa_outbox SET status='sending',attempts=1,provider_message_id=NULL WHERE id=?", (result["id"],))
    with pytest.raises(ProviderError):
        rt.provider.fresh_run_budget.reserve(result["id"], clock[0], kind="document")
    assert len(state["sends"]) == len(attempts(rt)) == 2
