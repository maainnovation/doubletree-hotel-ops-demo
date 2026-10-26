"""Durable, same-sender human confirmation of a complete local OCR candidate.

This service never extracts, sends, calls a provider, or calculates a roster.
The caller must authenticate normal inbound messages before calling it. Tokens
are capabilities only within the original tenant/business/sender enrollment; a
reply never supplies settings, source bytes, replacement counts, or authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
import secrets
import sqlite3
import uuid

from services.forecast_import import NUMERIC_FIELDS
from services.hotel_parameters import saved_room_capacity
from services.whatsapp_forecast import ForecastNeedsCorrection

REVIEW_SECONDS = 1800
_ACCEPTED = frozenset({"accepted", "sent", "delivered", "read"})
_MIMES = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}
_FIELDS = ("departures", "stayovers", "arrivals", "rooms_on_books", "forecast_pickup", "forecast_occupied", "occupancy_pct")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _time(now):
    if type(now) not in {int, float} or not math.isfinite(now):
        raise ValueError("A finite reference clock is required")


def _deny():
    raise ForecastNeedsCorrection("The forecast review could not be bound safely. Please resend the complete report for a new review; no roster was changed.")


def _validated_rows(rows, capacity, now):
    from services.whatsapp_forecast import validate_confirmed_forecast_rows
    if not isinstance(rows, list) or len(rows) != 7:
        _deny()
    for row in rows:
        if (not isinstance(row, dict) or not set(NUMERIC_FIELDS).issubset(row)
                or set(row) - {"date", "prior_day_occupancy", *NUMERIC_FIELDS}
                or any(type(row[field]) not in {int, float} or not math.isfinite(row[field]) for field in NUMERIC_FIELDS)
                or row.get("prior_day_occupancy") is not None):
            _deny()
    normalized = validate_confirmed_forecast_rows(rows, capacity, datetime.fromtimestamp(now, timezone.utc))
    # The confirmation must show the exact stored percentage, never round a
    # different number into the human-visible matrix.
    if any(not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", row["date"]) or row["occupancy_pct"] != round(row["occupancy_pct"], 2) for row in normalized):
        _deny()
    return normalized


def _prompt(rows, code, expires_at):
    lines = ["Review extracted figures against your ORIGINAL file. No roster has been calculated.",
             "Each date: Departures / Stayovers / Arrivals / Rooms on books / Forecast pickup / Forecast occupied / Occupancy %"]
    for row in rows:
        values = [str(row[field]) if field != "occupancy_pct" else str(row[field]).removesuffix(".0") for field in _FIELDS]
        lines.append(row["date"] + ": " + " / ".join(values))
    deadline = datetime.fromtimestamp(expires_at, timezone.utc).strftime("%H:%M UTC")
    lines += ["Check ALL 7 dates and 49 values. If any value is wrong, cancel and resend a clearer complete file.",
              f"Reply CONFIRM {code} or CANCEL {code} before {deadline} (at most 30 minutes)."]
    text = "\n".join(lines)
    if len(text) > 1600:
        _deny()
    return text


class WhatsAppReviewService:
    def __init__(self, store, files):
        self.store, self.files = store, files
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS wa_forecast_reviews (
                review_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL, phone_number_id TEXT NOT NULL, sender TEXT NOT NULL,
                original_job_id TEXT NOT NULL UNIQUE, candidate_json TEXT NOT NULL,
                candidate_hash TEXT NOT NULL, source_hash TEXT NOT NULL, source_key TEXT NOT NULL,
                mime TEXT NOT NULL, settings_revision INTEGER NOT NULL, settings_hash TEXT NOT NULL,
                enrollment_json TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                prompt_outbox_id TEXT NOT NULL UNIQUE, prompt_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','confirmed','cancelled','expired','invalid','settings_changed')),
                confirmed_job_id TEXT UNIQUE, resolved_at REAL, resolved_job_id TEXT,
                CHECK(expires_at > created_at AND expires_at <= created_at + 1800))""")
            if "resolved_job_id" not in {row[1] for row in db.execute("PRAGMA table_info(wa_forecast_reviews)")}:
                db.execute("ALTER TABLE wa_forecast_reviews ADD COLUMN resolved_job_id TEXT")

    def _claimed(self, db, job, now):
        row = db.execute("SELECT * FROM wa_jobs WHERE id=? AND claim_token=? AND status='processing' AND lease_until>=?",
                         (job.get("id"), job.get("claim_token"), now)).fetchone()
        if not row or any(row[key] != job.get(key) for key in ("tenant_id", "phone_number_id", "sender", "message_id")):
            return None
        return row

    def _context(self, db, job):
        enrolled = self.store._enrollment(db, job["phone_number_id"], job["sender"])
        if not enrolled or enrolled["tenant_id"] != job["tenant_id"]:
            _deny()
        metadata = json.loads(job["media_json"])
        if metadata.get("_enrollment_binding") != self.store.enrollment_binding(enrolled):
            _deny()
        binding = _json(self.store.enrollment_binding(enrolled))
        saved = db.execute("SELECT payload,revision,dataset_mode FROM staff_settings WHERE manager_id=?", (enrolled["manager_id"],)).fetchone()
        if not saved or saved["dataset_mode"] != "demo":
            _deny()
        settings = json.loads(saved["payload"])
        capacity = saved_room_capacity(settings, required=True)
        return enrolled, metadata, binding, saved["revision"], _hash(_json(settings)), capacity

    def _source(self, tenant_id, digest, key, mime):
        if (mime not in _MIMES or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or key != hashlib.sha256(tenant_id.encode()).hexdigest() + "/" + digest + "." + _MIMES[mime]):
            _deny()
        try:
            content = self.files.read(tenant_id, key)
        except (OSError, ValueError):
            _deny()
        if not content or hashlib.sha256(content).hexdigest() != digest:
            _deny()

    def _reusable(self, db, review, staff, now):
        """Dedup only recoverable in-flight work or a still-current PDF result."""
        if review["status"] == "pending":
            prompt = db.execute("SELECT * FROM wa_outbox WHERE id=?", (review["prompt_outbox_id"],)).fetchone()
            origin = db.execute("SELECT status FROM wa_jobs WHERE id=?", (review["original_job_id"],)).fetchone()
            return bool(review["expires_at"] > now and origin and origin["status"] == "awaiting_review"
                        and prompt and prompt["job_id"] == review["original_job_id"] and prompt["kind"] == "text"
                        and prompt["purpose"] == "result" and prompt["tenant_id"] == review["tenant_id"]
                        and prompt["phone_number_id"] == review["phone_number_id"] and prompt["recipient"] == review["sender"]
                        and _hash(prompt["text"]) == review["prompt_hash"]
                        and prompt["status"] in {"pending", "retry", "sending", "accepted", "sent", "delivered", "read", "reconciliation_required"})
        confirming = db.execute("SELECT * FROM wa_jobs WHERE id=?", (review["confirmed_job_id"],)).fetchone()
        if (not confirming or any(confirming[key] != review[key] for key in ("tenant_id", "phone_number_id", "sender", "source_hash", "source_key"))
                or json.loads(confirming["media_json"]).get("_confirmed_review_id") != review["review_id"]):
            return False
        if confirming["status"] in {"pending", "processing", "retry"}:
            recoverable = (confirming["attempts"] < 5 + confirming["ack_base_attempts"]
                           or (confirming["status"] == "processing" and confirming["lease_until"] is not None and confirming["lease_until"] >= now))
            return bool(review["expires_at"] > now and recoverable)
        if confirming["status"] not in {"calculated", "needs_attention"}:
            return False
        reply = db.execute("SELECT * FROM wa_outbox WHERE job_id=? AND purpose='result'", (confirming["id"],)).fetchone()
        if (not reply or reply["kind"] != "document" or not reply["object_key"]
                or reply["status"] not in {"pending", "retry", "sending", "accepted", "sent", "delivered", "read", "reconciliation_required"}
                or reply["tenant_id"] != review["tenant_id"] or reply["phone_number_id"] != review["phone_number_id"]
                or reply["recipient"] != review["sender"]):
            return False
        binding = json.loads(review["enrollment_json"])
        roster = db.execute("SELECT payload,version FROM weekly_rosters WHERE manager_id=? AND week_start=?",
                            (binding["manager_id"], confirming["week_start"])).fetchone()
        if not roster or roster["version"] != confirming["version"] or _hash(review["candidate_json"]) != review["candidate_hash"]:
            return False
        from services.weekly_roster import is_roster_current
        return is_roster_current(json.loads(roster["payload"]), json.loads(review["candidate_json"]), staff)

    def stage(self, job, rows, source_hash, source_key, mime, saved_settings, now):
        """Commit one immutable candidate and one prompt, never an upload ACK."""
        _time(now)
        try:
            with self.store._connect() as db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                origin = self._claimed(db, job, now)
                if not origin:
                    return {"status": "invalid"}
                enrolled, metadata, binding, revision, settings_hash, capacity = self._context(db, origin)
                caller_settings = {key: value for key, value in saved_settings.items() if not key.startswith("_")}
                if (saved_settings.get("_settings_revision") != revision or saved_settings.get("_dataset_mode") != "demo"
                        or _hash(_json(caller_settings)) != settings_hash or metadata.get("mime") != mime
                        or metadata.get("type") not in {"document", "image"}
                        or not 0 <= now - origin["inbound_at"] < 86400
                        or not 0 <= now - enrolled["last_inbound"] < 86400
                        or origin["source_hash"] not in {None, source_hash}
                        or origin["source_key"] not in {None, source_key}):
                    _deny()
                normalized = _validated_rows(rows, capacity, now)
                candidate_json = _json(normalized)
                candidate_hash = _hash(candidate_json)
                self._source(origin["tenant_id"], source_hash, source_key, mime)
                previous = db.execute("SELECT * FROM wa_forecast_reviews WHERE original_job_id=?", (origin["id"],)).fetchone()
                if previous:
                    if (previous["candidate_hash"] != candidate_hash or previous["source_hash"] != source_hash
                            or previous["source_key"] != source_key or previous["settings_revision"] != revision
                            or previous["settings_hash"] != settings_hash or previous["enrollment_json"] != binding):
                        _deny()
                    state = "awaiting_review" if previous["status"] == "pending" else "review_" + previous["status"]
                    db.execute("UPDATE wa_jobs SET status=?,lease_until=NULL WHERE id=?", (state, origin["id"]))
                    return {"status": "duplicate", "review_id": previous["review_id"], "outbox_id": previous["prompt_outbox_id"]}
                prior_reviews = db.execute("""SELECT * FROM wa_forecast_reviews WHERE tenant_id=? AND phone_number_id=? AND sender=?
                    AND source_hash=? AND source_key=? AND settings_revision=? AND settings_hash=? AND enrollment_json=?
                    AND (status='confirmed' OR (status='pending' AND expires_at>?)) ORDER BY created_at DESC""",
                    (origin["tenant_id"], origin["phone_number_id"], origin["sender"], source_hash, source_key, revision, settings_hash, binding, now)).fetchall()
                previous = next((item for item in prior_reviews if self._reusable(db, item, caller_settings, now)), None)
                if previous:
                    # A second OCR interpretation of identical bytes is not an
                    # opportunity to replace the already displayed candidate.
                    db.execute("UPDATE wa_jobs SET status='duplicate',source_hash=?,source_key=?,lease_until=NULL WHERE id=?", (source_hash, source_key, origin["id"]))
                    return {"status": "duplicate", "review_id": previous["review_id"], "outbox_id": previous["prompt_outbox_id"]}
                if db.execute("SELECT 1 FROM wa_outbox WHERE job_id=?", (origin["id"],)).fetchone():
                    _deny()
                expires = min(now + REVIEW_SECONDS, origin["inbound_at"] + 86400, enrolled["last_inbound"] + 86400)
                code, review_id = secrets.token_hex(8), uuid.uuid4().hex
                text = _prompt(normalized, code, expires)
                outbox_id = self.store.add_outbox(db, origin, now, text, purpose="result")
                db.execute("""INSERT INTO wa_forecast_reviews
                    (review_id,token_hash,tenant_id,phone_number_id,sender,original_job_id,candidate_json,candidate_hash,
                     source_hash,source_key,mime,settings_revision,settings_hash,enrollment_json,created_at,expires_at,prompt_outbox_id,prompt_hash)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (review_id, _hash(code), origin["tenant_id"], origin["phone_number_id"], origin["sender"], origin["id"], candidate_json,
                     candidate_hash, source_hash, source_key, mime, revision, settings_hash, binding, now, expires, outbox_id, _hash(text)))
                metadata["_forecast_review_id"] = review_id
                db.execute("UPDATE wa_jobs SET status='awaiting_review',source_hash=?,source_key=?,media_json=?,lease_until=NULL WHERE id=?",
                           (source_hash, source_key, _json(metadata), origin["id"]))
                return {"status": "awaiting_review", "review_id": review_id, "outbox_id": outbox_id}
        except ForecastNeedsCorrection:
            raise
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
            _deny()

    def _finish(self, db, review, status, now, command_job_id=None):
        db.execute("UPDATE wa_forecast_reviews SET status=?,resolved_at=?,resolved_job_id=? WHERE review_id=? AND status='pending'", (status, now, command_job_id, review["review_id"]))
        suffix = status if status in {"cancelled", "expired"} else "invalid"
        db.execute("UPDATE wa_jobs SET status=?,lease_until=NULL WHERE id=? AND status='awaiting_review'", ("review_" + suffix, review["original_job_id"]))
        return {"status": status}

    def prompt_dispatch_allowed(self, db, item, now):
        """Recheck a queued prompt inside the dispatcher's existing transaction.

        An ordinary result/ACK is outside this gate. A review prompt cannot
        bypass it by changing its recorded outbox ID, job ID, or metadata.
        """
        _time(now)
        try:
            reviews = db.execute("SELECT * FROM wa_forecast_reviews WHERE prompt_outbox_id=? OR original_job_id=?",
                                 (item["id"], item["job_id"])).fetchall()
            origin = db.execute("SELECT * FROM wa_jobs WHERE id=?", (item["job_id"],)).fetchone()
            if not reviews:
                return not origin or "_forecast_review_id" not in json.loads(origin["media_json"])
            if len(reviews) != 1:
                return False
            review = reviews[0]
            if review["status"] != "pending":
                return False
            if not review["created_at"] <= now < review["expires_at"] <= review["created_at"] + REVIEW_SECONDS:
                self._finish(db, review, "expired", now)
                return False
            if (not origin or origin["id"] != review["original_job_id"] or origin["status"] != "awaiting_review"
                    or any(origin[key] != review[key] for key in ("tenant_id", "phone_number_id", "sender", "source_hash", "source_key"))):
                self._finish(db, review, "invalid", now)
                return False
            enrolled, metadata, binding, revision, settings_hash, capacity = self._context(db, origin)
            if (binding != review["enrollment_json"] or metadata.get("_forecast_review_id") != review["review_id"]
                    or metadata.get("mime") != review["mime"] or not 0 <= now - enrolled["last_inbound"] < 86400):
                self._finish(db, review, "invalid", now)
                return False
            if revision != review["settings_revision"] or settings_hash != review["settings_hash"]:
                self._finish(db, review, "settings_changed", now)
                return False
            prompt = db.execute("SELECT * FROM wa_outbox WHERE id=?", (item["id"],)).fetchone()
            if (not prompt or prompt["id"] != review["prompt_outbox_id"] or prompt["job_id"] != origin["id"]
                    or prompt["purpose"] != "result" or prompt["kind"] != "text"
                    or prompt["status"] not in {"pending", "retry", "sending"} or prompt["provider_message_id"] is not None
                    or prompt["tenant_id"] != review["tenant_id"] or prompt["phone_number_id"] != review["phone_number_id"]
                    or prompt["recipient"] != review["sender"] or prompt["object_key"] is not None or prompt["filename"] is not None
                    or _hash(prompt["text"]) != review["prompt_hash"]
                    or any(prompt[key] != item[key] for key in ("job_id", "tenant_id", "phone_number_id", "recipient", "kind", "text", "object_key", "filename", "purpose"))):
                self._finish(db, review, "invalid", now)
                return False
            self._source(review["tenant_id"], review["source_hash"], review["source_key"], review["mime"])
            if _hash(review["candidate_json"]) != review["candidate_hash"]:
                self._finish(db, review, "invalid", now)
                return False
            rows = _validated_rows(json.loads(review["candidate_json"]), capacity, now)
            if _json(rows) != review["candidate_json"]:
                self._finish(db, review, "invalid", now)
                return False
            return True
        except (ForecastNeedsCorrection, OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
            return False

    def resolve(self, command_job, command, code, now):
        """Resolve one authenticated exact command; no sends or new outbox rows."""
        _time(now)
        if not isinstance(command, str) or command not in {"CONFIRM", "CANCEL"} or not isinstance(code, str) or not re.fullmatch(r"[a-fA-F0-9]{16}", code):
            return {"status": "invalid"}
        try:
            with self.store._connect() as db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN IMMEDIATE")
                current = self._claimed(db, command_job, now)
                if not current:
                    return {"status": "invalid"}
                enrolled, metadata, binding, revision, settings_hash, capacity = self._context(db, current)
                if (metadata.get("type") != "review_command" or metadata.get("command") != command
                        or not isinstance(metadata.get("review_code"), str) or metadata["review_code"].lower() != code.lower()
                        or "_pilot_attestation" in metadata):
                    return {"status": "invalid"}
                review = db.execute("""SELECT * FROM wa_forecast_reviews WHERE token_hash=? AND tenant_id=? AND phone_number_id=? AND sender=?""",
                    (_hash(code.lower()), current["tenant_id"], current["phone_number_id"], current["sender"])).fetchone()
                if not review:
                    return {"status": "invalid"}
                if review["status"] == "confirmed" and (review["confirmed_job_id"] != current["id"] or command != "CONFIRM"):
                    return {"status": "duplicate"}
                if review["status"] not in {"pending", "confirmed"}:
                    if review["resolved_job_id"] is None:
                        db.execute("UPDATE wa_forecast_reviews SET resolved_job_id=? WHERE review_id=? AND resolved_job_id IS NULL", (current["id"], review["review_id"]))
                        return {"status": review["status"]}
                    return {"status": review["status"] if review["resolved_job_id"] == current["id"] else "duplicate"}
                if not review["created_at"] <= current["inbound_at"] <= now or not 0 <= now - enrolled["last_inbound"] < 86400:
                    return {"status": "invalid"}
                if not now < review["expires_at"] <= review["created_at"] + REVIEW_SECONDS:
                    return self._finish(db, review, "expired", now, current["id"])
                if binding != review["enrollment_json"]:
                    return self._finish(db, review, "invalid", now, current["id"])
                if revision != review["settings_revision"] or settings_hash != review["settings_hash"]:
                    return self._finish(db, review, "settings_changed", now, current["id"])
                origin = db.execute("SELECT * FROM wa_jobs WHERE id=?", (review["original_job_id"],)).fetchone()
                prompt = db.execute("SELECT * FROM wa_outbox WHERE id=?", (review["prompt_outbox_id"],)).fetchone()
                if (not origin or origin["status"] not in {"awaiting_review", "review_confirmed"}
                        or any(origin[key] != review[key] for key in ("tenant_id", "phone_number_id", "sender", "source_hash", "source_key"))
                        or json.loads(origin["media_json"]).get("_enrollment_binding") != json.loads(binding)
                        or json.loads(origin["media_json"]).get("_forecast_review_id") != review["review_id"]
                        or json.loads(origin["media_json"]).get("mime") != review["mime"]
                        or not prompt or prompt["job_id"] != origin["id"] or prompt["purpose"] != "result"
                        or prompt["tenant_id"] != review["tenant_id"] or prompt["phone_number_id"] != review["phone_number_id"]
                        or prompt["recipient"] != review["sender"] or prompt["kind"] != "text"
                        or prompt["object_key"] is not None or prompt["filename"] is not None
                        or _hash(prompt["text"]) != review["prompt_hash"]):
                    return self._finish(db, review, "invalid", now, current["id"])
                if prompt["status"] not in _ACCEPTED or not prompt["provider_message_id"]:
                    return {"status": "invalid"}
                self._source(review["tenant_id"], review["source_hash"], review["source_key"], review["mime"])
                if _hash(review["candidate_json"]) != review["candidate_hash"]:
                    return self._finish(db, review, "invalid", now, current["id"])
                normalized = _validated_rows(json.loads(review["candidate_json"]), capacity, now)
                if _json(normalized) != review["candidate_json"]:
                    return self._finish(db, review, "invalid", now, current["id"])
                if command == "CANCEL":
                    return self._finish(db, review, "cancelled", now, current["id"])
                if review["status"] == "pending":
                    db.execute("UPDATE wa_forecast_reviews SET status='confirmed',confirmed_job_id=?,resolved_at=? WHERE review_id=? AND status='pending'",
                               (current["id"], now, review["review_id"]))
                    db.execute("UPDATE wa_jobs SET status='review_confirmed' WHERE id=? AND status='awaiting_review'", (origin["id"],))
                elif review["confirmed_job_id"] != current["id"]:
                    return {"status": "duplicate"}
                metadata.update(_confirmed_review_id=review["review_id"], _review_source_job_id=origin["id"], _review_settings_revision=revision)
                db.execute("UPDATE wa_jobs SET source_hash=?,source_key=?,media_json=? WHERE id=? AND claim_token=? AND status='processing'",
                           (review["source_hash"], review["source_key"], _json(metadata), current["id"], current["claim_token"]))
                return {"status": "ready", "rows": normalized, "source_hash": review["source_hash"], "source_key": review["source_key"],
                        "settings_revision": revision, "mime": review["mime"], "original_job_id": origin["id"], "review_id": review["review_id"]}
        except (ForecastNeedsCorrection, OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
            return {"status": "invalid"}
