"""Development-only separate worker; default fake transport, never Streamlit.

Run once: python -m scripts.whatsapp_worker --once
For a supervised LOCAL synthetic experiment: --watch (Ctrl+C stops).
The complete no-network demo is scripts.demo_whatsapp_workflow.
"""
import argparse
import json
import os
from pathlib import Path
import time

from dotenv import load_dotenv

from services.whatsapp_provider import provider_from_env
from services.whatsapp_store import PrivateFileStore, WorkflowStore
from services.whatsapp_workflow import ForecastWorker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true")
    group.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    if os.getenv("WHATSAPP_FORECAST_ENABLED", "0") != "1":
        parser.error("Set WHATSAPP_FORECAST_ENABLED=1 for an explicitly configured synthetic pilot")
    store = WorkflowStore()
    files = PrivateFileStore(os.getenv("WHATSAPP_PRIVATE_STORAGE", root / "data" / "whatsapp-private"))
    worker = ForecastWorker(store, files, provider_from_env(), acknowledge_uploads=os.getenv("WHATSAPP_UPLOAD_ACK_ENABLED", "0") == "1",
                            review_images=os.getenv('WHATSAPP_REVIEW_ENABLED','0')=='1')
    try:
        while True:
            print(json.dumps({"mode": "development-fake", **worker.drain_ready()}), flush=True)
            if not args.watch:
                break
            time.sleep(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
