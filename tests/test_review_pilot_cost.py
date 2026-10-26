"""Hermetic own-account usage monitoring; no real credentials or calls."""
from datetime import datetime, timezone
import sqlite3

import httpx
import pytest

from services.state_store import StateStore
from services.review_pilot_cost import ReviewPilotCostMonitor, CostMonitorUnavailable

SID='AC'+'a'*32
NOW=datetime(2026,10,26,1,0,tzinfo=timezone.utc).timestamp()


@pytest.fixture
def setup(tmp_path):
    store=StateStore(tmp_path/'state.db')
    with store._connect() as db:
        db.executescript('''CREATE TABLE pilot_review_authorizations(approval_id TEXT,approved_at REAL,expires_at REAL,monitored_budget_usd REAL);
        CREATE TABLE pilot_review_attempts(approval_id TEXT);
        CREATE TABLE pilot_review_uploads(approval_id TEXT);
        CREATE TABLE pilot_review_commands(approval_id TEXT);''')
        db.execute('INSERT INTO pilot_review_authorizations VALUES(?,?,?,10)',('approved',NOW,NOW+864000))
    state={'now':NOW,'price':'12.00','calls':[], 'status':200,'mutate':lambda p:p}
    def handler(req):
        state['calls'].append(req)
        p={'next_page_uri':None,'usage_records':[{'account_sid':SID,'category':'totalprice','price':state['price'],
            'price_unit':'usd','start_date':'2026-10-26','end_date':datetime.fromtimestamp(state['now'],timezone.utc).date().isoformat(),
            'as_of':datetime.fromtimestamp(state['now'],timezone.utc).isoformat()}]}
        return httpx.Response(state['status'],json=state['mutate'](p))
    monitor=ReviewPilotCostMonitor(store,SID,'fake-token','approved',clock=lambda:state['now'],transport=httpx.MockTransport(handler))
    return store,state,monitor


def gate(store,monitor,now=NOW):
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        return monitor.cost_gate(db,now)


def test_missing_baseline_is_never_zero(setup):
    store,state,m=setup
    assert not m.refresh()
    assert not state['calls']
    assert not gate(store,m)


def test_fixed_get_dates_baseline_and_cross_midnight(setup):
    store,state,m=setup
    assert m.initialize_baseline()
    assert gate(store,m)
    req=state['calls'][0]
    assert req.method=='GET' and req.url.host=='api.twilio.com'
    assert req.url.path==f'/2010-04-01/Accounts/{SID}/Usage/Records.json'
    assert dict(req.url.params)=={'Category':'totalprice','StartDate':'2026-10-26','EndDate':'2026-10-26','PageSize':'2'}
    state.update(now=NOW+86400,price='13.00')
    assert m.refresh()
    assert state['calls'][-1].url.params['StartDate']=='2026-10-26'
    assert state['calls'][-1].url.params['EndDate']=='2026-10-27'
    assert gate(store,m,state['now'])
    with pytest.raises(CostMonitorUnavailable):m.initialize_baseline()


def test_monotonic_delta_refunds_never_release_reserve(setup):
    store,state,m=setup
    m.initialize_baseline()
    state['price']='19.85'
    assert m.refresh()
    assert gate(store,m)
    with store._connect() as db:m.observe_inbound(db,'SM'+'1'*32,NOW)
    assert not gate(store,m)
    state['price']='12.00'
    assert m.refresh()
    assert not gate(store,m)


def test_lifetime_reserve_attempts_and_dedup(setup):
    store,state,m=setup
    m.initialize_baseline()
    with store._connect() as db:
        for _ in range(2):m.observe_inbound(db,'SM'+'2'*32,NOW)
        assert db.execute('SELECT COUNT(*) FROM review_cost_inbound').fetchone()[0]==1
        db.executemany('INSERT INTO pilot_review_attempts VALUES(?)',[('approved',)]*77)
    assert gate(store,m) # (1+77+next1)*.10=7.90
    with store._connect() as db:db.execute("INSERT INTO pilot_review_attempts VALUES('approved')")
    assert not gate(store,m) # projected $8 is blocked
    assert len(state['calls'])==1 # gate never calls HTTP


def test_stale_and_future_cache_fail_closed(setup):
    store,state,m=setup
    m.initialize_baseline()
    assert not gate(store,m,NOW+301)
    assert not gate(store,m,NOW-1)


@pytest.mark.parametrize('field,value', [('price_unit','eur'),('price','NaN'),('price','-1'),('price',None),
    ('account_sid','AC'+'b'*32),('category','sms'),('start_date','2026-10-25'),('end_date','2026-10-28'),
    ('as_of','2026-10-26T00:00:00+00:00'),('as_of','2026-10-26T02:00:00+00:00'),('as_of','2026-10-26T01:00:00')])
def test_malformed_error_invalidates_prior_good_snapshot(setup,field,value):
    store,state,m=setup
    m.initialize_baseline()
    def mutate(p):p['usage_records'][0][field]=value;return p
    state['mutate']=mutate
    assert not m.refresh()
    assert not gate(store,m)


@pytest.mark.parametrize('status',[301,401,429,500])
def test_http_error_no_redirect_no_retry_no_payload_leak(setup,status):
    store,state,m=setup
    m.initialize_baseline()
    state['status']=status
    assert not m.refresh()
    assert len(state['calls'])==2
    assert not gate(store,m)


def test_pagination_empty_oversized_and_append_only(setup):
    store,state,m=setup
    m.initialize_baseline()
    for mutate in [lambda p:dict(p,next_page_uri='https://evil.invalid'),lambda p:dict(p,usage_records=[]),
                   lambda p:dict(p,large='x'*65537)]:
        state['mutate']=mutate
        assert not m.refresh()
        assert not gate(store,m)
    with store._connect() as db:
        for table in ('review_cost_baselines','review_cost_snapshots','review_cost_inbound'):
            if table=='review_cost_inbound':m.observe_inbound(db,'SM'+'3'*32,NOW)
            with pytest.raises(sqlite3.IntegrityError):db.execute(f'DELETE FROM {table}')


def test_baseline_not_allowed_after_attempt(setup):
    store,state,m=setup
    with store._connect() as db:db.execute("INSERT INTO pilot_review_attempts VALUES('approved')")
    with pytest.raises(CostMonitorUnavailable):m.initialize_baseline()
    assert not gate(store,m)


def test_transport_error_is_generic_and_persists_deny(setup):
    store,state,m=setup
    m.initialize_baseline()
    def fail(req):raise httpx.ConnectError('secret-token provider raw error',request=req)
    m._transport=httpx.MockTransport(fail)
    assert not m.refresh()
    assert not gate(store,m)
    with pytest.raises(CostMonitorUnavailable) as error:m._fetch('2026-10-26',NOW)
    assert 'secret' not in str(error.value)


def test_fresh_poll_cannot_mask_stale_source_timestamp(setup):
    store,state,m=setup
    m.initialize_baseline()
    state['now']=NOW+301
    def stale(p):p['usage_records'][0]['as_of']=datetime.fromtimestamp(NOW,timezone.utc).isoformat();return p
    state['mutate']=stale
    assert not m.refresh()
    assert not gate(store,m,state['now'])


@pytest.mark.parametrize('operation',['baseline','refresh'])
def test_valid_timestamp_generated_during_request_is_not_future(setup,operation):
    store,state,m=setup
    if operation=='refresh':m.initialize_baseline()
    started=state['now']
    def generated_in_flight(p):
        p['usage_records'][0]['as_of']=datetime.fromtimestamp(started+.2,timezone.utc).isoformat()
        state['now']=started+.3
        return p
    state['mutate']=generated_in_flight
    assert (m.initialize_baseline() if operation=='baseline' else m.refresh())
    assert gate(store,m,state['now'])
    with store._connect() as db:
        row=db.execute('SELECT checked_at,as_of FROM review_cost_snapshots ORDER BY id DESC LIMIT 1').fetchone()
    assert row[0]==started and row[1]==started+.2
    # Cache age still runs from request start, not a later receipt time.
    assert not gate(store,m,started+300.01)


@pytest.mark.parametrize('clock_delta,stamp_delta',[
    (.3,.4),       # Truly future at receipt remains rejected; no skew tolerance.
    (.3,-299.9),   # Fresh at start but over300seconds old at receipt is rejected.
    (-.1,-1),     # Clock rollback invalidates the observation.
    (301,301),    # An overlong observation/large forward clock jump is rejected.
])
def test_response_time_check_preserves_strict_age_and_clock_guards(setup,clock_delta,stamp_delta):
    store,state,m=setup;m.initialize_baseline();started=state['now']
    def response(p):
        p['usage_records'][0]['as_of']=datetime.fromtimestamp(started+stamp_delta,timezone.utc).isoformat()
        state['now']=started+clock_delta
        return p
    state['mutate']=response
    assert not m.refresh()
    assert not gate(store,m,state['now'])
