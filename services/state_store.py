"""Small SQLite store for pending WhatsApp scenarios, approvals, and webhook deduplication."""
import json
import os
import sqlite3
from pathlib import Path


class StateStore:
    def __init__(self, path=None):
        self.path = str(path or os.getenv("COPILOT_STATE_DB", Path(__file__).resolve().parents[1] / "data" / "copilot_state.sqlite3"))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS manager_state (manager_id TEXT PRIMARY KEY, scenarios TEXT NOT NULL, pending TEXT, approval TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS webhook_messages (message_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, manager_id TEXT NOT NULL, event_json TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS staff_settings (manager_id TEXT PRIMARY KEY, payload TEXT NOT NULL, demo_seed_version INTEGER NOT NULL DEFAULT 0, dataset_mode TEXT NOT NULL DEFAULT 'operational', updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS weekly_rosters (manager_id TEXT NOT NULL, week_start TEXT NOT NULL, payload TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(manager_id,week_start));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(staff_settings)")}
            if "dataset_mode" not in columns:
                # Existing saved records must be treated conservatively after upgrading.
                db.execute("ALTER TABLE staff_settings ADD COLUMN dataset_mode TEXT NOT NULL DEFAULT 'operational'")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def get_manager(self, manager_id):
        with self._connect() as db:
            row = db.execute("SELECT scenarios,pending,approval FROM manager_state WHERE manager_id=?", (manager_id,)).fetchone()
            if not row:
                return {"scenarios": {"attendant_adjustment": 0, "checkout_minutes": None, "late_release_minutes": 0, "receipt_day_overrides": {}, "budget_usd": None}, "pending": None, "approval": {"status": "AWAITING APPROVAL", "history": []}}
            return {"scenarios": json.loads(row[0]), "pending": json.loads(row[1]) if row[1] else None, "approval": json.loads(row[2])}

    def save_manager(self, manager_id, state):
        with self._connect() as db:
            db.execute("INSERT INTO manager_state(manager_id,scenarios,pending,approval) VALUES(?,?,?,?) ON CONFLICT(manager_id) DO UPDATE SET scenarios=excluded.scenarios,pending=excluded.pending,approval=excluded.approval", (manager_id, json.dumps(state["scenarios"]), json.dumps(state["pending"]) if state.get("pending") else None, json.dumps(state["approval"])))

    def record_audit(self, manager_id, event):
        with self._connect() as db:
            db.execute("INSERT INTO audit_events(manager_id,event_json) VALUES(?,?)", (manager_id, json.dumps(event)))

    def get_audit(self, manager_id, limit=100):
        with self._connect() as db:
            rows = db.execute("SELECT event_json FROM audit_events WHERE manager_id=? ORDER BY id DESC LIMIT ?", (manager_id, int(limit))).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def has_seen_message(self, message_id):
        with self._connect() as db:
            return db.execute("SELECT 1 FROM webhook_messages WHERE message_id=?", (message_id,)).fetchone() is not None

    def mark_message_seen(self, message_id):
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO webhook_messages(message_id) VALUES(?)", (message_id,))

    def get_staff_settings(self, manager_id):
        with self._connect() as db:
            row = db.execute("SELECT payload,demo_seed_version,dataset_mode FROM staff_settings WHERE manager_id=?", (manager_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        payload["_demo_seed_version"] = row[1]
        payload["_dataset_mode"] = row[2]
        return payload

    def save_staff_settings(self, manager_id, payload, demo_seed_version=0, dataset_mode="operational"):
        if dataset_mode not in {"demo", "operational"}:
            raise ValueError("Dataset mode must be 'demo' or 'operational'.")
        encoded = json.dumps(payload)
        with self._connect() as db:
            db.execute("INSERT INTO staff_settings(manager_id,payload,demo_seed_version,dataset_mode) VALUES(?,?,?,?) ON CONFLICT(manager_id) DO UPDATE SET payload=excluded.payload,demo_seed_version=excluded.demo_seed_version,dataset_mode=excluded.dataset_mode,updated_at=CURRENT_TIMESTAMP", (manager_id, encoded, int(demo_seed_version), dataset_mode))

    def get_weekly_roster(self, manager_id, week_start):
        with self._connect() as db:
            row = db.execute("SELECT payload,version,updated_at FROM weekly_rosters WHERE manager_id=? AND week_start=?", (manager_id, str(week_start))).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        payload["version"] = row[1]
        payload["updated_at"] = row[2]
        return payload

    def save_weekly_roster(self, manager_id, week_start, payload):
        if not isinstance(payload, dict) or payload.get("week_start") != str(week_start):
            raise ValueError("Roster week start must match the storage key.")
        if len(payload.get("forecast", [])) != 7 or not isinstance(payload.get("schedule"), list) or not isinstance(payload.get("constraints"), list):
            raise ValueError("A saved roster needs seven reviewed forecast days, schedule rows, and an explicit constraints list.")
        current = self.get_weekly_roster(manager_id, week_start)
        version = int(current["version"] + 1) if current else 1
        saved = dict(payload)
        saved["version"] = version
        encoded = json.dumps(saved, allow_nan=False)
        with self._connect() as db:
            db.execute("INSERT INTO weekly_rosters(manager_id,week_start,payload,version) VALUES(?,?,?,?) ON CONFLICT(manager_id,week_start) DO UPDATE SET payload=excluded.payload,version=excluded.version,updated_at=CURRENT_TIMESTAMP", (manager_id, str(week_start), encoded, version))
