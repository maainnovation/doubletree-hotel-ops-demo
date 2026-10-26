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

from services.whatsapp_forecast import ForecastNeedsCorrection, validate_confirmed_forecast_rows, validate_whatsapp_forecast
from services.whatsapp_provider import AmbiguousSendError, ProviderError, RetryableProviderError
from services.twilio_provider import TwilioRequestRejected
from services.weekly_roster import build_exports, generate_weekly_roster, is_roster_current
from services.hotel_parameters import saved_room_capacity


UPLOAD_ACK_TEXT = "Got your PDF. I’m calculating the forecast and will send the report shortly."
_ACK_ACCEPTED = {"accepted", "sent", "delivered", "read"}


class ForecastWorker:
    def __init__(self, store, files, provider, clock=time.time, *, acknowledge_uploads=False, review_images=False, media_evaluator=None):
        if getattr(provider, "is_live", False):
            from services.twilio_review_pilot import ReviewPilotProvider
            approved_review=(type(provider) is ReviewPilotProvider and provider.permits_review(store,files))
            if review_images and not approved_review:
                raise ValueError('Live WhatsApp is blocked unless the separately approved owner review runtime is used.')
            if getattr(store, "development_only", True) or getattr(files, "development_only", True):
                from services.twilio_pilot import SyntheticPilotProvider
                approved_legacy=(isinstance(provider,SyntheticPilotProvider) and provider.permits_development_storage(store,files))
                if not (approved_review or approved_legacy):
                    raise ValueError("Live WhatsApp is blocked with development storage; approved production adapters are required")
        if not isinstance(acknowledge_uploads, bool):
            raise ValueError("Acknowledgement enable flag must be boolean")
        if not isinstance(review_images,bool):
            raise ValueError('Image review enable flag must be boolean')
        self.acknowledge_uploads = acknowledge_uploads
        self.review_images=review_images
        self.media_evaluator=media_evaluator
        self.store, self.files, self.provider, self.clock = store, files, provider, clock
        self.review_service=None
        if review_images:
            from services.whatsapp_review import WhatsAppReviewService
            self.review_service=WhatsAppReviewService(store,files)

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
            if media['type']=='review_command':
                self._handle_review_command(job,media,now)
                return True
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
            suffix={'application/pdf':'pdf','image/png':'png','image/jpeg':'jpg'}[media['mime']]
            if self.review_images:
                # Even the first PDF text/stream inspection must run inside
                # resource isolation: decompression precedes OCR classification.
                evaluator=self.media_evaluator
                if evaluator is None:
                    from services.forecast_media_worker import evaluate_forecast_media_isolated
                    evaluator=evaluate_forecast_media_isolated
                try:
                    assessment=evaluator(data,media['mime'],enrolled['hotel_code'],capacity,datetime.fromtimestamp(now,timezone.utc),
                                         report_profile=enrolled['report_profile'],hotel_name=enrolled['hotel_name'])
                except ForecastNeedsCorrection:
                    raise
                except Exception:
                    raise ForecastNeedsCorrection('Local image/PDF review could not complete within its safety limits. Resend a sharper, smaller report or the original native-text PDF.') from None
                if not isinstance(assessment,dict):
                    raise ForecastNeedsCorrection('The report could not be assessed safely. Please resend its original native-text PDF.')
                native_automatic=(media['mime']=='application/pdf'
                                  and assessment.get('method')=='native_pdf'
                                  and assessment.get('automatic_roster_allowed') is True
                                  and assessment.get('requires_review') is False)
                if native_automatic:
                    # Cheap matrix checks only; no PDF decoding in the worker.
                    rows=validate_confirmed_forecast_rows(assessment.get('rows'),capacity,datetime.fromtimestamp(now,timezone.utc))
                else:
                    # No OCR evidence, confidence, or agreement may authorize
                    # automatic scheduling. A complete matrix needs consent.
                    if assessment.get('candidate_valid_for_confirmation') is not True or not assessment.get('rows'):
                        raise ForecastNeedsCorrection('A complete, current and internally consistent report could not be extracted for confirmation. Resend a sharper full report or its original native-text PDF; missing dates or counts will not be guessed.')
                    source_key=self.files.put(job['tenant_id'],data,suffix)
                    self.review_service.stage(job,assessment['rows'],source_hash,source_key,media['mime'],saved_settings,now)
                    return True
            else:
                rows=validate_whatsapp_forecast(data,media['mime'],enrolled['hotel_code'],capacity,datetime.fromtimestamp(now,timezone.utc),
                                               report_profile=enrolled['report_profile'],hotel_name=enrolled['hotel_name'])
            source_key = self.files.put(job["tenant_id"], data, suffix)
            with self.store._connect() as db:
                already_staged = db.execute("SELECT 1 FROM wa_outbox WHERE job_id=? AND purpose='ack'", (job['id'],)).fetchone()
            if self.acknowledge_uploads or already_staged:
                if not self._stage_ack(job, enrolled, rows, source_hash, source_key, saved_settings):
                    return True
            self._persist_roster(job, enrolled, rows, source_hash, source_key, saved_settings["_settings_revision"])
        except ForecastNeedsCorrection as exc:
            from services.private_storage_budget import PrivateStorageQuotaError
            if isinstance(exc, PrivateStorageQuotaError):
                self._correction(job, "Private storage is full or unavailable. No roster was changed. Contact the operator; do not repeatedly resend the file.")
            else:
                self._correction(job, f"Forecast needs correction: {str(exc)[:1800]} No roster was changed. Resend the corrected complete native-text PDF, or review it in the app.")
        except RetryableProviderError:
            if not self._terminal_ack_failure(job):
                self.store.job_failed(job, now, "media_temporarily_unavailable")
        except ProviderError:
            self._correction(job, "I could not retrieve that file safely. No roster was changed. Please resend the original native-text PDF; contact the pilot administrator if this repeats.")
        except Exception:
            # Only a non-sensitive error code is persisted; never document text,
            # secrets, employee data or raw provider exception strings.
            if not self._terminal_ack_failure(job):
                self.store.job_failed(job, now, "processing_error")
        return True

    def _handle_review_command(self,job,media,now):
        if not self.review_images:
            self.store.job_failed(job,now,'review_disabled',retry=False)
            return
        result=self.review_service.resolve(job,media['command'],media['review_code'],now)
        status=result['status']
        if status=='ready':
            enrolled=self.store.enrollment(job['phone_number_id'],job['sender'])
            self._persist_roster(job,enrolled,result['rows'],result['source_hash'],result['source_key'],result['settings_revision'])
        elif status=='duplicate':
            with self.store._connect() as db:
                if self._owns_job(db,job):
                    db.execute("UPDATE wa_jobs SET status='duplicate',error_code='review_already_resolved',lease_until=NULL WHERE id=?",(job['id'],))
        else:
            messages={
                'cancelled':'Review cancelled. No roster was generated from that upload.',
                'expired':'That review expired. Please resend the complete current forecast for a new review.',
                'settings_changed':'Saved Parameters, staff or hotel enrollment changed. Please resend the report so the review uses the latest saved setup.',
            }
            self._correction(job,messages.get(status,'That review could not be confirmed safely. Please resend the complete current report; no roster was changed.'))

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

    def _terminal_ack_failure(self, job):
        if job['attempts'] < 5 + job.get('ack_base_attempts',0):
            return False
        with self.store._connect() as db:
            ack = db.execute("SELECT status FROM wa_outbox WHERE job_id=? AND purpose='ack'", (job['id'],)).fetchone()
            reviewed=False
            if self.review_service is not None:
                reviewed=bool(db.execute("SELECT 1 FROM wa_forecast_reviews WHERE confirmed_job_id=? AND status='confirmed' AND tenant_id=? AND phone_number_id=? AND sender=?",(job['id'],job['tenant_id'],job['phone_number_id'],job['sender'])).fetchone())
        if (not ack or ack[0] not in _ACK_ACCEPTED) and not reviewed:
            return False
        self._correction(job, "The confirmed forecast calculation could not be completed safely. Please resend for a new review or contact the hotel administrator; no report was generated." if reviewed else "I received your PDF, but the automatic calculation could not be completed safely. Please review the app or contact the hotel administrator; no report will be sent automatically.")
        return True

    def _stage_ack(self, job, enrolled, rows, digest, source_key, settings):
        """Validate first, commit one receipt, then wait for provider acceptance.

        No network or calculation in this transaction. A restart revalidates the
        immutable input and current settings, but never recreates an existing ACK.
        """
        now = self.clock()
        revision = settings['_settings_revision']
        staff = {key:value for key,value in settings.items() if not key.startswith('_')}
        staff['dataset_mode'] = settings['_dataset_mode']
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            if not self._owns_job(db, job):
                return False
            current = self.store._enrollment(db, job['phone_number_id'], job['sender'])
            if not current or self.store.enrollment_binding(current) != self.store.enrollment_binding(enrolled):
                raise ForecastNeedsCorrection('The enrolled hotel setup changed; ask the administrator to review it before resending.')
            if not 0 <= now-current['last_inbound'] < 86400:
                db.execute("UPDATE wa_jobs SET status='failed',error_code='ack_window_expired',lease_until=NULL WHERE id=?", (job['id'],))
                return False
            saved = db.execute('SELECT revision,dataset_mode FROM staff_settings WHERE manager_id=?', (enrolled['manager_id'],)).fetchone()
            if not saved or saved['dataset_mode']!='demo' or saved['revision']!=revision:
                raise ValueError('Saved settings changed during acknowledgement admission')
            ack = db.execute("SELECT * FROM wa_outbox WHERE job_id=? AND purpose='ack'", (job['id'],)).fetchone()
            origin = db.execute('SELECT source_hash,media_json FROM wa_jobs WHERE id=?', (job['id'],)).fetchone()
            metadata = json.loads(origin['media_json'])
            if ack:
                if origin['source_hash']!=digest:
                    raise ForecastNeedsCorrection('The source file changed after acknowledgement; resend the original complete PDF.')
                if ack['status'] in _ACK_ACCEPTED:
                    metadata['_upload_ack_settings_revision'] = revision
                    db.execute('UPDATE wa_jobs SET media_json=? WHERE id=?', (json.dumps(metadata),job['id']))
                    return True
                blocked_status = 'failed' if ack['status'] in {'failed','cancelled','delivery_failed'} else 'awaiting_ack'
                db.execute("UPDATE wa_jobs SET status=?,lease_until=NULL,error_code=? WHERE id=?", (blocked_status,'ack_'+ack['status'],job['id']))
                return False
            if self._reuse_previous_result(db, job, enrolled, rows, digest, staff, now):
                return False
            # Serializing this lookup with insertion also covers a duplicate file
            # arriving under a DIFFERENT provider message ID before calculation.
            prior = db.execute("SELECT j.id,j.media_json,o.id AS ack_id,o.status AS ack_status,o.provider_message_id FROM wa_jobs j JOIN wa_outbox o ON o.job_id=j.id AND o.purpose='ack' WHERE j.status IN ('awaiting_ack','processing','retry') AND o.status NOT IN ('failed','cancelled','delivery_failed') AND j.tenant_id=? AND j.sender=? AND j.phone_number_id=? AND j.source_hash=? AND j.id<>?", (job['tenant_id'],job['sender'],job['phone_number_id'],digest,job['id'])).fetchall()
            for previous in prior:
                prior_meta=json.loads(previous['media_json'])
                if (prior_meta.get('_enrollment_binding')==self.store.enrollment_binding(enrolled)
                        and prior_meta.get('_upload_ack_settings_revision')==revision):
                    if previous['ack_status']=='outside_window' and previous['provider_message_id'] is None:
                        db.execute("UPDATE wa_outbox SET status='cancelled',error_code='superseded_by_fresh_upload',updated_at=? WHERE id=?", (now,previous['ack_id']))
                        db.execute("UPDATE wa_jobs SET status='failed',error_code='ack_window_expired',lease_until=NULL WHERE id=?", (previous['id'],))
                        metadata['_ack_supersedes_job_id']=previous['id']
                        continue
                    db.execute("UPDATE wa_jobs SET status='duplicate',source_hash=?,source_key=?,lease_until=NULL WHERE id=?", (digest,source_key,job['id']))
                    return False
            metadata['_upload_ack_settings_revision'] = revision
            self.store.add_outbox(db, job, now, UPLOAD_ACK_TEXT, purpose='ack')
            db.execute("UPDATE wa_jobs SET status='awaiting_ack',ack_base_attempts=attempts,source_hash=?,source_key=?,media_json=?,lease_until=NULL WHERE id=?", (digest,source_key,json.dumps(metadata),job['id']))
            return False

    def _reuse_previous_result(self, db, job, enrolled, rows, digest, staff, now):
        duplicate = db.execute("SELECT d.job_id FROM wa_documents d JOIN wa_jobs j ON j.id=d.job_id WHERE d.tenant_id=? AND d.sender=? AND d.source_hash=? AND j.phone_number_id=?", (job["tenant_id"], job["sender"], digest, job["phone_number_id"])).fetchone()
        if duplicate:
            previous_job = db.execute("SELECT media_json,week_start,version FROM wa_jobs WHERE id=?", (duplicate["job_id"],)).fetchone()
            previous_binding = json.loads(previous_job["media_json"]).get("_enrollment_binding") if previous_job else None
            if previous_binding != self.store.enrollment_binding(enrolled):
                raise ForecastNeedsCorrection("This file was previously processed under different hotel setup. Ask the administrator to review the existing version; an old PDF reply will not be reused.")
            previous_roster_row = db.execute("SELECT payload,version FROM weekly_rosters WHERE manager_id=? AND week_start=?", (enrolled["manager_id"], previous_job["week_start"])).fetchone()
            previous_roster = json.loads(previous_roster_row["payload"]) if previous_roster_row else None
            previous_reply = db.execute("SELECT kind,status FROM wa_outbox WHERE job_id=? AND purpose='result'", (duplicate["job_id"],)).fetchone()
            previous_ack = db.execute("SELECT status,tenant_id,phone_number_id,recipient FROM wa_outbox WHERE job_id=? AND purpose='ack'", (duplicate['job_id'],)).fetchone()
            ack_reusable = previous_ack is None or (previous_ack['status'] in _ACK_ACCEPTED
                and previous_ack['tenant_id']==job['tenant_id'] and previous_ack['phone_number_id']==job['phone_number_id']
                and previous_ack['recipient']==job['sender'])
            reusable_reply = ack_reusable and previous_reply and previous_reply["kind"] == "document" and previous_reply["status"] in {"pending", "retry", "outside_window", "sending", "accepted", "sent", "delivered", "read", "reconciliation_required"}
            unchanged = reusable_reply and previous_roster_row and previous_roster_row["version"] == previous_job["version"] and is_roster_current(previous_roster, rows, staff)
            if unchanged:
                # Reopen only a provably-unsent, unchanged result. The same
                # file with changed saved Parameters/staff creates a new version.
                db.execute("UPDATE wa_outbox SET status='pending',error_code=NULL,available_at=?,updated_at=? WHERE job_id=? AND tenant_id=? AND recipient=? AND purpose='result' AND status='outside_window'", (now, now, duplicate["job_id"], job["tenant_id"], job["sender"]))
                db.execute("UPDATE wa_jobs SET status='duplicate',source_hash=?,lease_until=NULL WHERE id=?", (digest, job["id"]))
                return True
        return False

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
            if self._reuse_previous_result(db, job, enrolled, rows, digest, staff, now):
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
            if self.review_service is None:
                queued_origin=db.execute('SELECT media_json FROM wa_jobs WHERE id=?',(item['job_id'],)).fetchone()
                if queued_origin and '_forecast_review_id' in json.loads(queued_origin['media_json']):
                    db.execute("UPDATE wa_outbox SET status='cancelled',error_code='review_disabled',updated_at=? WHERE id=?",(now,item['id']))
                    return True
            if self.review_service is not None and not self.review_service.prompt_dispatch_allowed(db,item,now):
                db.execute("UPDATE wa_outbox SET status='cancelled',error_code='review_prompt_stale',updated_at=? WHERE id=?",(now,item['id']))
                return True
            enrolled = self.store._enrollment(db, item["phone_number_id"], item["recipient"])
            if not enrolled or enrolled["tenant_id"] != item["tenant_id"]:
                db.execute("UPDATE wa_outbox SET status='cancelled',error_code='enrollment_revoked',updated_at=? WHERE id=?", (now, item["id"]))
                self.store.sync_ack_job(db, item, 'cancelled')
                return True
            origin = db.execute("SELECT media_json,week_start,version FROM wa_jobs WHERE id=?", (item["job_id"],)).fetchone()
            if item['purpose'] == 'result':
                ack = db.execute("SELECT status FROM wa_outbox WHERE job_id=? AND purpose='ack'", (item['job_id'],)).fetchone()
                if ack and ack['status'] not in _ACK_ACCEPTED:
                    db.execute("UPDATE wa_outbox SET status='cancelled',error_code='ack_not_accepted',updated_at=? WHERE id=?", (now,item['id']))
                    self.store.sync_ack_job(db, item, 'cancelled')
                    return True
            if item['purpose'] == 'ack':
                metadata=json.loads(origin['media_json']) if origin else {}
                current=db.execute('SELECT revision,dataset_mode FROM staff_settings WHERE manager_id=?', (enrolled['manager_id'],)).fetchone()
                if (metadata.get('_enrollment_binding')!=self.store.enrollment_binding(enrolled)
                        or not current or current['dataset_mode']!='demo'
                        or current['revision']!=metadata.get('_upload_ack_settings_revision')):
                    db.execute("UPDATE wa_outbox SET status='cancelled',error_code='ack_setup_changed',updated_at=? WHERE id=?", (now,item['id']))
                    self.store.sync_ack_job(db, item, 'cancelled')
                    return True
            if item["kind"] == "document":
                binding = json.loads(origin["media_json"]).get("_enrollment_binding") if origin else None
                if binding != self.store.enrollment_binding(enrolled):
                    db.execute("UPDATE wa_outbox SET status='cancelled',error_code='enrollment_changed',updated_at=? WHERE id=?", (now, item["id"]))
                    self.store.sync_ack_job(db, item, 'cancelled')
                    return True
                current = db.execute("SELECT payload,dataset_mode FROM staff_settings WHERE manager_id=?", (enrolled["manager_id"],)).fetchone()
                latest = db.execute("SELECT payload,version FROM weekly_rosters WHERE manager_id=? AND week_start=?", (enrolled["manager_id"], origin["week_start"])).fetchone()
                latest_roster = json.loads(latest["payload"]) if latest else None
                if not current or current["dataset_mode"] != "demo":
                    db.execute("UPDATE wa_outbox SET status='cancelled',error_code='settings_unavailable',updated_at=? WHERE id=?", (now, item["id"]))
                    self.store.sync_ack_job(db, item, 'cancelled')
                    return True
                if not latest or latest["version"] != origin["version"] or not is_roster_current(latest_roster, latest_roster["forecast"], json.loads(current["payload"])):
                    item.update(kind="text", object_key=None, filename=None, text="Saved Parameters, staff or roster changed before this PDF could be sent. No outdated PDF was sent. Please resend the forecast to recalculate using the current saved settings.")
                    db.execute("UPDATE wa_outbox SET kind='text',text=?,object_key=NULL,filename=NULL WHERE id=?", (item["text"], item["id"]))
            provider_phone = getattr(self.provider, "phone_number_id", item["phone_number_id"])
            if provider_phone != item["phone_number_id"]:
                db.execute("UPDATE wa_outbox SET status='cancelled',error_code='provider_number_mismatch',updated_at=? WHERE id=?", (now, item["id"]))
                self.store.sync_ack_job(db, item, 'cancelled')
                return True
            if now - enrolled["last_inbound"] >= 24 * 3600 or enrolled["last_inbound"] > now:
                db.execute("UPDATE wa_outbox SET status='outside_window',error_code='approved_template_required',updated_at=? WHERE id=?", (now, item["id"]))
                self.store.sync_ack_job(db, item, 'outside_window')
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
            retry_allowed = getattr(self.provider, 'allows_automatic_retry', lambda key: True)(item['id'])
            status = "retry" if retry_allowed and item["attempts"] + 1 < 5 else "failed"
            code, message_id = "safe_presend_retry", None
        except AmbiguousSendError:
            status, code, message_id = "reconciliation_required", "send_outcome_unknown", None
        except TwilioRequestRejected as exc:
            status, code, message_id = "failed", exc.safe_diagnostic_code, None
        except ProviderError:
            status, code, message_id = "failed", "provider_rejected", None
        except Exception:
            # Includes process-local failures; conservative because whether the
            # provider accepted a message may no longer be knowable.
            status, code, message_id = "reconciliation_required", "send_outcome_unknown", None
        with self.store._connect() as db:
            db.execute("UPDATE wa_outbox SET status=?,error_code=?,provider_message_id=?,available_at=?,updated_at=? WHERE id=? AND status='sending'", (status, code, message_id, now + min(3600, 30 * 2 ** (item["attempts"] + 1)), now, item["id"]))
            self.store.sync_ack_job(db, item, status)
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
