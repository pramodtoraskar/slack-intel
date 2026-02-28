# fetcher.py — Fetches Slack channel history into local SQLite.
# Run standalone: python fetcher.py  (requires SLACK_TOKEN + TARGET_CHANNELS in config.py)

import sqlite3
import time
import json
from datetime import datetime, timezone, timedelta
import config

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    """Return a sqlite3 connection with WAL mode enabled.

    Uses row_factory=sqlite3.Row so all query results support both index
    and column-name access: row[0] and row['column_name'] both work.
    """
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # safe optimisation for WAL mode
    return conn


def init_db():
    """Create all tables and indexes if they don't exist yet.

    Tables:
      channels      — one row per Slack channel fetched
      messages      — all fetched messages and thread replies
      fetch_state   — checkpoint cursor per channel (is_complete = full history,
                      is_recent_complete = 90-day window done)
      analysis_state — tracks message_count at last LLM analysis per channel
    """
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            is_private  INTEGER,
            fetched_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id           TEXT PRIMARY KEY,
            channel_id   TEXT NOT NULL,
            channel_name TEXT,
            ts           REAL NOT NULL,
            date         TEXT,
            user_id      TEXT,
            user_name    TEXT,
            text         TEXT,
            thread_ts    TEXT,
            is_reply     INTEGER DEFAULT 0,
            raw_json     TEXT
        );

        -- Composite index covers the most common query: messages for channel X ordered by time
        CREATE INDEX IF NOT EXISTS idx_messages_channel_ts ON messages(channel_id, ts);
        -- Individual ts index retained for ORDER BY ts queries without channel filter
        CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);

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
