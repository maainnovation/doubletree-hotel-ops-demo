"""Explicit owner-only, review-aware pilot authorization; disabled by default.

An operator-approved epoch is independent of the unchanged historical pilot.
Every new inbound is bound at intake and every outgoing API attempt is reserved
once before transport. This is a bounded demo-staff experiment, not operational
hotel authorization. A monitored USD budget is not a provider billing hard cap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit

import httpx

from services.hotel_parameters import saved_room_capacity
from services.staff_settings import validate_staff_settings
from services.twilio_media import TwilioMediaPublisher
from services.twilio_pilot import (PilotConfig, PilotConfigurationError, TENANT, MANAGER, HOTEL_CODE,
    HOTEL_NAME, _persistent_mount, synthetic_forecast)
from services.twilio_provider import TwilioWhatsAppProvider, validate_twilio_media_url
from services.twilio_run_budget import _identity
from services.whatsapp_provider import ProviderError
from services.whatsapp_store import WorkflowStore, PrivateFileStore
from services.private_storage_budget import QuotaPrivateFileStore

MAX_SECONDS = 240 * 3600
MAX_ATTEMPTS = 500
MAX_UPLOADS = 1000
MAX_COMMANDS = 2000
MAX_INTAKES_PER_MINUTE = 20
_ACCEPTED = {'accepted', 'sent', 'delivered', 'read'}
# Exact internal notice emitted when dispatch refuses an outdated roster PDF.
# Any different text remains ineligible while the source job is calculated.
_STALE_RESULT_NOTICE = ('Saved Parameters, staff or roster changed before this PDF could be sent. '
                        'No outdated PDF was sent. Please resend the forecast to recalculate using the current saved settings.')
_MIMES = {'application/pdf': 'pdf', 'image/png': 'png', 'image/jpeg': 'jpg'}
_TABLES = ('pilot_review_authorizations', 'pilot_review_uploads', 'pilot_review_commands',
           'pilot_review_verified', 'pilot_review_sources', 'pilot_review_attempts', 'pilot_review_revocations')


def _deny():
    raise ProviderError('Owner review pilot is not authorized for this operation.')


def _number(value):
    return type(value) in {int, float} and math.isfinite(value)


def _inputs(identifier, now):
    if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', identifier) or not _number(now):
        _deny()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _legacy_config(config):
    # Legacy expiry is identity, NOT authorization for this new epoch. Validate
    # structure without pretending an expired old approval is currently active.
    if not isinstance(config, PilotConfig) or not _number(config.approved_until) or config.approved_until <= 0:
        _deny()
    try:
        parsed = urlsplit(config.origin)
        if (parsed.scheme != 'https' or not re.fullmatch(r'[a-z0-9-]+\.onrender\.com', parsed.netloc)
                or parsed.path or parsed.query or parsed.fragment
                or not re.fullmatch(r'[1-9][0-9]{6,14}', config.manager_phone)
                or not isinstance(config.auth_token, str) or not 1 <= len(config.auth_token) <= 256
                or not config.auth_token.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in config.auth_token)
                or config.week_start.weekday() != 4 or not config.data_root.is_absolute()
                or '..' in config.data_root.parts):
            _deny()
        config.phone_id
    except (ValueError, TypeError, AttributeError):
        _deny()


def _open_existing(config, *, mocked):
    """Reuse legacy mount/link/identity protections, never seed or remap state."""
    _legacy_config(config)
    root = config.data_root
    if not mocked and not _persistent_mount(root):
        raise PilotConfigurationError('Owner review requires the existing dedicated persistent volume.')
    if not root.is_dir() or root.is_symlink() or any(p.is_symlink() for p in root.parents):
        raise PilotConfigurationError('Existing private pilot storage is required.')
    for name in ('.init.lock', 'pilot.sqlite3', 'pilot.sqlite3-wal', 'pilot.sqlite3-shm', 'pilot.sqlite3-journal', 'private'):
        path = root / name
        if path.is_symlink() or (path.exists() and path.is_file() and path.stat().st_nlink != 1):
            raise PilotConfigurationError('Pilot state and lock files must not be links.')
    if not (root / 'pilot.sqlite3').is_file() or not (root / '.init.lock').is_file():
        raise PilotConfigurationError('An existing initialized pilot database is required.')
    identity, _ = _identity(config)
    fd = os.open(root / '.init.lock', os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with sqlite3.connect(root / 'pilot.sqlite3') as db:
            try:
                prior = db.execute('SELECT identity FROM pilot_identity WHERE id=1').fetchone()
            except sqlite3.Error:
                _deny()
            if not prior or prior[0] != identity:
                _deny()
        store = WorkflowStore(root / 'pilot.sqlite3')
        files = QuotaPrivateFileStore(root / 'private')
    return store, files


def _schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS pilot_review_authorizations (
        approval_id TEXT PRIMARY KEY, legacy_identity TEXT NOT NULL, approved_at REAL NOT NULL,
        expires_at REAL NOT NULL, minimum_job_rowid INTEGER NOT NULL, enrollment_json TEXT NOT NULL,
        previous_profile TEXT NOT NULL, report_profile TEXT NOT NULL, input_policy TEXT NOT NULL,
        initial_settings_revision INTEGER NOT NULL, max_attempts INTEGER NOT NULL,
        max_uploads INTEGER NOT NULL, max_commands INTEGER NOT NULL, rate_limit INTEGER NOT NULL,
        monitored_budget_usd REAL NOT NULL, fixture_week TEXT NOT NULL,
        CHECK(expires_at>approved_at AND expires_at<=approved_at+864000),
        CHECK(max_attempts BETWEEN 1 AND 500), CHECK(max_uploads BETWEEN 1 AND 1000),
        CHECK(max_commands BETWEEN 1 AND 2000), CHECK(rate_limit BETWEEN 1 AND 20))''')
    db.execute('''CREATE TABLE IF NOT EXISTS pilot_review_uploads (
        approval_id TEXT NOT NULL, job_id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
        mime TEXT NOT NULL, media_url TEXT NOT NULL, admitted_at REAL NOT NULL,
        UNIQUE(approval_id,message_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS pilot_review_commands (
        approval_id TEXT NOT NULL, job_id TEXT PRIMARY KEY, original_job_id TEXT NOT NULL UNIQUE,
        review_id TEXT NOT NULL UNIQUE, command TEXT NOT NULL, admitted_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS pilot_review_verified (
        approval_id TEXT NOT NULL, job_id TEXT PRIMARY KEY, created_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS pilot_review_sources (
        approval_id TEXT NOT NULL, job_id TEXT PRIMARY KEY, source_hash TEXT NOT NULL, mime TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS pilot_review_attempts (
        approval_id TEXT NOT NULL, original_job_id TEXT NOT NULL, phase TEXT NOT NULL CHECK(phase IN ('first','final')),
        outbox_id TEXT NOT NULL UNIQUE, job_id TEXT NOT NULL, kind TEXT NOT NULL, reserved_at REAL NOT NULL,
        PRIMARY KEY(approval_id,original_job_id,phase))''')
    db.execute('''CREATE TABLE IF NOT EXISTS pilot_review_revocations (
        approval_id TEXT PRIMARY KEY, revoked_at REAL NOT NULL)''')
    for table in _TABLES:
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'Owner review audit is append-only'); END")


def _context(db, store, config, expected_profile=None):
    identity, _ = _identity(config)
    prior = db.execute('SELECT identity FROM pilot_identity WHERE id=1').fetchone()
    enrolled = store._enrollment(db, config.phone_id, config.manager_phone)
    if (not prior or prior[0] != identity or not enrolled
            or db.execute('SELECT COUNT(*) FROM pilot_send_attempts').fetchone()[0] != 3
            or any(enrolled[key] != value for key, value in {
                'tenant_id': TENANT, 'manager_id': MANAGER, 'hotel_code': HOTEL_CODE, 'hotel_name': HOTEL_NAME}.items())
            or (expected_profile is not None and enrolled['report_profile'] != expected_profile)):
        _deny()
    saved = db.execute('SELECT payload,dataset_mode,revision FROM staff_settings WHERE manager_id=?', (MANAGER,)).fetchone()
    if not saved or saved['dataset_mode'] != 'demo' or type(saved['revision']) is not int or saved['revision'] < 1:
        _deny()
    settings = json.loads(saved['payload'])
    if not isinstance(settings, dict):
        _deny()
    # Identity is not calculation readiness. Keep owner repair access when a
    # saved demo parameter is temporarily incomplete; worker validators and
    # current-result publication still reject invalid inputs.
    return enrolled, saved['revision'], settings


def create_review_pilot_approval(store, config, approval_id, now, *, report_profile,
        input_policy='synthetic_exact', expires_at=None, max_attempts=MAX_ATTEMPTS,
        max_uploads=MAX_UPLOADS, max_commands=MAX_COMMANDS, rate_limit=MAX_INTAKES_PER_MINUTE,
        monitored_budget_usd=10):
    """Operator only, following explicit approval; no transport or implicit seed."""
    _legacy_config(config)
    _inputs(approval_id, now)
    if approval_id in {config.auth_token,config.account_sid}:
        _deny()
    expires = now + MAX_SECONDS if expires_at is None else expires_at
    if (not _number(expires) or not 0 < expires - now <= MAX_SECONDS
            or report_profile not in {'canonical_v1', 'hotel_daily_table_v1'}
            or input_policy not in {'synthetic_exact', 'redacted_owner_reports'}
            or any(type(value) is not int or not 1 <= value <= bound for value, bound in (
                (max_attempts, MAX_ATTEMPTS), (max_uploads, MAX_UPLOADS), (max_commands, MAX_COMMANDS), (rate_limit, MAX_INTAKES_PER_MINUTE)))
            or not _number(monitored_budget_usd) or not 0 < monitored_budget_usd <= 10
            or not getattr(store, 'development_only', False)
            or Path(store.path).resolve() != (config.data_root / 'pilot.sqlite3').resolve()):
        _deny()
    identity, _ = _identity(config)
    today = datetime.fromtimestamp(now, timezone.utc).date()
    week = today - timedelta(days=(today.weekday() - 4) % 7)
    try:
        with store._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            enrolled, revision, settings = _context(db, store, config)
            if validate_staff_settings(settings):
                _deny()
            saved_room_capacity(settings, required=True)
            # No unresolved previous provider attempt is converted into new send
            # permission. Prior jobs/counters remain untouched.
            if db.execute("SELECT 1 FROM wa_outbox WHERE status IN ('pending','retry','sending','reconciliation_required','outside_window')").fetchone():
                _deny()
            if db.execute("SELECT 1 FROM wa_jobs WHERE status IN ('pending','retry','processing','awaiting_ack','awaiting_review')").fetchone():
                _deny()
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='pilot_run_authorizations'").fetchone():
                for prior in db.execute('SELECT run_id,max_attempts FROM pilot_run_authorizations WHERE expires_at>?', (now,)):
                    count = db.execute('SELECT COUNT(*) FROM pilot_run_attempts WHERE run_id=?', (prior['run_id'],)).fetchone()[0]
                    if count < prior['max_attempts']:
                        _deny()
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='pilot_text_diagnostics'").fetchone():
                if db.execute("SELECT 1 FROM pilot_text_diagnostics WHERE status IN ('reserved_no_retry','uncertain_no_retry')").fetchone():
                    _deny()
            _schema(db)
            if db.execute('SELECT 1 FROM pilot_review_authorizations a WHERE expires_at>? AND NOT EXISTS (SELECT 1 FROM pilot_review_revocations r WHERE r.approval_id=a.approval_id)', (now,)).fetchone():
                _deny()
            minimum = db.execute('SELECT COALESCE(MAX(rowid),0) FROM wa_jobs').fetchone()[0]
            previous = enrolled['report_profile']
            binding = store.enrollment_binding(dict(enrolled, report_profile=report_profile))
            db.execute('''INSERT INTO pilot_review_authorizations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (approval_id, identity, now, expires, minimum, _json(binding), previous, report_profile, input_policy,
                 revision, max_attempts, max_uploads, max_commands, rate_limit, monitored_budget_usd, week.isoformat()))
            db.execute('UPDATE wa_tenants SET report_profile=? WHERE tenant_id=? AND report_profile=?', (report_profile, TENANT, previous))
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        _deny()
    return {'approval_id': approval_id, 'approved_at': now, 'expires_at': expires, 'max_attempts': max_attempts,
            'max_uploads': max_uploads, 'max_commands': max_commands, 'rate_limit': rate_limit,
            'report_profile': report_profile, 'input_policy': input_policy, 'monitored_budget_usd': monitored_budget_usd}


def revoke_review_pilot(store, approval_id, now):
    _inputs(approval_id, now)
    with store._connect() as db:
        db.execute('INSERT INTO pilot_review_revocations(approval_id,revoked_at) VALUES(?,?)', (approval_id, now))


class ReviewPilotBudget:
    def __init__(self, store, files, config, approval_id, *, clock=time.time, cost_gate=None):
        self.store, self.files, self.config, self.approval_id = store, files, config, approval_id
        self.clock, self.cost_gate = clock, cost_gate
        _legacy_config(config)
        if (Path(store.path).resolve() != (config.data_root / 'pilot.sqlite3').resolve()
                or Path(files.root).resolve() != (config.data_root / 'private').resolve()):
            _deny()
        self.assert_active()
        from services.whatsapp_review import WhatsAppReviewService
        self.review_service = WhatsAppReviewService(store, files)

    def _record(self, db, now):
        _inputs(self.approval_id, now)
        row = db.execute('SELECT * FROM pilot_review_authorizations WHERE approval_id=?', (self.approval_id,)).fetchone()
        identity, _ = _identity(self.config)
        if (not row or row['legacy_identity'] != identity or not row['approved_at'] <= now < row['expires_at']
                or not 0 < row['expires_at'] - row['approved_at'] <= MAX_SECONDS
                or row['input_policy'] not in {'synthetic_exact','redacted_owner_reports'}
                or row['report_profile'] not in {'canonical_v1','hotel_daily_table_v1'}
                or any(type(row[key]) is not int or not 1<=row[key]<=bound for key,bound in (
                    ('max_attempts',MAX_ATTEMPTS),('max_uploads',MAX_UPLOADS),('max_commands',MAX_COMMANDS),('rate_limit',MAX_INTAKES_PER_MINUTE)))
                or type(row['minimum_job_rowid']) is not int or row['minimum_job_rowid']<0
                or not _number(row['monitored_budget_usd']) or not 0<row['monitored_budget_usd']<=10
                or db.execute('SELECT 1 FROM pilot_review_revocations WHERE approval_id=?', (self.approval_id,)).fetchone()):
            _deny()
        enrolled, revision, settings = _context(db, self.store, self.config, row['report_profile'])
        if _json(self.store.enrollment_binding(enrolled)) != row['enrollment_json'] or revision < row['initial_settings_revision']:
            _deny()
        return row, enrolled, revision

    def assert_active(self):
        try:
            with self.store._connect() as db:
                db.row_factory = sqlite3.Row
                return dict(self._record(db, self.clock())[0])
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            _deny()

    def admit_inbound(self, db, job, now):
        """Called BEFORE job INSERT inside the store's BEGIN IMMEDIATE lock."""
        try:
            approval, enrolled, _ = self._record(db, now)
            if (job['tenant_id'] != TENANT or job['phone_number_id'] != self.config.phone_id
                    or job['sender'] != self.config.manager_phone or job['inbound_at'] != now
                    or not re.fullmatch(r'[a-f0-9]{32}', job['id'])
                    or not re.fullmatch(r'(?:SM|MM)[a-fA-F0-9]{32}', job['message_id'])):
                return False
            metadata = json.loads(job['media_json'])
            if (metadata.get('_provider') != 'twilio' or metadata.get('_enrollment_binding') != self.store.enrollment_binding(enrolled)
                    or '_pilot_attestation' in metadata
                    or db.execute('SELECT 1 FROM wa_jobs WHERE id=? OR (phone_number_id=? AND message_id=?)', (job['id'], job['phone_number_id'], job['message_id'])).fetchone()):
                return False
            if db.execute('SELECT COUNT(*) FROM pilot_review_attempts WHERE approval_id=?', (self.approval_id,)).fetchone()[0] >= approval['max_attempts']:
                return False
            recent = sum(db.execute(f'SELECT COUNT(*) FROM {table} WHERE approval_id=? AND admitted_at>?', (self.approval_id, now-60)).fetchone()[0]
                         for table in ('pilot_review_uploads', 'pilot_review_commands'))
            if recent >= approval['rate_limit']:
                return False
            if metadata.get('type') in {'document', 'image'}:
                if metadata.get('mime') not in _MIMES or (metadata['mime'] == 'application/pdf') != (metadata['type'] == 'document'):
                    return False
                if not validate_twilio_media_url(metadata.get('id'), self.config.account_sid, message_sid=job['message_id']):
                    return False
                if db.execute('SELECT COUNT(*) FROM pilot_review_uploads WHERE approval_id=?', (self.approval_id,)).fetchone()[0] >= approval['max_uploads']:
                    return False
                db.execute('INSERT INTO pilot_review_uploads VALUES(?,?,?,?,?,?)',
                           (self.approval_id, job['id'], job['message_id'], metadata['mime'], metadata['id'], now))
                return True
            if metadata.get('type') != 'review_command' or metadata.get('command') not in {'CONFIRM','CANCEL'} or not re.fullmatch(r'[a-f0-9]{16}', metadata.get('review_code', '')):
                return False
            if db.execute('SELECT COUNT(*) FROM pilot_review_commands WHERE approval_id=?', (self.approval_id,)).fetchone()[0] >= approval['max_commands']:
                return False
            review = db.execute('''SELECT r.* FROM wa_forecast_reviews r JOIN pilot_review_uploads u ON u.job_id=r.original_job_id
                WHERE u.approval_id=? AND r.token_hash=? AND r.tenant_id=? AND r.phone_number_id=? AND r.sender=?''',
                (self.approval_id, hashlib.sha256(metadata['review_code'].encode()).hexdigest(), TENANT, self.config.phone_id, self.config.manager_phone)).fetchone()
            if not review or review['status'] != 'pending' or not review['created_at'] <= now < review['expires_at']:
                return False
            first = db.execute('''SELECT o.* FROM pilot_review_attempts a JOIN wa_outbox o ON o.id=a.outbox_id
                WHERE a.approval_id=? AND a.original_job_id=? AND a.phase='first' AND a.kind='review' ''',
                (self.approval_id, review['original_job_id'])).fetchone()
            if (not first or first['id'] != review['prompt_outbox_id'] or first['status'] not in _ACCEPTED
                    or not re.fullmatch(r'(?:SM|MM)[0-9a-fA-F]{32}',first['provider_message_id'] or '')):
                return False
            db.execute('INSERT INTO pilot_review_commands VALUES(?,?,?,?,?,?)',
                       (self.approval_id, job['id'], review['original_job_id'], review['review_id'], metadata['command'], now))
            return True
        except (sqlite3.Error, ValueError, TypeError, KeyError, ProviderError):
            return False

    def claimed(self, db, message_sid, sender, now):
        approval, enrolled, _ = self._record(db, now)
        job = db.execute("SELECT rowid AS intake_rowid,* FROM wa_jobs WHERE phone_number_id=? AND message_id=? AND sender=? AND status='processing'",
                         (self.config.phone_id, message_sid, sender)).fetchone()
        if (not job or not job['claim_token'] or job['tenant_id'] != TENANT or job['sender'] != self.config.manager_phone
                or job['intake_rowid'] <= approval['minimum_job_rowid']):
            _deny()
        upload = db.execute('SELECT * FROM pilot_review_uploads WHERE approval_id=? AND job_id=?', (self.approval_id, job['id'])).fetchone()
        command = db.execute('SELECT * FROM pilot_review_commands WHERE approval_id=? AND job_id=?', (self.approval_id, job['id'])).fetchone()
        metadata = json.loads(job['media_json'])
        if (bool(upload) == bool(command) or metadata.get('_provider') != 'twilio'
                or metadata.get('_enrollment_binding') != self.store.enrollment_binding(enrolled) or '_pilot_attestation' in metadata):
            _deny()
        return job, upload, command, approval

    def _source_ok(self, db, job, upload):
        source = db.execute('SELECT * FROM pilot_review_sources WHERE approval_id=? AND job_id=?', (self.approval_id, upload['job_id'])).fetchone()
        if not source or job['source_hash'] != source['source_hash'] or source['mime'] != upload['mime']:
            _deny()
        expected = hashlib.sha256(TENANT.encode()).hexdigest() + '/' + source['source_hash'] + '.' + _MIMES[source['mime']]
        if job['source_key'] != expected or hashlib.sha256(self.files.read(TENANT, expected)).hexdigest() != source['source_hash']:
            _deny()

    def reserve(self, key, now, *, kind, text=None):
        # Guarded cached pricing and the next outbound reservation share this
        # write lock. The callback MUST be local/read-only: polling is a separate
        # supervised worker action, never a webhook or send-transaction request.
        now = self.clock()
        try:
            with self.store._connect() as db:
                db.row_factory = sqlite3.Row
                db.execute('BEGIN IMMEDIATE')
                approval, enrolled, revision = self._record(db, now)
                try:
                    if self.cost_gate is None or self.cost_gate(db, now) is not True:
                        _deny()
                except Exception:
                    _deny()
                now = self.clock()
                approval, enrolled, revision = self._record(db, now)
                item = db.execute('SELECT * FROM wa_outbox WHERE id=?', (key,)).fetchone()
                if (not item or item['status'] != 'sending' or item['attempts'] != 1 or item['provider_message_id'] is not None
                        or item['tenant_id'] != TENANT or item['phone_number_id'] != self.config.phone_id
                        or item['recipient'] != self.config.manager_phone or item['kind'] != kind
                        or (kind == 'text' and text != item['text']) or not 0 <= now - enrolled['last_inbound'] < 86400):
                    _deny()
                job = db.execute('SELECT rowid AS intake_rowid,* FROM wa_jobs WHERE id=?', (item['job_id'],)).fetchone()
                verified = db.execute('SELECT created_at FROM pilot_review_verified WHERE approval_id=? AND job_id=?', (self.approval_id, item['job_id'])).fetchone()
                if (not job or not verified or job['intake_rowid'] <= approval['minimum_job_rowid']
                        or job['inbound_at'] != verified['created_at'] or not approval['approved_at'] <= job['inbound_at'] <= now
                        or any(job[a] != item[b] for a,b in (('tenant_id','tenant_id'),('phone_number_id','phone_number_id'),('sender','recipient')))):
                    _deny()
                metadata = json.loads(job['media_json'])
                if metadata.get('_enrollment_binding') != self.store.enrollment_binding(enrolled) or '_pilot_attestation' in metadata:
                    _deny()
                upload = db.execute('SELECT * FROM pilot_review_uploads WHERE approval_id=? AND job_id=?', (self.approval_id, job['id'])).fetchone()
                command = db.execute('SELECT * FROM pilot_review_commands WHERE approval_id=? AND job_id=?', (self.approval_id, job['id'])).fetchone()
                if bool(upload) == bool(command):
                    _deny()
                original_id = upload['job_id'] if upload else command['original_job_id']
                first = db.execute('''SELECT a.kind AS reservation_kind,o.* FROM pilot_review_attempts a JOIN wa_outbox o ON o.id=a.outbox_id
                    WHERE a.approval_id=? AND a.original_job_id=? AND a.phase='first' ''', (self.approval_id, original_id)).fetchone()
                if first and (first['tenant_id']!=TENANT or first['phone_number_id']!=self.config.phone_id
                              or first['recipient']!=self.config.manager_phone or first['kind']!='text'
                              or first['object_key'] is not None or first['filename'] is not None):
                    _deny()
                stale_notice = (kind=='text' and text==_STALE_RESULT_NOTICE
                                and job['status'] in {'calculated','needs_attention'}
                                and item['object_key'] is None and item['filename'] is None)
                phase, reservation_kind = 'first', 'correction'
                if upload:
                    if item['purpose'] == 'ack':
                        from services.whatsapp_workflow import UPLOAD_ACK_TEXT
                        if first or kind != 'text' or text != UPLOAD_ACK_TEXT or job['status'] != 'awaiting_ack' or metadata.get('_upload_ack_settings_revision') != revision:
                            _deny()
                        self._source_ok(db, job, upload)
                        reservation_kind = 'ack'
                    elif item['purpose'] == 'result' and job['status'] == 'awaiting_review':
                        review = db.execute('SELECT * FROM wa_forecast_reviews WHERE original_job_id=?', (job['id'],)).fetchone()
                        if first or kind != 'text' or not review or review['prompt_outbox_id'] != key or review['status'] != 'pending' or not now < review['expires_at']:
                            _deny()
                        if (hashlib.sha256(text.encode()).hexdigest() != review['prompt_hash'] or review['settings_revision'] != revision
                                or not self.review_service.prompt_dispatch_allowed(db,dict(item),now)):
                            _deny()
                        self._source_ok(db, job, upload)
                        reservation_kind = 'review'
                    elif item['purpose'] == 'result' and kind == 'text' and (job['status'] == 'needs_correction' or stale_notice):
                        if stale_notice:
                            if not first or first['reservation_kind']!='ack':
                                _deny()
                            self._source_ok(db,job,upload)
                        if first:
                            if first['reservation_kind'] != 'ack':
                                _deny()
                            phase = 'final'
                    elif item['purpose'] == 'result' and job['status'] in {'calculated','needs_attention'} and kind == 'document':
                        if not first or first['reservation_kind'] != 'ack':
                            _deny()
                        self._source_ok(db, job, upload)
                        phase, reservation_kind = 'final', 'result'
                    else:
                        _deny()
                else:
                    review = db.execute('SELECT * FROM wa_forecast_reviews WHERE review_id=? AND original_job_id=?', (command['review_id'], original_id)).fetchone()
                    if (not first or first['reservation_kind'] != 'review' or not review or item['purpose'] != 'result'
                            or metadata.get('type') != 'review_command' or metadata.get('command') != command['command']):
                        _deny()
                    if kind == 'document':
                        if job['status'] not in {'calculated','needs_attention'} or review['status'] != 'confirmed' or review['confirmed_job_id'] != job['id']:
                            _deny()
                        original_upload = db.execute('SELECT * FROM pilot_review_uploads WHERE job_id=?', (original_id,)).fetchone()
                        self._source_ok(db, job, original_upload)
                    elif kind != 'text' or (job['status'] != 'needs_correction' and not stale_notice):
                        _deny()
                    elif stale_notice:
                        if review['status']!='confirmed' or review['confirmed_job_id']!=job['id']:
                            _deny()
                        original_upload=db.execute('SELECT * FROM pilot_review_uploads WHERE job_id=?',(original_id,)).fetchone()
                        self._source_ok(db,job,original_upload)
                    phase, reservation_kind = 'final', 'result' if kind == 'document' else 'correction'
                if phase == 'final' and (not first or first['status'] not in _ACCEPTED or not re.fullmatch(r'(?:SM|MM)[0-9a-fA-F]{32}', first['provider_message_id'] or '')):
                    _deny()
                if db.execute('SELECT COUNT(*) FROM pilot_review_attempts WHERE approval_id=?', (self.approval_id,)).fetchone()[0] >= approval['max_attempts']:
                    _deny()
                db.execute('INSERT INTO pilot_review_attempts VALUES(?,?,?,?,?,?,?)', (self.approval_id, original_id, phase, key, job['id'], reservation_kind, now))
        except (sqlite3.Error, ValueError, TypeError, KeyError, OSError):
            _deny()


class ReviewPilotProvider:
    """Typed live exception for this exact approved owner/store/files only."""
    def __init__(self, transport_provider, budget, fixtures):
        if not isinstance(transport_provider, TwilioWhatsAppProvider) or transport_provider.phone_number_id != budget.config.phone_id:
            _deny()
        self.transport_provider, self.budget, self.fixtures = transport_provider, budget, fixtures
        self.config, self.store, self.files, self.clock = budget.config, budget.store, budget.files, budget.clock
        self.phone_number_id, self.is_live = self.config.phone_id, transport_provider.is_live
        if self.is_live and not _persistent_mount(self.config.data_root):
            raise PilotConfigurationError('The live owner adapter requires the existing persistent mount.')

    def assert_active(self):
        return self.budget.assert_active()

    def permits_review(self, store, files):
        self.assert_active()
        return store is self.store and files is self.files

    def owner_active_for_app(self, tenant_id, manager_id, now):
        try:
            with self.store._connect() as db:
                db.row_factory = sqlite3.Row
                self.budget._record(db, now)
            return tenant_id == TENANT and manager_id == MANAGER
        except (ProviderError, sqlite3.Error, ValueError, TypeError, KeyError):
            return False

    def admit_inbound(self, db, job, now):
        return self.budget.admit_inbound(db, job, now)

    def verify_inbound_message(self, message_sid, sender, now):
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            job, _, _, approval = self.budget.claimed(db, message_sid, sender, now)
        # Absolutely no legacy Media-404 attestation fallback, even for uploads.
        created = self.transport_provider.verify_inbound_message(message_sid, sender, now)
        if not approval['approved_at'] <= created <= now:
            _deny()
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            current, _, _, _ = self.budget.claimed(db, message_sid, sender, self.clock())
            if current['claim_token'] != job['claim_token']:
                _deny()
            prior = db.execute('SELECT created_at FROM pilot_review_verified WHERE job_id=?', (job['id'],)).fetchone()
            if prior and prior['created_at'] != created:
                _deny()
            if not prior:
                db.execute('INSERT INTO pilot_review_verified VALUES(?,?,?)', (self.budget.approval_id, job['id'], created))
        return created

    def fetch_media(self, media_id, expected_mime, max_bytes):
        now = self.clock()
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            approval, _, _ = self.budget._record(db, now)
            upload = db.execute('''SELECT u.*,j.claim_token,j.status,v.created_at FROM pilot_review_uploads u
                JOIN wa_jobs j ON j.id=u.job_id JOIN pilot_review_verified v ON v.job_id=j.id
                WHERE u.approval_id=? AND u.media_url=? AND u.mime=? AND j.status='processing' ''',
                (self.budget.approval_id, media_id, expected_mime)).fetchone()
            if not upload:
                _deny()
        limit = 10 * 1024 * 1024 if expected_mime == 'application/pdf' else 5 * 1024 * 1024
        data = self.transport_provider.fetch_media(media_id, expected_mime, min(max_bytes, limit))
        digest = hashlib.sha256(data).hexdigest()
        if approval['input_policy'] == 'synthetic_exact':
            fixture = self.fixtures.get(expected_mime)
            if fixture is None or digest != hashlib.sha256(fixture).hexdigest():
                _deny()
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            self.budget._record(db, self.clock())
            current = db.execute("SELECT claim_token FROM wa_jobs WHERE id=? AND status='processing'", (upload['job_id'],)).fetchone()
            if not current or current['claim_token'] != upload['claim_token']:
                _deny()
            prior = db.execute('SELECT source_hash,mime FROM pilot_review_sources WHERE job_id=?', (upload['job_id'],)).fetchone()
            if prior and (prior['source_hash'] != digest or prior['mime'] != expected_mime):
                _deny()
            if not prior:
                db.execute('INSERT INTO pilot_review_sources VALUES(?,?,?,?)', (self.budget.approval_id, upload['job_id'], digest, expected_mime))
        return data

    def send_text(self, to, text, idempotency_key):
        if to != self.config.manager_phone:
            _deny()
        self.budget.reserve(idempotency_key, self.clock(), kind='text', text=text)
        self.assert_active()
        return self.transport_provider.send_text(to, text, idempotency_key)

    def send_document(self, to, pdf_bytes, filename, caption, idempotency_key):
        if to != self.config.manager_phone:
            _deny()
        with self.store._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM wa_outbox WHERE id=?', (idempotency_key,)).fetchone()
            if not row or row['filename'] != filename or row['text'] != caption or self.files.read(TENANT, row['object_key']) != pdf_bytes:
                _deny()
        self.budget.reserve(idempotency_key, self.clock(), kind='document')
        self.assert_active()
        return self.transport_provider.send_document(to, pdf_bytes, filename, caption, idempotency_key)

    def allows_automatic_retry(self, outbox_id):
        return False


class ReviewPilotMediaPublisher(TwilioMediaPublisher):
    def __init__(self, budget):
        self.budget = budget
        super().__init__(budget.store, budget.files, budget.config.origin, clock=budget.clock)

    def _context(self, db, outbox_id, now, *, publishing=False):
        try:
            self.budget._record(db, now)
        except ProviderError:
            self._deny()
        if not db.execute('SELECT 1 FROM pilot_review_attempts WHERE approval_id=? AND outbox_id=?', (self.budget.approval_id, outbox_id)).fetchone():
            self._deny()
        return super()._context(db, outbox_id, now, publishing=publishing)


def _fixtures(week, report_profile):
    """Initial-test artifacts for the explicitly approved profile, fictional only.

    Exact synthetic images must be sent as original documents; WhatsApp photo
    recompression changes bytes and is intentionally not guessed/whitelisted.
    The stored approval week is never silently advanced after activation.
    """
    import pymupdf
    if report_profile == 'canonical_v1':
        pdf = synthetic_forecast(week)
    elif report_profile == 'hotel_daily_table_v1':
        from io import BytesIO
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import landscape, letter
        output = BytesIO()
        document = canvas.Canvas(output, pagesize=landscape(letter), invariant=1)
        document.setTitle('Fictional owner review test - not an actual hotel report')
        days = [week + timedelta(days=i) for i in range(7)]
        lines = [f'Hotel code: {HOTEL_CODE}', 'FRI SAT SUN MON TUE WED THU',
                 ' '.join(f'{day.month}/{day.day}/{day.year}' for day in days), 'Prior Day Occupancy']
        metrics = [('Arrivals',40),('Departures',200),('Stayovers',70),('Rooms on the books',110),
                   ('Forecasted Pick up',10),('Forcasted occupied rooms',120),('Occupancy %',round(120/292*100,2))]
        lines += [label+' '+' '.join(str(value) for _ in range(7)) for label,value in metrics]
        for i,line in enumerate(lines):
            document.drawString(25,560-26*i,line)
        document.save()
        pdf = output.getvalue()
    else:
        _deny()
    with pymupdf.open(stream=pdf, filetype='pdf') as document:
        png = document[0].get_pixmap(matrix=pymupdf.Matrix(2,2), alpha=False).tobytes('png')
    return {'application/pdf': pdf, 'image/png': png}


@dataclass
class ReviewPilotRuntime:
    config: PilotConfig
    approval_id: str
    store: WorkflowStore
    files: PrivateFileStore
    publisher: ReviewPilotMediaPublisher
    provider: ReviewPilotProvider
    fixtures: dict = field(repr=False)
    clock: object = field(repr=False)

    def assert_active(self):
        return self.provider.assert_active()

    @property
    def expires_at(self):
        return self.assert_active()["expires_at"]

    def owner_active_for_app(self, tenant_id, manager_id, now):
        return self.provider.owner_active_for_app(tenant_id, manager_id, now)


def build_review_pilot_runtime(config, approval_id, *, enabled=False, transport=None, clock=time.time, cost_gate=None):
    if enabled is not True:
        raise PilotConfigurationError('Owner review pilot is disabled without explicit activation.')
    mocked = type(transport) is httpx.MockTransport
    if not mocked and cost_gate is None:
        raise PilotConfigurationError('An explicit current cost guard is required for live owner replies.')
    store, files = _open_existing(config, mocked=mocked)
    budget = ReviewPilotBudget(store, files, config, approval_id, clock=clock, cost_gate=cost_gate)
    approval = budget.assert_active()
    from datetime import date
    fixtures = _fixtures(date.fromisoformat(approval['fixture_week']), approval['report_profile'])
    publisher = ReviewPilotMediaPublisher(budget)
    actual = TwilioWhatsAppProvider(config.account_sid, config.auth_token, config.business_sender, media_publisher=publisher,
        status_callback_url=config.callback_url, production_approved=not mocked, transport=transport)
    provider = ReviewPilotProvider(actual, budget, fixtures)
    return ReviewPilotRuntime(config, approval_id, store, files, publisher, provider, fixtures, clock)
