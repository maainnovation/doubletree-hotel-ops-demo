"""One sequential owner worker, private OS lock, no HTTP worker trigger."""
from contextlib import contextmanager
import fcntl
import json
import os
import stat
import time

from services.owner_review_runtime import build_owner_components
from services.whatsapp_provider import ProviderError
from services.whatsapp_workflow import ForecastWorker


@contextmanager
def singleton_worker(root):
    path=root/'.review-worker.lock'
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('Unsafe worker lock')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:os.close(fd)


def run_worker(components,*,once=False,sleep=time.sleep):
    rt=components.runtime
    worker=ForecastWorker(rt.store,rt.files,rt.provider,clock=rt.clock,acknowledge_uploads=True,
                          review_images=True,media_evaluator=components.media_evaluator)
    last_refresh=None
    with singleton_worker(rt.config.data_root):
        with rt.store._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS pilot_review_heartbeat(approval_id TEXT PRIMARY KEY,updated_at REAL NOT NULL)')
        while True:
            try:rt.assert_active()
            except ProviderError:
                if once:return {'processed':0,'dispatched':0,'app_processed':0,'active':False}
                sleep(60);continue
            now=rt.clock()
            if last_refresh is None or now-last_refresh>=60:
                components.cost_monitor.refresh(now)
                last_refresh=now
            with rt.store._connect() as db:
                db.execute('INSERT INTO pilot_review_heartbeat VALUES(?,?) ON CONFLICT(approval_id) DO UPDATE SET updated_at=excluded.updated_at',(rt.approval_id,now))
            result=worker.drain_ready(limit=1) if components.whatsapp_enabled is True else {"processed":0,"dispatched":0}
            # Sequential, not a second OCR process while WhatsApp is working.
            result['app_processed']=int(components.app_workflow.process_one())
            if any(result.values()):print(json.dumps({'owner_review':True,**result}),flush=True)
            if once:return result
            sleep(1)


def main():
    try:components=build_owner_components()
    except Exception:
        print('owner_review_worker_configuration_unavailable',flush=True);components=None
    if components is None:
        while True:time.sleep(60)
    run_worker(components)


if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:pass
    except Exception:
        print('owner_review_worker_stopped',flush=True)
        raise SystemExit(1) from None
