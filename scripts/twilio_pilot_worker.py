"""Separate process supervised alongside the isolated pilot HTTP server."""
import json
import os
import time

from services.twilio_pilot import PilotConfig, build_runtime
from services.whatsapp_workflow import ForecastWorker


def run_worker(runtime, *, once=False):
    worker=ForecastWorker(runtime.store,runtime.files,runtime.provider,clock=runtime.clock)
    while True:
        runtime.assert_active()
        with runtime.store._connect() as db:
            db.execute('INSERT INTO pilot_heartbeat(id,updated_at) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at',(runtime.clock(),))
        result=worker.drain_ready(limit=1)
        if result['processed'] or result['dispatched']:
            print(json.dumps({'synthetic_only':True,**result}),flush=True)
        if once:
            return result
        time.sleep(1)


def main():
    if os.getenv('PILOT_ENABLED','0')!='1':
        # Initial approved hosting may run without secrets; no provider created.
        while True:
            time.sleep(60)
    try:
        run_worker(build_runtime(PilotConfig.from_env()))
    except KeyboardInterrupt:
        pass
    except Exception:
        # Never emit configuration repr, credentials, URL capabilities or documents.
        print('Synthetic pilot worker stopped: configuration, expiry or runtime check failed.',flush=True)
        raise SystemExit(1) from None


if __name__=='__main__':
    main()
