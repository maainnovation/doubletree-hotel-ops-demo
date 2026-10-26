"""Hermetic human-review state machine: no OCR/provider/network is needed."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
import re
import sqlite3
import uuid

import pytest

from scripts.demo_whatsapp_workflow import DEMO_CODE, DEMO_NOW, DEMO_PHONE_ID, DEMO_SENDER, forecast_pdf, webhook_payload
from services.staff_settings import demo_defaults
from services.whatsapp_forecast import ForecastNeedsCorrection, validate_whatsapp_forecast
from services.whatsapp_review import WhatsAppReviewService
from services.whatsapp_store import PrivateFileStore, WorkflowStore


@pytest.fixture
def env(tmp_path):
    store = WorkflowStore(tmp_path / 'state.sqlite3')
    files = PrivateFileStore(tmp_path / 'private')
    store.enroll_tenant(tenant_id='review-demo', manager_id='review-gm', hotel_code=DEMO_CODE, hotel_name='Fictional Review Hotel')
    store.enroll_sender(tenant_id='review-demo', phone_number_id=DEMO_PHONE_ID, sender=DEMO_SENDER)
    store.save_staff_settings('review-gm', demo_defaults(), dataset_mode='demo')
    store.save_hotel_parameters('review-gm', {'room_capacity': 400})
    data = forecast_pdf()
    rows = validate_whatsapp_forecast(data, 'application/pdf', DEMO_CODE, 400, DEMO_NOW)
    now = DEMO_NOW.timestamp()
    service = WhatsAppReviewService(store, files)
    return {'store': store, 'files': files, 'service': service, 'now': now, 'data': data, 'rows': rows}


def query(env, sql, args=()):
    with env['store']._connect() as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, args)]


def update(env, sql, args=()):
    with env['store']._connect() as db:
        db.execute(sql, args)


def stage(env, *, rows=None, message_id=None):
    store, now = env['store'], env['now']
    store.ingest(webhook_payload(env['data'], message_id=message_id or 'wamid.review.' + uuid.uuid4().hex), now)
    job = store.claim_job(now)
    digest = hashlib.sha256(env['data']).hexdigest()
    key = env['files'].put(job['tenant_id'], env['data'], 'pdf')
    result = env['service'].stage(job, env['rows'] if rows is None else rows, digest, key, 'application/pdf', store.get_staff_settings('review-gm'), now)
    return job, result


def prepared(env):
    job, result = stage(env)
    prompt = query(env, 'SELECT * FROM wa_outbox WHERE id=?', (result['outbox_id'],))[0]
    code = re.search(r'CONFIRM ([0-9a-f]{16})', prompt['text']).group(1)
    update(env, "UPDATE wa_outbox SET status='accepted',provider_message_id='wamid.fake.accepted' WHERE id=?", (prompt['id'],))
    return job, result, code


def command(env, code, verb='CONFIRM', *, sender=DEMO_SENDER, phone_id=DEMO_PHONE_ID, tenant='review-demo', inbound=None):
    store, now = env['store'], env['now']
    enrolled = store.enrollment(phone_id, sender)
    identifier = uuid.uuid4().hex
    metadata = {'type': 'review_command', 'command': verb, 'review_code': code,
                '_enrollment_binding': store.enrollment_binding(enrolled) if enrolled else {}}
    with store._connect() as db:
        db.execute("""INSERT INTO wa_jobs(id,phone_number_id,message_id,sender,tenant_id,inbound_at,media_json,available_at)
            VALUES(?,?,?,?,?,?,?,?)""", (identifier,phone_id,'wamid.command.' + identifier,sender,tenant,now if inbound is None else inbound,json.dumps(metadata),now))
    return store.claim_job(now)


def test_complete_matrix_is_displayed_once_and_confirmation_does_not_schedule_or_send(env):
    original, result, code = prepared(env)
    assert result['status'] == 'awaiting_review'
    prompt = query(env, 'SELECT * FROM wa_outbox')[0]
    assert prompt['purpose'] == 'result' and len(prompt['text']) <= 1600
    assert 'Check ALL 7 dates and 49 values' in prompt['text']
    for row in env['rows']:
        assert row['date'] + ': 30 / 60 / 40 / 100 / 10 / 110 / 27.5' in prompt['text']
    review = query(env, 'SELECT * FROM wa_forecast_reviews')[0]
    assert review['token_hash'] == hashlib.sha256(code.encode()).hexdigest()
    assert code not in json.dumps(review) and review['expires_at'] == env['now'] + 1800
    assert query(env, 'SELECT status FROM wa_jobs WHERE id=?', (original['id'],))[0]['status'] == 'awaiting_review'
    cmd = command(env, code)
    resolved = env['service'].resolve(cmd, 'CONFIRM', code, env['now'])
    assert resolved['status'] == 'ready' and resolved['rows'] == env['rows']
    assert resolved['source_hash'] == hashlib.sha256(env['data']).hexdigest()
    assert resolved['settings_revision'] == 2 and resolved['original_job_id'] == original['id']
    assert query(env, 'SELECT * FROM weekly_rosters') == []
    assert len(query(env, 'SELECT * FROM wa_outbox')) == 1
    assert query(env, 'SELECT status FROM wa_jobs WHERE id=?', (cmd['id'],))[0]['status'] == 'processing'
    assert env['files'].read('review-demo', resolved['source_key']) == env['data']


def test_same_confirm_job_resumes_after_crash_but_different_confirm_is_duplicate(env):
    _, _, code = prepared(env)
    cmd = command(env, code)
    first = env['service'].resolve(cmd, 'CONFIRM', code, env['now'])
    assert env['service'].resolve(cmd, 'CONFIRM', code, env['now']) == first
    update(env, "UPDATE wa_jobs SET status='retry',lease_until=NULL WHERE id=?", (cmd['id'],))
    reclaimed = env['store'].claim_job(env['now'])
    assert reclaimed['id'] == cmd['id'] and reclaimed['claim_token'] != cmd['claim_token']
    assert env['service'].resolve(reclaimed, 'CONFIRM', code, env['now']) == first
    assert env['service'].resolve(cmd, 'CONFIRM', code, env['now']) == {'status': 'invalid'}
    other = command(env, code)
    assert env['service'].resolve(other, 'CONFIRM', code, env['now']) == {'status': 'duplicate'}
    assert len(query(env, 'SELECT * FROM wa_outbox')) == 1


def test_cancel_is_terminal_and_cannot_generate(env):
    original, _, code = prepared(env)
    cmd = command(env, code, 'CANCEL')
    assert env['service'].resolve(cmd, 'CANCEL', code, env['now']) == {'status': 'cancelled'}
    assert query(env, 'SELECT status FROM wa_jobs WHERE id=?', (original['id'],))[0]['status'] == 'review_cancelled'
    next_cmd = command(env, code)
    assert env['service'].resolve(next_cmd, 'CONFIRM', code, env['now']) == {'status': 'duplicate'}
    assert not query(env, 'SELECT * FROM weekly_rosters')


@pytest.mark.parametrize('status', ['pending', 'sending', 'failed', 'reconciliation_required', 'outside_window', 'cancelled', 'delivery_failed'])
def test_prompt_must_be_provider_accepted_before_confirm(env, status):
    _, result, code = prepared(env)
    update(env, 'UPDATE wa_outbox SET status=? WHERE id=?', (status, result['outbox_id']))
    cmd = command(env, code)
    assert env['service'].resolve(cmd, 'CONFIRM', code, env['now']) == {'status': 'invalid'}
    assert query(env, 'SELECT status FROM wa_forecast_reviews')[0]['status'] == 'pending'


@pytest.mark.parametrize('status', ['accepted', 'sent', 'delivered', 'read'])
def test_all_accepted_prompt_states_allow_confirmation(env, status):
    _, result, code = prepared(env)
    update(env, 'UPDATE wa_outbox SET status=? WHERE id=?', (status, result['outbox_id']))
    cmd = command(env, code)
    assert env['service'].resolve(cmd, 'CONFIRM', code, env['now'])['status'] == 'ready'


@pytest.mark.parametrize('change', ['revision', 'payload', 'operational', 'deleted'])
def test_settings_changes_fail_closed(env, change):
    _, _, code = prepared(env)
    if change == 'revision':
        env['store'].save_hotel_parameters('review-gm', {'checkout_minutes': 23})
    elif change == 'payload':
        row = query(env, 'SELECT payload FROM staff_settings')[0]
        payload = json.loads(row['payload'])
        payload['settings']['notes'] = 'New synthetic note'
        update(env, 'UPDATE staff_settings SET payload=?', (json.dumps(payload),))
    elif change == 'operational':
        update(env, "UPDATE staff_settings SET dataset_mode='operational'")
    else:
        update(env, 'DELETE FROM staff_settings')
    cmd = command(env, code)
    assert env['service'].resolve(cmd, 'CONFIRM', code, env['now'])['status'] in {'settings_changed', 'invalid'}
    assert not query(env, 'SELECT * FROM weekly_rosters')


@pytest.mark.parametrize('target', ['sender', 'tenant', 'profile', 'hotel_name'])
def test_enrollment_changes_fail_closed(env, target):
    _, _, code = prepared(env)
    cmd = command(env, code)
    if target == 'sender':
        update(env, 'UPDATE wa_senders SET enabled=0')
    elif target == 'tenant':
        update(env, 'UPDATE wa_tenants SET enabled=0')
    elif target == 'profile':
        update(env, "UPDATE wa_tenants SET report_profile='hotel_daily_table_v1'")
    else:
        update(env, "UPDATE wa_tenants SET hotel_name='Another fictional hotel'")
    assert env['service'].resolve(cmd, 'CONFIRM', code, env['now']) == {'status': 'invalid'}


@pytest.mark.parametrize('scope', ['sender', 'business', 'tenant'])
def test_code_never_authorizes_another_scope(env, scope):
    _, _, code = prepared(env)
    store = env['store']
    if scope == 'sender':
        store.enroll_sender(tenant_id='review-demo', phone_number_id=DEMO_PHONE_ID, sender='15555550999')
        cmd = command(env, code, sender='15555550999')
    elif scope == 'business':
        store.enroll_sender(tenant_id='review-demo', phone_number_id='109876543211', sender=DEMO_SENDER)
        cmd = command(env, code, phone_id='109876543211')
    else:
        store.enroll_tenant(tenant_id='other-demo', manager_id='other-gm', hotel_code='OTHER', hotel_name='Other Fictional Hotel')
        store.save_staff_settings('other-gm', demo_defaults(), dataset_mode='demo')
        store.save_hotel_parameters('other-gm', {'room_capacity': 400})
        store.enroll_sender(tenant_id='other-demo', phone_number_id=DEMO_PHONE_ID, sender='15555550998')
        cmd = command(env, code, sender='15555550998', tenant='other-demo')
    assert env['service'].resolve(cmd, 'CONFIRM', code, env['now']) == {'status': 'invalid'}
    assert query(env, 'SELECT status FROM wa_forecast_reviews')[0]['status'] == 'pending'


@pytest.mark.parametrize('code', ['', 'a'*15, 'a'*17, '../'+'a'*13, 'g'*16, None, 123, 'a'*16+'\n'])
def test_malformed_codes_are_silent_invalid(env, code):
    _, _, good = prepared(env)
    cmd = command(env, good)
    assert env['service'].resolve(cmd, 'CONFIRM', code, env['now']) == {'status': 'invalid'}


def test_unknown_code_and_mismatched_command_arguments_are_invalid(env):
    _, _, code = prepared(env)
    cmd = command(env, code)
    for verb, supplied in [('CONFIRM','0'*16),('CANCEL',code),('confirm',code),('CONFIRM '+code,code)]:
        assert env['service'].resolve(cmd, verb, supplied, env['now']) == {'status':'invalid'}


@pytest.mark.parametrize('alteration', ['hash', 'source', 'candidate', 'prompt', 'provider_sid', 'origin_key', 'origin_tenant', 'expired_lease', 'claim_token'])
def test_corruption_or_lost_claim_cannot_confirm(env, alteration):
    original, result, code = prepared(env)
    cmd = command(env, code)
    if alteration == 'hash': update(env, "UPDATE wa_forecast_reviews SET candidate_hash=?", ('0'*64,))
    elif alteration == 'source':
        key = query(env, 'SELECT source_key FROM wa_forecast_reviews')[0]['source_key']
        (env['files'].root / key).write_bytes(b'changed synthetic file')
    elif alteration == 'candidate':
        candidate = deepcopy(env['rows']); candidate[0]['departures'] = 31
        update(env, 'UPDATE wa_forecast_reviews SET candidate_json=?', (json.dumps(candidate),))
    elif alteration == 'prompt': update(env, "UPDATE wa_outbox SET text='Conflicting matrix'")
    elif alteration == 'provider_sid': update(env, 'UPDATE wa_outbox SET provider_message_id=NULL')
    elif alteration == 'origin_key': update(env, 'UPDATE wa_jobs SET source_key=NULL WHERE id=?', (original['id'],))
    elif alteration == 'origin_tenant': update(env, "UPDATE wa_jobs SET tenant_id='wrong' WHERE id=?", (original['id'],))
    elif alteration == 'expired_lease': update(env, 'UPDATE wa_jobs SET lease_until=? WHERE id=?', (env['now']-1,cmd['id']))
    elif alteration == 'claim_token': update(env, "UPDATE wa_jobs SET claim_token='stolen' WHERE id=?", (cmd['id'],))
    assert env['service'].resolve(cmd, 'CONFIRM', code, env['now']) == {'status': 'invalid'}
    assert not query(env, 'SELECT * FROM weekly_rosters')


def test_review_expiry_is_exact_and_original_window_can_shorten_it(env):
    _, _, code = prepared(env)
    env['now'] += 1800
    cmd = command(env, code)
    assert env['service'].resolve(cmd, 'CONFIRM', code, env['now']) == {'status': 'expired'}
    assert query(env, 'SELECT status FROM wa_forecast_reviews')[0]['status'] == 'expired'


def test_stage_respects_nearly_closed_original_window(env):
    store, now = env['store'], env['now']
    store.ingest(webhook_payload(env['data'], timestamp=now-86300), now)
    job = store.claim_job(now)
    key = env['files'].put(job['tenant_id'], env['data'], 'pdf')
    env['service'].stage(job, env['rows'], hashlib.sha256(env['data']).hexdigest(), key, 'application/pdf',store.get_staff_settings('review-gm'),now)
    assert query(env,'SELECT expires_at FROM wa_forecast_reviews')[0]['expires_at'] == now+100


@pytest.mark.parametrize('alteration', ['missing', 'duplicate_date', 'capacity', 'arithmetic', 'boolean', 'nan', 'partial', 'pct_precision', 'prior_day'])
def test_incomplete_or_invalid_candidate_never_creates_prompt(env, alteration):
    rows = deepcopy(env['rows'])
    if alteration == 'missing': del rows[0]['departures']
    elif alteration == 'duplicate_date': rows[1]['date'] = rows[0]['date']
    elif alteration == 'capacity': rows[0]['departures'] = 401
    elif alteration == 'arithmetic': rows[0]['rooms_on_books'] = 99
    elif alteration == 'boolean': rows[0]['departures'] = True
    elif alteration == 'nan': rows[0]['occupancy_pct'] = float('nan')
    elif alteration == 'partial': rows.pop()
    elif alteration == 'pct_precision': rows[0]['occupancy_pct'] = 27.555
    elif alteration == 'prior_day': rows[0]['prior_day_occupancy'] = 100
    with pytest.raises(ForecastNeedsCorrection): stage(env, rows=rows)
    assert not query(env, 'SELECT * FROM wa_forecast_reviews')
    assert not query(env, 'SELECT * FROM wa_outbox')


def test_historical_week_keeps_source_bound_prompt_and_real_review_expiry(env):
    from datetime import date
    rows=deepcopy(env['rows'])
    for row in rows:row['date']=(date.fromisoformat(row['date'])-timedelta(days=28)).isoformat()
    job,result=stage(env,rows=rows)
    assert result['status']=='awaiting_review'
    review=query(env,'SELECT * FROM wa_forecast_reviews')[0]
    assert review['original_job_id']==job['id']
    assert review['source_hash']==hashlib.sha256(env['data']).hexdigest()
    assert review['expires_at']==env['now']+1800
    prompt=query(env,'SELECT * FROM wa_outbox')[0]['text']
    assert rows[0]['date'] in prompt and 'Review extracted figures' in prompt
    assert not env['store'].list_weekly_roster_versions('review-gm')


def test_same_content_reupload_and_same_message_do_not_repeat_prompt(env):
    original, result = stage(env, message_id='wamid.same')
    assert env['store'].ingest(webhook_payload(env['data'], message_id='wamid.same'), env['now']) == 0
    _, second = stage(env)
    assert second['status'] == 'duplicate' and second['review_id'] == result['review_id']
    assert len(query(env, 'SELECT * FROM wa_forecast_reviews')) == len(query(env, 'SELECT * FROM wa_outbox')) == 1
    assert env['service'].stage(original,env['rows'],hashlib.sha256(env['data']).hexdigest(),query(env,'SELECT source_key FROM wa_forecast_reviews')[0]['source_key'],'application/pdf',env['store'].get_staff_settings('review-gm'),env['now']) == {'status':'invalid'}


def test_two_confirmation_jobs_race_only_one_wins(env):
    _, _, code = prepared(env)
    commands = [command(env, code), command(env, code)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda job: env['service'].resolve(job,'CONFIRM',code,env['now']),commands))
    assert sorted(result['status'] for result in results) == ['duplicate','ready']
    winner = next(job for job,result in zip(commands,results) if result['status']=='ready')
    assert query(env,'SELECT confirmed_job_id FROM wa_forecast_reviews')[0]['confirmed_job_id'] == winner['id']


def test_reinitialization_preserves_candidate_and_source(env):
    _, _, code = prepared(env)
    before = query(env,'SELECT * FROM wa_forecast_reviews')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: WhatsAppReviewService(env['store'],env['files']),range(8)))
    assert query(env,'SELECT * FROM wa_forecast_reviews') == before
    cmd = command(env,code)
    assert WhatsAppReviewService(env['store'],env['files']).resolve(cmd,'CONFIRM',code,env['now'])['status'] == 'ready'


def test_failed_review_insert_rolls_back_prompt_and_original_job(env):
    update(env, "CREATE TRIGGER force_review_insert_failure BEFORE INSERT ON wa_forecast_reviews BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(ForecastNeedsCorrection): stage(env)
    assert not query(env,'SELECT * FROM wa_outbox')
    assert not query(env,'SELECT * FROM wa_forecast_reviews')
    job = query(env,'SELECT * FROM wa_jobs')[0]
    assert job['status']=='processing' and job['source_hash'] is None and job['source_key'] is None


def test_source_key_from_another_tenant_is_never_staged(env):
    store, now = env['store'], env['now']
    store.ingest(webhook_payload(env['data']), now)
    job = store.claim_job(now)
    wrong_key = env['files'].put('another-tenant',env['data'],'pdf')
    with pytest.raises(ForecastNeedsCorrection):
        env['service'].stage(job,env['rows'],hashlib.sha256(env['data']).hexdigest(),wrong_key,'application/pdf',store.get_staff_settings('review-gm'),now)
    assert not query(env,'SELECT * FROM wa_outbox')


def test_unknown_extra_candidate_fields_are_not_silently_dropped(env):
    rows=deepcopy(env['rows']);rows[0]['hotel_code']='ANOTHER-HOTEL'
    with pytest.raises(ForecastNeedsCorrection):stage(env,rows=rows)
    assert not query(env,'SELECT * FROM wa_outbox')


@pytest.mark.parametrize('field,value', [('mime','image/png'),('_forecast_review_id','another-review'),('_enrollment_binding',{})])
def test_original_review_metadata_cannot_change(env,field,value):
    original, _, code = prepared(env)
    row=query(env,'SELECT media_json FROM wa_jobs WHERE id=?',(original['id'],))[0]
    metadata=json.loads(row['media_json']);metadata[field]=value
    update(env,'UPDATE wa_jobs SET media_json=? WHERE id=?',(json.dumps(metadata),original['id']))
    cmd=command(env,code)
    assert env['service'].resolve(cmd,'CONFIRM',code,env['now'])=={'status':'invalid'}


def test_command_before_review_or_with_pilot_media_attestation_is_invalid(env):
    _, _, code = prepared(env)
    before=command(env,code,inbound=env['now']-1)
    assert env['service'].resolve(before,'CONFIRM',code,env['now'])=={'status':'invalid'}
    cmd=command(env,code)
    row=query(env,'SELECT media_json FROM wa_jobs WHERE id=?',(cmd['id'],))[0]
    metadata=json.loads(row['media_json']);metadata['_pilot_attestation']={'kind':'authenticated_media_after_message_404'}
    update(env,'UPDATE wa_jobs SET media_json=? WHERE id=?',(json.dumps(metadata),cmd['id']))
    assert env['service'].resolve(cmd,'CONFIRM',code,env['now'])=={'status':'invalid'}


def test_expiry_still_blocks_same_confirmation_job_resume(env):
    _, _, code = prepared(env)
    cmd=command(env,code)
    assert env['service'].resolve(cmd,'CONFIRM',code,env['now'])['status']=='ready'
    env['now']+=1800
    update(env,'UPDATE wa_jobs SET lease_until=? WHERE id=?',(env['now']+300,cmd['id']))
    assert env['service'].resolve(cmd,'CONFIRM',code,env['now'])=={'status':'expired'}
    assert not query(env,'SELECT * FROM weekly_rosters')


def test_stage_with_preexisting_ack_refuses_third_message(env):
    store,now=env['store'],env['now'];store.ingest(webhook_payload(env['data']),now)
    job=store.claim_job(now)
    with store._connect() as db:store.add_outbox(db,job,now,'Synthetic prior ACK',purpose='ack')
    key=env['files'].put(job['tenant_id'],env['data'],'pdf')
    with pytest.raises(ForecastNeedsCorrection):
        env['service'].stage(job,env['rows'],hashlib.sha256(env['data']).hexdigest(),key,'application/pdf',store.get_staff_settings('review-gm'),now)
    assert len(query(env,'SELECT * FROM wa_outbox'))==1
    assert not query(env,'SELECT * FROM wa_forecast_reviews')


@pytest.mark.parametrize('verb', [None, [], {}, 123, True])
def test_nonstring_command_is_silent_invalid(env, verb):
    _, _, code = prepared(env)
    cmd = command(env, code)
    assert env['service'].resolve(cmd, verb, code, env['now']) == {'status': 'invalid'}


def dispatch_allowed(env, item):
    with env['store']._connect() as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        return env['service'].prompt_dispatch_allowed(db,item,env['now'])


def test_only_current_pending_review_prompt_can_dispatch(env):
    _, result = stage(env)
    item=query(env,'SELECT * FROM wa_outbox WHERE id=?',(result['outbox_id'],))[0]
    assert dispatch_allowed(env,item) is True
    assert query(env,'SELECT status FROM wa_forecast_reviews')[0]['status']=='pending'
    update(env,"UPDATE wa_outbox SET status='accepted',provider_message_id='wamid.accepted'")
    assert dispatch_allowed(env,item) is False


@pytest.mark.parametrize('changed', ['expiry','settings_revision','settings_payload','enrollment','source','candidate','prompt','outbox_pointer','origin_pointer','origin_state'])
def test_stale_or_corrupted_review_prompt_cannot_dispatch(env,changed):
    original,result=stage(env)
    item=query(env,'SELECT * FROM wa_outbox WHERE id=?',(result['outbox_id'],))[0]
    if changed=='expiry':env['now']+=1800
    elif changed=='settings_revision':env['store'].save_hotel_parameters('review-gm',{'checkout_minutes':24})
    elif changed=='settings_payload':
        payload=json.loads(query(env,'SELECT payload FROM staff_settings')[0]['payload']);payload['settings']['notes']='Changed'
        update(env,'UPDATE staff_settings SET payload=?',(json.dumps(payload),))
    elif changed=='enrollment':update(env,'UPDATE wa_senders SET enabled=0')
    elif changed=='source':
        key=query(env,'SELECT source_key FROM wa_forecast_reviews')[0]['source_key'];(env['files'].root/key).write_bytes(b'corrupted')
    elif changed=='candidate':update(env,"UPDATE wa_forecast_reviews SET candidate_hash=?",('a'*64,))
    elif changed=='prompt':update(env,"UPDATE wa_outbox SET text='different'")
    elif changed=='outbox_pointer':update(env,"UPDATE wa_forecast_reviews SET prompt_outbox_id='different'")
    elif changed=='origin_pointer':update(env,"UPDATE wa_forecast_reviews SET original_job_id='different'")
    elif changed=='origin_state':update(env,"UPDATE wa_jobs SET status='failed' WHERE id=?",(original['id'],))
    assert dispatch_allowed(env,item) is False
    assert not query(env,'SELECT * FROM weekly_rosters')


def test_deleted_review_row_does_not_turn_prompt_into_an_ordinary_message(env):
    _,result=stage(env)
    item=query(env,'SELECT * FROM wa_outbox WHERE id=?',(result['outbox_id'],))[0]
    update(env,'DELETE FROM wa_forecast_reviews')
    assert dispatch_allowed(env,item) is False


def test_nonreview_outbox_remains_outside_prompt_gate(env):
    store,now=env['store'],env['now'];store.ingest(webhook_payload(env['data']),now)
    job=store.claim_job(now)
    with store._connect() as db:store.add_outbox(db,job,now,'Ordinary safe correction')
    item=query(env,'SELECT * FROM wa_outbox')[0]
    assert dispatch_allowed(env,item) is True


@pytest.mark.parametrize('failed_state', ['failed','needs_correction','superseded'])
def test_terminal_confirming_calculation_failure_allows_fresh_review(env,failed_state):
    _, first, code=prepared(env)
    cmd=command(env,code)
    assert env['service'].resolve(cmd,'CONFIRM',code,env['now'])['status']=='ready'
    update(env,'UPDATE wa_jobs SET status=?,lease_until=NULL WHERE id=?',(failed_state,cmd['id']))
    _, fresh=stage(env)
    assert fresh['status']=='awaiting_review' and fresh['review_id']!=first['review_id']
    assert len(query(env,'SELECT * FROM wa_forecast_reviews'))==2
    assert not query(env,'SELECT * FROM weekly_rosters')


@pytest.mark.parametrize('prompt_state',['failed','cancelled','delivery_failed'])
def test_terminal_failed_prompt_allows_fresh_review(env,prompt_state):
    _,first=stage(env)
    update(env,'UPDATE wa_outbox SET status=? WHERE id=?',(prompt_state,first['outbox_id']))
    _,fresh=stage(env)
    assert fresh['status']=='awaiting_review' and fresh['review_id']!=first['review_id']
    assert len(query(env,'SELECT * FROM wa_forecast_reviews'))==2


def completed_review(env):
    from services.whatsapp_provider import FakeWhatsAppProvider
    from services.whatsapp_workflow import ForecastWorker
    _,first,code=prepared(env)
    cmd=command(env,code)
    result=env['service'].resolve(cmd,'CONFIRM',code,env['now'])
    worker=ForecastWorker(env['store'],env['files'],FakeWhatsAppProvider(),clock=lambda:env['now'])
    worker._persist_roster(cmd,env['store'].enrollment(DEMO_PHONE_ID,DEMO_SENDER),result['rows'],result['source_hash'],result['source_key'],result['settings_revision'])
    return first,cmd


def test_unchanged_current_completed_result_suppresses_duplicate_prompt(env):
    first,_=completed_review(env)
    _,fresh=stage(env)
    assert fresh['status']=='duplicate' and fresh['review_id']==first['review_id']
    assert len(query(env,'SELECT * FROM wa_forecast_reviews'))==1


def test_manual_roster_new_version_allows_fresh_review(env):
    first,_=completed_review(env)
    roster=env['store'].get_weekly_roster('review-gm',env['rows'][0]['date'])
    roster['schedule'][0]['locked']=True
    roster['schedule'][0]['notes']='Synthetic manual edit'
    env['store'].save_weekly_roster('review-gm',roster['week_start'],roster)
    _,fresh=stage(env)
    assert fresh['status']=='awaiting_review' and fresh['review_id']!=first['review_id']
    assert len(query(env,'SELECT * FROM weekly_roster_versions'))==2


@pytest.mark.parametrize('result_status',['failed','cancelled','delivery_failed','outside_window'])
def test_unusable_completed_pdf_reply_allows_fresh_review(env,result_status):
    first,cmd=completed_review(env)
    update(env,"UPDATE wa_outbox SET status=? WHERE job_id=? AND purpose='result'",(result_status,cmd['id']))
    _,fresh=stage(env)
    assert fresh['status']=='awaiting_review' and fresh['review_id']!=first['review_id']


def test_live_confirmation_processing_still_suppresses_new_review(env):
    _,first,code=prepared(env);cmd=command(env,code)
    env['service'].resolve(cmd,'CONFIRM',code,env['now'])
    _,fresh=stage(env)
    assert fresh['status']=='duplicate' and fresh['review_id']==first['review_id']


def test_cancel_resume_is_idempotent_but_new_cancel_silent_duplicate(env):
    _,_,code=prepared(env);cmd=command(env,code,'CANCEL')
    assert env['service'].resolve(cmd,'CANCEL',code,env['now'])=={'status':'cancelled'}
    assert env['service'].resolve(cmd,'CANCEL',code,env['now'])=={'status':'cancelled'}
    another=command(env,code,'CANCEL')
    assert env['service'].resolve(another,'CANCEL',code,env['now'])=={'status':'duplicate'}


def test_expired_first_notice_resumes_but_new_expiry_command_is_duplicate(env):
    _,_,code=prepared(env);env['now']+=1800
    cmd=command(env,code)
    assert env['service'].resolve(cmd,'CONFIRM',code,env['now'])=={'status':'expired'}
    assert env['service'].resolve(cmd,'CONFIRM',code,env['now'])=={'status':'expired'}
    another=command(env,code)
    assert env['service'].resolve(another,'CONFIRM',code,env['now'])=={'status':'duplicate'}


def dispatched_review(env):
    """Actual fake-provider prompt dispatch; no direct acceptance mutation."""
    from services.whatsapp_provider import FakeWhatsAppProvider
    from services.whatsapp_workflow import ForecastWorker
    provider = FakeWhatsAppProvider(media={'synthetic-media-1': env['data']})
    worker = ForecastWorker(env['store'],env['files'],provider,clock=lambda:env['now'],
                            acknowledge_uploads=True,review_images=True,
                            media_evaluator=lambda *args,**kwargs: {
                                'method':'local_scanned_pdf_ocr','requires_review':True,
                                'automatic_roster_allowed':False,'candidate_valid_for_confirmation':True,
                                'rows':deepcopy(env['rows'])})
    original,review = stage(env)
    assert worker.dispatch_one() is True
    assert len(provider.sent)==1 and provider.sent[0]['type']=='text'
    code=re.search(r'CONFIRM ([a-f0-9]{16})',provider.sent[0]['text']).group(1)
    payload=webhook_payload(env['data'],message_id='wamid.confirm.failure.'+uuid.uuid4().hex,timestamp=env['now'])
    message=payload['entry'][0]['changes'][0]['value']['messages'][0]
    message.pop('document');message.update(type='text',text={'body':'CONFIRM '+code})
    assert env['store'].ingest(payload,env['now'])==1
    cmd=query(env,'SELECT * FROM wa_jobs WHERE message_id=?',(message['id'],))[0]
    return worker,provider,original,review,cmd


def assert_single_failure_and_fresh_review(env,worker,provider,review,cmd):
    failures=query(env,"SELECT * FROM wa_outbox WHERE job_id=? AND purpose='result'",(cmd['id'],))
    assert len(failures)==1
    notice=failures[0]
    assert notice['kind']=='text' and notice['object_key'] is None and notice['filename'] is None
    assert 'confirmed forecast calculation could not be completed safely' in notice['text']
    assert 'no report was generated' in notice['text'] and 'synthetic-sensitive-error' not in notice['text']
    assert notice['recipient']==DEMO_SENDER and notice['phone_number_id']==DEMO_PHONE_ID
    assert not query(env,'SELECT * FROM weekly_rosters')
    assert not query(env,'SELECT * FROM weekly_roster_versions')
    assert not query(env,"SELECT * FROM wa_outbox WHERE kind='document' OR purpose='ack'")
    failed=query(env,'SELECT * FROM wa_jobs WHERE id=?',(cmd['id'],))[0]
    assert failed['status']=='needs_correction' and failed['attempts']==5
    assert worker.process_one() is False
    assert worker.dispatch_one() is True
    assert worker.dispatch_one() is False
    assert len(provider.sent)==2 and all(item['type']=='text' for item in provider.sent)
    assert env['store'].claim_job(env['now']) is None
    assert len(query(env,'SELECT * FROM wa_outbox'))==2
    assert query(env,'SELECT status,confirmed_job_id FROM wa_forecast_reviews')[0]=={'status':'confirmed','confirmed_job_id':cmd['id']}
    # A genuinely new upload can recover, without modifying the old review,
    # failure notice, processing counter or immutable source.
    payload=webhook_payload(env['data'],message_id='wamid.fresh.after.failure.'+uuid.uuid4().hex,timestamp=env['now'])
    assert env['store'].ingest(payload,env['now'])==1
    assert worker.process_one() is True
    reviews=query(env,'SELECT * FROM wa_forecast_reviews')
    assert len(reviews)==2
    fresh=next(row for row in reviews if row['review_id']!=review['review_id'])
    assert fresh['status']=='pending' and fresh['source_hash']==hashlib.sha256(env['data']).hexdigest()
    assert len(query(env,"SELECT * FROM wa_outbox WHERE job_id=?",(cmd['id'],)))==1
    assert query(env,'SELECT attempts FROM wa_jobs WHERE id=?',(cmd['id'],))[0]['attempts']==5
    assert not query(env,'SELECT * FROM weekly_roster_versions')


def test_repeated_confirmed_calculation_exceptions_emit_one_failure_notice_and_recover(env,monkeypatch):
    import services.whatsapp_workflow as workflow
    worker,provider,_,review,cmd=dispatched_review(env)
    calls=[]
    def failure(*args,**kwargs):
        calls.append(1)
        raise RuntimeError('synthetic-sensitive-error must not be retained')
    with monkeypatch.context() as patched:
        patched.setattr(workflow,'generate_weekly_roster',failure)
        for attempt in range(1,6):
            assert worker.process_one() is True
            saved=query(env,'SELECT * FROM wa_jobs WHERE id=?',(cmd['id'],))[0]
            assert saved['attempts']==attempt
            if attempt<5:
                assert saved['status']=='retry'
                assert not query(env,'SELECT * FROM wa_outbox WHERE job_id=?',(cmd['id'],))
                env['now']=saved['available_at']
            else:
                assert saved['status']=='needs_correction'
    assert len(calls)==5
    assert_single_failure_and_fresh_review(env,worker,provider,review,cmd)


def test_expired_exhausted_confirmation_lease_emits_one_failure_under_concurrent_claims(env,monkeypatch):
    import services.whatsapp_workflow as workflow
    worker,provider,_,review,cmd=dispatched_review(env)
    calls=[]
    class SimulatedWorkerCrash(BaseException):
        pass
    def failure_then_crash(*args,**kwargs):
        calls.append(1)
        if len(calls)==5:
            raise SimulatedWorkerCrash()
        raise RuntimeError('synthetic-sensitive-error must not be retained')
    with monkeypatch.context() as patched:
        patched.setattr(workflow,'generate_weekly_roster',failure_then_crash)
        for attempt in range(1,5):
            assert worker.process_one() is True
            saved=query(env,'SELECT * FROM wa_jobs WHERE id=?',(cmd['id'],))[0]
            assert saved['status']=='retry' and saved['attempts']==attempt
            env['now']=saved['available_at']
        with pytest.raises(SimulatedWorkerCrash):worker.process_one()
    crashed=query(env,'SELECT * FROM wa_jobs WHERE id=?',(cmd['id'],))[0]
    assert crashed['status']=='processing' and crashed['attempts']==5
    assert not query(env,'SELECT * FROM wa_outbox WHERE job_id=?',(cmd['id'],))
    env['now']=crashed['lease_until']+1
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(lambda _:env['store'].claim_job(env['now']),range(6)))==[None]*6
    terminal=query(env,'SELECT * FROM wa_jobs WHERE id=?',(cmd['id'],))[0]
    assert terminal['error_code']=='processing_attempts_exhausted' and terminal['lease_until'] is None
    assert_single_failure_and_fresh_review(env,worker,provider,review,cmd)
