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
                CREATE TABLE IF NOT EXISTS staff_settings (manager_id TEXT PRIMARY KEY, payload TEXT NOT NULL, demo_seed_version INTEGER NOT NULL DEFAULT 0, dataset_mode TEXT NOT NULL DEFAULT 'operational', revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS weekly_roster_versions (manager_id TEXT NOT NULL, week_start TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(manager_id,week_start,version));
                CREATE TABLE IF NOT EXISTS weekly_rosters (manager_id TEXT NOT NULL, week_start TEXT NOT NULL, payload TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(manager_id,week_start));
            """)
            # Preserve the existing latest snapshot when upgrading; never rewrite history.
            db.execute("INSERT OR IGNORE INTO weekly_roster_versions(manager_id,week_start,version,payload,created_at) SELECT manager_id,week_start,version,payload,updated_at FROM weekly_rosters")
            columns = {row[1] for row in db.execute("PRAGMA table_info(staff_settings)")}
            if "revision" not in columns:
                db.execute("ALTER TABLE staff_settings ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
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
            row = db.execute("SELECT payload,demo_seed_version,dataset_mode,revision FROM staff_settings WHERE manager_id=?", (manager_id,)).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        payload["_demo_seed_version"] = row[1]
        payload["_dataset_mode"] = row[2]
        payload["_settings_revision"] = row[3]
        return payload

    def save_staff_settings(self, manager_id, payload, demo_seed_version=0, dataset_mode="operational", expected_revision=None):
        if dataset_mode not in {"demo", "operational"}:
            raise ValueError("Dataset mode must be 'demo' or 'operational'.")
        encoded = json.dumps({k: v for k, v in payload.items() if not k.startswith("_")}, allow_nan=False)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT revision FROM staff_settings WHERE manager_id=?", (manager_id,)).fetchone()
            if expected_revision is not None and (not current or current[0] != expected_revision):
                raise ValueError("Saved settings changed in another session. Reload before saving this draft.")
            revision = current[0] + 1 if current else 1
            db.execute("INSERT INTO staff_settings(manager_id,payload,demo_seed_version,dataset_mode,revision) VALUES(?,?,?,?,?) ON CONFLICT(manager_id) DO UPDATE SET payload=excluded.payload,demo_seed_version=excluded.demo_seed_version,dataset_mode=excluded.dataset_mode,revision=excluded.revision,updated_at=CURRENT_TIMESTAMP", (manager_id, encoded, int(demo_seed_version), dataset_mode, revision))
        return revision

    def save_hotel_parameters(self, manager_id, values, expected_revision=None):
        """Merge shared Parameters fields into the latest canonical saved settings.

        Daily service-count/session inputs remain separate. No WhatsApp-specific
        capacity copy exists and no inventory value is inferred from workload.
        """
        from services.hotel_parameters import apply_shared_parameters
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload,demo_seed_version,dataset_mode,revision FROM staff_settings WHERE manager_id=?", (manager_id,)).fetchone()
            if not row:
                raise ValueError("Save hotel staff/settings before editing shared parameters.")
            if expected_revision is not None and row[3] != expected_revision:
                raise ValueError("Saved settings changed before initializing shared parameters; reload without overwriting.")
            current = json.loads(row[0])
            current["dataset_mode"] = row[2]
            saved = apply_shared_parameters(current, values)
            encoded = json.dumps({k: v for k, v in saved.items() if not k.startswith("_")}, allow_nan=False)
            revision = row[3] + 1
            db.execute("UPDATE staff_settings SET payload=?,revision=?,updated_at=CURRENT_TIMESTAMP WHERE manager_id=?", (encoded, revision, manager_id))
        return dict(saved, _demo_seed_version=row[1], _dataset_mode=row[2], _settings_revision=revision)

    def get_weekly_roster(self, manager_id, week_start):
        with self._connect() as db:
            row = db.execute("SELECT payload,version,updated_at FROM weekly_rosters WHERE manager_id=? AND week_start=?", (manager_id, str(week_start))).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        payload["version"] = row[1]
        payload["updated_at"] = row[2]
        return payload

    def is_whatsapp_manager(self, manager_id):
        """Development UI guard, not production authentication."""
        with self._connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wa_tenants'").fetchone()
            return bool(exists and db.execute("SELECT 1 FROM wa_tenants WHERE manager_id=?", (manager_id,)).fetchone())

    def list_weekly_roster_versions(self, manager_id, week_start=None):
        query = "SELECT payload,version,created_at FROM weekly_roster_versions WHERE manager_id=?"
        args = [manager_id]
        if week_start is not None:
            query += " AND week_start=?"
            args.append(str(week_start))
        query += " ORDER BY created_at DESC,version DESC,week_start DESC"
        with self._connect() as db:
            rows = db.execute(query, args).fetchall()
        return [dict(json.loads(row[0]), version=row[1], updated_at=row[2]) for row in rows]

    @staticmethod
    def _save_roster_in_transaction(db, manager_id, week_start, payload):
        if not isinstance(payload, dict) or payload.get("week_start") != str(week_start):
            raise ValueError("Roster week start must match the storage key.")
        if len(payload.get("forecast", [])) != 7 or not isinstance(payload.get("schedule"), list) or not isinstance(payload.get("constraints"), list):
            raise ValueError("A saved roster needs seven reviewed forecast days, schedule rows, and an explicit constraints list.")
        row = db.execute("SELECT version FROM weekly_rosters WHERE manager_id=? AND week_start=?", (manager_id, str(week_start))).fetchone()
        version = int(row[0]) + 1 if row else 1
        saved = dict(payload, version=version)
        encoded = json.dumps(saved, allow_nan=False)
        db.execute("INSERT INTO weekly_roster_versions(manager_id,week_start,version,payload) VALUES(?,?,?,?)", (manager_id, str(week_start), version, encoded))
        db.execute("INSERT INTO weekly_rosters(manager_id,week_start,payload,version) VALUES(?,?,?,?) ON CONFLICT(manager_id,week_start) DO UPDATE SET payload=excluded.payload,version=excluded.version,updated_at=CURRENT_TIMESTAMP", (manager_id, str(week_start), encoded, version))
        return saved

    def save_weekly_roster(self, manager_id, week_start, payload):
        # Allocate a version and append its snapshot under one write lock.
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._save_roster_in_transaction(db, manager_id, week_start, payload)
