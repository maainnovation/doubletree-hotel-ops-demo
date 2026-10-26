"""Bounded private-app upload queue over the exact shared hotel repository.

No HTTP extraction/scheduling and no WhatsApp/provider/outbox access. Sources
remain private, candidates need explicit review, and only the separate worker
calculates. This is the synthetic, single-host pilot adapter, not production
storage or a general document-processing service. Policy defaults to deny.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid

from services.hotel_parameters import saved_room_capacity
from services.private_storage_budget import PrivateStorageQuotaError
from services.weekly_roster import generate_weekly_roster
from services.whatsapp_forecast import ForecastNeedsCorrection, validate_confirmed_forecast_rows

MIMES = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg"}
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SOURCE_SECONDS = 24 * 3600
REVIEW_SECONDS = 1800
ACTIVE = ("queued", "extracting", "needs_review", "confirmed", "calculating")
MESSAGES = {
    "queued": "Upload queued. A separate worker will extract the report; no roster has changed.",
    "extracting": "Extracting within isolated PDF/photo safety limits.",
    "needs_review": "Compare every date and count below with your original report before confirming. No roster has changed.",
    "confirmed": "Review confirmed; calculation is queued. No WhatsApp message will be sent.",
    "calculating": "Calculating with the reviewed saved settings and existing locks.",
    "calculated": "A new roster version is saved. Review it before operational use.",
    "needs_attention": "A new version is saved, but shortages or constraints remain. It is not a confirmed workable schedule.",
    "needs_correction": "A complete, current, internally consistent forecast could not be extracted. Resend a clearer full report or original PDF; missing values are never guessed.",
    "stale": "Settings, hotel mapping, or the saved roster changed. Request a fresh review before calculating.",
    "cancelled": "Cancelled. This upload will not generate a roster.",
    "expired": "The review or source expired. Upload the complete current report again.",
    "failed": "Processing could not complete safely. No automatic retry was made; upload again or request a fresh review.",
    "blocked": "Owner approval or the synthetic hotel setup is no longer active.",
    "storage_blocked": "Private storage quota or safety limit reached. Contact the operator; do not repeatedly resend the file.",
}


class PrivateForecastError(ValueError):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value).hexdigest()


class PrivateForecastWorkflow:
    def __init__(self, store, files, clock=time.time, admission_check=None, media_evaluator=None,
                 *, max_uploads=1000, uploads_per_hour=20):
        if type(max_uploads) is not int or not 1 <= max_uploads <= 1000 or type(uploads_per_hour) is not int or not 1 <= uploads_per_hour <= 100:
            raise ValueError("Bounded private upload limits are required")
        self.store, self.files, self.clock = store, files, clock
        self.admission_check = admission_check or (lambda tenant_id, manager_id, now: False)
        self.media_evaluator = media_evaluator
        self.max_uploads, self.uploads_per_hour = max_uploads, uploads_per_hour
        with store._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS private_forecast_uploads (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, manager_id TEXT NOT NULL,
                request_key TEXT NOT NULL, source_hash TEXT NOT NULL, source_key TEXT NOT NULL,
                mime TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL, expires_at REAL NOT NULL, review_expires_at REAL,
                binding_json TEXT NOT NULL, settings_revision INTEGER, settings_hash TEXT,
                candidate_json TEXT, candidate_hash TEXT, method TEXT, prior_version INTEGER,
                week_start TEXT, result_version INTEGER, claim_token TEXT, lease_until REAL,
                UNIQUE(tenant_id,manager_id,request_key))""")

    def _now(self):
        now = self.clock()
        if type(now) not in {int, float} or not math.isfinite(now):
            raise PrivateForecastError(503, "A reliable reference clock is unavailable")
        return now

    def _context(self, db, tenant_id, manager_id, now):
        try:
            allowed = self.admission_check(tenant_id, manager_id, now) is True
        except Exception:
            allowed = False
        if not allowed:
            raise PrivateForecastError(403, "Owner upload approval is inactive or expired")
        row = db.execute("SELECT tenant_id,manager_id,hotel_code,hotel_name,report_profile FROM wa_tenants WHERE tenant_id=? AND manager_id=? AND enabled=1", (tenant_id, manager_id)).fetchone()
        saved = db.execute("SELECT payload,revision,dataset_mode FROM staff_settings WHERE manager_id=?", (manager_id,)).fetchone()
        if not row or not saved or saved["dataset_mode"] != "demo":
            raise PrivateForecastError(403, "An explicitly enrolled synthetic hotel setup is required")
        binding = dict(row)
        staff = json.loads(saved["payload"])
        staff["dataset_mode"] = saved["dataset_mode"]
        try:
            capacity = saved_room_capacity(staff, required=True)
        except ValueError:
            raise PrivateForecastError(422, "Save Total rooms in the shared Parameters before uploading") from None
        return binding, staff, saved["revision"], capacity

    @staticmethod
    def _row(db, tenant_id, manager_id, upload_id):
        row = db.execute("SELECT * FROM private_forecast_uploads WHERE id=? AND tenant_id=? AND manager_id=?", (upload_id, tenant_id, manager_id)).fetchone()
        if not row:
            raise PrivateForecastError(404, "Upload not found")
        return dict(row)

    @staticmethod
    def _view(row):
        result = {key: row[key] for key in ("id", "status", "revision", "created_at", "expires_at", "review_expires_at", "settings_revision", "week_start", "result_version", "method")}
        result["message"] = MESSAGES[row["status"]]
        if row["status"] == "needs_review" and row["method"] != "native_pdf":
            result["message"] += " Photo/scanned-PDF OCR can misread digits even when checks agree. Cancel and resend if any value differs from the original."
        result["rows"] = json.loads(row["candidate_json"]) if row["status"] == "needs_review" and row["candidate_json"] else []
        return result

    @staticmethod
    def _expire(db, now):
        db.execute("UPDATE private_forecast_uploads SET status='expired',revision=revision+1,claim_token=NULL,lease_until=NULL WHERE status IN ('queued','extracting','needs_review','confirmed','calculating') AND (expires_at<=? OR (status IN ('needs_review','confirmed','calculating') AND review_expires_at<=?))", (now, now))
        # Interrupted jobs are deliberately not automatically retried.
        db.execute("UPDATE private_forecast_uploads SET status='failed',revision=revision+1,claim_token=NULL,lease_until=NULL WHERE status IN ('extracting','calculating') AND lease_until<=?", (now,))

    def available(self, tenant_id, manager_id):
        try:
            with self.store._connect() as db:
                db.row_factory = sqlite3.Row
                self._context(db, tenant_id, manager_id, self._now())
            return True
        except PrivateForecastError:
            return False

    def list_uploads(self, tenant_id, manager_id):
        now = self._now()
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            self._context(db, tenant_id, manager_id, now)
            self._expire(db, now)
            rows = db.execute("SELECT * FROM private_forecast_uploads WHERE tenant_id=? AND manager_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100", (tenant_id, manager_id)).fetchall()
            return [self._view(dict(row)) for row in rows]

    def get(self, tenant_id, manager_id, upload_id):
        now = self._now()
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            self._context(db, tenant_id, manager_id, now)
            self._expire(db, now)
            return self._view(self._row(db, tenant_id, manager_id, upload_id))

    def _insert(self, db, tenant_id, manager_id, data, mime, request_key, now, *, reference=None):
        binding, _, _, _ = self._context(db, tenant_id, manager_id, now)
        if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_key):
            raise PrivateForecastError(422, "A bounded upload idempotency key is required")
        if reference is None:
            if mime not in MIMES or not isinstance(data, bytes) or not data or len(data) > (MAX_PDF_BYTES if mime == "application/pdf" else MAX_IMAGE_BYTES):
                raise PrivateForecastError(422, "Use a nonempty PDF up to 10 MB or PNG/JPEG up to 5 MB")
            digest = _hash(data)
        else:
            # Internal same-tenant DB reference only; regeneration enqueues
            # without reading/copying a potentially 10 MiB source in HTTP.
            digest = reference["source_hash"]
        previous = db.execute("SELECT * FROM private_forecast_uploads WHERE tenant_id=? AND manager_id=? AND request_key=?", (tenant_id, manager_id, request_key)).fetchone()
        if previous:
            if previous["source_hash"] != digest or previous["mime"] != mime:
                raise PrivateForecastError(409, "That upload key already belongs to another file")
            return self._view(dict(previous))
        total = db.execute("SELECT COUNT(*) FROM private_forecast_uploads WHERE tenant_id=? AND manager_id=?", (tenant_id, manager_id)).fetchone()[0]
        recent = db.execute("SELECT COUNT(*) FROM private_forecast_uploads WHERE tenant_id=? AND manager_id=? AND created_at>?", (tenant_id, manager_id, now - 3600)).fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM private_forecast_uploads WHERE tenant_id=? AND manager_id=? AND status IN ('queued','extracting','needs_review','confirmed','calculating')", (tenant_id, manager_id)).fetchone()[0]
        if total >= self.max_uploads or recent >= self.uploads_per_hour or pending >= 8:
            raise PrivateForecastError(429, "Private upload limit reached; wait or cancel pending reviews")
        key = self.files.put(tenant_id, data, MIMES[mime]) if reference is None else reference["source_key"]
        upload_id = uuid.uuid4().hex
        db.execute("INSERT INTO private_forecast_uploads(id,tenant_id,manager_id,request_key,source_hash,source_key,mime,status,created_at,expires_at,binding_json) VALUES(?,?,?,?,?,?,?,'queued',?,?,?)", (upload_id, tenant_id, manager_id, request_key, digest, key, mime, now, now + SOURCE_SECONDS, _json(binding)))
        return self._view(self._row(db, tenant_id, manager_id, upload_id))

    def enqueue(self, tenant_id, manager_id, data, mime, request_key):
        now = self._now()
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, now)
            return self._insert(db, tenant_id, manager_id, data, mime, request_key, now)

    def action(self, tenant_id, manager_id, upload_id, action, expected_revision, request_key=None):
        now = self._now()
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            binding, staff, revision, capacity = self._context(db, tenant_id, manager_id, now)
            self._expire(db, now)
            row = self._row(db, tenant_id, manager_id, upload_id)
            if type(expected_revision) is not int or row["revision"] != expected_revision:
                raise PrivateForecastError(409, "Upload changed; refresh before acting")
            if action == "regenerate":
                if row["expires_at"] <= now:
                    raise PrivateForecastError(410, "Saved source expired; upload the original again")
                return self._insert(db, tenant_id, manager_id, None, row["mime"], request_key, now, reference=row)
            if action == "cancel":
                if row["status"] == "cancelled":
                    return self._view(row)
                if row["status"] not in ACTIVE:
                    raise PrivateForecastError(409, "This upload is already resolved")
                db.execute("UPDATE private_forecast_uploads SET status='cancelled',revision=revision+1,claim_token=NULL,lease_until=NULL WHERE id=?", (upload_id,))
            elif action == "confirm":
                if row["status"] != "needs_review":
                    raise PrivateForecastError(409, "Only a complete, unexpired review can be confirmed")
                self._check_snapshot(db, row, binding, staff, revision)
                validate_confirmed_forecast_rows(json.loads(row["candidate_json"]), capacity, datetime.fromtimestamp(now, timezone.utc))
                db.execute("UPDATE private_forecast_uploads SET status='confirmed',revision=revision+1 WHERE id=?", (upload_id,))
            else:
                raise PrivateForecastError(422, "Unsupported upload action")
            return self._view(self._row(db, tenant_id, manager_id, upload_id))

    @staticmethod
    def _check_snapshot(db, row, binding, staff, revision):
        prior = db.execute("SELECT version FROM weekly_rosters WHERE manager_id=? AND week_start=?", (row["manager_id"], row["week_start"])).fetchone()
        if (row["binding_json"] != _json(binding) or row["settings_revision"] != revision
                or row["settings_hash"] != _hash(_json(staff).encode())
                or row["candidate_hash"] != _hash(row["candidate_json"].encode())
                or row["prior_version"] != (prior[0] if prior else 0)):
            raise PrivateForecastError(409, "Settings, source review, or roster changed; request a fresh review")

    def process_one(self):
        now = self._now()
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            self._expire(db, now)
            row = db.execute("SELECT * FROM private_forecast_uploads WHERE status IN ('queued','confirmed') ORDER BY created_at,rowid LIMIT 1").fetchone()
            if not row:
                return False
            job = dict(row)
            token = uuid.uuid4().hex
            stage = "extracting" if job["status"] == "queued" else "calculating"
            db.execute("UPDATE private_forecast_uploads SET status=?,claim_token=?,lease_until=?,revision=revision+1 WHERE id=?", (stage, token, now + 120, job["id"]))
        try:
            with self.store._connect() as db:
                db.row_factory = sqlite3.Row
                binding, staff, revision, capacity = self._context(db, job["tenant_id"], job["manager_id"], now)
            if _json(binding) != job["binding_json"]:
                raise PrivateForecastError(409, "Hotel mapping changed")
            data = self.files.read(job["tenant_id"], job["source_key"])
            if _hash(data) != job["source_hash"]:
                raise PrivateForecastError(409, "Source integrity check failed")
            if stage == "extracting":
                evaluator = self.media_evaluator
                if evaluator is None:
                    from services.forecast_media_worker import evaluate_forecast_media_isolated
                    evaluator = evaluate_forecast_media_isolated
                assessment = evaluator(data, job["mime"], binding["hotel_code"], capacity, datetime.fromtimestamp(now, timezone.utc), report_profile=binding["report_profile"], hotel_name=binding["hotel_name"])
                valid = isinstance(assessment, dict) and (assessment.get("candidate_valid_for_confirmation") is True or (assessment.get("method") == "native_pdf" and assessment.get("automatic_roster_allowed") is True and assessment.get("requires_review") is False))
                if not valid:
                    raise ForecastNeedsCorrection("No complete candidate")
                rows = validate_confirmed_forecast_rows(assessment.get("rows"), capacity, datetime.fromtimestamp(now, timezone.utc))
                candidate = _json(rows)
            with self.store._connect() as db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                current = self._row(db, job["tenant_id"], job["manager_id"], job["id"])
                fresh = self._now()
                if current["claim_token"] != token or current["status"] != stage:
                    return True
                binding2, staff2, revision2, capacity2 = self._context(db, job["tenant_id"], job["manager_id"], fresh)
                if current["lease_until"] is None or current["lease_until"] <= fresh:
                    raise PrivateForecastError(503, "Processing lease expired")
                if current["expires_at"] <= fresh or (stage == "calculating" and current["review_expires_at"] <= fresh):
                    raise PrivateForecastError(410, "Expired")
                if revision2 != revision or _json(binding2) != job["binding_json"] or _json(staff2) != _json(staff):
                    raise PrivateForecastError(409, "Settings changed")
                if stage == "extracting":
                    rows = validate_confirmed_forecast_rows(rows, capacity2, datetime.fromtimestamp(fresh, timezone.utc))
                    prior = db.execute("SELECT version FROM weekly_rosters WHERE manager_id=? AND week_start=?", (job["manager_id"], rows[0]["date"])).fetchone()
                    method = assessment.get("method") if assessment.get("method") in {"native_pdf", "local_scanned_pdf_ocr", "local_image_ocr"} else "review"
                    db.execute("UPDATE private_forecast_uploads SET status='needs_review',revision=revision+1,settings_revision=?,settings_hash=?,candidate_json=?,candidate_hash=?,method=?,week_start=?,prior_version=?,review_expires_at=?,claim_token=NULL,lease_until=NULL WHERE id=?", (revision, _hash(_json(staff).encode()), candidate, _hash(candidate.encode()), method, rows[0]["date"], prior[0] if prior else 0, min(current["expires_at"], fresh + REVIEW_SECONDS), job["id"]))
                else:
                    self._check_snapshot(db, current, binding2, staff2, revision2)
                    rows = validate_confirmed_forecast_rows(json.loads(current["candidate_json"]), capacity2, datetime.fromtimestamp(fresh, timezone.utc))
                    prior_row = db.execute("SELECT payload FROM weekly_rosters WHERE manager_id=? AND week_start=?", (job["manager_id"], rows[0]["date"])).fetchone()
                    prior = json.loads(prior_row[0]) if prior_row else None
                    if prior:
                        for assignment in prior.get("schedule", []):
                            if assignment.get("manual") or assignment.get("assignment_source") == "manual":
                                assignment["locked"] = True
                    roster = generate_weekly_roster(rows, staff2, prior=prior)
                    roster["source"] = {"channel": "private_app", "hotel_name": binding2["hotel_name"], "hotel_code": binding2["hotel_code"], "report_profile": binding2["report_profile"], "settings_revision": revision2, "dataset_mode": "demo", "source_sha256": job["source_hash"], "created_at": datetime.fromtimestamp(fresh, timezone.utc).isoformat()}
                    status = "needs_attention" if roster.get("constraints") or not roster["feasible"] else "calculated"
                    roster["workflow_status"] = status
                    # Scheduler time cannot extend the approved access/review
                    # window. Recheck immediately before the atomic commit.
                    finished = self._now()
                    self._context(db, job["tenant_id"], job["manager_id"], finished)
                    if finished >= min(current["expires_at"], current["review_expires_at"], current["lease_until"]):
                        raise PrivateForecastError(410, "Calculation expired before commit")
                    roster = self.store._save_roster_in_transaction(db, job["manager_id"], rows[0]["date"], roster)
                    db.execute("UPDATE private_forecast_uploads SET status=?,revision=revision+1,result_version=?,claim_token=NULL,lease_until=NULL WHERE id=?", (status, roster["version"], job["id"]))
        except PrivateStorageQuotaError:
            self._finish(job["id"], token, "storage_blocked")
        except ForecastNeedsCorrection:
            self._finish(job["id"], token, "needs_correction")
        except PrivateForecastError as exc:
            self._finish(job["id"], token, {403: "blocked", 409: "stale", 410: "expired"}.get(exc.status_code, "failed"))
        except Exception:
            self._finish(job["id"], token, "failed")
        return True

    def _finish(self, upload_id, token, status):
        with self.store._connect() as db:
            db.execute("UPDATE private_forecast_uploads SET status=?,revision=revision+1,claim_token=NULL,lease_until=NULL WHERE id=? AND claim_token=?", (status, upload_id, token))
