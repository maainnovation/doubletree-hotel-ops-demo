"""Separate durable worker for the WhatsApp-first forecast workflow.

No extraction, storage of uploads, LLM, scheduling or network send happens in
webhook requests. This pilot uses explicit development adapters and synthetic
staff only; production adapters/authentication are a separate deployment gate.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import time
from datetime import datetime, timezone

from services.whatsapp_forecast import ForecastNeedsCorrection, validate_whatsapp_forecast
from services.whatsapp_provider import AmbiguousSendError, ProviderError, RetryableProviderError
from services.twilio_provider import TwilioRequestRejected
from services.weekly_roster import build_exports, generate_weekly_roster, is_roster_current
from services.hotel_parameters import saved_room_capacity


class ForecastWorker:
    def __init__(self, store, files, provider, clock=time.time):
        if getattr(provider, "is_live", False) and (getattr(store, "development_only", True) or getattr(files, "development_only", True)):
            from services.twilio_pilot import SyntheticPilotProvider
            if not isinstance(provider, SyntheticPilotProvider) or not provider.permits_development_storage(store, files):
                raise ValueError("Live WhatsApp is blocked with development storage; approved production adapters are required")
        self.store, self.files, self.provider, self.clock = store, files, provider, clock

    def process_one(self):
        now = self.clock()
        phone_id = getattr(self.provider, "phone_number_id", None)
        job = self.store.claim_job(now, phone_id, exclude_twilio=phone_id is None)
        if not job:
            return False
        try:
            enrolled = self.store.enrollment(job["phone_number_id"], job["sender"])
            if not enrolled or enrolled["tenant_id"] != job["tenant_id"]:
                self.store.job_failed(job, now, "enrollment_revoked", retry=False)
                return True
            if getattr(self.provider, "phone_number_id", job["phone_number_id"]) != job["phone_number_id"]:
                self.store.job_failed(job, now, "provider_number_mismatch", retry=False)
                return True
            media = json.loads(job["media_json"])
            if media.get("_enrollment_binding") != self.store.enrollment_binding(enrolled):
                self._correction(job, "The enrolled hotel setup or report format changed after this upload. No roster was changed. Please resend the complete forecast after the administrator confirms setup.")
                return True
            if job["phone_number_id"].startswith("twilio:"):
                if media.get("_provider") != "twilio":
                    self.store.job_failed(job, now, "invalid_provider_binding", retry=False)
                    return True
                try:
                    inbound_at = self.provider.verify_inbound_message(job["message_id"], job["sender"], now)
                except RetryableProviderError:
                    raise
                except (ProviderError, AttributeError):
                    self.store.job_failed(job, now, "inbound_identity_or_window_unverified", retry=False)
                    return True
                with self.store._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = self.store._enrollment(db, job["phone_number_id"], job["sender"])
                    if not self._owns_job(db, job) or not current or self.store.enrollment_binding(current) != media["_enrollment_binding"]:
                        return True
                    db.execute("UPDATE wa_jobs SET inbound_at=? WHERE id=?", (inbound_at, job["id"]))
                    db.execute("UPDATE wa_senders SET last_inbound=MAX(last_inbound,?) WHERE phone_number_id=? AND sender=?", (min(inbound_at, now), job["phone_number_id"], job["sender"]))
                job["inbound_at"] = inbound_at
            if media["type"] == "unsupported":
                self._correction(job, "Please send the complete standard native-text forecast PDF, including Hotel code, all seven Friday–Thursday dates and all forecast counts. Photos/scans need review; no roster was changed.")
                return True
            saved_settings = self.store.get_staff_settings(enrolled["manager_id"])
            if not saved_settings:
                raise ForecastNeedsCorrection("No saved staff/settings are enrolled for this hotel. Ask the administrator to complete setup; no defaults are inferred.")
            if saved_settings["_dataset_mode"] != "demo":
                raise ForecastNeedsCorrection("This development pilot accepts fictional demo staff only. Real hotel data requires the approved production deployment.")
            try:
                capacity = saved_room_capacity(saved_settings, required=True)
            except ValueError as exc:
                raise ForecastNeedsCorrection("Hotel room inventory is not configured in the app's saved Parameters. Set it there once; no capacity is inferred from service counts or the report.") from exc
            maximum = 10 * 1024 * 1024 if media["mime"] == "application/pdf" else 5 * 1024 * 1024
            data = self.provider.fetch_media(media["id"], media["mime"], maximum)
            if not isinstance(data, bytes) or not data or len(data) > maximum:
                raise ForecastNeedsCorrection("The file is empty or too large. Resend a PDF under 10 MB or an image under 5 MB.")
            actual_hash = hashlib.sha256(data).digest()
            # Meta supplies a signed checksum; Twilio supplies an authenticated
            # immutable media URL, not a checksum. Always retain our content hash.
            if not job["phone_number_id"].startswith("twilio:") and not hmac.compare_digest(actual_hash, base64.b64decode(media["sha256"], validate=True)):
                raise ForecastNeedsCorrection("The uploaded file failed its integrity check. Please resend the original PDF.")
            source_hash = actual_hash.hex()
            rows = validate_whatsapp_forecast(data, media["mime"], enrolled["hotel_code"], capacity, datetime.fromtimestamp(now, timezone.utc), report_profile=enrolled["report_profile"], hotel_name=enrolled["hotel_name"])
            source_key = self.files.put(job["tenant_id"], data, "pdf")
            self._persist_roster(job, enrolled, rows, source_hash, source_key, saved_settings["_settings_revision"])
        except ForecastNeedsCorrection as exc:
            self._correction(job, f"Forecast needs correction: {str(exc)[:1800]} No roster was changed. Resend the corrected complete native-text PDF, or review it in the app.")
        except RetryableProviderError:
            self.store.job_failed(job, now, "media_temporarily_unavailable")
        except ProviderError:
            self._correction(job, "I could not retrieve that file safely. No roster was changed. Please resend the original native-text PDF; contact the pilot administrator if this repeats.")
        except Exception:
            # Only a non-sensitive error code is persisted; never document text,
            # secrets, employee data or raw provider exception strings.
            self.store.job_failed(job, now, "processing_error")
        return True

    def _correction(self, job, text):
        now = self.clock()
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._owns_job(db, job):
                return
            enrolled = self.store._enrollment(db, job["phone_number_id"], job["sender"])
            if not enrolled or enrolled["tenant_id"] != job["tenant_id"]:
                db.execute("UPDATE wa_jobs SET status='failed',error_code='enrollment_revoked' WHERE id=?", (job["id"],))
                return
            self.store.add_outbox(db, job, now, text)
            db.execute("UPDATE wa_jobs SET status='needs_correction',lease_until=NULL WHERE id=?", (job["id"],))

    @staticmethod
    def _owns_job(db, job):
        return db.execute("SELECT 1 FROM wa_jobs WHERE id=? AND claim_token=? AND status='processing'", (job["id"], job["claim_token"])).fetchone() is not None

    def _persist_roster(self, job, enrolled, rows, digest, source_key, settings_revision):
        now = self.clock()
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            # Development single-host serialization: final saved settings, prior
            # locks, version append and outbox commit are one atomic write.
            db.execute("BEGIN IMMEDIATE")
            if not self._owns_job(db, job):
                return
            current_enrollment = self.store._enrollment(db, job["phone_number_id"], job["sender"])
            if not current_enrollment or current_enrollment["tenant_id"] != job["tenant_id"]:
                db.execute("UPDATE wa_jobs SET status='failed',error_code='enrollment_revoked' WHERE id=?", (job["id"],))
                return
            if any(current_enrollment[key] != enrolled[key] for key in ("manager_id", "hotel_code", "hotel_name", "report_profile")):
                raise ValueError("Enrollment changed during processing")
            staff_row = db.execute("SELECT payload,dataset_mode,revision FROM staff_settings WHERE manager_id=?", (enrolled["manager_id"],)).fetchone()
            if not staff_row:
                raise ForecastNeedsCorrection("No saved staff/settings are enrolled for this hotel. Ask the administrator to complete setup; no defaults are inferred.")
            if staff_row["dataset_mode"] != "demo":
                raise ForecastNeedsCorrection("This development pilot accepts fictional demo staff only. Real hotel data requires the approved production deployment.")
            if staff_row["revision"] != settings_revision:
                raise ValueError("Saved Parameters/staff changed during validation; retry against the latest revision")
            staff = json.loads(staff_row["payload"])
            staff["dataset_mode"] = staff_row["dataset_mode"]
            duplicate = db.execute("SELECT d.job_id FROM wa_documents d JOIN wa_jobs j ON j.id=d.job_id WHERE d.tenant_id=? AND d.sender=? AND d.source_hash=? AND j.phone_number_id=?", (job["tenant_id"], job["sender"], digest, job["phone_number_id"])).fetchone()
            if duplicate:
                previous_job = db.execute("SELECT media_json,week_start,version FROM wa_jobs WHERE id=?", (duplicate["job_id"],)).fetchone()
                previous_binding = json.loads(previous_job["media_json"]).get("_enrollment_binding") if previous_job else None
                if previous_binding != self.store.enrollment_binding(enrolled):
                    raise ForecastNeedsCorrection("This file was previously processed under different hotel setup. Ask the administrator to review the existing version; an old PDF reply will not be reused.")
                previous_roster_row = db.execute("SELECT payload,version FROM weekly_rosters WHERE manager_id=? AND week_start=?", (enrolled["manager_id"], previous_job["week_start"])).fetchone()
                previous_roster = json.loads(previous_roster_row["payload"]) if previous_roster_row else None
                previous_reply = db.execute("SELECT kind,status FROM wa_outbox WHERE job_id=?", (duplicate["job_id"],)).fetchone()
                reusable_reply = previous_reply and previous_reply["kind"] == "document" and previous_reply["status"] in {"pending", "retry", "outside_window", "sending", "accepted", "sent", "delivered", "read", "reconciliation_required"}
                unchanged = reusable_reply and previous_roster_row and previous_roster_row["version"] == previous_job["version"] and is_roster_current(previous_roster, rows, staff)
                if unchanged:
                    # Reopen only a provably-unsent, unchanged result. The same
                    # file with changed saved Parameters/staff creates a new version.
                    db.execute("UPDATE wa_outbox SET status='pending',error_code=NULL,available_at=?,updated_at=? WHERE job_id=? AND tenant_id=? AND recipient=? AND status='outside_window'", (now, now, duplicate["job_id"], job["tenant_id"], job["sender"]))
                    db.execute("UPDATE wa_jobs SET status='duplicate',source_hash=?,lease_until=NULL WHERE id=?", (digest, job["id"]))
                    return
            week_start = rows[0]["date"]
            prior_row = db.execute("SELECT payload,version FROM weekly_rosters WHERE manager_id=? AND week_start=?", (enrolled["manager_id"], week_start)).fetchone()
            prior = dict(json.loads(prior_row["payload"]), version=prior_row["version"]) if prior_row else None
            if prior and prior.get("source", {}).get("channel") == "whatsapp":
                previous = prior["source"]
                previous_timestamp = previous.get("inbound_at", 0)
                old_order = db.execute("SELECT rowid FROM wa_jobs WHERE id=?", (previous.get("job_id", ""),)).fetchone()
                this_order = db.execute("SELECT rowid FROM wa_jobs WHERE id=?", (job["id"],)).fetchone()
                if previous_timestamp > job["inbound_at"] or (previous_timestamp == job["inbound_at"] and old_order and old_order[0] > this_order[0]):
                    db.execute("UPDATE wa_jobs SET status='superseded',error_code='newer_forecast_already_saved',source_hash=?,lease_until=NULL WHERE id=?", (digest, job["id"]))
                    return
            if prior:
                # UI already auto-locks changed assignments. Preserve explicit
                # manual provenance from adapters too, even if a lock was omitted.
                for row in prior.get("schedule", []):
                    if row.get("manual") or row.get("assignment_source") == "manual":
                        row["locked"] = True
            try:
                roster = generate_weekly_roster(rows, staff, prior=prior)
            except (ValueError, KeyError, TypeError) as exc:
                raise ForecastNeedsCorrection("Saved hotel staff/settings need administrator review before automatic scheduling.") from exc
            roster["source"] = {"channel": "whatsapp", "report_profile": enrolled["report_profile"], "hotel_code": enrolled["hotel_code"], "hotel_name": enrolled["hotel_name"], "dataset_mode": staff["dataset_mode"], "settings_revision": settings_revision, "job_id": job["id"], "inbound_at": job["inbound_at"], "source_sha256": digest, "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat()}
            roster["workflow_status"] = "needs_attention" if roster.get("constraints") or not roster["feasible"] else "calculated"
            roster = self.store._save_roster_in_transaction(db, enrolled["manager_id"], week_start, roster)
            _, pdf = build_exports(roster)
            pdf_key = self.files.put(job["tenant_id"], pdf, "pdf")
            caption = ("NEEDS ATTENTION: shortages or constraints remain; this is not a confirmed workable schedule. " if roster["workflow_status"] == "needs_attention" else "Calculated roster; review before operational use. ") + f"{enrolled['hotel_name']} | {week_start} to {roster['week_end']} | version {roster['version']}. Saved in the app. Fictional development pilot."
            filename = f"roster-{week_start.replace('-', '')}.pdf" if job["phone_number_id"].startswith("twilio:") else f"weekly-roster-{week_start}-v{roster['version']}.pdf"
            self.store.add_outbox(db, job, now, caption, pdf_key, filename)
            db.execute("INSERT INTO wa_documents(tenant_id,sender,source_hash,job_id) VALUES(?,?,?,?) ON CONFLICT(tenant_id,sender,source_hash) DO UPDATE SET job_id=excluded.job_id", (job["tenant_id"], job["sender"], digest, job["id"]))
            db.execute("UPDATE wa_jobs SET status=?,source_hash=?,source_key=?,week_start=?,version=?,lease_until=NULL WHERE id=?", (roster["workflow_status"], digest, source_key, week_start, roster["version"], job["id"]))

    def dispatch_one(self):
        now = self.clock()
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            # A crashed/overdue send is NOT a safe retry: Meta has no send
            # idempotency guarantee. Reconcile signed status callbacks/operator.
            phone_id = getattr(self.provider, "phone_number_id", None)
            db.execute("UPDATE wa_outbox SET status='reconciliation_required',error_code='send_outcome_unknown' WHERE status='sending' AND updated_at<? AND (? IS NULL OR phone_number_id=?) AND (?=0 OR phone_number_id NOT LIKE 'twilio:%')", (now - 300, phone_id, phone_id, phone_id is None))
            row = db.execute("SELECT * FROM wa_outbox WHERE status IN ('pending','retry') AND available_at<=? AND attempts<5 AND (? IS NULL OR phone_number_id=?) AND (?=0 OR phone_number_id NOT LIKE 'twilio:%') ORDER BY available_at,id LIMIT 1", (now, phone_id, phone_id, phone_id is None)).fetchone()
            if not row:
                return False
            item = dict(row)
            enrolled = self.store._enrollment(db, item["phone_number_id"], item["recipient"])
            if not enrolled or enrolled["tenant_id"] != item["tenant_id"]:
                db.execute("UPDATE wa_outbox SET status='cancelled',error_code='enrollment_revoked',updated_at=? WHERE id=?", (now, item["id"]))
                return True
            origin = db.execute("SELECT media_json,week_start,version FROM wa_jobs WHERE id=?", (item["job_id"],)).fetchone()
            if item["kind"] == "document":
                binding = json.loads(origin["media_json"]).get("_enrollment_binding") if origin else None
                if binding != self.store.enrollment_binding(enrolled):
                    db.execute("UPDATE wa_outbox SET status='cancelled',error_code='enrollment_changed',updated_at=? WHERE id=?", (now, item["id"]))
                    return True
                current = db.execute("SELECT payload,dataset_mode FROM staff_settings WHERE manager_id=?", (enrolled["manager_id"],)).fetchone()
                latest = db.execute("SELECT payload,version FROM weekly_rosters WHERE manager_id=? AND week_start=?", (enrolled["manager_id"], origin["week_start"])).fetchone()
                latest_roster = json.loads(latest["payload"]) if latest else None
                if not current or current["dataset_mode"] != "demo":
                    db.execute("UPDATE wa_outbox SET status='cancelled',error_code='settings_unavailable',updated_at=? WHERE id=?", (now, item["id"]))
                    return True
                if not latest or latest["version"] != origin["version"] or not is_roster_current(latest_roster, latest_roster["forecast"], json.loads(current["payload"])):
                    item.update(kind="text", object_key=None, filename=None, text="Saved Parameters, staff or roster changed before this PDF could be sent. No outdated PDF was sent. Please resend the forecast to recalculate using the current saved settings.")
                    db.execute("UPDATE wa_outbox SET kind='text',text=?,object_key=NULL,filename=NULL WHERE id=?", (item["text"], item["id"]))
            provider_phone = getattr(self.provider, "phone_number_id", item["phone_number_id"])
            if provider_phone != item["phone_number_id"]:
                db.execute("UPDATE wa_outbox SET status='cancelled',error_code='provider_number_mismatch',updated_at=? WHERE id=?", (now, item["id"]))
                return True
            if now - enrolled["last_inbound"] >= 24 * 3600 or enrolled["last_inbound"] > now:
                db.execute("UPDATE wa_outbox SET status='outside_window',error_code='approved_template_required',updated_at=? WHERE id=?", (now, item["id"]))
                return True
            db.execute("UPDATE wa_outbox SET status='sending',attempts=attempts+1,updated_at=? WHERE id=?", (now, item["id"]))
        try:
            if item["kind"] == "document":
                data = self.files.read(item["tenant_id"], item["object_key"])
                message_id = self.provider.send_document(item["recipient"], data, item["filename"], item["text"], item["id"])
            else:
                message_id = self.provider.send_text(item["recipient"], item["text"], item["id"])
            if not isinstance(message_id, str) or not message_id:
                raise AmbiguousSendError("No send receipt")
            status, code = "accepted", None  # Not equivalent to delivered.
        except RetryableProviderError:
            status = "retry" if item["attempts"] + 1 < 5 else "failed"
            code, message_id = "safe_presend_retry", None
        except AmbiguousSendError:
            status, code, message_id = "reconciliation_required", "send_outcome_unknown", None
        except TwilioRequestRejected as exc:
            status, code, message_id = "failed", exc.safe_error_code, None
        except ProviderError:
            status, code, message_id = "failed", "provider_rejected", None
        except Exception:
            # Includes process-local failures; conservative because whether the
            # provider accepted a message may no longer be knowable.
            status, code, message_id = "reconciliation_required", "send_outcome_unknown", None
        with self.store._connect() as db:
            db.execute("UPDATE wa_outbox SET status=?,error_code=?,provider_message_id=?,available_at=?,updated_at=? WHERE id=? AND status='sending'", (status, code, message_id, now + min(3600, 30 * 2 ** (item["attempts"] + 1)), now, item["id"]))
        return True

    def drain_ready(self, limit=100):
        processed = dispatched = 0
        for _ in range(limit):
            if not self.process_one():
                break
            processed += 1
        for _ in range(limit):
            if not self.dispatch_one():
                break
            dispatched += 1
        return {"processed": processed, "dispatched": dispatched}
