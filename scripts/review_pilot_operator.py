"""Operator-only owner-pilot backup/activation; never changes enable flags or sends.

Run on the approved disabled deployment after its resource checks. Credentials
and the distinct app key must be entered privately into the environment, never
CLI arguments. Activation is not performed by application startup. A failed
baseline leaves the immutable approval present but non-sending; retry only the
baseline command or revoke, never silently create a fresh approval/expiry.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

import httpx

from services.owner_review_runtime import legacy_config_from_environment
from scripts.measure_review_runtime import resource_profile
from services.review_pilot_cost import ReviewPilotCostMonitor, CostMonitorUnavailable
from services.twilio_pilot import _persistent_mount
from services.twilio_review_pilot import (_legacy_config, _open_existing, create_review_pilot_approval,
    ReviewPilotBudget, revoke_review_pilot)
from services.twilio_run_budget import _identity


class OperatorDenied(ValueError):
    def __init__(self):
        super().__init__('Owner pilot operator action denied; keep sending disabled.')


def _identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',value):
        raise OperatorDenied()
    return value


def _configuration(environment, require_key):
    if (environment.get('PILOT_ENABLED','0')!='0' or environment.get('PILOT_REVIEW_ENABLED','0')!='0'
            or environment.get('PILOT_REVIEW_WHATSAPP_ENABLED','0')!='0'
            or environment.get('PILOT_VOLUME_CONFIRMED')!='1'
            or environment.get('PILOT_REVIEW_RESOURCE_CONFIRMED')!='1'):
        raise OperatorDenied()
    try:
        resource_profile(environment)
    except ValueError:
        raise OperatorDenied() from None
    config=legacy_config_from_environment(environment)
    _legacy_config(config)
    if require_key:
        key=environment.get('PILOT_REVIEW_APP_KEY','')
        if (not isinstance(key,str) or not 32<=len(key)<=512 or not key.isascii()
                or any(ord(c)<=32 or ord(c)==127 for c in key)
                or key in {config.auth_token,config.account_sid}):
            raise OperatorDenied()
    return config


def _plain_file(path):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
        raise OperatorDenied()
    return info


@contextmanager
def _locked_source(config, mocked):
    root=config.data_root
    if (not mocked and not _persistent_mount(root)) or not root.is_dir() or root.is_symlink() or any(p.is_symlink() for p in root.parents):
        raise OperatorDenied()
    source=root/'pilot.sqlite3';lock=root/'.init.lock'
    _plain_file(source);_plain_file(lock)
    for suffix in ('-wal','-shm','-journal'):
        path=Path(str(source)+suffix)
        if path.exists() or path.is_symlink():_plain_file(path)
    fd=os.open(lock,os.O_RDWR|os.O_NOFOLLOW)
    with os.fdopen(fd,'a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        # Read-only URI prevents accidental creation/adoption of an unknown DB.
        with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as db:
            prior=db.execute('SELECT identity FROM pilot_identity WHERE id=1').fetchone()
            if not prior or prior[0]!=_identity(config)[0] or db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:
                raise OperatorDenied()
            yield db


def backup_database(config, *, mocked=False):
    """Consistent SQLite backup, private 0600, exclusive name, integrity checked."""
    _legacy_config(config)
    with _locked_source(config,mocked) as source:
        directory=config.data_root/'review-backups'
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise OperatorDenied()
        directory.mkdir(mode=0o700,exist_ok=True)
        os.chmod(directory,0o700)
        name='review-backup-'+uuid.uuid4().hex+'.sqlite3'
        destination=directory/name
        fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_RDWR|os.O_NOFOLLOW,0o600)
        identity=os.fstat(fd)
        os.close(fd)
        succeeded=False
        try:
            deadline=time.monotonic()+60
            def progress(status,remaining,total):
                if time.monotonic()>deadline:raise OperatorDenied()
            with sqlite3.connect(destination) as target:
                source.backup(target,pages=256,progress=progress,sleep=0.01)
                if target.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise OperatorDenied()
            after=_plain_file(destination)
            if (after.st_dev,after.st_ino)!=(identity.st_dev,identity.st_ino):raise OperatorDenied()
            os.chmod(destination,0o600)
            with destination.open('rb') as handle:os.fsync(handle.fileno())
            dirfd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(dirfd)
            finally:os.close(dirfd)
            succeeded=True
            return name
        finally:
            if not succeeded:
                # Remove only our own incomplete output, never a replaced path
                # or the source database. A failed backup cannot activate sends.
                try:
                    current=destination.lstat()
                    if (current.st_dev,current.st_ino)==(identity.st_dev,identity.st_ino):destination.unlink()
                except FileNotFoundError:pass


def execute(command, approval_id=None, *, environment=None, clock=time.time, transport=None):
    """Local/operator entry point; only exact MockTransport supports test roots."""
    if command not in {'backup','activate','baseline','revoke'}:
        raise OperatorDenied()
    if transport is not None and type(transport) is not httpx.MockTransport:
        raise OperatorDenied()
    environment=os.environ if environment is None else environment
    config=_configuration(environment,require_key=command in {'activate','baseline'})
    mocked=type(transport) is httpx.MockTransport
    if command!='backup':
        _identifier(approval_id)
        if approval_id in {config.auth_token,config.account_sid,environment.get('PILOT_REVIEW_APP_KEY','')}:
            raise OperatorDenied()
    now=clock()
    if command=='backup':
        return {'status':'backup_verified','backup':backup_database(config,mocked=mocked)}
    backup=None
    if command=='activate':
        # Backup precedes WorkflowStore initialization, profile audit and approval.
        backup=backup_database(config,mocked=mocked)
    store,files=_open_existing(config,mocked=mocked)
    if command=='activate':
        approval=create_review_pilot_approval(store,config,approval_id,now,
            report_profile='hotel_daily_table_v1',input_policy='redacted_owner_reports')
    elif command=='revoke':
        with store._connect() as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT * FROM pilot_review_authorizations WHERE approval_id=?',(approval_id,)).fetchone()
            if not row or row['legacy_identity']!=_identity(config)[0]:raise OperatorDenied()
            approval=dict(row)
    else:
        budget=ReviewPilotBudget(store,files,config,approval_id,clock=clock)
        approval=budget.assert_active()
    output={'approval_id':approval_id,'expires_at':approval['expires_at']}
    if backup is not None:output['backup']=backup
    if command=='revoke':
        with store._connect() as db:
            prior=db.execute('SELECT 1 FROM pilot_review_revocations WHERE approval_id=?',(approval_id,)).fetchone()
        if not prior:revoke_review_pilot(store,approval_id,now)
        return dict(output,status='revoked_disabled')
    monitor=ReviewPilotCostMonitor(store,config.account_sid,config.auth_token,approval_id,clock=clock,transport=transport)
    with store._connect() as db:
        if db.execute('SELECT 1 FROM review_cost_baselines WHERE approval_id=?',(approval_id,)).fetchone():
            return dict(output,status='baseline_already_present_disabled')
    try:
        monitor.initialize_baseline(now)
    except CostMonitorUnavailable:
        return dict(output,status='approved_baseline_missing_disabled')
    return dict(output,status='approved_baseline_ready_disabled')


def main(argv=None):
    class SafeParser(argparse.ArgumentParser):
        def error(self,message):raise OperatorDenied()
    parser=SafeParser(description=__doc__)
    parser.add_argument('command',choices=('backup','activate','baseline','revoke'))
    parser.add_argument('--approval-id')
    try:
        args=parser.parse_args(argv)
        result=execute(args.command,args.approval_id)
    except Exception:
        # Never print raw exceptions, environment, HTTP response or credentials.
        print(json.dumps({'status':'operator_action_denied_keep_disabled'}),flush=True)
        return 1
    print(json.dumps(result,sort_keys=True),flush=True)
    return 2 if result['status']=='approved_baseline_missing_disabled' else 0


if __name__=='__main__':raise SystemExit(main())
