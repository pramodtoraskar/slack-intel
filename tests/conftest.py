import sqlite3
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def tmp_db(tmp_path):
    """In-memory-style SQLite DB in a temp file with the full schema."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            channel_id TEXT,
            channel_name TEXT,
            ts REAL,
            date TEXT,
            user_id TEXT,
            user_name TEXT,
            text TEXT,
            thread_ts TEXT,
            is_reply INTEGER,
            raw_json TEXT
        );
        CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY,
            name TEXT,
            is_private INTEGER,
            fetched_at TEXT
        );
        CREATE TABLE IF NOT EXISTS fetch_state (
            channel_id TEXT PRIMARY KEY,
            last_cursor TEXT,
            last_ts REAL,
            oldest_ts REAL,
            is_complete INTEGER,
            is_recent_complete INTEGER,
            message_count INTEGER,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS analysis_state (
            channel_id TEXT PRIMARY KEY,
            channel_name TEXT,
            message_count_at_analysis INTEGER,
            analyzed_at TEXT,
            report_path TEXT
        );
    """)
    conn.commit()
    yield conn, db_path
    conn.close()
