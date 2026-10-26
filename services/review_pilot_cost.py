"""Conservative monitored budget, NOT a provider/account hard spend cap.

Official contract: https://www.twilio.com/docs/usage/api/usage-record
GET own-account Usage/Records.json, Category=totalprice and inclusive GMT dates.
The account-wide delta includes unrelated usage. Billing can lag; freshness of
as_of does not guarantee all accrued charges have posted. Lifetime reserves are
never released: .10 USD per observed inbound and outbound attempt is deliberately
added to reported delta (double counting is safe). Stop below $8, leaving $2 of
headroom, possibly long before 500 replies. This cannot bound unseen inbound,
Meta fees, taxes or delayed billing. No HTTP is performed by cost_gate.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import math
import re
import sqlite3
import time

import httpx

MAX_AGE = 300
POLL_SECONDS = 60
RESERVE_MICRO = 100_000
STOP_MICRO = 8_000_000


class CostMonitorUnavailable(ValueError):
    def __init__(self):
        super().__init__('Cost monitor unavailable; outbound sending remains blocked.')


def _now(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise CostMonitorUnavailable()
    return float(value)


def _date(value):
    return datetime.fromtimestamp(value, timezone.utc).date().isoformat()


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise CostMonitorUnavailable()
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal('1000000000'):
            raise CostMonitorUnavailable()
        return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError, OverflowError):
        raise CostMonitorUnavailable() from None


def _schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS review_cost_baselines (
        approval_id TEXT PRIMARY KEY, account_sid TEXT NOT NULL, start_date TEXT NOT NULL,
        created_at REAL NOT NULL, total_micro INTEGER NOT NULL, as_of REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS review_cost_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, approval_id TEXT NOT NULL, checked_at REAL NOT NULL,
        valid INTEGER NOT NULL, as_of REAL, total_micro INTEGER, high_delta_micro INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS review_cost_inbound (
        approval_id TEXT NOT NULL, message_sid TEXT NOT NULL, observed_at REAL NOT NULL,
        PRIMARY KEY(approval_id,message_sid))''')
    for table in ('review_cost_baselines', 'review_cost_snapshots', 'review_cost_inbound'):
        for op in ('UPDATE', 'DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'Cost audit is append-only'); END")


class ReviewPilotCostMonitor:
    def __init__(self, store, account_sid, auth_token, approval_id, *, clock=time.time, transport=None):
        if (not isinstance(account_sid, str) or not re.fullmatch(r'AC[0-9a-fA-F]{32}', account_sid)
                or not isinstance(auth_token, str) or not 1 <= len(auth_token) <= 256
                or not auth_token.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in auth_token)
                or not isinstance(approval_id, str) or not 1 <= len(approval_id) <= 128
                or (transport is not None and not isinstance(transport, httpx.MockTransport))):
            raise CostMonitorUnavailable()
        self.store, self.account_sid, self.approval_id = store, account_sid, approval_id
        self.clock, self._token, self._transport = clock, auth_token, transport
        with store._connect() as db:
            _schema(db)

    def _fetch(self, start_date, now):
        """One fixed-origin GET; never follow provider URLs, redirects or retries."""
        try:
            url = f'https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Usage/Records.json'
            with httpx.Client(transport=self._transport, trust_env=False, follow_redirects=False,
                              timeout=10.0, auth=(self.account_sid, self._token)) as client:
                with client.stream('GET', url, params={'Category':'totalprice', 'StartDate':start_date,
                                   'EndDate':_date(now), 'PageSize':2}, headers={'Accept':'application/json'}) as response:
                    if response.status_code != 200 or response.headers.get('content-type','').split(';')[0].lower() != 'application/json':
                        raise CostMonitorUnavailable()
                    raw = bytearray()
                    for part in response.iter_bytes():
                        if len(raw) + len(part) > 65536:
                            raise CostMonitorUnavailable()
                        raw.extend(part)
            # The provider can generate as_of while this request is in flight.
            # Compare to observation time, not the earlier poll-start timestamp.
            # Retain poll-start checked_at below, so cache lifetime is not extended.
            observed_at = _now(self.clock())
            if not 0 <= observed_at - now <= MAX_AGE:
                raise CostMonitorUnavailable()
            payload = json.loads(raw, parse_float=Decimal)
            rows = payload['usage_records']
            if payload.get('next_page_uri') is not None or not isinstance(rows,list) or len(rows) != 1:
                raise CostMonitorUnavailable()
            row = rows[0]
            if (row['account_sid'] != self.account_sid or row['category'] != 'totalprice'
                    or row['price_unit'] != 'usd' or row['start_date'] != start_date or row['end_date'] != _date(now)):
                raise CostMonitorUnavailable()
            stamp = datetime.fromisoformat(row['as_of'])
            if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
                raise CostMonitorUnavailable()
            as_of = stamp.timestamp()
            if not 0 <= observed_at - as_of <= MAX_AGE:
                raise CostMonitorUnavailable()
            return _money(row['price']), as_of
        except Exception:
            raise CostMonitorUnavailable() from None

    def initialize_baseline(self, now=None):
        """Explicit operator activation step only; never called by refresh/gate.

        Requires existing authorization and zero attempts. Reinitialization is
        rejected, including after errors/restarts. The HTTP read precedes lock.
        """
        now = _now(self.clock() if now is None else now)
        with self.store._connect() as db:
            row = db.execute('SELECT approved_at,expires_at FROM pilot_review_authorizations WHERE approval_id=?', (self.approval_id,)).fetchone()
            if not row or not row[0] <= now < row[1]:
                raise CostMonitorUnavailable()
            start_date = _date(row[0])
        total, as_of = self._fetch(start_date, now)
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM pilot_review_attempts WHERE approval_id=?', (self.approval_id,)).fetchone():
                raise CostMonitorUnavailable()
            try:
                db.execute('INSERT INTO review_cost_baselines VALUES(?,?,?,?,?,?)',
                           (self.approval_id,self.account_sid,start_date,now,total,as_of))
            except sqlite3.IntegrityError:
                raise CostMonitorUnavailable() from None
            db.execute('INSERT INTO review_cost_snapshots(approval_id,checked_at,valid,as_of,total_micro,high_delta_micro) VALUES(?,?,1,?,?,0)',
                       (self.approval_id,now,as_of,total))
        return True

    def refresh(self, now=None):
        """Worker calls every <=60 seconds. Errors append immediate deny state."""
        now = _now(self.clock() if now is None else now)
        with self.store._connect() as db:
            base = db.execute('SELECT account_sid,start_date,total_micro FROM review_cost_baselines WHERE approval_id=?', (self.approval_id,)).fetchone()
        valid, total, as_of = 0, None, None
        try:
            if not base or base[0] != self.account_sid:
                raise CostMonitorUnavailable()
            total, as_of = self._fetch(base[1], now)
            valid = 1
        except CostMonitorUnavailable:
            pass
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            high = db.execute('SELECT COALESCE(MAX(high_delta_micro),0) FROM review_cost_snapshots WHERE approval_id=?', (self.approval_id,)).fetchone()[0]
            if valid:
                high = max(high, total-base[2], 0)
            db.execute('INSERT INTO review_cost_snapshots(approval_id,checked_at,valid,as_of,total_micro,high_delta_micro) VALUES(?,?,?,?,?,?)',
                       (self.approval_id,now,valid,as_of,total,high))
        return bool(valid)

    def observe_inbound(self, db, message_sid, now):
        """Call for every signature-verified inbound before admission, same DB."""
        now = _now(now)
        if not isinstance(message_sid,str) or not re.fullmatch(r'(?:SM|MM)[0-9a-fA-F]{32}',message_sid):
            raise CostMonitorUnavailable()
        db.execute('INSERT OR IGNORE INTO review_cost_inbound VALUES(?,?,?)', (self.approval_id,message_sid,now))

    def cost_gate(self, db, now):
        """Called INSIDE outbound reservation transaction, BEFORE attempt insert."""
        try:
            now = _now(now)
            base = db.execute('SELECT account_sid,created_at FROM review_cost_baselines WHERE approval_id=?', (self.approval_id,)).fetchone()
            row = db.execute('SELECT checked_at,valid,as_of,high_delta_micro FROM review_cost_snapshots WHERE approval_id=? ORDER BY id DESC LIMIT 1', (self.approval_id,)).fetchone()
            if (not base or base[0] != self.account_sid or base[1] > now or not row or row[1] != 1
                    or not 0 <= now-row[0] <= MAX_AGE or not 0 <= now-row[2] <= MAX_AGE):
                return False
            approval = db.execute('SELECT approved_at,expires_at,monitored_budget_usd FROM pilot_review_authorizations WHERE approval_id=?', (self.approval_id,)).fetchone()
            if not approval or not approval[0] <= now < approval[1] or _money(approval[2]) != 10_000_000:
                return False
            incoming = db.execute('SELECT COUNT(*) FROM review_cost_inbound WHERE approval_id=?', (self.approval_id,)).fetchone()[0]
            # Fallback count avoids under-reserving admitted intake if observer wiring fails.
            admitted = sum(db.execute(f'SELECT COUNT(*) FROM {table} WHERE approval_id=?', (self.approval_id,)).fetchone()[0]
                           for table in ('pilot_review_uploads','pilot_review_commands'))
            outgoing = db.execute('SELECT COUNT(*) FROM pilot_review_attempts WHERE approval_id=?', (self.approval_id,)).fetchone()[0]
            return row[3] + (max(incoming,admitted)+outgoing+1)*RESERVE_MICRO < STOP_MICRO
        except Exception:
            return False
