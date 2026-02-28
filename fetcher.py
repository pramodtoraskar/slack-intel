# fetcher.py — Fetches Slack channel history into local SQLite.
# Run standalone: python fetcher.py  (requires SLACK_TOKEN + TARGET_CHANNELS in config.py)

import sqlite3
import time
import json
from datetime import datetime, timezone, timedelta
import config

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    """Return a sqlite3 connection with WAL mode enabled."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create all tables if they don't exist yet."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            id          TEXT PRIMARY KEY,
            name        TEXT,
            is_private  INTEGER,
            fetched_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id           TEXT PRIMARY KEY,
            channel_id   TEXT,
            channel_name TEXT,
            ts           REAL,
            date         TEXT,
            user_id      TEXT,
            user_name    TEXT,
            text         TEXT,
            thread_ts    TEXT,
            is_reply     INTEGER DEFAULT 0,
            raw_json     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id);
        CREATE INDEX IF NOT EXISTS idx_messages_ts      ON messages(ts);

        CREATE TABLE IF NOT EXISTS fetch_state (
            channel_id         TEXT PRIMARY KEY,
            last_cursor        TEXT,
            last_ts            REAL,
            oldest_ts          REAL,
            is_complete        INTEGER DEFAULT 0,
            is_recent_complete INTEGER DEFAULT 0,
            message_count      INTEGER DEFAULT 0,
            updated_at         TEXT
        );

        CREATE TABLE IF NOT EXISTS analysis_state (
            channel_id                TEXT PRIMARY KEY,
            channel_name              TEXT,
            message_count_at_analysis INTEGER,
            analyzed_at               TEXT,
            report_path               TEXT
        );
    """)
    conn.commit()
    conn.close()
    print("DB initialised at", config.DB_PATH)
