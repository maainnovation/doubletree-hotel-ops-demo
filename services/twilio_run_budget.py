"""Explicit operator approval for one fresh synthetic ACK/result pair.

No public activation route, no automatic retries and no reset of previous pilot
or text-diagnostic reservations. A reservation commits before transport and is
never reclaimed, including connection errors or an uncertain acknowledgement.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time

from services.whatsapp_provider import ProviderError

RUN_SECONDS = 15 * 60
MAX_RUN_ATTEMPTS = 2
_ACCEPTED = {"accepted", "sent", "delivered", "read"}


def _deny():
    raise ProviderError("Fresh synthetic run is not authorized for this operation.")


def _inputs(run_id, now):
    if (not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id)
            or type(now) not in {int, float} or not math.isfinite(now)):
        _deny()


def _identity(config):
    from services.twilio_pilot import _fingerprint, synthetic_forecast, synthetic_settings
    source_hash = hashlib.sha256(synthetic_forecast(config.week_start)).hexdigest()
    identity = _fingerprint({**config.public_identity(), "fixture": source_hash,
                             "settings": _fingerprint(synthetic_settings())})
    return identity, source_hash


def _pilot_context(db, store, config, identity):
    from services.twilio_pilot import TENANT, MANAGER, HOTEL_CODE, HOTEL_NAME, MAX_SEND_ATTEMPTS, _fingerprint, synthetic_settings
    prior = db.execute("SELECT identity FROM pilot_identity WHERE id=1").fetchone()
    enrolled = store._enrollment(db, config.phone_id, config.manager_phone)
    if (not prior or prior[0] != identity or not enrolled
            or db.execute("SELECT COUNT(*) FROM pilot_send_attempts").fetchone()[0] != MAX_SEND_ATTEMPTS
            or any(enrolled[key] != value for key, value in {
                "tenant_id": TENANT, "manager_id": MANAGER, "hotel_code": HOTEL_CODE,
                "hotel_name": HOTEL_NAME, "report_profile": "canonical_v1"}.items())):
        _deny()
    saved = db.execute("SELECT payload,dataset_mode,revision FROM staff_settings WHERE manager_id=?", (MANAGER,)).fetchone()
    if (not saved or saved["dataset_mode"] != "demo"
            or _fingerprint(json.loads(saved["payload"])) != _fingerprint(synthetic_settings())):
        _deny()
    return enrolled, saved["revision"]


def _schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS pilot_run_authorizations (
        run_id TEXT PRIMARY KEY, identity_hash TEXT NOT NULL,
        approved_at REAL NOT NULL, expires_at REAL NOT NULL,
        minimum_job_rowid INTEGER NOT NULL, bound_job_id TEXT UNIQUE,
        max_attempts INTEGER NOT NULL DEFAULT 2 CHECK(max_attempts=2),
        CHECK(expires_at>approved_at))""")
    db.execute("""CREATE TABLE IF NOT EXISTS pilot_run_attempts (
        run_id TEXT NOT NULL, purpose TEXT NOT NULL CHECK(purpose IN ('ack','result')),
        outbox_id TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL, reserved_at REAL NOT NULL,
        PRIMARY KEY(run_id,purpose))""")


def create_run_approval(store, config, run_id, now):
    """Operator-only activation after explicit approval; never resets prior state."""
    _inputs(run_id, now)
    if (not getattr(store, "development_only", False)
            or type(config.approved_until) not in {int, float}
            or not math.isfinite(config.approved_until)
            or not 0 < config.approved_until - now <= 86400):
        _deny()
    identity, _ = _identity(config)
    expires = min(now + RUN_SECONDS, config.approved_until)
    try:
        with store._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            _pilot_context(db, store, config, identity)
            _schema(db)
            if db.execute("SELECT 1 FROM pilot_run_authorizations WHERE expires_at>?", (now,)).fetchone():
                _deny()
            minimum = db.execute("SELECT COALESCE(MAX(rowid),0) FROM wa_jobs").fetchone()[0]
            db.execute("""INSERT INTO pilot_run_authorizations
                (run_id,identity_hash,approved_at,expires_at,minimum_job_rowid)
                VALUES(?,?,?,?,?)""", (run_id, identity, now, expires, minimum))
    except sqlite3.Error:
        _deny()
    return {"run_id": run_id, "approved_at": now, "expires_at": expires,
            "minimum_job_rowid": minimum, "max_attempts": MAX_RUN_ATTEMPTS}


class FreshRunBudget:
    def __init__(self, store, config, run_id, *, clock=time.time):
        _inputs(run_id, clock())
        self.store, self.config, self.run_id, self.clock = store, config, run_id, clock
        self.identity, self.source_hash = _identity(config)
        try:
            with store._connect() as db:
                db.row_factory = sqlite3.Row
                self._record(db, clock())
                _pilot_context(db, store, config, self.identity)
        except sqlite3.Error:
            _deny()

    def _record(self, db, now):
        _inputs(self.run_id, now)
        row = db.execute("SELECT * FROM pilot_run_authorizations WHERE run_id=?", (self.run_id,)).fetchone()
        if (not row or row["identity_hash"] != self.identity or row["max_attempts"] != MAX_RUN_ATTEMPTS
                or not row["approved_at"] <= now < row["expires_at"] <= self.config.approved_until
                or not 0 < row["expires_at"] - row["approved_at"] <= RUN_SECONDS
                or type(row["minimum_job_rowid"]) is not int or row["minimum_job_rowid"] < 0):
            _deny()
        return row

    def reserve(self, outbox_id, now, *, kind, text=None):
        """Reserve one purpose before the corresponding single transport call."""
        from services.whatsapp_workflow import UPLOAD_ACK_TEXT
        from services.twilio_pilot import TENANT
        try:
            with self.store._connect() as db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                run = self._record(db, now)
                enrolled, revision = _pilot_context(db, self.store, self.config, self.identity)
                item = db.execute("SELECT * FROM wa_outbox WHERE id=?", (outbox_id,)).fetchone()
                if (not item or item["status"] != "sending" or item["attempts"] != 1
                        or item["tenant_id"] != TENANT or item["phone_number_id"] != self.config.phone_id
                        or item["recipient"] != self.config.manager_phone or item["kind"] != kind
                        or item["provider_message_id"] is not None
                        or not 0 <= now - enrolled["last_inbound"] < 86400):
                    _deny()
                job = db.execute("SELECT rowid AS intake_rowid,* FROM wa_jobs WHERE id=?", (item["job_id"],)).fetchone()
                if (not job or job["intake_rowid"] <= run["minimum_job_rowid"]
                        or job["tenant_id"] != TENANT or job["phone_number_id"] != self.config.phone_id
                        or job["sender"] != self.config.manager_phone or job["source_hash"] != self.source_hash
                        or job["source_key"] != hashlib.sha256(TENANT.encode()).hexdigest() + "/" + self.source_hash + ".pdf"):
                    _deny()
                metadata = json.loads(job["media_json"])
                if (metadata.get("_enrollment_binding") != self.store.enrollment_binding(enrolled)
                        or metadata.get("_upload_ack_settings_revision") != revision
                        or metadata.get("_provider") != "twilio" or metadata.get("type") != "document"
                        or metadata.get("mime") != "application/pdf"):
                    _deny()
                created = job["inbound_at"]
                attestation = metadata.get("_pilot_attestation")
                if attestation is not None:
                    if (not isinstance(attestation, dict)
                            or attestation.get("kind") != "authenticated_media_after_message_404"
                            or attestation.get("window_anchor") != job["inbound_at"]):
                        _deny()
                    created = attestation.get("media_created_at")
                if type(created) not in {int, float} or not math.isfinite(created) or not run["approved_at"] <= created <= now:
                    _deny()
                if item["purpose"] == "ack":
                    if (kind != "text" or text != UPLOAD_ACK_TEXT or item["text"] != UPLOAD_ACK_TEXT
                            or item["object_key"] is not None or item["filename"] is not None
                            or job["status"] != "awaiting_ack" or job["version"] is not None
                            or run["bound_job_id"] is not None):
                        _deny()
                    db.execute("UPDATE pilot_run_authorizations SET bound_job_id=? WHERE run_id=? AND bound_job_id IS NULL",
                               (job["id"], self.run_id))
                elif item["purpose"] == "result":
                    ack = db.execute("""SELECT o.* FROM pilot_run_attempts a JOIN wa_outbox o ON o.id=a.outbox_id
                        WHERE a.run_id=? AND a.purpose='ack' AND a.job_id=? AND o.job_id=? AND o.purpose='ack'""",
                        (self.run_id, job["id"], job["id"])).fetchone()
                    if (run["bound_job_id"] != job["id"] or not ack or ack["status"] not in _ACCEPTED
                            or ack["tenant_id"] != TENANT or ack["phone_number_id"] != self.config.phone_id
                            or ack["recipient"] != self.config.manager_phone or ack["kind"] != "text"
                            or ack["text"] != UPLOAD_ACK_TEXT
                            or not isinstance(ack["provider_message_id"], str)
                            or not re.fullmatch(r"(?:SM|MM)[0-9a-fA-F]{32}", ack["provider_message_id"])
                            or job["status"] not in {"calculated", "needs_attention", "needs_correction"}
                            or kind not in {"text", "document"}
                            or (kind == "text" and (text != item["text"] or text == UPLOAD_ACK_TEXT))):
                        _deny()
                else:
                    _deny()
                if db.execute("SELECT COUNT(*) FROM pilot_run_attempts WHERE run_id=?", (self.run_id,)).fetchone()[0] >= MAX_RUN_ATTEMPTS:
                    _deny()
                db.execute("INSERT INTO pilot_run_attempts(run_id,purpose,outbox_id,job_id,reserved_at) VALUES(?,?,?,?,?)",
                           (self.run_id, item["purpose"], outbox_id, job["id"], now))
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            _deny()

    def allows_automatic_retry(self, outbox_id):
        try:
            with self.store._connect() as db:
                return db.execute("SELECT 1 FROM pilot_run_attempts WHERE outbox_id=?", (outbox_id,)).fetchone() is None
        except sqlite3.Error:
            return False
