"""Signed Twilio form -> same roster engine -> private PDF URL, entirely mocked."""
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
from io import BytesIO
from urllib.parse import parse_qs, urlencode

from fastapi.testclient import TestClient
import httpx
from pypdf import PdfReader
import pytest
from twilio.request_validator import RequestValidator

from scripts.demo_whatsapp_workflow import DEMO_CODE, DEMO_NOW, DEMO_SENDER, FrozenDateTime, forecast_pdf
from services.staff_settings import demo_defaults
from services.twilio_media import TwilioMediaPublisher
from services.twilio_provider import TwilioWhatsAppProvider, twilio_business_id
from services.whatsapp_provider import provider_from_env, ProviderError
from services.whatsapp_store import PrivateFileStore, WorkflowStore
from services.whatsapp_workflow import ForecastWorker

ACCOUNT = 'AC' + 'a' * 32
INBOUND = 'SM' + 'b' * 32
OUTBOUND = 'SM' + 'd' * 32
BUSINESS = '15555550900'
TOKEN = 'synthetic-twilio-auth-token'
BASE = 'https://synthetic-pilot.example'
WEBHOOK = BASE + '/webhooks/twilio/whatsapp?route=pilot'
CALLBACK = BASE + '/webhooks/twilio/status'


def form_data(**overrides):
    form = {'AccountSid': ACCOUNT, 'MessageSid': INBOUND, 'From': 'whatsapp:+' + DEMO_SENDER,
            'To': 'whatsapp:+' + BUSINESS, 'NumMedia': '1', 'MediaContentType0': 'application/pdf',
            'MediaUrl0': f'https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT}/Messages/{INBOUND}/Media/ME' + 'c'*32,
            'Body': '', 'FutureTwilioField': 'must be signed too'}
    form.update(overrides)
    return form


def post(client, form=None, *, url=WEBHOOK, signing_url=None, signature=None):
    form = form_data() if form is None else form
    signature = signature or RequestValidator(TOKEN).compute_signature(signing_url or url, form)
    return client.post(url, content=urlencode(form), headers={'Content-Type': 'application/x-www-form-urlencoded', 'X-Twilio-Signature': signature})


@pytest.fixture
def system(tmp_path, monkeypatch):
    import api.main as api
    store = WorkflowStore(tmp_path / 'workflow.sqlite3')
    phone = twilio_business_id(ACCOUNT, BUSINESS)
    store.enroll_tenant(tenant_id='synthetic', manager_id='gm', hotel_code=DEMO_CODE, hotel_name='Synthetic Hotel')
    store.enroll_sender(tenant_id='synthetic', phone_number_id=phone, sender=DEMO_SENDER)
    store.save_staff_settings('gm', demo_defaults(), dataset_mode='demo')
    store.save_hotel_parameters('gm', {'room_capacity': 400})
    files = PrivateFileStore(tmp_path / 'private')
    clock = [DEMO_NOW.timestamp()]
    publisher = TwilioMediaPublisher(store, files, BASE, clock=lambda: clock[0])
    monkeypatch.setattr(api, 'store', store)
    monkeypatch.setattr(api, 'datetime', FrozenDateTime)
    monkeypatch.setattr(api, '_twilio_media_publisher', lambda: publisher)
    for key, value in {'WHATSAPP_FORECAST_ENABLED':'1', 'TWILIO_WEBHOOK_ENABLED':'1', 'TWILIO_DEV_MEDIA_ENABLED':'1', 'TWILIO_AUTH_TOKEN':TOKEN, 'TWILIO_ACCOUNT_SID':ACCOUNT, 'TWILIO_WHATSAPP_FROM':BUSINESS, 'TWILIO_WEBHOOK_URL':WEBHOOK, 'TWILIO_STATUS_CALLBACK_URL':CALLBACK}.items():
        monkeypatch.setenv(key, value)
    client = TestClient(api.app, base_url=BASE, client=('127.0.0.1', 12345))
    state = {'pdf':forecast_pdf(), 'calls':[], 'sent':[], 'downloaded':[], 'created':clock[0], 'post_error':None, 'metadata':{}}
    def transport(request):
        state['calls'].append((request.method, str(request.url)))
        assert request.url.host == 'api.twilio.com'
        if request.method == 'GET' and request.url.path.endswith('.json'):
            return httpx.Response(200, json={'sid':request.url.path.split('/')[-1][:-5], 'account_sid':ACCOUNT,
                'from':'whatsapp:+'+DEMO_SENDER, 'to':'whatsapp:+'+BUSINESS, 'direction':'inbound',
                'date_created':format_datetime(datetime.fromtimestamp(state['created'],timezone.utc)), **state['metadata']})
        if request.method == 'GET':
            return httpx.Response(200, content=state['pdf'], headers={'Content-Type':'application/pdf'})
        payload = {k:v[0] for k,v in parse_qs(request.content.decode()).items()}
        state['sent'].append(payload)
        if state['post_error'] == 'timeout':
            raise httpx.ReadTimeout('synthetic', request=request)
        if state['post_error'] == 'rate':
            return httpx.Response(429)
        if 'MediaUrl' in payload:
            head = client.head(payload['MediaUrl'])
            get = client.get(payload['MediaUrl'])
            assert head.status_code == get.status_code == 200
            assert head.headers['content-type'] == 'application/pdf'
            assert head.content == b'' and int(head.headers['content-length']) == len(get.content)
            state['downloaded'].append(get.content)
        return httpx.Response(201, json={'sid':OUTBOUND})
    provider = TwilioWhatsAppProvider(ACCOUNT, TOKEN, BUSINESS, media_publisher=publisher, status_callback_url=CALLBACK, transport=httpx.MockTransport(transport))
    worker = ForecastWorker(store, files, provider, clock=lambda:clock[0])
    return store, provider, worker, client, state, clock


def test_full_signed_pdf_workflow_uses_shared_parameters_and_private_delivery(system):
    store, provider, worker, client, state, clock = system
    assert post(client).status_code == 200
    assert not state['calls']  # Intake has no network/extraction/send.
    assert store.enrollment(provider.phone_number_id, DEMO_SENDER)['last_inbound'] == 0
    assert worker.drain_ready() == {'processed':1,'dispatched':1}
    roster = store.get_weekly_roster('gm', '2026-10-23')
    assert roster['source']['settings_revision'] == store.get_staff_settings('gm')['_settings_revision']
    assert store.rows('wa_outbox')[0]['status'] == 'accepted'
    payload = state['sent'][0]
    assert payload['To'] == 'whatsapp:+'+DEMO_SENDER and payload['From'] == 'whatsapp:+'+BUSINESS
    assert 'MediaUrl' in payload and 'Body' not in payload and payload['StatusCallback'] == CALLBACK
    text = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(state['downloaded'][0])).pages)
    assert 'Day-by-day calculation summary' in text and '2026-10-23' in text
    assert not provider.is_live


@pytest.mark.parametrize('changes', [{'From':'whatsapp:+15555550888'}, {'AccountSid':'AC'+'f'*32}, {'To':'whatsapp:+15555550888'}])
def test_unauthorized_identity_no_job_fetch_or_reply(system, changes):
    store, _, worker, client, state, _ = system
    assert post(client, form_data(**changes)).status_code == 200
    assert worker.drain_ready() == {'processed':0,'dispatched':0}
    assert not store.rows('wa_jobs') and not state['calls']


def test_signature_all_fields_and_exact_external_url(system):
    store, _, _, client, state, _ = system
    form = form_data()
    sig = RequestValidator(TOKEN).compute_signature(WEBHOOK, form)
    form['FutureTwilioField'] = 'tampered'
    assert post(client, form, signature=sig).status_code == 401
    assert post(client, signing_url=WEBHOOK.replace('https:', 'http:')).status_code == 401
    assert post(client, url=WEBHOOK+'&extra=1').status_code == 400
    assert client.post(WEBHOOK, json=form_data()).status_code == 415
    assert not store.rows('wa_jobs') and not state['calls']


@pytest.mark.parametrize('body', ['From=x&From=y', 'x=%XX', 'x=%ff', 'x'*65537])
def test_malformed_duplicate_oversize_form(system, body):
    assert system[3].post(WEBHOOK, content=body, headers={'Content-Type':'application/x-www-form-urlencoded'}).status_code in (400,413)
    assert not system[0].rows('wa_jobs')


@pytest.mark.parametrize('changes', [{'MediaUrl0':'http://localhost/secret'}, {'MediaUrl0':form_data()['MediaUrl0'].replace(INBOUND,'SM'+'e'*32)}, {'NumMedia':'2'}, {'SmsSid':'SM'+'f'*32}, {'MediaUrl1':form_data()['MediaUrl0']}])
def test_invalid_media_identity_fails_closed(system, changes):
    assert post(system[3], form_data(**changes)).status_code == 400
    assert not system[0].rows('wa_jobs') and not system[4]['calls']


def test_duplicate_signed_webhook_never_extends_window_or_replies(system):
    store, provider, worker, client, state, clock = system
    post(client); worker.drain_ready()
    before = store.enrollment(provider.phone_number_id, DEMO_SENDER)['last_inbound']
    clock[0] += 100
    post(client)
    assert worker.drain_ready() == {'processed':0,'dispatched':0}
    assert store.enrollment(provider.phone_number_id, DEMO_SENDER)['last_inbound'] == before
    assert len(state['sent']) == 1


@pytest.mark.parametrize('metadata', [{'from':'whatsapp:+15555550999'}, {'direction':'outbound-api'}, {'account_sid':'AC'+'f'*32}])
def test_worker_revalidates_provider_message_identity(system, metadata):
    store, _, worker, client, state, _ = system
    state['metadata'] = metadata
    post(client); worker.drain_ready()
    assert store.rows('wa_jobs')[0]['status'] == 'failed'
    assert not state['sent'] and len(state['calls']) == 1


def test_old_signed_message_cannot_open_twenty_four_hour_window(system):
    store, provider, worker, client, state, clock = system
    state['created'] = clock[0]-86401
    post(client); worker.drain_ready()
    assert not state['sent'] and store.enrollment(provider.phone_number_id, DEMO_SENDER)['last_inbound'] == 0


def test_window_expiring_after_processing_does_not_send_pdf(system):
    store, _, worker, client, state, clock = system
    post(client); worker.process_one()
    clock[0] += 86401
    worker.dispatch_one()
    assert store.rows('wa_outbox')[0]['status'] == 'outside_window' and not state['sent']


def test_invalid_pdf_requests_correction(system):
    store, _, worker, client, state, _ = system
    state['pdf'] = b'not a pdf'
    post(client); worker.drain_ready()
    assert store.rows('wa_jobs')[0]['status'] == 'needs_correction'
    assert 'No roster was changed' in state['sent'][0]['Body'] and not state['downloaded']


def test_ambiguous_send_never_blindly_retried(system):
    store, _, worker, client, state, clock = system
    state['post_error'] = 'timeout'
    post(client); worker.drain_ready()
    assert store.rows('wa_outbox')[0]['status'] == 'reconciliation_required'
    clock[0] += 600
    worker.drain_ready()
    assert len(state['sent']) == 1


def test_safe_retry_then_success(system):
    store, _, worker, client, state, clock = system
    state['post_error'] = 'rate'
    post(client); worker.drain_ready()
    assert store.rows('wa_outbox')[0]['status'] == 'retry'
    old_url = state['sent'][0]['MediaUrl']
    state['post_error'] = None
    clock[0] += 61
    worker.drain_ready()
    assert store.rows('wa_outbox')[0]['status'] == 'accepted'
    assert client.get(old_url).status_code == 404


def test_status_callbacks_validate_signature_and_provider_scope(system):
    store, _, worker, client, state, _ = system
    post(client); worker.drain_ready()
    form = {'AccountSid':ACCOUNT,'From':'whatsapp:+'+BUSINESS,'To':'whatsapp:+'+DEMO_SENDER,'MessageSid':OUTBOUND,'MessageStatus':'delivered'}
    assert post(client,form,url=CALLBACK,signature='bad').status_code == 401
    wrong = dict(form,AccountSid='AC'+'f'*32)
    assert post(client,wrong,url=CALLBACK).status_code == 204
    assert store.rows('wa_outbox')[0]['status'] == 'accepted'
    assert post(client,form,url=CALLBACK).status_code == 204
    assert store.rows('wa_outbox')[0]['status'] == 'delivered'
    assert post(client,dict(form,MessageStatus='sent'),url=CALLBACK).status_code == 204
    assert store.rows('wa_outbox')[0]['status'] == 'delivered'


def test_current_saved_settings_and_manual_locks_preserved(system):
    store, _, worker, client, state, clock = system
    post(client); worker.drain_ready()
    prior = store.get_weekly_roster('gm','2026-10-23')
    locked = next(row for row in prior['schedule'] if row['code']=='SHIFT')
    locked['locked'] = True
    saved = deepcopy(locked)
    store.save_weekly_roster('gm',prior['week_start'],prior)
    store.save_hotel_parameters('gm',{'checkout_minutes':31})
    sid = 'SM'+'e'*32
    form = form_data(MessageSid=sid, MediaUrl0=form_data()['MediaUrl0'].replace(INBOUND,sid))
    post(client,form); worker.drain_ready()
    latest = store.get_weekly_roster('gm','2026-10-23')
    retained = next(row for row in latest['schedule'] if row['staff_id']==saved['staff_id'] and row['date']==saved['date'])
    assert retained['locked'] and retained['start']==saved['start']
    assert latest['source']['settings_revision']==store.get_staff_settings('gm')['_settings_revision']
    assert latest['version']==3 and len(state['sent'])==2


def test_capability_endpoint_denies_remote_spoofed_proxy_and_expired(system):
    import api.main as api
    _, _, worker, client, state, clock = system
    post(client); worker.drain_ready()
    url=state['sent'][0]['MediaUrl']
    remote=TestClient(api.app,client=('198.51.100.10',12345))
    assert remote.get(url,headers={'X-Forwarded-For':'127.0.0.1'}).status_code==404
    clock[0] += 601
    assert client.get(url).status_code==404


def test_defaults_disable_twilio_and_factory_rejects_live_dev(system, monkeypatch):
    monkeypatch.delenv('TWILIO_WEBHOOK_ENABLED')
    assert post(system[3]).status_code==503
    assert not system[0].rows('wa_jobs')
    with pytest.raises(ProviderError):
        provider_from_env({'WHATSAPP_PROVIDER':'twilio','WHATSAPP_PRODUCTION_APPROVED':'1'})


def test_other_provider_jobs_outbox_and_hash_are_not_consumed(system):
    from scripts.demo_whatsapp_workflow import DEMO_PHONE_ID, webhook_payload
    from services.whatsapp_provider import FakeWhatsAppProvider
    store, _, twilio_worker, client, state, clock = system
    store.enroll_sender(tenant_id='synthetic', phone_number_id=DEMO_PHONE_ID, sender=DEMO_SENDER)
    meta_fake = FakeWhatsAppProvider(media={'synthetic-media-1':forecast_pdf()})
    meta_fake.phone_number_id = DEMO_PHONE_ID
    meta_worker = ForecastWorker(store, twilio_worker.files, meta_fake, clock=lambda:clock[0])
    store.ingest(webhook_payload(forecast_pdf()), clock[0])
    assert not twilio_worker.process_one()
    assert store.rows('wa_jobs')[0]['status']=='pending'
    meta_worker.process_one()
    assert not twilio_worker.dispatch_one()
    assert store.rows('wa_outbox')[0]['status']=='pending'
    meta_worker.dispatch_one()
    post(client)
    assert not meta_worker.process_one()
    twilio_worker.process_one()
    assert not meta_worker.dispatch_one()
    twilio_worker.dispatch_one()
    assert len(meta_fake.sent)==len(state['sent'])==1
    assert len(store.list_weekly_roster_versions('gm'))==2


def test_missing_account_config_fails_closed_not_server_error(system, monkeypatch):
    monkeypatch.setenv('TWILIO_ACCOUNT_SID','')
    assert post(system[3]).status_code==503
    assert not system[0].rows('wa_jobs')


def test_foreign_provider_expired_leases_not_mutated(system):
    from scripts.demo_whatsapp_workflow import DEMO_PHONE_ID, webhook_payload
    store, _, worker, _, _, clock = system
    store.enroll_sender(tenant_id='synthetic', phone_number_id=DEMO_PHONE_ID, sender=DEMO_SENDER)
    store.ingest(webhook_payload(forecast_pdf()),clock[0])
    with store._connect() as db:
        db.execute("UPDATE wa_jobs SET status='processing',attempts=5,lease_until=?",(clock[0]-1,))
        job=store.rows('wa_jobs')[0]
        store.add_outbox(db,job,clock[0]-1000,'Synthetic response')
        db.execute("UPDATE wa_outbox SET status='sending'")
    assert not worker.process_one() and not worker.dispatch_one()
    assert store.rows('wa_jobs')[0]['status']=='processing'
    assert store.rows('wa_outbox')[0]['status']=='sending'


def test_default_unscoped_fake_cannot_consume_twilio_jobs_or_outbox(system):
    from services.whatsapp_provider import FakeWhatsAppProvider
    store, _, worker, client, state, clock = system
    fake=FakeWhatsAppProvider()
    fake_worker=ForecastWorker(store,worker.files,fake,clock=lambda:clock[0])
    post(client)
    assert fake_worker.drain_ready()=={'processed':0,'dispatched':0}
    assert store.rows('wa_jobs')[0]['status']=='pending'
    worker.process_one()
    assert not fake_worker.dispatch_one()
    assert store.rows('wa_outbox')[0]['status']=='pending'
    worker.dispatch_one()
    assert len(state['sent'])==1 and not fake.sent


def test_default_fake_cannot_mutate_expired_twilio_leases(system):
    from services.whatsapp_provider import FakeWhatsAppProvider
    store, _, worker, client, _, clock = system
    post(client)
    job=store.rows('wa_jobs')[0]
    with store._connect() as db:
        db.execute("UPDATE wa_jobs SET status='processing',attempts=5,lease_until=?",(clock[0]-1,))
        store.add_outbox(db,job,clock[0]-1000,'Synthetic response')
        db.execute("UPDATE wa_outbox SET status='sending'")
    fake_worker=ForecastWorker(store,worker.files,FakeWhatsAppProvider(),clock=lambda:clock[0])
    assert fake_worker.drain_ready()=={'processed':0,'dispatched':0}
    assert store.rows('wa_jobs')[0]['status']=='processing'
    assert store.rows('wa_outbox')[0]['status']=='sending'
