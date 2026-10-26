"""Durable, single-host DEVELOPMENT repository and private storage adapters.

Not a production database/object store or an authentication system. No public
file URLs are generated. Enrollment is an operator-only Python interface, not a
web endpoint; callers must explicitly provide every hotel and sender mapping.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

from services.state_store import StateStore


_OUTBOX_FIELDS = (
    "id TEXT PRIMARY KEY, job_id TEXT NOT NULL, tenant_id TEXT NOT NULL, "
    "phone_number_id TEXT NOT NULL, recipient TEXT NOT NULL, kind TEXT NOT NULL, "
    "text TEXT NOT NULL, object_key TEXT, filename TEXT, status TEXT NOT NULL DEFAULT 'pending', "
    "attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL, "
    "provider_message_id TEXT, error_code TEXT, updated_at REAL NOT NULL"
)
_OUTBOX_COLUMNS = (
    "id", "job_id", "tenant_id", "phone_number_id", "recipient", "kind", "text",
    "object_key", "filename", "status", "attempts", "available_at",
    "provider_message_id", "error_code", "updated_at",
)
_OUTBOX_SCHEMA = (
    _OUTBOX_FIELDS + ", purpose TEXT NOT NULL DEFAULT 'result' "
    "CHECK(purpose IN ('ack','result')), UNIQUE(job_id,purpose)"
)


def _ensure_outbox_purpose_schema(db):
    """Atomic, serialized upgrade of the known legacy schema; never reset rows.

    The caller holds BEGIN IMMEDIATE. Unknown columns, constraints or dependent
    objects require an operator migration rather than silently dropping them.
    """
    current = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='wa_outbox'").fetchone()
    if current is None:
        db.execute("CREATE TABLE wa_outbox (" + _OUTBOX_SCHEMA + ")")
        return

    def normalized(sql):
        sql = re.sub(r"(?i)^\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+", "CREATE TABLE ", sql)
        tokens = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[^\s'\"]+", sql)
        # Ignore layout/case only outside quoted literals: custom defaults and
        # constraints must not be mistaken for the recognized schema.
        return "".join("wa_outbox" if token == '"wa_outbox"' else token
                       if token.startswith(("'", '"')) else token.lower() for token in tokens)

    legacy_schema = _OUTBOX_FIELDS.replace("job_id TEXT NOT NULL,", "job_id TEXT NOT NULL UNIQUE,", 1)
    expected_legacy = normalized("CREATE TABLE wa_outbox (" + legacy_schema + ")")
    expected_current = normalized("CREATE TABLE wa_outbox (" + _OUTBOX_SCHEMA + ")")
    actual = normalized(current[0])
    columns = tuple(row[1] for row in db.execute("PRAGMA table_xinfo(wa_outbox)"))
    if actual == expected_current and columns == _OUTBOX_COLUMNS + ("purpose",):
        return
    if actual != expected_legacy or columns != _OUTBOX_COLUMNS:
        raise ValueError("Unrecognized outbox schema; operator migration required")

    indexes = set()
    for index in db.execute("PRAGMA index_list(wa_outbox)").fetchall():
        if index[2] != 1 or index[3] not in {"pk", "u"} or index[4] != 0:
            raise ValueError("Custom outbox index requires an operator migration")
        quoted = '"' + index[1].replace('"', '""') + '"'
        indexes.add((index[3], tuple(row[2] for row in db.execute("PRAGMA index_info(" + quoted + ")"))))
    if indexes != {("pk", ("id",)), ("u", ("job_id",))}:
        raise ValueError("Unrecognized outbox indexes; operator migration required")
    for kind, name, sql in db.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('trigger','view')").fetchall():
        if sql and re.search(r"\bwa_outbox\b", sql, re.IGNORECASE):
            raise ValueError("Dependent outbox objects require an operator migration")
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        quoted = '"' + table.replace('"', '""') + '"'
        if any(row[2].lower() == "wa_outbox" for row in db.execute("PRAGMA foreign_key_list(" + quoted + ")")):
            raise ValueError("Outbox foreign keys require an operator migration")
    temporary = "wa_outbox_purpose_migration"
    if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (temporary,)).fetchone():
        raise ValueError("Outbox migration name is already in use")
    db.execute("CREATE TABLE " + temporary + " (" + _OUTBOX_SCHEMA + ")")
    names = ",".join(_OUTBOX_COLUMNS)
    # Preserve implicit rowids as well as every explicit field, ID and counter.
    db.execute("INSERT INTO " + temporary + " (rowid," + names + ",purpose) SELECT rowid," + names + ",'result' FROM wa_outbox")
    db.execute("DROP TABLE wa_outbox")
    db.execute("ALTER TABLE " + temporary + " RENAME TO wa_outbox")


class PrivateFileStore:
    development_only = True

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def put(self, tenant_id: str, content: bytes, suffix: str) -> str:
        if suffix not in {"pdf", "jpg", "png"}:
            raise ValueError("Unsupported private object type")
        namespace = hashlib.sha256(tenant_id.encode()).hexdigest()
        key = f"{namespace}/{hashlib.sha256(content).hexdigest()}.{suffix}"
        path = self.root / key
        path.parent.mkdir(mode=0o700, exist_ok=True)
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("Private object path must not be a symlink")
        # Never expose a partially written final hash path. A crashed writer may
        # leave an unreferenced temp file, but a retry can safely publish again.
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)  # Atomic, no-clobber publication.
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != content:
                    raise ValueError("Private object integrity failure")
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.unlink(temporary)
        return key

    def read(self, tenant_id: str, key: str) -> bytes:
        namespace = hashlib.sha256(tenant_id.encode()).hexdigest()
        if not re.fullmatch(r"[a-f0-9]{64}/[a-f0-9]{64}\.(pdf|jpg|png)", key) or key.split("/")[0] != namespace:
            raise ValueError("Object does not belong to tenant")
        path = self.root / key
        if path.is_symlink() or path.parent.is_symlink() or self.root not in path.resolve().parents:
            raise ValueError("Unsafe private object path")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != path.stem:
            raise ValueError("Private object integrity failure")
        return data


class WorkflowStore(StateStore):
    development_only = True

    @staticmethod
    def _review_command(body):
        if not isinstance(body,str) or len(body)>64:return None
        match=re.fullmatch(r'(CONFIRM|CANCEL)\s+([0-9a-fA-F]{16})',body.strip(),re.I)
        return {'type':'review_command','command':match.group(1).upper(),'review_code':match.group(2).lower()} if match else None

    def __init__(self, path=None):
        super().__init__(path)
        os.chmod(self.path, 0o600)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS wa_tenants (
                    tenant_id TEXT PRIMARY KEY, manager_id TEXT NOT NULL UNIQUE,
                    hotel_code TEXT NOT NULL, hotel_name TEXT NOT NULL, room_capacity INTEGER NOT NULL,
                    report_profile TEXT NOT NULL DEFAULT 'canonical_v1',
                    enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS wa_senders (
                    phone_number_id TEXT NOT NULL, sender TEXT NOT NULL, tenant_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, last_inbound REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(phone_number_id,sender));
                CREATE TABLE IF NOT EXISTS wa_jobs (
                    id TEXT PRIMARY KEY, phone_number_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    sender TEXT NOT NULL, tenant_id TEXT NOT NULL, inbound_at REAL NOT NULL,
                    media_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL, lease_until REAL, claim_token TEXT, source_hash TEXT,
                    source_key TEXT, week_start TEXT, version INTEGER, error_code TEXT,
                    UNIQUE(phone_number_id,message_id));
                CREATE TABLE IF NOT EXISTS wa_documents (
                    tenant_id TEXT NOT NULL, sender TEXT NOT NULL, source_hash TEXT NOT NULL,
                    job_id TEXT NOT NULL, PRIMARY KEY(tenant_id,sender,source_hash));
            """)
            db.execute("BEGIN IMMEDIATE")
            _ensure_outbox_purpose_schema(db)
            job_columns = {row[1] for row in db.execute("PRAGMA table_info(wa_jobs)")}
            if "ack_base_attempts" not in job_columns:
                db.execute("ALTER TABLE wa_jobs ADD COLUMN ack_base_attempts INTEGER NOT NULL DEFAULT 0")
            columns = {row[1] for row in db.execute("PRAGMA table_info(wa_tenants)")}
            if "report_profile" not in columns:
                db.execute("ALTER TABLE wa_tenants ADD COLUMN report_profile TEXT NOT NULL DEFAULT 'canonical_v1'")

    def enroll_tenant(self, *, tenant_id, manager_id, hotel_code, hotel_name, report_profile="canonical_v1"):
        # Never infer a profile from document text, filename or hotel name. The
        # headerless format requires this deliberate operator enrollment value.
        if report_profile not in {"canonical_v1", "hotel_daily_table_v1"}:
            raise ValueError("Choose an explicitly supported report profile")
        for value in (tenant_id, manager_id, hotel_code):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
                raise ValueError("Explicit stable tenant, manager and hotel codes are required")
        if not isinstance(hotel_name, str) or not 1 <= len(hotel_name.strip()) <= 160:
            raise ValueError("Hotel display name required")
        with self._connect() as db:
            # No implicit remapping: changes need an operator migration/review.
            db.execute("INSERT INTO wa_tenants(tenant_id,manager_id,hotel_code,hotel_name,room_capacity,report_profile) VALUES(?,?,?,?,?,?)", (tenant_id, manager_id, hotel_code, hotel_name, 0, report_profile))

    def enroll_sender(self, *, tenant_id, phone_number_id, sender):
        if not isinstance(phone_number_id, str) or not re.fullmatch(r"(?:[0-9]{5,32}|twilio:AC[0-9a-fA-F]{32}:[1-9][0-9]{6,14})", phone_number_id) or not re.fullmatch(r"[0-9]{7,15}", sender):
            raise ValueError("Exact provider-scoped business ID and E.164 sender digits required")
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM wa_tenants WHERE tenant_id=? AND enabled=1", (tenant_id,)).fetchone():
                raise ValueError("Tenant must be explicitly enrolled first")
            db.execute("INSERT INTO wa_senders(phone_number_id,sender,tenant_id) VALUES(?,?,?)", (phone_number_id, sender, tenant_id))

    @staticmethod
    def enrollment_binding(enrollment):
        return {key: enrollment[key] for key in ("tenant_id", "manager_id", "hotel_code", "hotel_name", "report_profile")}

    @staticmethod
    def _enrollment(db, phone_number_id, sender):
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT t.*,s.phone_number_id,s.sender,s.last_inbound FROM wa_senders s JOIN wa_tenants t ON t.tenant_id=s.tenant_id WHERE s.phone_number_id=? AND s.sender=? AND s.enabled=1 AND t.enabled=1", (phone_number_id, sender)).fetchone()
        return dict(row) if row else None

    def enrollment(self, phone_number_id, sender):
        with self._connect() as db:
            return self._enrollment(db, phone_number_id, sender)

    def ingest(self, payload, now: float) -> int:
        """Only validate envelopes and persist small jobs; no downloads/extraction/send."""
        if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
            raise ValueError("Unsupported webhook envelope")
        entries = payload.get("entry", [])
        if not isinstance(entries, list) or len(entries) > 20:
            raise ValueError("Invalid entry list")
        accepted = 0
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("changes", []), list):
                    raise ValueError("Invalid changes")
                for change in entry.get("changes", []):
                    if not isinstance(change, dict) or change.get("field") != "messages":
                        continue
                    value = change.get("value", {})
                    if not isinstance(value, dict) or not isinstance(value.get("metadata", {}), dict):
                        raise ValueError("Invalid message envelope")
                    phone_id = value.get("metadata", {}).get("phone_number_id")
                    if not isinstance(phone_id, str) or not re.fullmatch(r"[0-9]{5,32}", phone_id):
                        continue
                    self._statuses(db, phone_id, value.get("statuses", []), now)
                    messages = value.get("messages", [])
                    if not isinstance(messages, list) or len(messages) > 20:
                        raise ValueError("Invalid message list")
                    for message in messages:
                        if not isinstance(message, dict):
                            raise ValueError("Invalid message")
                        sender, message_id = message.get("from"), message.get("id")
                        if not isinstance(sender, str) or not isinstance(message_id, str) or not 1 <= len(message_id) <= 256:
                            continue
                        enrolled = self._enrollment(db, phone_id, sender)
                        if not enrolled:
                            continue  # No reply or media fetch to unauthorized identities.
                        try:
                            timestamp = float(message["timestamp"])
                            if not 0 < timestamp <= now + 300:
                                continue
                        except (KeyError, ValueError, TypeError, OverflowError):
                            continue
                        kind = message.get("type")
                        media = {"type": "unsupported"}
                        if kind=='text':
                            review=self._review_command(message.get('text',{}).get('body')) if isinstance(message.get('text'),dict) else None
                            if review:media.update(review)
                        if kind in {"document", "image"}:
                            source = message.get(kind, {})
                            if not isinstance(source, dict):
                                continue
                            media_id, mime, digest = source.get("id"), source.get("mime_type"), source.get("sha256")
                            if not isinstance(media_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", media_id):
                                continue
                            try:
                                if not isinstance(digest, str) or len(base64.b64decode(digest, validate=True)) != 32:
                                    continue
                            except (ValueError, TypeError):
                                continue
                            if mime in {"application/pdf", "image/jpeg", "image/png"}:
                                media = {"type": kind, "id": media_id, "mime": mime, "sha256": digest}
                        # Freeze the explicitly enrolled parser profile at intake.
                        # A later setup change requires a resend, not reinterpretation.
                        media["_enrolled_report_profile"] = enrolled["report_profile"]
                        media["_enrollment_binding"] = self.enrollment_binding(enrolled)
                        # Redeliveries must not extend the customer service window.
                        inserted = db.execute("INSERT OR IGNORE INTO wa_jobs(id,phone_number_id,message_id,sender,tenant_id,inbound_at,media_json,available_at) VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, phone_id, message_id, sender, enrolled["tenant_id"], timestamp, json.dumps(media), now)).rowcount
                        if inserted:
                            accepted += 1
                            db.execute("UPDATE wa_senders SET last_inbound=MAX(last_inbound,?) WHERE phone_number_id=? AND sender=?", (min(timestamp, now), phone_id, sender))
        return accepted

    def ingest_twilio(self, form, now: float, *, account_sid: str, business_sender: str, max_jobs=None) -> int:
        """Signed form envelope only. No fetch; receipt time never opens a send window.

        Twilio does not sign a timestamp. The worker must verify the immutable
        Message resource and its real creation time before processing/replying.
        """
        from services.twilio_provider import twilio_business_id
        phone_id = twilio_business_id(account_sid, business_sender)
        if form.get("AccountSid") != account_sid or form.get("To") != "whatsapp:+" + phone_id.rsplit(":", 1)[1]:
            return 0
        sender = form.get("From", "")
        message_id = form.get("MessageSid", "")
        if not re.fullmatch(r"whatsapp:\+[1-9][0-9]{6,14}", sender) or not re.fullmatch(r"(?:SM|MM)[0-9a-fA-F]{32}", message_id):
            raise ValueError("Invalid Twilio message identity")
        if form.get("SmsMessageSid", message_id) != message_id or form.get("SmsSid", message_id) != message_id:
            raise ValueError("Conflicting Twilio message identity")
        sender = sender.removeprefix("whatsapp:+")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            enrolled = self._enrollment(db, phone_id, sender)
            if not enrolled:
                return 0
            if max_jobs is not None and not db.execute("SELECT 1 FROM wa_jobs WHERE phone_number_id=? AND message_id=?", (phone_id, message_id)).fetchone():
                if not isinstance(max_jobs, int) or isinstance(max_jobs, bool) or not 1 <= max_jobs <= 100 or db.execute("SELECT COUNT(*) FROM wa_jobs").fetchone()[0] >= max_jobs:
                    raise ValueError("Pilot intake limit reached")
            media = {"type": "unsupported", "_provider": "twilio"}
            if form.get('NumMedia')=='0':
                review=self._review_command(form.get('Body'))
                if review:media.update(review)
            if form.get("NumMedia") == "1":
                url, mime = form.get("MediaUrl0", ""), form.get("MediaContentType0", "")
                expected = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages/{message_id}/Media/"
                if not url.startswith(expected) or not re.fullmatch(r"ME[0-9a-fA-F]{32}", url[len(expected):]):
                    raise ValueError("Invalid account/message-bound media URL")
                if mime in {"application/pdf", "image/jpeg", "image/png"}:
                    media.update(type="document" if mime == "application/pdf" else "image", id=url, mime=mime)
            elif form.get("NumMedia") != "0":
                raise ValueError("One media attachment per message is required")
            if any(re.fullmatch(r"Media(?:Url|ContentType)[1-9][0-9]*", key) for key in form):
                raise ValueError("Unexpected extra media")
            media["_enrolled_report_profile"] = enrolled["report_profile"]
            media["_enrollment_binding"] = self.enrollment_binding(enrolled)
            return db.execute("INSERT OR IGNORE INTO wa_jobs(id,phone_number_id,message_id,sender,tenant_id,inbound_at,media_json,available_at) VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, phone_id, message_id, sender, enrolled["tenant_id"], now, json.dumps(media), now)).rowcount

    def twilio_status(self, form, now: float, *, account_sid: str, business_sender: str):
        """Signed callbacks reconcile known IDs only, never unknown/lost acknowledgements."""
        from services.twilio_provider import twilio_business_id
        phone_id = twilio_business_id(account_sid, business_sender)
        if form.get("AccountSid") != account_sid or form.get("From") != "whatsapp:+" + phone_id.rsplit(":", 1)[1]:
            return
        recipient, sid = form.get("To", ""), form.get("MessageSid", "")
        if not re.fullmatch(r"whatsapp:\+[1-9][0-9]{6,14}", recipient) or not re.fullmatch(r"(?:SM|MM)[0-9a-fA-F]{32}", sid):
            raise ValueError("Invalid status identity")
        status = form.get("MessageStatus")
        if status == "undelivered":
            status = "failed"
        with self._connect() as db:
            self._statuses(db, phone_id, [{"recipient_id": recipient.removeprefix("whatsapp:+"), "id": sid, "status": status}], now)

    @staticmethod
    def _statuses(db, phone_id, statuses, now):
        if not isinstance(statuses, list) or len(statuses) > 100:
            raise ValueError("Invalid statuses")
        rank = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3}
        for event in statuses:
            if not isinstance(event, dict) or event.get("status") not in {"sent", "delivered", "read", "failed"}:
                continue
            recipient = event.get("recipient_id")
            provider_id = event.get("id")
            callback = event.get("biz_opaque_callback_data", "")
            if not isinstance(recipient, str) or not isinstance(provider_id, str) or not isinstance(callback, str):
                continue
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM wa_outbox WHERE phone_number_id=? AND recipient=? AND (provider_message_id=? OR id=?)", (phone_id, recipient, provider_id, callback)).fetchone()
            if not row or row["status"] not in {"accepted", "sent", "delivered", "read", "sending", "reconciliation_required"}:
                continue
            incoming = event["status"]
            if incoming == "failed" and row["status"] in {"delivered", "read"}:
                continue
            if incoming != "failed" and rank.get(incoming, -1) < rank.get(row["status"], -1):
                continue
            db.execute("UPDATE wa_outbox SET status=?,provider_message_id=?,updated_at=? WHERE id=?", ("delivery_failed" if incoming == "failed" else incoming, provider_id, now, row["id"]))
            WorkflowStore.sync_ack_job(db, row, "delivery_failed" if incoming == "failed" else incoming)

    @staticmethod
    def sync_ack_job(db, item, status):
        if item['purpose'] != 'ack':
            return
        if status in {'failed','cancelled','delivery_failed'}:
            db.execute("UPDATE wa_jobs SET status='failed',error_code=?,lease_until=NULL WHERE id=? AND status IN ('awaiting_ack','processing','retry')", ('ack_'+status,item['job_id']))
        elif status in {'accepted','sent','delivered','read'}:
            db.execute("UPDATE wa_jobs SET error_code=NULL WHERE id=? AND status='awaiting_ack'", (item['job_id'],))
        elif status in {'outside_window','reconciliation_required'}:
            db.execute("UPDATE wa_jobs SET error_code=? WHERE id=? AND status='awaiting_ack'", ('ack_'+status,item['job_id']))

    def claim_job(self, now, phone_number_id=None, *, exclude_twilio=False):
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            exhausted = db.execute("SELECT * FROM wa_jobs WHERE status='processing' AND lease_until<? AND attempts>=5+ack_base_attempts AND (? IS NULL OR phone_number_id=?) AND (?=0 OR phone_number_id NOT LIKE 'twilio:%')", (now,phone_number_id,phone_number_id,exclude_twilio)).fetchall()
            for stale in exhausted:
                ack = db.execute("SELECT status FROM wa_outbox WHERE job_id=? AND purpose='ack'", (stale['id'],)).fetchone()
                reviewed=False
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wa_forecast_reviews'").fetchone():
                    reviewed=bool(db.execute("SELECT 1 FROM wa_forecast_reviews WHERE confirmed_job_id=? AND status='confirmed' AND tenant_id=? AND phone_number_id=? AND sender=?",(stale['id'],stale['tenant_id'],stale['phone_number_id'],stale['sender'])).fetchone())
                if (ack and ack['status'] in {'accepted','sent','delivered','read'}) or reviewed:
                    if not db.execute("SELECT 1 FROM wa_outbox WHERE job_id=? AND purpose='result'", (stale['id'],)).fetchone():
                        self.add_outbox(db, stale, now, "The confirmed forecast calculation could not be completed safely. Please resend for a new review or contact the hotel administrator; no report was generated." if reviewed else "I received your PDF, but the automatic calculation could not be completed safely. Please review the app or contact the hotel administrator; no report will be sent automatically.")
                    terminal = 'needs_correction'
                else:
                    terminal = 'failed'
                db.execute("UPDATE wa_jobs SET status=?,error_code='processing_attempts_exhausted',lease_until=NULL WHERE id=?", (terminal,stale['id']))
            row = db.execute("""SELECT * FROM wa_jobs WHERE (
                (status IN ('pending','retry') AND available_at<=?)
                OR (status='awaiting_ack' AND available_at<=? AND EXISTS (
                    SELECT 1 FROM wa_outbox a WHERE a.job_id=wa_jobs.id AND a.purpose='ack'
                    AND a.status IN ('accepted','sent','delivered','read')
                    AND a.tenant_id=wa_jobs.tenant_id AND a.phone_number_id=wa_jobs.phone_number_id
                    AND a.recipient=wa_jobs.sender))
                OR (status='processing' AND lease_until<?))
                AND attempts<5+ack_base_attempts AND (? IS NULL OR phone_number_id=?)
                AND (?=0 OR phone_number_id NOT LIKE 'twilio:%')
                ORDER BY available_at,id LIMIT 1""", (now, now, now, phone_number_id, phone_number_id, exclude_twilio)).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE wa_jobs SET status='processing',attempts=attempts+1,error_code=NULL,lease_until=?,claim_token=? WHERE id=?", (now + 300, token, row["id"]))
            return dict(row, status="processing", attempts=row["attempts"] + 1, claim_token=token)

    def job_failed(self, job, now, code, retry=True):
        with self._connect() as db:
            db.execute("UPDATE wa_jobs SET status=?,error_code=?,available_at=?,lease_until=NULL WHERE id=? AND claim_token=? AND status='processing'", ("retry" if retry and job["attempts"] < 5 + job.get('ack_base_attempts',0) else "failed", code, now + min(3600, 30 * 2 ** job["attempts"]), job["id"], job["claim_token"]))

    @staticmethod
    def add_outbox(db, job, now, text, object_key=None, filename=None, purpose="result"):
        if not isinstance(purpose, str) or purpose not in {"ack", "result"}:
            raise ValueError("Unsupported outbox purpose")
        identifier = uuid.uuid4().hex
        db.execute("INSERT INTO wa_outbox(id,job_id,tenant_id,phone_number_id,recipient,kind,text,object_key,filename,available_at,updated_at,purpose) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (identifier, job["id"], job["tenant_id"], job["phone_number_id"], job["sender"], "document" if object_key else "text", text, object_key, filename, now, now, purpose))
        return identifier

    def rows(self, table):
        """Operator/test inspection only; never exposed by the public HTTP API."""
        if table not in {"wa_jobs", "wa_outbox", "wa_documents"}:
            raise ValueError("Unsupported inspection table")
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
