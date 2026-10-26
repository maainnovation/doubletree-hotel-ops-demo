"""Operator actions are filesystem/MockTransport tests, never live activation."""
from copy import deepcopy
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sqlite3
import stat

import httpx
import pytest

from scripts.review_pilot_operator import execute,backup_database,main
from services.twilio_review_pilot import MAX_SECONDS
from test_twilio_review_pilot import owner,history,query
from test_twilio_live_pilot import env as legacy_env, ACCOUNT


def disabled(owner):
    values=legacy_env(owner['config'].data_root)
    values.update(PILOT_ENABLED='0',PILOT_REVIEW_ENABLED='0',PILOT_REVIEW_RESOURCE_CONFIRMED='1',
                  PILOT_REVIEW_APP_KEY='synthetic-private-owner-app-key-0123456789')
    return values


def monitor_transport(owner,status=200):
    calls=[]
    def handle(request):
        calls.append(request)
        assert request.method=='GET' and request.url.host=='api.twilio.com'
        assert request.url.path==f'/2010-04-01/Accounts/{ACCOUNT}/Usage/Records.json'
        stamp=datetime.fromtimestamp(owner['clock'][0],timezone.utc)
        return httpx.Response(status,json={'usage_records':[{'account_sid':ACCOUNT,'category':'totalprice','price_unit':'usd',
            'start_date':stamp.date().isoformat(),'end_date':stamp.date().isoformat(),'price':'0.10','as_of':stamp.isoformat()}],
            'next_page_uri':None,'untrusted_message':'synthetic-secret-never-print'})
    return httpx.MockTransport(handle),calls


def test_backup_is_consistent_private_unique_and_preserves_every_old_ledger(owner):
    before=history(owner['old'])
    first=backup_database(owner['config'],mocked=True)
    second=backup_database(owner['config'],mocked=True)
    assert first!=second
    for name in (first,second):
        path=owner['config'].data_root/'review-backups'/name
        assert stat.S_IMODE(path.stat().st_mode)==0o600
        with sqlite3.connect(path) as db:
            assert db.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
            for table,records in before.items():assert db.execute('SELECT * FROM '+table).fetchall()==records
    assert history(owner['old'])==before and not owner['state']['requests']


@pytest.mark.parametrize('change',[
    {'PILOT_ENABLED':'1'},{'PILOT_REVIEW_ENABLED':'1'},{'PILOT_REVIEW_WHATSAPP_ENABLED':'1'},{'PILOT_VOLUME_CONFIRMED':'0'},
    {'PILOT_REVIEW_RESOURCE_CONFIRMED':'0'},{'PILOT_REVIEW_APP_KEY':''},{'PILOT_REVIEW_APP_KEY':'short'},
    {'PILOT_REVIEW_APP_KEY':'synthetic whitespace app key '+('x'*32)},
    {'PILOT_REVIEW_RESOURCE_PROFILE':'custom_unbounded'},
])
def test_activation_requires_disabled_flags_resource_gate_and_private_key(owner,change):
    values=disabled(owner);values.update(change);transport,calls=monitor_transport(owner)
    with pytest.raises(ValueError):execute('activate','operator-owner',environment=values,clock=lambda:owner['clock'][0],transport=transport)
    assert not (owner['config'].data_root/'review-backups').exists() and not calls


def test_activation_backup_then_append_only_approval_and_baseline_never_enables_or_sends(owner):
    values=disabled(owner);original=deepcopy(values);before=history(owner['old']);transport,calls=monitor_transport(owner)
    result=execute('activate','operator-owner',environment=values,clock=lambda:owner['clock'][0],transport=transport)
    assert result['status']=='approved_baseline_ready_disabled'
    assert result['expires_at']==owner['clock'][0]+MAX_SECONDS
    assert set(result)=={'status','approval_id','expires_at','backup'}
    assert (owner['config'].data_root/'review-backups'/result['backup']).exists()
    rows=query(owner['old'],'SELECT * FROM pilot_review_authorizations')
    assert rows[0]['max_attempts']==500 and rows[0]['report_profile']=='hotel_daily_table_v1' and rows[0]['input_policy']=='redacted_owner_reports'
    assert query(owner['old'],'SELECT * FROM pilot_review_attempts')==[]
    assert history(owner['old'])==before and values==original and len(calls)==1
    assert not owner['state']['requests'] and not owner['state']['sends']
    output=json.dumps(result)
    assert values['PILOT_REVIEW_APP_KEY'] not in output and values['TWILIO_AUTH_TOKEN'] not in output


def test_failed_baseline_remains_disabled_and_retry_preserves_original_expiry(owner):
    values=disabled(owner);bad,calls=monitor_transport(owner,403)
    first=execute('activate','operator-owner',environment=values,clock=lambda:owner['clock'][0],transport=bad)
    assert first['status']=='approved_baseline_missing_disabled' and len(calls)==1
    before=query(owner['old'],'SELECT * FROM pilot_review_authorizations')
    assert not query(owner['old'],'SELECT * FROM review_cost_baselines')
    owner['clock'][0]+=60
    good,calls=monitor_transport(owner)
    second=execute('baseline','operator-owner',environment=values,clock=lambda:owner['clock'][0],transport=good)
    assert second['status']=='approved_baseline_ready_disabled' and second['expires_at']==first['expires_at']
    assert query(owner['old'],'SELECT * FROM pilot_review_authorizations')==before
    third=execute('baseline','operator-owner',environment=values,clock=lambda:owner['clock'][0],transport=good)
    assert third['status']=='baseline_already_present_disabled' and len(calls)==1
    assert len(query(owner['old'],'SELECT * FROM review_cost_baselines'))==1
    assert values['PILOT_ENABLED']==values['PILOT_REVIEW_ENABLED']=='0'


def test_revoke_does_not_need_app_key_or_valid_settings_and_is_idempotent(owner):
    values=disabled(owner);transport,_=monitor_transport(owner)
    execute('activate','operator-owner',environment=values,clock=lambda:owner['clock'][0],transport=transport)
    before=query(owner['old'],'SELECT * FROM pilot_review_authorizations')
    values.pop('PILOT_REVIEW_APP_KEY')
    with owner['old'].store._connect() as db:db.execute("UPDATE staff_settings SET dataset_mode='operational'")
    result=execute('revoke','operator-owner',environment=values,clock=lambda:owner['clock'][0],transport=transport)
    assert result['status']=='revoked_disabled'
    again=execute('revoke','operator-owner',environment=values,clock=lambda:owner['clock'][0],transport=transport)
    assert again==result and len(query(owner['old'],'SELECT * FROM pilot_review_revocations'))==1
    assert query(owner['old'],'SELECT * FROM pilot_review_authorizations')==before


def test_backup_refuses_symlink_destination_and_never_overwrites_existing_name(owner,monkeypatch,tmp_path):
    import scripts.review_pilot_operator as operator
    root=owner['config'].data_root
    outside=tmp_path/'outside';outside.mkdir()
    (root/'review-backups').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError):backup_database(owner['config'],mocked=True)
    assert not list(outside.iterdir())
    (root/'review-backups').unlink();(root/'review-backups').mkdir()
    class Fixed:hex='a'*32
    monkeypatch.setattr(operator.uuid,'uuid4',lambda:Fixed())
    existing=root/'review-backups'/('review-backup-'+'a'*32+'.sqlite3');existing.write_bytes(b'existing untouched')
    with pytest.raises(FileExistsError):backup_database(owner['config'],mocked=True)
    assert existing.read_bytes()==b'existing untouched'


def test_same_app_key_as_provider_secret_is_rejected_before_backup(owner):
    values=disabled(owner);values['PILOT_REVIEW_APP_KEY']=values['TWILIO_ACCOUNT_SID'];transport,calls=monitor_transport(owner)
    with pytest.raises(ValueError):execute('activate','operator-owner',environment=values,clock=lambda:owner['clock'][0],transport=transport)
    assert not calls and not (owner['config'].data_root/'review-backups').exists()


def test_main_prints_only_safe_status_on_raw_error(monkeypatch,capsys):
    import scripts.review_pilot_operator as operator
    def fail(*args,**kwargs):raise ValueError('synthetic-secret-never-print https://private.example/token')
    monkeypatch.setattr(operator,'execute',fail)
    assert main(['activate','--approval-id','operator-owner'])==1
    output=capsys.readouterr().out
    assert json.loads(output)=={'status':'operator_action_denied_keep_disabled'}
    assert 'synthetic-secret' not in output and 'https:' not in output


def test_cli_argument_errors_do_not_echo_unexpected_secret_arguments(capsys):
    assert main(['activate','--unrecognized-secret','synthetic-secret-never-print'])==1
    capture=capsys.readouterr()
    assert json.loads(capture.out)=={'status':'operator_action_denied_keep_disabled'}
    assert not capture.err and 'synthetic-secret' not in capture.out


def test_credentials_cannot_be_misused_as_public_approval_identifiers(owner):
    values=disabled(owner);transport,calls=monitor_transport(owner)
    for identifier in (values['TWILIO_ACCOUNT_SID'],values['PILOT_REVIEW_APP_KEY']):
        with pytest.raises(ValueError):execute('activate',identifier,environment=values,clock=lambda:owner['clock'][0],transport=transport)
    assert not calls and not (owner['config'].data_root/'review-backups').exists()
