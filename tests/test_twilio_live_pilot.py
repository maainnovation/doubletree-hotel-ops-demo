"""All external transport mocked; these test the separate deployable pilot gates."""
from datetime import datetime,timezone,timedelta
from email.utils import format_datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path
from urllib.parse import parse_qs,urlencode

from fastapi.testclient import TestClient
import httpx
from pypdf import PdfReader
import pytest
from twilio.request_validator import RequestValidator

from api.twilio_pilot import create_app
from scripts.demo_whatsapp_workflow import DEMO_NOW
from scripts.twilio_pilot_worker import run_worker
from services.twilio_pilot import PilotConfig,PilotConfigurationError,build_runtime,MANAGER,MAX_SEND_ATTEMPTS,synthetic_forecast
from services.whatsapp_provider import ProviderError
from services.whatsapp_workflow import ForecastWorker

ACCOUNT='AC'+'a'*32
INBOUND='SM'+'b'*32
TOKEN='synthetic-secret-never-real'
BUSINESS='15555550900'
SENDER='15555550123'
ORIGIN='https://synthetic-pilot.onrender.com'


def env(root):
    return {'PILOT_ENABLED':'1','PILOT_VOLUME_CONFIRMED':'1','TWILIO_ACCOUNT_SID':ACCOUNT,'TWILIO_AUTH_TOKEN':TOKEN,
            'TWILIO_WHATSAPP_FROM':BUSINESS,'PILOT_MANAGER_PHONE':SENDER,'PILOT_EXTERNAL_ORIGIN':ORIGIN,
            'PILOT_APPROVED_UNTIL':(DEMO_NOW+timedelta(hours=2)).isoformat(),'PILOT_WEEK_START':'2026-10-23','PILOT_DATA_ROOT':str(root)}


def form(sid=INBOUND):
    return {'AccountSid':ACCOUNT,'MessageSid':sid,'From':'whatsapp:+'+SENDER,'To':'whatsapp:+'+BUSINESS,
            'NumMedia':'1','MediaContentType0':'application/pdf','MediaUrl0':f'https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages/{sid}/Media/ME'+'c'*32}


def signed(client,data=None,path='/webhooks/twilio/whatsapp'):
    data=form() if data is None else data
    return client.post(path,content=urlencode(data),headers={'Content-Type':'application/x-www-form-urlencoded','X-Twilio-Signature':RequestValidator(TOKEN).compute_signature(ORIGIN+path,data)})


@pytest.fixture
def pilot(tmp_path):
    clock=[DEMO_NOW.timestamp()]
    config=PilotConfig.from_env(env(tmp_path/'pilot'),now=clock[0])
    fixture=synthetic_forecast(config.week_start)
    state={'fixture':fixture,'requests':[],'sends':[],'pdfs':[]}
    client=None
    def transport(request):
        state['requests'].append((request.method,str(request.url)))
        assert request.url.host=='api.twilio.com'
        if request.method=='GET' and request.url.path.endswith('.json'):
            return httpx.Response(200,json={'sid':request.url.path.split('/')[-1][:-5],'account_sid':ACCOUNT,'from':'whatsapp:+'+SENDER,'to':'whatsapp:+'+BUSINESS,'direction':'inbound','date_created':format_datetime(DEMO_NOW)})
        if request.method=='GET':
            return httpx.Response(200,content=state['fixture'],headers={'Content-Type':'application/pdf'})
        payload={k:v[0] for k,v in parse_qs(request.content.decode()).items()}
        state['sends'].append(payload)
        if 'MediaUrl' in payload:
            response=client.get(payload['MediaUrl'])
            assert response.status_code==200
            state['pdfs'].append(response.content)
        return httpx.Response(201,json={'sid':'SM'+'d'*32})
    mock=httpx.MockTransport(transport)
    rt=build_runtime(config,transport=mock,clock=lambda:clock[0])
    with TestClient(create_app(rt),base_url=ORIGIN,client=('198.51.100.20',20000)) as client:
        yield rt,client,state,clock,mock


def test_remote_signed_roundtrip_exact_synthetic_fixture_only(pilot):
    rt,client,state,_,_=pilot
    assert client.get('/health').status_code==503
    run_worker(rt,once=True)
    assert client.get('/health').json()['live_test_ready'] is True
    source=client.get('/synthetic-forecast.pdf')
    assert source.status_code==200 and source.content==rt.fixture
    assert signed(client).status_code==200 and not state['requests']
    assert run_worker(rt,once=True)=={'processed':1,'dispatched':1}
    assert len(state['sends'])==1 and len(state['pdfs'])==1
    text='\n'.join(p.extract_text() for p in PdfReader(BytesIO(state['pdfs'][0])).pages)
    assert 'Day-by-day calculation summary' in text and 'NEEDS ATTENTION' in text
    assert rt.store.get_staff_settings(MANAGER)['settings']['hotel_inventory']['room_capacity']==292
    assert len(rt.store.list_weekly_roster_versions(MANAGER))==1
    assert signed(client).status_code==200
    assert run_worker(rt,once=True)=={'processed':0,'dispatched':0}


@pytest.mark.parametrize('changed',[
    {'PILOT_ENABLED':'0'},{'PILOT_VOLUME_CONFIRMED':'0'},{'PILOT_MANAGER_PHONE':'not-number'},
    {'PILOT_EXTERNAL_ORIGIN':'http://pilot.onrender.com'},{'PILOT_EXTERNAL_ORIGIN':'https://evil.example'},
    {'PILOT_EXTERNAL_ORIGIN':'https://pilot.onrender.com@evil.example'},
    {'PILOT_EXTERNAL_ORIGIN':'https://pilot.onrender.com/path'},{'TWILIO_AUTH_TOKEN':''},
    {'PILOT_APPROVED_UNTIL':(DEMO_NOW-timedelta(seconds=1)).isoformat()},
    {'PILOT_APPROVED_UNTIL':(DEMO_NOW+timedelta(days=2)).isoformat()},
    {'PILOT_WEEK_START':'2026-10-22'},{'PILOT_WEEK_START':'2026-10-16'},
    {'PILOT_DATA_ROOT':'relative'},{'PILOT_DATA_ROOT':'/var/data/../secret'}])
def test_config_fail_closed(tmp_path,changed):
    config=env(tmp_path/'pilot');config.update(changed)
    with pytest.raises(PilotConfigurationError):PilotConfig.from_env(config,now=DEMO_NOW.timestamp())


def test_credentials_never_in_config_repr(tmp_path):
    config=PilotConfig.from_env(env(tmp_path/'pilot'),now=DEMO_NOW.timestamp())
    assert TOKEN not in repr(config)


def test_real_transport_cannot_use_unmounted_development_root(tmp_path):
    config=PilotConfig.from_env(env(tmp_path/'pilot'),now=DEMO_NOW.timestamp())
    with pytest.raises(PilotConfigurationError,match='persistent mount'):build_runtime(config,clock=lambda:DEMO_NOW.timestamp())
    assert not config.data_root.exists()


def test_existing_nonpilot_database_never_adopted(tmp_path):
    import sqlite3
    root=tmp_path/'pilot';root.mkdir()
    with sqlite3.connect(root/'pilot.sqlite3') as db:db.execute('CREATE TABLE real_data(value TEXT)')
    before=(root/'pilot.sqlite3').read_bytes()
    config=PilotConfig.from_env(env(root),now=DEMO_NOW.timestamp())
    with pytest.raises(PilotConfigurationError,match='non-pilot state'):build_runtime(config,transport=httpx.MockTransport(lambda r:None),clock=lambda:DEMO_NOW.timestamp())
    assert (root/'pilot.sqlite3').read_bytes()==before


def test_restart_preserves_state_budget_and_identity(pilot):
    rt,client,state,clock,mock=pilot
    signed(client);run_worker(rt,once=True)
    restarted=build_runtime(rt.config,transport=mock,clock=lambda:clock[0])
    assert len(restarted.store.rows('wa_jobs'))==1
    with restarted.store._connect() as db:assert db.execute('SELECT COUNT(*) FROM pilot_send_attempts').fetchone()[0]==1
    changed=env(rt.config.data_root);changed['PILOT_MANAGER_PHONE']='15555550124'
    with pytest.raises(PilotConfigurationError,match='no state was overwritten'):
        build_runtime(PilotConfig.from_env(changed,now=clock[0]),transport=mock,clock=lambda:clock[0])


def test_non_fixture_pdf_blocked_before_parser_or_storage(pilot,monkeypatch):
    rt,client,state,_,_=pilot
    state['fixture']=b'%PDF-malicious-untrusted-not-approved'
    def forbidden(*a,**k):raise AssertionError('Unapproved bytes reached parser')
    monkeypatch.setattr('services.whatsapp_workflow.validate_whatsapp_forecast',forbidden)
    signed(client);run_worker(rt,once=True)
    assert not rt.store.list_weekly_roster_versions(MANAGER)
    assert 'Body' in state['sends'][0] and not state['pdfs']
    assert not list(rt.files.root.rglob('*.pdf'))


def test_settings_change_revokes_every_hosted_route_and_send(pilot):
    rt,client,state,_,_=pilot
    signed(client)
    rt.store.save_hotel_parameters(MANAGER,{'room_capacity':300})
    assert client.get('/synthetic-forecast.pdf').status_code==503
    assert signed(client).status_code==503
    with pytest.raises(ProviderError):run_worker(rt,once=True)
    assert not state['sends']


def test_expiry_blocks_worker_webhooks_and_media(pilot):
    rt,client,state,clock,_=pilot
    signed(client);run_worker(rt,once=True)
    url=state['sends'][0]['MediaUrl']
    clock[0]=rt.config.approved_until
    assert client.get(url).status_code==503
    assert signed(client).status_code==503
    with pytest.raises(ProviderError):run_worker(rt,once=True)


def test_persistent_message_budget_caps_attempts_even_with_more_inputs(pilot):
    rt,client,state,_,_=pilot
    state['fixture']=b'unknown-file'
    for i in range(5):
        assert signed(client,form('SM'+f'{i:032x}')).status_code==200
        run_worker(rt,once=True)
    assert len(state['sends'])==MAX_SEND_ATTEMPTS
    with rt.store._connect() as db:assert db.execute('SELECT COUNT(*) FROM pilot_send_attempts').fetchone()[0]==3


def test_intake_quota_duplicate_does_not_consume_extra(pilot):
    rt,client,state,_,_=pilot
    for i in range(10):assert signed(client,form('SM'+f'{i:032x}')).status_code==200
    assert signed(client,form('SM'+f'{0:032x}')).status_code==200
    assert signed(client,form('SM'+f'{11:032x}')).status_code==400
    assert len(rt.store.rows('wa_jobs'))==10 and not state['requests']


def test_public_surface_excludes_admin_manual_app_and_raw_files(pilot):
    _,client,state,_,_=pilot
    for path in ['/docs','/openapi.json','/api/chat','/api/today','/data/pilot.sqlite3','/private','/']:
        assert client.get(path).status_code==404
    assert client.get('/media/twilio/not-a-token/file.pdf').status_code==404
    assert not state['requests']


def test_disabled_deploy_does_not_read_credentials_or_create_provider(monkeypatch):
    monkeypatch.setenv('PILOT_ENABLED','0')
    monkeypatch.setattr('api.twilio_pilot.build_runtime',lambda *a,**k:(_ for _ in ()).throw(AssertionError('disabled')))
    with TestClient(create_app()) as client:
        assert client.get('/health').json()['status']=='disabled'
        assert client.get('/synthetic-forecast.pdf').status_code==503
        assert signed(client).status_code==503


def test_remote_requests_cannot_bypass_signature_or_body_limits(pilot):
    rt,client,state,_,_=pilot
    assert client.post('/webhooks/twilio/whatsapp',content=urlencode(form()),headers={'Content-Type':'application/x-www-form-urlencoded'}).status_code==401
    assert client.post('/webhooks/twilio/whatsapp',content='x'*65537,headers={'Content-Type':'application/x-www-form-urlencoded'}).status_code==413
    assert not rt.store.rows('wa_jobs') and not state['requests']


def test_global_request_limit(pilot):
    _,client,_,_,_=pilot
    for _ in range(60):assert client.get('/synthetic-forecast.pdf').status_code==200
    assert client.get('/synthetic-forecast.pdf').status_code==429


def test_direct_config_cannot_bypass_expiry_validation(tmp_path):
    from dataclasses import replace
    config=PilotConfig.from_env(env(tmp_path/'pilot'),now=DEMO_NOW.timestamp())
    config=replace(config,approved_until=DEMO_NOW.timestamp()+86401)
    with pytest.raises(PilotConfigurationError):
        build_runtime(config,transport=httpx.MockTransport(lambda r:None),clock=lambda:DEMO_NOW.timestamp())
    assert not config.data_root.exists()


@pytest.mark.parametrize('name',['.init.lock','pilot.sqlite3','pilot.sqlite3-wal','pilot.sqlite3-shm','pilot.sqlite3-journal','private'])
def test_state_symlinks_rejected_without_touching_target(tmp_path,name):
    root=tmp_path/'pilot';root.mkdir()
    target=tmp_path/'outside';target.write_bytes(b'untouched')
    (root/name).symlink_to(target)
    config=PilotConfig.from_env(env(root),now=DEMO_NOW.timestamp())
    with pytest.raises(PilotConfigurationError,match='links'):
        build_runtime(config,transport=httpx.MockTransport(lambda r:None),clock=lambda:DEMO_NOW.timestamp())
    assert target.read_bytes()==b'untouched'
