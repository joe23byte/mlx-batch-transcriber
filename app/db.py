import json
import sqlite3
import threading
import time

from .config import DB_PATH, DEFAULT_SETTINGS

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    out_rel TEXT NOT NULL,
    is_upload INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    duration REAL,
    status TEXT NOT NULL DEFAULT 'queued',
    phase TEXT,
    progress REAL NOT NULL DEFAULT 0,
    model TEXT,
    language TEXT,
    detected_language TEXT,
    output_dir TEXT,
    elapsed REAL,
    started_at REAL,
    finished_at REAL,
    error TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

JOB_FIELDS = {
    "source_path", "name", "out_rel", "is_upload", "size", "mtime", "duration", "status", "phase",
    "progress", "model", "language", "detected_language", "output_dir", "elapsed", "started_at",
    "finished_at", "error",
}


class Database:
    def __init__(self, path=DB_PATH):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def _exec(self, sql, params=()):
        with self._lock:
            self._conn.execute(sql, params)

    # Rows must be fetched while holding the lock: the connection is shared between threads.
    def _all(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql, params=()):
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    # ---- settings -------------------------------------------------------
    def get_settings(self):
        rows = self._all("SELECT key, value FROM settings")
        settings = dict(DEFAULT_SETTINGS)
        for r in rows:
            if r["key"] in DEFAULT_SETTINGS:
                settings[r["key"]] = json.loads(r["value"])
        return settings

    def get_value(self, key, default=None):
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_value(self, key, value):
        self._exec(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # ---- jobs -----------------------------------------------------------
    def list_jobs(self):
        return self._all("SELECT * FROM jobs ORDER BY id")

    def get_job(self, job_id):
        return self._one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def get_job_by_path(self, source_path):
        return self._one("SELECT * FROM jobs WHERE source_path=?", (source_path,))

    def out_rel_taken(self, out_rel, exclude_path):
        return self._one(
            "SELECT 1 AS x FROM jobs WHERE out_rel=? AND source_path<>?", (out_rel, exclude_path)
        ) is not None

    def insert_job(self, **fields):
        fields = {k: v for k, v in fields.items() if k in JOB_FIELDS}
        fields["created_at"] = time.time()
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._lock:
            cur = self._conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(fields.values()))
            return cur.lastrowid

    def update_job(self, job_id, **fields):
        fields = {k: v for k, v in fields.items() if k in JOB_FIELDS}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))

    def delete_job(self, job_id):
        self._exec("DELETE FROM jobs WHERE id=?", (job_id,))

    def next_queued(self):
        return self._one("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1")

    def set_status_where(self, new_status, statuses, reset_progress=True):
        marks = ", ".join("?" for _ in statuses)
        extra = ", progress=0, phase=NULL, error=NULL" if reset_progress else ""
        self._exec(f"UPDATE jobs SET status=?{extra} WHERE status IN ({marks})", (new_status, *statuses))

    def recover_after_restart(self):
        """Jobs that were running when the app stopped go back into the queue."""
        self._exec("UPDATE jobs SET status='queued', progress=0, phase=NULL WHERE status='running'")
