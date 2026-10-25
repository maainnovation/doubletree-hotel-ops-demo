"""Operator-created, one-download synthetic PDF probes; never a messaging path.

No public mint/admin endpoint. Empty probe state is the default. Probe creation
requires the existing isolated pilot's terminal failed document and exhausted
three-attempt budget. It never changes delivery state, grants or reservations.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import time

from services.twilio_media import MediaGrantError, TwilioMediaPublisher


class PilotDiagnosticMediaPublisher(TwilioMediaPublisher):
    def __init__(self, store, files, base_url, *, approved_until, clock=time.time):
        if (type(approved_until) not in {int,float} or not math.isfinite(approved_until)
                or not 0 < approved_until-clock() <= 86400):
            raise MediaGrantError('A current bounded pilot approval is required.')
        super().__init__(store,files,base_url,clock=clock)
        self.approved_until=approved_until
        with self.store._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS pilot_media_probes (
                token_hash TEXT PRIMARY KEY, outbox_id TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL, object_key TEXT NOT NULL, filename TEXT NOT NULL,
                content_sha256 TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                gets INTEGER NOT NULL DEFAULT 0 CHECK(gets IN (0,1)), revoked_at REAL)''')

    def _probe_context(self,db,outbox_id,now):
        # Imported here to avoid a circular module dependency at pilot startup.
        from services.twilio_pilot import TENANT, MAX_SEND_ATTEMPTS
        if now >= self.approved_until:
            self._deny()
        item,enrolled=self._bound_context(db,outbox_id,now,{'failed'})
        if (item['tenant_id']!=TENANT or item['attempts']!=MAX_SEND_ATTEMPTS
                or item['provider_message_id'] is not None
                or db.execute('SELECT COUNT(*) FROM pilot_send_attempts').fetchone()[0]!=MAX_SEND_ATTEMPTS
                or db.execute('SELECT COUNT(*) FROM pilot_send_attempts WHERE outbox_id=?',(outbox_id,)).fetchone()[0]!=MAX_SEND_ATTEMPTS):
            self._deny()
        return item,enrolled

    def enable_diagnostic_probe(self,outbox_id):
        """Explicit operator action after approval; returned capability stays private."""
        if not isinstance(outbox_id,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',outbox_id):
            self._deny()
        now=self.clock()
        with self.store._connect() as db:
            db.row_factory=sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            item,enrolled=self._probe_context(db,outbox_id,now)
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}\.pdf',item['filename'] or ''):
                self._deny()
            # One audited probe per original outbox. Revocation does not erase
            # this record or silently permit repeated new grants.
            if db.execute('SELECT 1 FROM pilot_media_probes WHERE outbox_id=?',(outbox_id,)).fetchone():
                self._deny()
            try:
                data=self.files.read(item['tenant_id'],item['object_key'])
            except (ValueError,OSError):
                self._deny()
            if not data.startswith(b'%PDF-') or len(data)>10*1024*1024:
                self._deny()
            token=secrets.token_urlsafe(32)
            db.execute('''INSERT INTO pilot_media_probes
                (token_hash,outbox_id,tenant_id,object_key,filename,content_sha256,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?)''',(hashlib.sha256(token.encode()).hexdigest(),outbox_id,
                    item['tenant_id'],item['object_key'],item['filename'],hashlib.sha256(data).hexdigest(),now,
                    min(now+600,self.approved_until,enrolled['last_inbound']+86400)))
        return f"{self.base_url}/media/twilio/{token}/{item['filename']}"

    def revoke_diagnostic_probe(self,token):
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):
            self._deny()
        with self.store._connect() as db:
            return db.execute('UPDATE pilot_media_probes SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL',
                              (self.clock(),hashlib.sha256(token.encode()).hexdigest())).rowcount==1

    def get(self,token,filename,*,head=False):
        try:
            return super().get(token,filename,head=head)
        except MediaGrantError:
            pass
        if (not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token)
                or not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}\.pdf',filename)
                or not isinstance(head,bool)):
            self._deny()
        now=self.clock()
        with self.store._connect() as db:
            db.row_factory=sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM pilot_media_probes WHERE token_hash=?',
                           (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if (not row or row['filename']!=filename or row['revoked_at'] is not None
                    or not now < row['expires_at'] or row['gets']!=0):
                self._deny()
            item,_=self._probe_context(db,row['outbox_id'],now)
            if any(item[key]!=row[key] for key in ('tenant_id','object_key','filename')):
                self._deny()
            try:
                data=self.files.read(row['tenant_id'],row['object_key'])
            except (ValueError,OSError):
                self._deny()
            if (not data.startswith(b'%PDF-') or len(data)>10*1024*1024
                    or not hmac.compare_digest(hashlib.sha256(data).hexdigest(),row['content_sha256'])):
                self._deny()
            if not head:
                db.execute('UPDATE pilot_media_probes SET gets=1 WHERE token_hash=?',(row['token_hash'],))
        return data
