"""SQLite persistence: settings, games, saved jobs, run history, item metadata."""

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

_path: str = ""
_local = threading.local()
_write_lock = threading.RLock()

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
    appid      TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS saved_jobs (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    appid          TEXT NOT NULL,
    game_name      TEXT NOT NULL DEFAULT '',
    source_type    TEXT NOT NULL DEFAULT 'workshop',
    source_value   TEXT NOT NULL DEFAULT '',
    pages          TEXT NOT NULL DEFAULT 'all',
    sort_key       TEXT NOT NULL DEFAULT 'trend_week',
    delay_min      REAL NOT NULL DEFAULT 1.0,
    delay_max      REAL NOT NULL DEFAULT 2.5,
    scrape_only    INTEGER NOT NULL DEFAULT 0,
    download_only  INTEGER NOT NULL DEFAULT 0,
    schedule_type  TEXT NOT NULL DEFAULT 'none',
    schedule_value TEXT NOT NULL DEFAULT '',
    enabled        INTEGER NOT NULL DEFAULT 1,
    next_run_at    REAL,
    last_run_at    REAL,
    last_status    TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,
    trigger        TEXT NOT NULL,
    saved_job_id   TEXT,
    appid          TEXT NOT NULL DEFAULT '',
    game_name      TEXT NOT NULL DEFAULT '',
    source_type    TEXT NOT NULL DEFAULT 'workshop',
    source_value   TEXT NOT NULL DEFAULT '',
    params         TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL,
    phase          TEXT NOT NULL DEFAULT '',
    queued_at      REAL NOT NULL,
    started_at     REAL,
    finished_at    REAL,
    total          INTEGER NOT NULL DEFAULT 0,
    done           INTEGER NOT NULL DEFAULT 0,
    downloaded     INTEGER NOT NULL DEFAULT 0,
    updated        INTEGER NOT NULL DEFAULT 0,
    skipped        INTEGER NOT NULL DEFAULT 0,
    failed         INTEGER NOT NULL DEFAULT 0,
    unavailable    INTEGER NOT NULL DEFAULT 0,
    failed_ids     TEXT NOT NULL DEFAULT '[]',
    bytes_estimate INTEGER NOT NULL DEFAULT 0,
    error          TEXT NOT NULL DEFAULT '',
    hint           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS runs_appid ON runs(appid);
CREATE TABLE IF NOT EXISTS items (
    appid                   TEXT NOT NULL,
    item_id                 TEXT NOT NULL,
    title                   TEXT NOT NULL DEFAULT '',
    description             TEXT NOT NULL DEFAULT '',
    tags                    TEXT NOT NULL DEFAULT '[]',
    creator                 TEXT NOT NULL DEFAULT '',
    file_size               INTEGER NOT NULL DEFAULT 0,
    file_url                TEXT NOT NULL DEFAULT '',
    filename                TEXT NOT NULL DEFAULT '',
    preview_url             TEXT NOT NULL DEFAULT '',
    time_created            INTEGER NOT NULL DEFAULT 0,
    time_updated            INTEGER NOT NULL DEFAULT 0,
    result                  INTEGER NOT NULL DEFAULT 0,
    removed                 INTEGER NOT NULL DEFAULT 0,
    fetched_at              REAL NOT NULL DEFAULT 0,
    downloaded_at           REAL NOT NULL DEFAULT 0,
    downloaded_time_updated INTEGER NOT NULL DEFAULT 0,
    disk_size               INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (appid, item_id)
);
"""


def init(path: str) -> None:
    global _path
    close()
    _path = path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _write_lock:
        conn().executescript(_SCHEMA)
        conn().execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != _path:
        # Autocommit mode: transactions are explicit via transaction().
        c = sqlite3.connect(_path, timeout=30, check_same_thread=False, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
        _local.path = _path
    return c


def close() -> None:
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
        _local.conn = None


@contextmanager
def transaction():
    with _write_lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def query(sql: str, params=()) -> list[sqlite3.Row]:
    return conn().execute(sql, params).fetchall()


def query_one(sql: str, params=()) -> sqlite3.Row | None:
    return conn().execute(sql, params).fetchone()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def get_settings() -> dict:
    return {r["key"]: json.loads(r["value"]) for r in query("SELECT key, value FROM settings")}


def set_settings(values: dict) -> None:
    with transaction() as c:
        c.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v)) for k, v in values.items()],
        )


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------
def get_game_name(appid: str) -> str | None:
    row = query_one("SELECT name FROM games WHERE appid = ?", (appid,))
    return row["name"] if row else None


def set_game_name(appid: str, name: str) -> None:
    with transaction() as c:
        c.execute(
            "INSERT INTO games(appid, name, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(appid) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at",
            (appid, name, time.time()),
        )


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------
def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
