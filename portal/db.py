import sqlite3
import os
from contextlib import contextmanager
from pathlib import Path

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'student',
 enabled INTEGER NOT NULL DEFAULT 1, session_version INTEGER NOT NULL DEFAULT 1,
 created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
 id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 model TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 driver TEXT NOT NULL DEFAULT 'simulated' CHECK(driver IN ('simulated','pynq')),
 maintenance INTEGER NOT NULL DEFAULT 0, max_minutes INTEGER NOT NULL DEFAULT 120,
 host TEXT NOT NULL DEFAULT '', ssh_port INTEGER NOT NULL DEFAULT 22,
 ssh_user TEXT NOT NULL DEFAULT 'xilinx', jupyter_url TEXT NOT NULL DEFAULT '',
 last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rentals (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 device_id INTEGER NOT NULL REFERENCES devices(id),
 starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN
 ('reserved','preparing','active','releasing','completed','cancelled','failed')),
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 claimed_at INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
 access_secret TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 finished_at INTEGER, reason TEXT NOT NULL DEFAULT '',
 CHECK(ends_at > starts_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_rental_per_user ON rentals(user_id)
 WHERE status IN ('reserved','preparing','active','releasing');
CREATE UNIQUE INDEX IF NOT EXISTS one_running_rental_per_device ON rentals(device_id)
 WHERE status IN ('preparing','active','releasing');
CREATE INDEX IF NOT EXISTS rental_schedule ON rentals(device_id, starts_at, ends_at, status);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id),
 rental_id INTEGER REFERENCES rentals(id), action TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_limits (
 key TEXT PRIMARY KEY, count INTEGER NOT NULL, resets_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_state (
 id INTEGER PRIMARY KEY CHECK(id = 1), heartbeat INTEGER NOT NULL
);
"""


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE_PATH"])
    return g.db


@contextmanager
def transaction():
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def init_app(app):
    @app.teardown_appcontext
    def close_db(_error):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()


def initialize():
    conn = get_db()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # Never repopulate an existing inventory or touch hardware.
    if not conn.execute("SELECT 1 FROM devices LIMIT 1").fetchone():
        with transaction() as conn:
            for i in range(1, 4):
                conn.execute(
                    "INSERT INTO devices(slug,name,model,description) VALUES(?,?,?,?)",
                    (f"pynq-{i:02}", f"PYNQ {i:02}", "PYNQ-Z2",
                     "適合數位邏輯、Python 與 FPGA 加速實驗。"))
