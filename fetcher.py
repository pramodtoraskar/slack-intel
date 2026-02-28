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


# ── User resolution ───────────────────────────────────────────────────────────

def load_users(client):
    """Fetch all workspace users, return dict {user_id: display_name}."""
    users = {}
    cursor = None
    while True:
        resp = client.users_list(cursor=cursor, limit=200)
        for u in resp["members"]:
            name = u.get("real_name") or u.get("name") or u["id"]
            users[u["id"]] = name
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(config.RATE_LIMIT_DELAY)
    print(f"  Resolved {len(users)} users")
    return users


# ── Message helpers ───────────────────────────────────────────────────────────

SKIP_SUBTYPES = {"channel_join", "channel_leave", "bot_message",
                 "channel_archive", "channel_unarchive"}


def should_skip(msg):
    """Return True if this message should be excluded from storage."""
    return msg.get("subtype") in SKIP_SUBTYPES or not msg.get("text", "").strip()


def store_messages(conn, channel_id, channel_name, messages, users):
    """Insert messages into DB, ignoring duplicates. Returns count stored."""
    stored = 0
    for msg in messages:
        if should_skip(msg):
            continue
        ts = float(msg["ts"])
        msg_id = f"{channel_id}_{msg['ts']}"
        user_id = msg.get("user", "")
        conn.execute("""
            INSERT OR IGNORE INTO messages
                (id, channel_id, channel_name, ts, date, user_id, user_name,
                 text, thread_ts, is_reply, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            msg_id, channel_id, channel_name, ts,
            datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            user_id,
            users.get(user_id, user_id),
            msg.get("text", ""),
            msg.get("thread_ts"),
            0,
            json.dumps(msg),
        ))
        stored += 1
    conn.commit()
    return stored


# ── Thread replies ────────────────────────────────────────────────────────────

def fetch_threads(client, conn, channel_id, channel_name, users, thread_messages):
    """Fetch replies for messages that have reply_count > 0."""
    for msg in thread_messages:
        if not msg.get("reply_count"):
            continue
        cursor = None
        while True:
            try:
                resp = client.conversations_replies(
                    channel=channel_id,
                    ts=msg["ts"],
                    cursor=cursor,
                    limit=200,
                )
            except Exception as e:
                err_str = str(e).lower()
                if "ratelimited" in err_str or "429" in err_str:
                    retry_after = 60
                    if hasattr(e, "headers") and e.headers:
                        retry_after = int(e.headers.get("Retry-After", 60))
                    print(f"  Thread rate limited, sleeping {retry_after}s …")
                    time.sleep(retry_after)
                    continue
                raise
            # First message in replies is the parent — skip it
            replies = resp.get("messages", [])[1:]
            for r in replies:
                if should_skip(r):
                    continue
                ts = float(r["ts"])
                msg_id = f"{channel_id}_{r['ts']}"
                user_id = r.get("user", "")
                conn.execute("""
                    INSERT OR IGNORE INTO messages
                        (id, channel_id, channel_name, ts, date, user_id, user_name,
                         text, thread_ts, is_reply, raw_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    msg_id, channel_id, channel_name, ts,
                    datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                    user_id, users.get(user_id, user_id),
                    r.get("text", ""), r.get("thread_ts"), 1, json.dumps(r),
                ))
            conn.commit()
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            time.sleep(config.RATE_LIMIT_DELAY)
        time.sleep(config.RATE_LIMIT_DELAY)


# ── Per-channel fetch ─────────────────────────────────────────────────────────

def fetch_channel(client, conn, channel_id, channel_name, users, backfill=False):
    """Fetch messages for one channel with checkpoint/resume.

    Normal mode:  fetches messages newer than last_ts (or last 90 days on first run).
    Backfill mode: pages backward through full history using cursor from Slack API.
    Checkpoints every page so an interrupted run resumes from where it left off.
    """
    state = conn.execute(
        "SELECT * FROM fetch_state WHERE channel_id=?", (channel_id,)
    ).fetchone()

    now_ts = datetime.now(tz=timezone.utc).timestamp()

    if backfill:
        if state and state["is_complete"]:
            print(f"  [{channel_name}] backfill already complete, skipping")
            return
        # Resume from saved cursor if available, otherwise start fresh
        saved_cursor = state["last_cursor"] if state else None
        oldest = state["oldest_ts"] if state and state["oldest_ts"] else now_ts
    else:
        saved_cursor = None
        if state and state["is_recent_complete"]:
            # Incremental: only fetch newer than last_ts
            oldest = state["last_ts"] if state and state["last_ts"] else (
                now_ts - config.DAYS_BACK_DEFAULT * 86400
            )
        else:
            # First run: 90-day window
            oldest = now_ts - config.DAYS_BACK_DEFAULT * 86400

    # next_cursor tracks pagination across loop iterations
    next_cursor = saved_cursor
    page = 0
    total = 0
    thread_candidates = []

    while True:
        kwargs = dict(channel=channel_id, limit=200)
        if next_cursor:
            kwargs["cursor"] = next_cursor  # advance pagination on each page
        elif backfill:
            kwargs["latest"] = oldest       # first backfill page: start from oldest known
        else:
            kwargs["oldest"] = oldest       # normal mode: only newer than cutoff

        try:
            resp = client.conversations_history(**kwargs)
        except Exception as e:
            err_str = str(e).lower()
            if "ratelimited" in err_str or "429" in err_str:
                retry_after = 60
                if hasattr(e, "headers") and e.headers:
                    retry_after = int(e.headers.get("Retry-After", 60))
                print(f"  Rate limited, sleeping {retry_after}s …")
                time.sleep(retry_after)
                continue
            raise

        msgs = resp.get("messages", [])
        stored = store_messages(conn, channel_id, channel_name, msgs, users)
        thread_candidates.extend(msgs)
        total += stored
        page += 1
        print(f"  [{channel_name}] page {page} — {stored} stored ({total} total)")

        next_cursor = resp.get("response_metadata", {}).get("next_cursor")
        has_more = resp.get("has_more", False)

        # Update checkpoint after every page
        last_ts = float(msgs[0]["ts"]) if msgs else now_ts
        oldest_seen = float(msgs[-1]["ts"]) if msgs else oldest

        conn.execute("""
            INSERT INTO fetch_state
                (channel_id, last_cursor, last_ts, oldest_ts,
                 is_complete, is_recent_complete, message_count, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(channel_id) DO UPDATE SET
                last_cursor=excluded.last_cursor,
                last_ts=MAX(COALESCE(last_ts, 0), excluded.last_ts),
                oldest_ts=MIN(COALESCE(oldest_ts, excluded.oldest_ts), excluded.oldest_ts),
                is_complete=CASE WHEN excluded.is_complete=1 THEN 1 ELSE is_complete END,
                is_recent_complete=CASE WHEN excluded.is_recent_complete=1 THEN 1 ELSE is_recent_complete END,
                message_count=message_count + excluded.message_count,
                updated_at=excluded.updated_at
        """, (channel_id, next_cursor if has_more else None,
              last_ts, oldest_seen,
              1 if (backfill and not has_more) else 0,
              1 if (not backfill and not has_more) else 0,
              stored, datetime.now(tz=timezone.utc).isoformat()))
        conn.commit()

        if not has_more:
            break
        time.sleep(config.RATE_LIMIT_DELAY)

    # Fetch thread replies for this batch
    print(f"  [{channel_name}] fetching threads …")
    fetch_threads(client, conn, channel_id, channel_name, users, thread_candidates)

    # Upsert into channels table
    conn.execute("""
        INSERT OR REPLACE INTO channels (id, name, is_private, fetched_at)
        VALUES (?, ?, ?, ?)
    """, (channel_id, channel_name, 0, datetime.now(tz=timezone.utc).isoformat()))
    conn.commit()
    print(f"  [{channel_name}] done — {total} messages stored")


# ── Entry point ───────────────────────────────────────────────────────────────

def run_fetch(backfill=False):
    """Fetch all TARGET_CHANNELS from Slack into SQLite.

    backfill=False (default): fetch last DAYS_BACK_DEFAULT days, then increment on re-runs.
    backfill=True:            page backward through full history (resumable via fetch_state).
    """
    from slack_sdk import WebClient

    if not config.SLACK_TOKEN:
        raise ValueError("SLACK_TOKEN is empty — fill it in config.py")
    if not config.TARGET_CHANNELS:
        raise ValueError("TARGET_CHANNELS is empty — fill it in config.py")

    init_db()
    client = WebClient(token=config.SLACK_TOKEN)
    conn = get_db()
    mode = "BACKFILL (full history)" if backfill else f"RECENT ({config.DAYS_BACK_DEFAULT} days)"
    print(f"\n=== Fetching {len(config.TARGET_CHANNELS)} channels [{mode}] ===\n")

    users = load_users(client)

    # Resolve channel names → IDs via conversations_list
    channel_map = {}
    cursor = None
    while True:
        resp = client.conversations_list(
            types="public_channel,private_channel", limit=1000, cursor=cursor
        )
        for ch in resp.get("channels", []):
            channel_map[ch["name"]] = ch["id"]
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(config.RATE_LIMIT_DELAY)

    for name in config.TARGET_CHANNELS:
        channel_id = channel_map.get(name)
        if not channel_id:
            print(f"  WARNING: channel #{name} not found in workspace — skipping")
            continue
        print(f"\n→ #{name} ({channel_id})")
        fetch_channel(client, conn, channel_id, name, users, backfill=backfill)

    conn.close()
    print("\n=== Fetch complete ===")


if __name__ == "__main__":
    import sys
    run_fetch(backfill="--backfill" in sys.argv)
