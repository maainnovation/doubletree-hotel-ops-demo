"""Actual composition, signed ingress and shared app workflow; mock provider only."""
from datetime import datetime,timezone
from io import BytesIO
import re
import sqlite3
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from twilio.request_validator import RequestValidator

from api.twilio_review_pilot import create_app
from services.owner_review_runtime import build_owner_components,legacy_config_from_environment
from services.twilio_pilot import PilotConfigurationError,MANAGER
from services.twilio_review_pilot import create_review_pilot_approval
from scripts.twilio_review_pilot_worker import run_worker,singleton_worker
from scripts.run_twilio_review_pilot import child_commands
from test_twilio_live_pilot import env,form,ACCOUNT,TOKEN,ORIGIN,SENDER
from test_twilio_review_pilot import owner,rows,history

KEY='fictional-owner-key-for-local-tests-only-00001'

class AllowMemory:
    def __call__(self,stage):return True

@pytest.fixture
def composed(owner):
    config=owner['config'];clock=owner['clock'];state=owner['state']
    create_review_pilot_approval(owner['old'].store,config,'owner-test',clock[0],report_profile='canonical_v1',input_policy='redacted_owner_reports')
    def handler(request):
        if request.url.path.endswith('/Usage/Records.json'):
            now=datetime.fromtimestamp(clock[0],timezone.utc)
            state.setdefault('usage_reads',[]).append(str(request.url))
            return httpx.Response(state.get('usage_status',200),json={'next_page_uri':None,'usage_records':[{
                'account_sid':ACCOUNT,'category':'totalprice','price':'20.00','price_unit':'usd',
                'start_date':datetime.fromtimestamp(state.setdefault('approved_at',clock[0]),timezone.utc).date().isoformat(),
                'end_date':now.date().isoformat(),'as_of':now.isoformat()}]})
        return owner['mock'].handle_request(request)
    environment=env(config.data_root)|{'PILOT_ENABLED':'0','PILOT_REVIEW_ENABLED':'1','PILOT_REVIEW_RESOURCE_CONFIRMED':'1',
      'PILOT_REVIEW_APPROVAL_ID':'owner-test','PILOT_REVIEW_APP_KEY':KEY,'PILOT_REVIEW_WHATSAPP_ENABLED':'1'}
    c=build_owner_components(environment,clock=lambda:clock[0],transport=httpx.MockTransport(handler),memory_guard_factory=AllowMemory)
    owner['runtime'][0]=c.runtime
    assert c.cost_monitor.initialize_baseline()
    def evaluator(data,mime,*args,**kwargs):
        matrix=rows(c.runtime,clock[0])
        if mime=='application/pdf':return {'method':'native_pdf','automatic_roster_allowed':True,'requires_review':False,'rows':matrix}
        return {'method':'local_image_ocr','automatic_roster_allowed':False,'requires_review':True,'rows':matrix,'candidate_valid_for_confirmation':True}
    c.media_evaluator=evaluator;c.app_workflow.media_evaluator=evaluator
    with TestClient(create_app(c),base_url=ORIGIN) as client:
        yield owner,c,client,environment


def signed(client,data,path='/webhooks/twilio/whatsapp',**kw):
    return client.post(path,content=urlencode(data),headers={'Content-Type':'application/x-www-form-urlencoded','X-Twilio-Signature':RequestValidator(TOKEN).compute_signature(ORIGIN+path,data)},**kw)


def upload(owner,c,client,number=1,mime='application/pdf'):
    data=form('SM'+format(number,'032x'));data['MediaContentType0']=mime
    owner['state']['media'][data['MediaUrl0']]=(c.runtime.fixtures[mime],mime)
    assert signed(client,data).status_code==200
    return data


def count(c,table):
    with c.runtime.store._connect() as db:return db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]


def test_default_disabled_without_any_config_and_private_auth_denied():
    assert build_owner_components({}) is None
    with TestClient(create_app()) as client:
        assert client.get('/health').json()['status']=='disabled'
        assert client.get('/private/api/rosters').status_code==401
        assert client.post('/webhooks/twilio/whatsapp').status_code==503


@pytest.mark.parametrize('change',[{'PILOT_ENABLED':'1'},{'PILOT_REVIEW_RESOURCE_CONFIRMED':'0'},
 {'PILOT_REVIEW_APP_KEY':''},{'PILOT_REVIEW_APP_KEY':'bad key'}, {'PILOT_REVIEW_APP_KEY':ACCOUNT}, {'PILOT_REVIEW_APP_KEY':'x'*32+'\x00'}, {'PILOT_REVIEW_APPROVAL_ID':'../bad'}, {'PILOT_REVIEW_RESOURCE_PROFILE':'custom_unbounded'}])
def test_missing_explicit_gates_fail_closed(composed,change):
    owner,c,client,environment=composed
    with pytest.raises(PilotConfigurationError):build_owner_components(environment|change,transport=owner['mock'],memory_guard_factory=AllowMemory)
    assert not owner['state']['sends']


def test_live_cannot_substitute_resource_guard(composed):
    _,_,_,environment=composed
    with pytest.raises(PilotConfigurationError):build_owner_components(environment,memory_guard_factory=AllowMemory)


def test_owner_guard_uses_explicit_environment_profile_not_process_default(composed,monkeypatch):
    owner,_,_,environment=composed
    monkeypatch.delenv('PILOT_REVIEW_RESOURCE_PROFILE',raising=False)
    c=build_owner_components(environment|{'PILOT_REVIEW_RESOURCE_PROFILE':'render_standard_2g'},
        clock=lambda:owner['clock'][0],transport=owner['mock'])
    guard=c.memory_guard_factory()
    assert guard.profile=='render_standard_2g' and guard.memory_limit==2048*1024*1024 and guard.cpu_cores==1
    assert not owner['state']['sends']


def test_signed_pdf_ack_result_visible_in_same_private_app(composed):
    owner,c,client,_=composed;before=history(c.runtime)
    assert client.get('/health').json()['live_test_ready'] is False
    run_worker(c,once=True)
    assert client.get('/health').json()['live_test_ready'] is True
    data=upload(owner,c,client)
    assert count(c,'review_cost_inbound')==1 and count(c,'pilot_review_uploads')==1
    assert not owner['state']['sends'] # webhook does no extraction or send
    run_worker(c,once=True);run_worker(c,once=True)
    assert len(owner['state']['sends'])==2 and len(owner['state']['pdfs'])==1
    pdf=PdfReader(BytesIO(owner['state']['pdfs'][0]));assert len(pdf.pages)>=1
    auth={'Authorization':'Bearer '+KEY}
    versions=client.get('/private/api/rosters',headers=auth).json()['versions']
    assert len(versions)==1 and history(c.runtime)==before
    assert signed(client,data).status_code==200
    run_worker(c,once=True)
    assert count(c,'review_cost_inbound')==1 and len(owner['state']['sends'])==2


def test_photo_requires_signed_bound_confirmation_and_no_repeated_reply(composed):
    owner,c,client,_=composed
    upload(owner,c,client,mime='image/png');run_worker(c,once=True)
    assert len(owner['state']['sends'])==1 and not owner['state']['pdfs']
    code=re.search(r'CONFIRM ([0-9a-f]{16})',owner['state']['sends'][0]['Body']).group(1)
    cmd=form('SM'+'2'*32)
    cmd={k:v for k,v in cmd.items() if not k.startswith('Media')}
    cmd.update(NumMedia='0',Body='CONFIRM '+code)
    assert signed(client,cmd).status_code==200
    run_worker(c,once=True)
    assert len(owner['state']['sends'])==2 and len(owner['state']['pdfs'])==1
    assert count(c,'pilot_review_attempts')==2
    assert signed(client,cmd).status_code==200
    run_worker(c,once=True)
    assert len(owner['state']['sends'])==2


def test_rejected_owner_input_still_counted_once_after_signature(composed):
    owner,c,client,_=composed
    data=form();data['From']='whatsapp:+15555550999'
    response=signed(client,data)
    assert response.status_code==200 # silent ACK to unenrolled sender; no job or reply
    assert count(c,'review_cost_inbound')==1 and count(c,'pilot_review_uploads')==0
    assert signed(client,data).status_code==200 and count(c,'review_cost_inbound')==1
    response=client.post('/webhooks/twilio/whatsapp',data=form())
    assert response.status_code==401 and count(c,'review_cost_inbound')==1
    assert not owner['state']['sends']


def test_form_boundaries_no_mutation(composed):
    _,c,client,_=composed
    for raw,expected in [('x=%GG',400),('x=1&x=2',400),('x='+'a'*65536,413)]:
        response=client.post('/webhooks/twilio/whatsapp',content=raw,headers={'content-type':'application/x-www-form-urlencoded'})
        assert response.status_code==expected
    assert signed(client,form(),path='/webhooks/twilio/whatsapp?query=1').status_code==400
    assert count(c,'review_cost_inbound')==0


def test_usage_unavailable_stops_send_not_reset_attempts(composed):
    owner,c,client,_=composed
    upload(owner,c,client);owner['state']['usage_status']=500
    run_worker(c,once=True)
    assert not owner['state']['sends'] and count(c,'pilot_review_attempts')==0
    assert client.get('/health').json()['cost_ready'] is False
    owner['state']['usage_status']=200
    run_worker(c,once=True)
    assert not owner['state']['sends'] # failed outbox is not automatically resent


def test_private_upload_review_calculation_never_enqueues_whatsapp(composed):
    owner,c,client,_=composed
    headers={'Authorization':'Bearer '+KEY,'Content-Type':'application/pdf','Idempotency-Key':'app-file-001'}
    response=client.post('/private/api/forecast-uploads',content=c.runtime.fixtures['application/pdf'],headers=headers)
    assert response.status_code==202,response.text
    upload_id=response.json()['id']
    run_worker(c,once=True)
    candidate=client.get('/private/api/forecast-uploads/'+upload_id,headers=headers).json()
    assert candidate['status']=='needs_review'
    response=client.post('/private/api/forecast-uploads/'+upload_id+'/confirm',headers={'Authorization':'Bearer '+KEY},json={'expected_revision':candidate['revision']})
    assert response.status_code==200,response.text
    run_worker(c,once=True)
    result=client.get('/private/api/forecast-uploads/'+upload_id,headers=headers).json()
    assert result['status'] in {'calculated','needs_attention'}
    assert count(c,'wa_jobs')==1 # original historical failed record only
    assert count(c,'wa_outbox')==0 and not owner['state']['sends']


def test_expiry_revokes_key_intake_and_fixture_but_keeps_known_callback_route(composed):
    owner,c,client,_=composed
    owner['clock'][0]=c.runtime.expires_at+1
    assert client.get('/private/api/rosters',headers={'Authorization':'Bearer '+KEY}).status_code==401
    assert client.get('/synthetic-forecast.pdf').status_code==503
    assert signed(client,form()).status_code==503
    assert signed(client,{'AccountSid':ACCOUNT},path='/webhooks/twilio/status').status_code==204 # unknown callback is ignored
    assert client.get('/health').json()['live_test_ready'] is False
    assert run_worker(c,once=True)['active'] is False


def test_singleton_worker_refuses_parallel_and_symlink(tmp_path):
    with singleton_worker(tmp_path):
        with pytest.raises(BlockingIOError):
            with singleton_worker(tmp_path):pass
    (tmp_path/'.review-worker.lock').unlink()
    (tmp_path/'victim').write_text('safe')
    (tmp_path/'.review-worker.lock').symlink_to(tmp_path/'victim')
    with pytest.raises(OSError):
        with singleton_worker(tmp_path):pass
    assert (tmp_path/'victim').read_text()=='safe'


def test_review_supervisor_keeps_one_api_and_worker_without_access_logs():
    api,worker=child_commands({'PORT':'10000'})
    assert api[3]=='api.twilio_review_pilot:app' and api[api.index('--workers')+1]=='1'
    assert '--no-access-log' in api and '--no-proxy-headers' in api
    assert worker[2]=='scripts.twilio_review_pilot_worker'


def test_durable_revocation_invalidates_private_key_immediately(composed):
    from services.twilio_review_pilot import revoke_review_pilot
    owner,c,client,_=composed
    headers={'Authorization':'Bearer '+KEY}
    assert client.get('/private/api/rosters',headers=headers).status_code==200
    revoke_review_pilot(c.runtime.store,'owner-test',owner['clock'][0])
    assert client.get('/private/api/rosters',headers=headers).status_code==401
    assert signed(client,form()).status_code==503
    assert not owner['state']['sends']


def test_slow_webhook_body_times_out_and_releases_intake_lock(composed,monkeypatch):
    import asyncio
    import api.twilio_review_pilot as api
    from starlette.requests import Request
    owner,c,client,_=composed
    original=Request.stream
    async def stream(request):
        if request.headers.get('x-local-slow-test')=='1':
            await asyncio.sleep(1)
        async for chunk in original(request):yield chunk
    monkeypatch.setattr(Request,'stream',stream)
    monkeypatch.setattr(api,'WEBHOOK_RECEIVE_SECONDS',0.01)
    response=client.post('/webhooks/twilio/whatsapp',content='x=1',headers={
        'content-type':'application/x-www-form-urlencoded','x-local-slow-test':'1'})
    assert response.status_code==408 and count(c,'review_cost_inbound')==0
    upload(owner,c,client)
    assert count(c,'review_cost_inbound')==1


def test_default_whatsapp_pause_allows_private_qa_but_no_intake_or_dispatch(composed):
    owner,c,client,environment=composed
    environment.pop('PILOT_REVIEW_WHATSAPP_ENABLED')
    paused=build_owner_components(environment,clock=lambda:owner['clock'][0],transport=owner['mock'],memory_guard_factory=AllowMemory)
    assert paused.whatsapp_enabled is False
    paused.media_evaluator=c.media_evaluator
    paused.app_workflow.media_evaluator=c.media_evaluator
    with TestClient(create_app(paused),base_url=ORIGIN) as private_client:
        assert private_client.get('/private/api/rosters',headers={'Authorization':'Bearer '+KEY}).status_code==200
        assert signed(private_client,form()).status_code==503
        assert count(c,'pilot_review_uploads')==0
        with paused.runtime.store._connect() as db:
            assert paused.runtime.provider.budget.cost_gate(db,owner['clock'][0]) is False
        # Even already queued owner work is not consumed during private QA.
        upload(owner,c,client)
        before=count(c,'pilot_review_attempts')
        result=run_worker(paused,once=True)
        assert result=={'processed':0,'dispatched':0,'app_processed':0}
        assert count(c,'pilot_review_attempts')==before and not owner['state']['sends']
        state=private_client.get('/health').json()
        assert state['status']=='private_app_only' and state['private_app_ready'] is True
        assert state['whatsapp_enabled'] is False and state['live_test_ready'] is False
        response=private_client.post('/private/api/forecast-uploads',content=c.runtime.fixtures['application/pdf'],headers={
            'Authorization':'Bearer '+KEY,'Content-Type':'application/pdf','Idempotency-Key':'paused-app-001'})
        assert response.status_code==202
        assert run_worker(paused,once=True)=={'processed':0,'dispatched':0,'app_processed':1}
        assert not owner['state']['sends'] and count(c,'pilot_review_attempts')==before


def test_disabled_startup_warms_actual_fixture_dependencies_without_onnx(tmp_path):
    import os,subprocess,sys,json
    code="""import json,sys
from services.forecast_media_worker import _install_network_denial
_install_network_denial()
from services.owner_review_runtime import build_owner_components,warm_owner_dependencies
assert build_owner_components({}) is None
from services import owner_review_runtime as owner
before=id(owner._WARM_FIXTURES)
warm_owner_dependencies()
assert id(owner._WARM_FIXTURES)==before
print(json.dumps({'profiles':len(owner._WARM_FIXTURES),'onnx':sys.modules.get('onnxruntime') is not None,'pymupdf':'pymupdf' in sys.modules}))
"""
    result=subprocess.run([sys.executable,'-c',code],env=dict(os.environ,ORT_DISABLE_TELEMETRY='1'),
        capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr.decode()[:300]
    assert json.loads(result.stdout)=={'profiles':2,'onnx':False,'pymupdf':True}
