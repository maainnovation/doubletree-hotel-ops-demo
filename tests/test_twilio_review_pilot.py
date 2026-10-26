"""Owner review pilot gates, mocked transport only; never live credentials."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from scripts.demo_whatsapp_workflow import DEMO_NOW
from services.twilio_pilot import PilotConfig, build_runtime, TENANT, MANAGER, HOTEL_CODE, synthetic_forecast
from services.twilio_review_pilot import (create_review_pilot_approval, build_review_pilot_runtime,
    revoke_review_pilot, ReviewPilotBudget, MAX_SECONDS)
from services.twilio_run_budget import _schema as fresh_schema
from services.whatsapp_forecast import validate_whatsapp_forecast
from services.whatsapp_provider import ProviderError
from services.whatsapp_workflow import ForecastWorker, UPLOAD_ACK_TEXT
from test_twilio_live_pilot import env as legacy_env, ACCOUNT, BUSINESS, SENDER


@pytest.fixture
def owner(tmp_path):
    clock=[DEMO_NOW.timestamp()]
    config=PilotConfig.from_env(legacy_env(tmp_path/'pilot'),now=clock[0])
    state={'requests':[],'sends':[],'media':{},'created':{},'pdfs':[],'post_status':201,'message_status':200}
    runtime=[None]
    def handle(request):
        state['requests'].append((request.method,str(request.url)))
        if request.method=='GET' and request.url.path.endswith('.json'):
            sid=request.url.path.split('/')[-1][:-5]
            return httpx.Response(state['message_status'],json={'sid':sid,'account_sid':ACCOUNT,
                'from':'whatsapp:+'+SENDER,'to':'whatsapp:+'+BUSINESS,'direction':'inbound',
                'date_created':format_datetime(datetime.fromtimestamp(state['created'].get(sid,clock[0]),timezone.utc))})
        if request.method=='GET':
            data,mime=state['media'][str(request.url)]
            return httpx.Response(200,content=data,headers={'Content-Type':mime})
        values={key:value[0] for key,value in parse_qs(request.content.decode()).items()}
        state['sends'].append(values)
        if 'MediaUrl' in values:
            parts=urlsplit(values['MediaUrl']).path.split('/')
            state['pdfs'].append(runtime[0].publisher.get(parts[-2],parts[-1]))
        return httpx.Response(state['post_status'],json={'sid':'SM'+format(len(state['sends']),'032x'),'code':20429})
    mock=httpx.MockTransport(handle)
    old=build_runtime(config,transport=mock,clock=lambda:clock[0])
    with old.store._connect() as db:
        db.executemany("INSERT INTO pilot_send_attempts VALUES('old-pdf',?)",[(1,),(2,),(3,)])
        db.execute('CREATE TABLE pilot_text_diagnostics(audit_id TEXT PRIMARY KEY,status TEXT)')
        db.execute("INSERT INTO pilot_text_diagnostics VALUES('old-text','confirmed_rejected_no_retry')")
        fresh_schema(db)
        identity=db.execute('SELECT identity FROM pilot_identity').fetchone()[0]
        for name,n in [('old-failed-ack',1),('old-completed-pair',2)]:
            db.execute('INSERT INTO pilot_run_authorizations VALUES(?,?,?,?,?,?,?)',(name,identity,clock[0]-3600,clock[0]-1800,0,'old-'+name,2))
            for i in range(n):db.execute('INSERT INTO pilot_run_attempts VALUES(?,?,?,?,?)',(name,'ack' if i==0 else 'result',name+str(i),'old-'+name,clock[0]-3500+i))
        binding=old.store.enrollment_binding(old.store._enrollment(db,config.phone_id,SENDER))
        db.execute("INSERT INTO wa_jobs(id,phone_number_id,message_id,sender,tenant_id,inbound_at,media_json,available_at,status) VALUES(?,?,?,?,?,?,?,?,?)",
                   ('old-job',config.phone_id,'SM'+'f'*32,SENDER,TENANT,clock[0]-100,json.dumps({'_enrollment_binding':binding}),clock[0]-100,'failed'))
    result={'old':old,'clock':clock,'config':config,'state':state,'mock':mock,'runtime':runtime}
    return result


def approve(owner,**kwargs):
    options={'report_profile':'canonical_v1','input_policy':'redacted_owner_reports'};options.update(kwargs)
    metadata=create_review_pilot_approval(owner['old'].store,owner['config'],'owner-run',owner['clock'][0],**options)
    rt=build_review_pilot_runtime(owner['config'],'owner-run',enabled=True,transport=owner['mock'],clock=lambda:owner['clock'][0],cost_gate=lambda *args:True)
    owner['runtime'][0]=rt
    return rt,metadata


def rows(rt,now):
    return validate_whatsapp_forecast(synthetic_forecast(rt.config.week_start),'application/pdf',HOTEL_CODE,292,datetime.fromtimestamp(now,timezone.utc))


def worker(owner,rt):
    def evaluator(data,mime,*args,**kwargs):
        matrix=rows(rt,owner['clock'][0])
        if mime=='application/pdf':return {'method':'native_pdf','automatic_roster_allowed':True,'requires_review':False,'rows':matrix}
        return {'method':'local_image_ocr','automatic_roster_allowed':False,'requires_review':True,'rows':matrix,'candidate_valid_for_confirmation':True}
    return ForecastWorker(rt.store,rt.files,rt.provider,clock=lambda:owner['clock'][0],review_images=True,acknowledge_uploads=True,media_evaluator=evaluator)


def admit(owner,rt,*,mime='application/pdf',command=None,code=None,sender=SENDER,message_sid=None):
    sid=message_sid or 'SM'+uuid.uuid4().hex
    now=owner['clock'][0]
    job={'id':uuid.uuid4().hex,'phone_number_id':rt.config.phone_id,'message_id':sid,'sender':sender,
         'tenant_id':TENANT,'inbound_at':now,'available_at':now}
    enrolled=rt.store.enrollment(rt.config.phone_id,SENDER)
    meta={'_provider':'twilio','_enrollment_binding':rt.store.enrollment_binding(enrolled),'_enrolled_report_profile':enrolled['report_profile']}
    if command:
        meta.update(type='review_command',command=command,review_code=code)
    else:
        url=f'https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages/{sid}/Media/ME'+'c'*32
        meta.update(type='document' if mime=='application/pdf' else 'image',mime=mime,id=url)
        owner['state']['media'][url]=(rt.fixtures.get(mime,rt.fixtures['image/png']),mime)
    job['media_json']=json.dumps(meta)
    with rt.store._connect() as db:
        db.row_factory=sqlite3.Row;db.execute('BEGIN IMMEDIATE')
        if not rt.provider.admit_inbound(db,job,now):raise ValueError('Admission refused')
        db.execute('INSERT INTO wa_jobs(id,phone_number_id,message_id,sender,tenant_id,inbound_at,media_json,available_at) VALUES(?,?,?,?,?,?,?,?)',tuple(job[key] for key in ('id','phone_number_id','message_id','sender','tenant_id','inbound_at','media_json','available_at')))
    owner['state']['created'][sid]=now
    return job


def query(rt,sql,args=()):
    with rt.store._connect() as db:
        db.row_factory=sqlite3.Row
        return [dict(row) for row in db.execute(sql,args)]


def history(rt):
    with rt.store._connect() as db:
        return {table:db.execute('SELECT * FROM '+table).fetchall() for table in ('pilot_identity','pilot_send_attempts','pilot_text_diagnostics','pilot_run_authorizations','pilot_run_attempts')}


def test_default_disabled_and_explicit_approval_has_ten_day_total_not_daily_limits(owner):
    before=history(owner['old'])
    rt,metadata=approve(owner)
    assert metadata['expires_at']==owner['clock'][0]+MAX_SECONDS
    assert metadata['max_attempts']==500 and metadata['max_uploads']==1000 and metadata['max_commands']==2000
    assert rt.expires_at>rt.config.approved_until and history(rt)==before
    assert rt.owner_active_for_app(TENANT,MANAGER,owner['clock'][0]) is True
    assert rt.owner_active_for_app('another',MANAGER,owner['clock'][0]) is False
    with pytest.raises(ValueError):build_review_pilot_runtime(owner['config'],'owner-run',transport=owner['mock'])
    with pytest.raises(ProviderError):create_review_pilot_approval(rt.store,rt.config,'second',owner['clock'][0],report_profile='canonical_v1')
    assert not owner['state']['requests']


def test_native_ack_then_pdf_and_photo_review_then_pdf_share_four_new_reservations(owner):
    before=history(owner['old']);rt,_=approve(owner);w=worker(owner,rt)
    native=admit(owner,rt)
    assert w.process_one() and w.dispatch_one()
    assert owner['state']['sends'][0]['Body']==UPLOAD_ACK_TEXT
    assert w.process_one() and w.dispatch_one()
    assert len(owner['state']['pdfs'])==1
    # A photo of different byte content still uses explicit review, never ACK.
    image=admit(owner,rt,mime='image/png')
    assert w.process_one() and w.dispatch_one()
    prompt=owner['state']['sends'][-1]['Body']
    assert '49 values' in prompt
    code=re.search(r'CONFIRM ([0-9a-f]{16})',prompt).group(1)
    confirmation=admit(owner,rt,command='CONFIRM',code=code)
    assert w.process_one() and w.dispatch_one()
    attempts=query(rt,'SELECT original_job_id,phase,kind FROM pilot_review_attempts ORDER BY rowid')
    assert [(row['phase'],row['kind']) for row in attempts]==[('first','ack'),('final','result'),('first','review'),('final','result')]
    assert attempts[2]['original_job_id']==attempts[3]['original_job_id']==image['id']
    assert len(owner['state']['sends'])==4 and len(owner['state']['pdfs'])==2
    assert history(rt)==before and not rt.provider.allows_automatic_retry('any')
    assert len(query(rt,'SELECT * FROM pilot_review_uploads'))==2 and len(query(rt,'SELECT * FROM pilot_review_commands'))==1


def test_settings_edits_are_read_from_shared_latest_revision_without_reset(owner):
    rt,_=approve(owner);before=history(rt)
    rt.store.save_hotel_parameters(MANAGER,{'checkout_minutes':24})
    assert rt.assert_active()['approval_id']=='owner-run'
    w=worker(owner,rt);admit(owner,rt);assert w.process_one() and w.dispatch_one() and w.process_one() and w.dispatch_one()
    roster=rt.store.get_weekly_roster(MANAGER,rt.config.week_start.isoformat())
    assert roster['source']['settings_revision']==2
    assert roster['input_snapshot']['settings']['cleaning_standards']['checkout_minutes']==24
    assert history(rt)==before


def test_restart_after_legacy_expiry_does_not_extend_or_rewrite_old_identity(owner):
    rt,_=approve(owner);before=history(rt)
    owner['clock'][0]=rt.config.approved_until+1
    rebuilt=build_review_pilot_runtime(rt.config,'owner-run',enabled=True,transport=owner['mock'],clock=lambda:owner['clock'][0],cost_gate=lambda *args:True)
    assert rebuilt.assert_active()['expires_at']>owner['clock'][0] and history(rebuilt)==before
    with pytest.raises(ProviderError):build_review_pilot_runtime(replace(rt.config,approved_until=rt.config.approved_until+1),'owner-run',enabled=True,transport=owner['mock'],clock=lambda:owner['clock'][0],cost_gate=lambda *args:True)


@pytest.mark.parametrize('kwargs',[{'expires_at':float('inf')},{'max_attempts':501},{'max_attempts':0},{'max_attempts':True},{'max_uploads':1001},{'max_commands':2001},{'rate_limit':21},{'monitored_budget_usd':11},{'input_policy':'automatic'},{'report_profile':'guess'}])
def test_overbroad_approval_limits_fail_without_state_change(owner,kwargs):
    before=history(owner['old'])
    with pytest.raises(ProviderError):approve(owner,**kwargs)
    assert history(owner['old'])==before and not owner['state']['requests']


def test_duration_above_ten_days_and_missing_explicit_profile_fail(owner):
    with pytest.raises(ProviderError):approve(owner,expires_at=owner['clock'][0]+MAX_SECONDS+1)
    with pytest.raises(TypeError):create_review_pilot_approval(owner['old'].store,owner['config'],'no-profile',owner['clock'][0])


def test_profile_transition_is_explicit_audited_and_not_repeated_at_startup(owner):
    rt,_=approve(owner,report_profile='hotel_daily_table_v1')
    audit=query(rt,'SELECT previous_profile,report_profile FROM pilot_review_authorizations')[0]
    assert audit=={'previous_profile':'canonical_v1','report_profile':'hotel_daily_table_v1'}
    with rt.store._connect() as db:db.execute("UPDATE wa_tenants SET report_profile='canonical_v1' WHERE tenant_id=?",(TENANT,))
    with pytest.raises(ProviderError):rt.assert_active()
    assert rt.store.enrollment(rt.config.phone_id,SENDER)['report_profile']=='canonical_v1'


def test_unknown_owner_old_message_and_unbound_old_job_are_rejected(owner):
    rt,_=approve(owner)
    with pytest.raises(ValueError):admit(owner,rt,sender='15555550999')
    with pytest.raises(ValueError):admit(owner,rt,message_sid='SM'+'f'*32)
    with pytest.raises(ProviderError):rt.provider.verify_inbound_message('SM'+'f'*32,SENDER,owner['clock'][0])
    assert not query(rt,'SELECT * FROM pilot_review_uploads') and not owner['state']['requests']


def test_admission_binds_upload_before_processing_and_rolls_back_with_job_insert(owner):
    rt,_=approve(owner);job=admit(owner,rt)
    assert query(rt,'SELECT job_id FROM pilot_review_uploads')==[{'job_id':job['id']}]
    assert not owner['state']['requests']
    # Full rollback, including admission ledger, on transaction failure.
    with pytest.raises(sqlite3.IntegrityError):
        with rt.store._connect() as db:
            db.row_factory=sqlite3.Row;db.execute('BEGIN IMMEDIATE')
            duplicate=dict(job,id=uuid.uuid4().hex,message_id='SM'+uuid.uuid4().hex)
            metadata=json.loads(duplicate['media_json']);metadata['id']=metadata['id'].replace(job['message_id'],duplicate['message_id']);duplicate['media_json']=json.dumps(metadata)
            assert rt.provider.admit_inbound(db,duplicate,owner['clock'][0])
            db.execute('INSERT INTO wa_jobs(id) VALUES(?)',(job['id'],))
    assert len(query(rt,'SELECT * FROM pilot_review_uploads'))==1


def test_normal_message_404_never_uses_media_fallback_or_sends(owner):
    rt,_=approve(owner);admit(owner,rt);owner['state']['message_status']=404
    w=worker(owner,rt);assert w.process_one()
    assert len(owner['state']['requests'])==1 and not owner['state']['sends']
    assert not query(rt,'SELECT * FROM pilot_review_verified')
    assert not query(rt,'SELECT * FROM pilot_review_attempts')


def test_preapproval_replay_cannot_get_media_or_reply(owner):
    rt,_=approve(owner);job=admit(owner,rt)
    owner['state']['created'][job['message_id']]=owner['clock'][0]-1
    assert worker(owner,rt).process_one()
    assert len(owner['state']['requests'])==1 and not query(rt,'SELECT * FROM pilot_review_verified')


def test_exact_synthetic_default_rejects_recompressed_photo_before_parser(owner):
    rt,_=approve(owner,input_policy='synthetic_exact');job=admit(owner,rt,mime='image/png')
    media=json.loads(job['media_json']);owner['state']['media'][media['id']]=(b'not the exact synthetic PNG','image/png')
    called=[]
    w=ForecastWorker(rt.store,rt.files,rt.provider,clock=lambda:owner['clock'][0],review_images=True,media_evaluator=lambda *a,**k:called.append(1))
    assert w.process_one() and not called
    assert w.dispatch_one() and len(owner['state']['sends'])==1
    assert query(rt,'SELECT kind FROM pilot_review_attempts')==[{'kind':'correction'}]
    assert not query(rt,'SELECT * FROM pilot_review_sources')


def test_send_transient_error_consumes_one_attempt_and_never_retries(owner):
    rt,_=approve(owner);w=worker(owner,rt);admit(owner,rt)
    assert w.process_one();owner['state']['post_status']=429
    assert w.dispatch_one()
    assert len(owner['state']['sends'])==1 and len(query(rt,'SELECT * FROM pilot_review_attempts'))==1
    owner['clock'][0]+=600
    assert w.dispatch_one() is False
    assert query(rt,"SELECT status FROM wa_outbox WHERE purpose='ack'")[0]['status']=='failed'


def test_cost_gate_denial_happens_before_reservation_or_provider_send(owner):
    rt,_=approve(owner);w=worker(owner,rt);admit(owner,rt);assert w.process_one()
    rt.provider.budget.cost_gate=lambda *args:False
    assert w.dispatch_one()
    assert not query(rt,'SELECT * FROM pilot_review_attempts') and not owner['state']['sends']


def test_campaign_total_cap_is_persistent_not_daily(owner):
    rt,_=approve(owner,max_attempts=1);w=worker(owner,rt);admit(owner,rt)
    assert w.process_one() and w.dispatch_one() and w.process_one() and w.dispatch_one()
    assert len(owner['state']['sends'])==1
    assert len(query(rt,'SELECT * FROM pilot_review_attempts'))==1
    owner['clock'][0]+=86400
    with pytest.raises(ValueError):admit(owner,rt)
    rebuilt=build_review_pilot_runtime(rt.config,'owner-run',enabled=True,transport=owner['mock'],clock=lambda:owner['clock'][0],cost_gate=lambda *args:True)
    assert len(query(rebuilt,'SELECT * FROM pilot_review_attempts'))==1


@pytest.mark.parametrize('limit',['max_uploads','rate_limit'])
def test_intake_limits_are_atomic_under_concurrency(owner,limit):
    rt,_=approve(owner,**{limit:1})
    def intake(_):
        try:admit(owner,rt);return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(intake,range(4)))
    assert results.count(True)==1 and len(query(rt,'SELECT * FROM pilot_review_uploads'))==1


def test_append_only_audits_cannot_be_reset_or_reclaimed(owner):
    rt,_=approve(owner);admit(owner,rt)
    for sql in ('DELETE FROM pilot_review_authorizations','UPDATE pilot_review_authorizations SET max_attempts=500','DELETE FROM pilot_review_uploads'):
        with pytest.raises(sqlite3.IntegrityError):
            with rt.store._connect() as db:db.execute(sql)
    assert len(query(rt,'SELECT * FROM pilot_review_uploads'))==1


def test_expiry_and_revocation_block_app_intake_and_reservation(owner):
    rt,_=approve(owner);assert rt.owner_active_for_app(TENANT,MANAGER,owner['clock'][0])
    revoke_review_pilot(rt.store,rt.approval_id,owner['clock'][0])
    assert not rt.owner_active_for_app(TENANT,MANAGER,owner['clock'][0])
    with pytest.raises(ValueError):admit(owner,rt)
    with pytest.raises(ProviderError):rt.assert_active()


def test_expired_campaign_does_not_extend_on_restart(owner):
    rt,metadata=approve(owner);owner['clock'][0]=metadata['expires_at']
    with pytest.raises(ProviderError):build_review_pilot_runtime(rt.config,rt.approval_id,enabled=True,transport=owner['mock'],clock=lambda:owner['clock'][0],cost_gate=lambda *args:True)


def staged_ack(owner,rt):
    w=worker(owner,rt);job=admit(owner,rt)
    assert w.process_one()
    item=query(rt,"SELECT * FROM wa_outbox WHERE job_id=? AND purpose='ack'",(job['id'],))[0]
    with rt.store._connect() as db:db.execute("UPDATE wa_outbox SET status='sending',attempts=1 WHERE id=?",(item['id'],))
    return w,job,item


@pytest.mark.parametrize('mutation',[
    "UPDATE wa_outbox SET recipient='15555550999'",
    "UPDATE wa_outbox SET tenant_id='other'",
    "UPDATE wa_outbox SET phone_number_id='12345'",
    "UPDATE wa_outbox SET attempts=2",
    "UPDATE wa_outbox SET text='Unapproved text'",
    "UPDATE wa_jobs SET source_hash='wrong' WHERE status='awaiting_ack'",
    "UPDATE wa_jobs SET source_key='wrong' WHERE status='awaiting_ack'",
    "UPDATE wa_jobs SET inbound_at=inbound_at-1 WHERE status='awaiting_ack'",
    "UPDATE wa_tenants SET enabled=0",
    "UPDATE staff_settings SET dataset_mode='operational'",
])
def test_direct_send_cannot_bypass_scope_source_settings_or_attempt_guards(owner,mutation):
    rt,_=approve(owner);_,_,item=staged_ack(owner,rt)
    with rt.store._connect() as db:db.execute(mutation)
    with pytest.raises(ProviderError):rt.provider.send_text(SENDER,UPLOAD_ACK_TEXT,item['id'])
    assert not owner['state']['sends'] and not query(rt,'SELECT * FROM pilot_review_attempts')


def test_cost_callback_is_inside_the_exact_reservation_transaction(owner):
    rt,_=approve(owner);_,_,item=staged_ack(owner,rt);seen=[]
    def gate(db,now):
        seen.append((db.in_transaction,db.execute('SELECT COUNT(*) FROM pilot_review_attempts').fetchone()[0],now))
        return True
    rt.provider.budget.cost_gate=gate
    rt.provider.send_text(SENDER,UPLOAD_ACK_TEXT,item['id'])
    assert seen==[(True,0,owner['clock'][0])]
    assert len(query(rt,'SELECT * FROM pilot_review_attempts'))==1


def test_same_outbox_concurrent_calls_never_send_or_reserve_twice(owner):
    rt,_=approve(owner);_,_,item=staged_ack(owner,rt)
    def send(_):
        try:rt.provider.send_text(SENDER,UPLOAD_ACK_TEXT,item['id']);return True
        except ProviderError:return False
    with ThreadPoolExecutor(max_workers=5) as pool:results=list(pool.map(send,range(5)))
    assert results.count(True)==1 and len(owner['state']['sends'])==1
    assert len(query(rt,'SELECT * FROM pilot_review_attempts'))==1


def test_slow_cost_gate_cannot_spend_after_epoch_expiry(owner):
    rt,metadata=approve(owner);_,_,item=staged_ack(owner,rt)
    def gate(db,now):owner['clock'][0]=metadata['expires_at'];return True
    rt.provider.budget.cost_gate=gate
    with pytest.raises(ProviderError):rt.provider.send_text(SENDER,UPLOAD_ACK_TEXT,item['id'])
    assert not owner['state']['sends'] and not query(rt,'SELECT * FROM pilot_review_attempts')


def test_commands_need_the_scoped_accepted_prompt_and_are_bound_once(owner):
    rt,_=approve(owner);w=worker(owner,rt);image=admit(owner,rt,mime='image/png')
    assert w.process_one()
    prompt=query(rt,"SELECT * FROM wa_outbox WHERE job_id=?",(image['id'],))[0]
    code=re.search(r'CONFIRM ([0-9a-f]{16})',prompt['text']).group(1)
    with pytest.raises(ValueError):admit(owner,rt,command='CONFIRM',code=code)
    assert w.dispatch_one()
    with pytest.raises(ValueError):admit(owner,rt,command='CONFIRM',code='0'*16)
    cmd=admit(owner,rt,command='CANCEL',code=code)
    with pytest.raises(ValueError):admit(owner,rt,command='CONFIRM',code=code)
    assert w.process_one() and w.dispatch_one()
    assert query(rt,'SELECT original_job_id,job_id FROM pilot_review_commands')==[{'original_job_id':image['id'],'job_id':cmd['id']}]
    assert query(rt,"SELECT kind FROM pilot_review_attempts WHERE phase='final'")==[{'kind':'correction'}]
    assert len(owner['state']['sends'])==2 and not owner['state']['pdfs']


def test_public_pdf_capability_expires_with_campaign_and_revocation(owner):
    from services.twilio_media import MediaGrantError
    rt,_=approve(owner);w=worker(owner,rt);admit(owner,rt)
    assert w.process_one() and w.dispatch_one() and w.process_one() and w.dispatch_one()
    path=urlsplit(owner['state']['sends'][-1]['MediaUrl']).path.split('/')
    assert rt.publisher.get(path[-2],path[-1],head=True).startswith(b'%PDF-')
    revoke_review_pilot(rt.store,rt.approval_id,owner['clock'][0])
    with pytest.raises(MediaGrantError):rt.publisher.get(path[-2],path[-1],head=True)


def test_unresolved_previous_attempt_blocks_activation_without_reset(owner):
    old=owner['old']
    with old.store._connect() as db:
        old.store.add_outbox(db,{'id':'old-job','tenant_id':TENANT,'phone_number_id':old.config.phone_id,'sender':SENDER},owner['clock'][0],'Uncertain older result')
        db.execute("UPDATE wa_outbox SET status='reconciliation_required'")
    before=history(old)
    with pytest.raises(ProviderError):approve(owner)
    assert history(old)==before
    assert query(old,'SELECT status FROM wa_outbox')==[{'status':'reconciliation_required'}]


def test_live_factory_requires_mount_and_cost_guard_without_network(owner,monkeypatch):
    rt,_=approve(owner)
    with pytest.raises(ValueError):build_review_pilot_runtime(rt.config,rt.approval_id,enabled=True,clock=lambda:owner['clock'][0])
    with pytest.raises(ValueError):build_review_pilot_runtime(rt.config,rt.approval_id,enabled=True,clock=lambda:owner['clock'][0],cost_gate=lambda *a:True)
    assert not owner['state']['requests']


def test_real_store_admission_hook_binds_once_and_rejection_leaves_no_job(owner):
    from test_twilio_live_pilot import form
    rt,_=approve(owner,max_uploads=1)
    first=form('SM'+'1'*32)
    assert rt.store.ingest_twilio(first,owner['clock'][0],account_sid=ACCOUNT,business_sender=BUSINESS,
                                  admission_hook=rt.provider.admit_inbound)==1
    assert rt.store.ingest_twilio(first,owner['clock'][0],account_sid=ACCOUNT,business_sender=BUSINESS,
                                  admission_hook=rt.provider.admit_inbound)==0
    second=form('SM'+'2'*32)
    with pytest.raises(ValueError):
        rt.store.ingest_twilio(second,owner['clock'][0],account_sid=ACCOUNT,business_sender=BUSINESS,
                              admission_hook=rt.provider.admit_inbound)
    assert len(query(rt,'SELECT * FROM pilot_review_uploads'))==1
    assert not query(rt,'SELECT * FROM wa_jobs WHERE message_id=?',(second['MessageSid'],))
    assert not owner['state']['requests']


@pytest.mark.parametrize('profile',['canonical_v1','hotel_daily_table_v1'])
def test_initial_synthetic_fixtures_validate_exact_approved_profile(owner,profile):
    from services.forecast_media_review import evaluate_forecast_media
    rt,_=approve(owner,report_profile=profile,input_policy='synthetic_exact')
    reference=datetime.fromtimestamp(owner['clock'][0],timezone.utc)
    matrix=validate_whatsapp_forecast(rt.fixtures['application/pdf'],'application/pdf',HOTEL_CODE,292,reference,report_profile=profile)
    assert len(matrix)==7 and all(row['departures']==200 and row['forecast_occupied']==120 for row in matrix)
    # Both generated artifacts are fictional format-equivalent input, not a
    # renamed real report; photo evaluation remains human-review-required.
    evaluation=evaluate_forecast_media(rt.fixtures['image/png'],'image/png',HOTEL_CODE,292,reference,report_profile=profile)
    assert evaluation['automatic_roster_allowed'] is False and evaluation['requires_review'] is True
    assert evaluation['candidate_valid_for_confirmation'] is True
    assert evaluation['rows']==matrix


def test_unset_inventory_after_activation_allows_owner_repair_but_only_correction(owner):
    rt,_=approve(owner)
    rt.store.save_hotel_parameters(MANAGER,{'room_capacity':None})
    assert rt.owner_active_for_app(TENANT,MANAGER,owner['clock'][0]) is True
    rebuilt=build_review_pilot_runtime(rt.config,rt.approval_id,enabled=True,transport=owner['mock'],clock=lambda:owner['clock'][0],cost_gate=lambda *a:True)
    owner['runtime'][0]=rebuilt
    w=worker(owner,rebuilt);admit(owner,rebuilt)
    assert w.process_one() and w.dispatch_one()
    assert len(owner['state']['sends'])==1 and 'inventory is not configured' in owner['state']['sends'][0]['Body']
    assert not owner['state']['pdfs'] and not rebuilt.store.list_weekly_roster_versions(MANAGER)
    assert query(rebuilt,'SELECT kind FROM pilot_review_attempts')==[{'kind':'correction'}]
    rebuilt.store.save_hotel_parameters(MANAGER,{'room_capacity':292})
    assert rebuilt.assert_active()['approval_id']=='owner-run'


def test_activation_requires_initially_valid_parameters(owner):
    owner['old'].store.save_hotel_parameters(MANAGER,{'room_capacity':None})
    with pytest.raises(ProviderError):approve(owner)
    assert not owner['state']['requests']


def test_owner_runtime_uses_shared_private_quota_without_changing_legacy_store(owner):
    from services.private_storage_budget import QuotaPrivateFileStore
    assert not isinstance(owner['old'].files,QuotaPrivateFileStore)
    rt,_=approve(owner)
    assert isinstance(rt.files,QuotaPrivateFileStore)
    assert rt.provider.permits_review(rt.store,rt.files) is True
    assert rt.provider.permits_review(owner['old'].store,owner['old'].files) is False


@pytest.mark.parametrize('mime',['application/pdf','image/png'])
def test_stale_calculated_result_sends_exact_honest_notice_within_two_attempts(owner,mime):
    rt,_=approve(owner);w=worker(owner,rt);original=admit(owner,rt,mime=mime)
    assert w.process_one() and w.dispatch_one()
    if mime=='image/png':
        code=re.search(r'CONFIRM ([0-9a-f]{16})',owner['state']['sends'][-1]['Body']).group(1)
        final_job=admit(owner,rt,command='CONFIRM',code=code)
    else:final_job=original
    assert w.process_one()
    assert len(rt.store.list_weekly_roster_versions(MANAGER))==1
    rt.store.save_hotel_parameters(MANAGER,{'checkout_minutes':24})
    assert w.dispatch_one()
    from services.twilio_review_pilot import _STALE_RESULT_NOTICE
    assert len(owner['state']['sends'])==2 and owner['state']['sends'][-1]['Body']==_STALE_RESULT_NOTICE
    assert not owner['state']['pdfs']
    attempts=query(rt,'SELECT phase,kind,original_job_id FROM pilot_review_attempts ORDER BY rowid')
    assert len(attempts)==2 and attempts[-1]=={'phase':'final','kind':'correction','original_job_id':original['id']}
    result=query(rt,"SELECT status,kind,object_key FROM wa_outbox WHERE job_id=? AND purpose='result'",(final_job['id'],))[0]
    assert result=={'status':'accepted','kind':'text','object_key':None}
    assert w.dispatch_one() is False and len(rt.store.list_weekly_roster_versions(MANAGER))==1


@pytest.mark.parametrize('mime',['application/pdf','image/png'])
def test_calculated_job_does_not_authorize_approximate_or_arbitrary_stale_notice(owner,mime):
    from services.twilio_review_pilot import _STALE_RESULT_NOTICE
    rt,_=approve(owner);w=worker(owner,rt);original=admit(owner,rt,mime=mime)
    assert w.process_one() and w.dispatch_one()
    if mime=='image/png':
        code=re.search(r'CONFIRM ([0-9a-f]{16})',owner['state']['sends'][-1]['Body']).group(1)
        final_job=admit(owner,rt,command='CONFIRM',code=code)
    else:final_job=original
    assert w.process_one()
    altered=_STALE_RESULT_NOTICE+' Unapproved added text.'
    with rt.store._connect() as db:
        db.execute("UPDATE wa_outbox SET status='sending',attempts=1,kind='text',text=?,object_key=NULL,filename=NULL WHERE job_id=? AND purpose='result'",(altered,final_job['id']))
    item=query(rt,"SELECT id FROM wa_outbox WHERE job_id=? AND purpose='result'",(final_job['id'],))[0]
    with pytest.raises(ProviderError):rt.provider.send_text(SENDER,altered,item['id'])
    assert len(owner['state']['sends'])==1 and len(query(rt,'SELECT * FROM pilot_review_attempts'))==1
