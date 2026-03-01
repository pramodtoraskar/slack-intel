# tests/test_analyzer_topic_notes.py
import sqlite3
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import analyzer


def test_ensure_topic_notes_table_creates_table(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    analyzer.ensure_topic_notes_table(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "topic_notes" in tables


def test_topic_notes_schema(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    analyzer.ensure_topic_notes_table(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(topic_notes)").fetchall()}
    assert {"topic", "created_at", "key_notes"}.issubset(cols)


def test_ensure_idempotent(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    analyzer.ensure_topic_notes_table(conn)
    analyzer.ensure_topic_notes_table(conn)  # second call must not raise
