"""Short-lived, bounded PDF capability grants for the synthetic Twilio adapter.

This is a DEVELOPMENT adapter, not public file hosting. The API must additionally
restrict its route to the explicitly enabled loopback-only synthetic experiment.
A production implementation needs reviewed private storage, routing and logging
controls. URLs are bearer capabilities: never log their path or retain them in
analytics. Only token hashes are persisted here; source objects stay private.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from services.weekly_roster import is_roster_current


class MediaGrantError(ValueError):
    """Generic denial deliberately excludes file, tenant, token and URL details."""


class TwilioMediaPublisher:
    development_only = True

    def __init__(self, store, files, base_url, clock=time.time, ttl_seconds=600, max_gets=4):
        try:
            parsed = urlsplit(base_url)
            valid_url = (parsed.scheme == "https" and parsed.hostname
                         and parsed.username is None and parsed.password is None
                         and parsed.port in {None, 443} and parsed.path in {"", "/"}
                         and not parsed.query and not parsed.fragment
                         and not any(c.isspace() for c in base_url))
        except (TypeError, ValueError):
            valid_url = False
        if not valid_url:
            raise MediaGrantError("An explicit HTTPS origin is required.")
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int)
                or not 1 <= ttl_seconds <= 600 or isinstance(max_gets, bool)
                or not isinstance(max_gets, int) or not 1 <= max_gets <= 8):
            raise MediaGrantError("Invalid development media grant limits.")
        if not getattr(store, "development_only", False) or not getattr(files, "development_only", False):
            raise MediaGrantError("This publisher supports development adapters only.")
        self.store, self.files, self.base_url = store, files, base_url.rstrip("/")
        self.clock, self.ttl_seconds, self.max_gets = clock, ttl_seconds, max_gets
        with self.store._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS wa_twilio_media_grants (
                token_hash TEXT PRIMARY KEY, outbox_id TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL, object_key TEXT NOT NULL, filename TEXT NOT NULL,
                content_sha256 TEXT NOT NULL, expires_at REAL NOT NULL,
                gets INTEGER NOT NULL DEFAULT 0, max_gets INTEGER NOT NULL)""")

    @staticmethod
    def _deny():
        raise MediaGrantError("PDF media grant is unavailable.")

    def _context(self, db, outbox_id, now, *, publishing=False):
        """Resolve current identity and saved-result state within the caller's lock."""
        allowed = {"sending"} if publishing else {"sending", "accepted", "sent", "delivered", "read", "reconciliation_required"}
        return self._bound_context(db, outbox_id, now, allowed)

    def _bound_context(self, db, outbox_id, now, allowed):
        """Internal identity/result validation; callers supply explicit states."""
        item = db.execute("SELECT * FROM wa_outbox WHERE id=?", (outbox_id,)).fetchone()
        if not item or item["kind"] != "document" or item["status"] not in allowed:
            self._deny()
        origin = db.execute("SELECT * FROM wa_jobs WHERE id=?", (item["job_id"],)).fetchone()
        if not origin or origin["status"] not in {"calculated", "needs_attention"} or any(origin[left] != item[right] for left, right in (
                ("tenant_id", "tenant_id"), ("phone_number_id", "phone_number_id"), ("sender", "recipient"))):
            self._deny()
        # A Meta outbox cannot be published accidentally through this transport.
        if not re.fullmatch(r"twilio:AC[0-9a-fA-F]{32}:[1-9][0-9]{6,14}", item["phone_number_id"]):
            self._deny()
        enrolled = self.store._enrollment(db, item["phone_number_id"], item["recipient"])
        if (not enrolled or enrolled["tenant_id"] != item["tenant_id"]
                or not 0 <= now - enrolled["last_inbound"] < 86400):
            self._deny()
        try:
            binding = json.loads(origin["media_json"]).get("_enrollment_binding")
            if binding != self.store.enrollment_binding(enrolled):
                self._deny()
            saved = db.execute("SELECT payload,dataset_mode FROM staff_settings WHERE manager_id=?", (enrolled["manager_id"],)).fetchone()
            latest = db.execute("SELECT payload,version FROM weekly_rosters WHERE manager_id=? AND week_start=?", (enrolled["manager_id"], origin["week_start"])).fetchone()
            if not saved or saved["dataset_mode"] != "demo" or not latest or latest["version"] != origin["version"]:
                self._deny()
            roster = json.loads(latest["payload"])
            if (roster.get("source", {}).get("job_id") != origin["id"]
                    or roster.get("source", {}).get("dataset_mode") != "demo"
                    or not is_roster_current(roster, roster["forecast"], json.loads(saved["payload"]))):
                self._deny()
        except (KeyError, TypeError, json.JSONDecodeError):
            self._deny()
        return item, enrolled

    def publish(self, pdf_bytes, filename, idempotency_key):
        if (not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-")
                or len(pdf_bytes) > 10 * 1024 * 1024
                or not isinstance(filename, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}\.pdf", filename)
                or not isinstance(idempotency_key, str)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key)):
            self._deny()
        now = self.clock()
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            item, enrolled = self._context(db, idempotency_key, now, publishing=True)
            if item["filename"] != filename:
                self._deny()
            try:
                private = self.files.read(item["tenant_id"], item["object_key"])
            except (ValueError, OSError):
                self._deny()
            if not hmac.compare_digest(private, pdf_bytes):
                self._deny()
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(pdf_bytes).hexdigest()
            # Replacement invalidates any prior capability after a safe pre-send
            # retry. Ambiguous sends are never retried by the workflow.
            db.execute("DELETE FROM wa_twilio_media_grants WHERE outbox_id=?", (idempotency_key,))
            db.execute("""INSERT INTO wa_twilio_media_grants
                (token_hash,outbox_id,tenant_id,object_key,filename,content_sha256,expires_at,max_gets)
                VALUES(?,?,?,?,?,?,?,?)""", (hashlib.sha256(token.encode()).hexdigest(), idempotency_key,
                    item["tenant_id"], item["object_key"], filename, digest,
                    min(now + self.ttl_seconds, enrolled["last_inbound"] + 86400), self.max_gets))
        return f"{self.base_url}/media/twilio/{token}/{filename}"

    def get(self, token, filename, *, head=False):
        if (not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
                or not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}\.pdf", filename)
                or not isinstance(head, bool)):
            self._deny()
        now = self.clock()
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            # Serialize grant accounting with revocation/settings/roster writes.
            db.execute("BEGIN IMMEDIATE")
            grant = db.execute("SELECT * FROM wa_twilio_media_grants WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if not grant or grant["filename"] != filename or not now < grant["expires_at"] or grant["gets"] >= grant["max_gets"]:
                self._deny()
            item, _ = self._context(db, grant["outbox_id"], now)
            if any(item[key] != grant[key] for key in ("tenant_id", "object_key", "filename")):
                self._deny()
            try:
                data = self.files.read(grant["tenant_id"], grant["object_key"])
            except (ValueError, OSError):
                self._deny()
            if (not data.startswith(b"%PDF-") or len(data) > 10 * 1024 * 1024
                    or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), grant["content_sha256"])):
                self._deny()
            if not head:
                db.execute("UPDATE wa_twilio_media_grants SET gets=gets+1 WHERE token_hash=?", (grant["token_hash"],))
        return data
