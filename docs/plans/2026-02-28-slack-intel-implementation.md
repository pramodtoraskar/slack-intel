# Slack Intelligence Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a fully local 7-file pipeline that fetches Slack history into SQLite, analyzes it with a local Ollama LLM, produces per-channel and cross-channel reports, and serves them via a local web dashboard.

**Architecture:** Fetch → SQLite → Analyze (Ollama HTTP) → Export (MD/JSON) → Dashboard (http.server). All state is in one SQLite DB. Incremental updates compare message counts against `analysis_state` table. Two-phase fetch: 90-day default, `--backfill` for full history.

**Tech Stack:** Python 3 stdlib + `slack_sdk` + `requests`. No langchain, no openai, no cloud deps.

---

## Pre-flight

Before starting, verify:
```bash
python3 -c "import slack_sdk; print('slack_sdk ok')"
python3 -c "import requests; print('requests ok')"
curl -s http://localhost:11434/api/tags | python3 -m json.tool | grep llama3
```
If `slack_sdk` or `requests` missing: `pip install slack_sdk requests`

---

### Task 1: Project Scaffold

**Files:**
- Create: `config.py`
- Create: `data/.gitkeep`
- Create: `data/output/.gitkeep`
- Create: `.gitignore`

**Step 1: Create directory structure**

```bash
mkdir -p data/output
touch data/.gitkeep data/output/.gitkeep
```

**Step 2: Create `.gitignore`**

```
data/slack_intel.db
data/output/*.md
data/output/*.json
data/chroma/
*.pyc
__pycache__/
.env
config_local.py
```

**Step 3: Create `config.py`**

```python
# config.py — All settings in one place. Edit SLACK_TOKEN and TARGET_CHANNELS before running.

# --- Slack ---
SLACK_TOKEN = ""          # Paste your token here: xoxb-...
TARGET_CHANNELS = []      # e.g. ["general", "engineering", "product"]

# --- Ollama ---
OLLAMA_BASE = "http://localhost:11434"
CHAT_MODEL = "llama3"
CONTEXT_WINDOW = 6000     # safe character limit per LLM call

# --- Fetch behaviour ---
RATE_LIMIT_DELAY = 1.2    # seconds between Slack API calls
DAYS_BACK_DEFAULT = 90    # first-run window; None = fetch all (use --backfill)
MIN_MESSAGES = 10         # skip analysis for channels below this threshold

# --- Paths ---
DB_PATH = "./data/slack_intel.db"
OUTPUT_DIR = "./data/output"

# --- Dashboard ---
DASHBOARD_PORT = 8080
```

**Step 4: Verify config imports cleanly**

```bash
python3 -c "import config; print('config ok:', config.OLLAMA_BASE)"
```
Expected output: `config ok: http://localhost:11434`

**Step 5: Commit**

```bash
git add config.py data/.gitkeep data/output/.gitkeep .gitignore
git commit -m "feat: project scaffold — config, data dirs, gitignore"
```

---

### Task 2: fetcher.py — SQLite Schema + DB Init

**Files:**
- Create: `fetcher.py` (schema + db_init only, fetch logic in Task 3)

**Step 1: Write `fetcher.py` with schema and `init_db()`**

```python
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
```

**Step 2: Verify DB init runs cleanly**

```bash
python3 -c "import fetcher; fetcher.init_db()"
```
Expected: `DB initialised at ./data/slack_intel.db`

```bash
python3 -c "
import sqlite3
conn = sqlite3.connect('./data/slack_intel.db')
tables = conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()
print([t[0] for t in tables])
"
```
Expected: `['channels', 'messages', 'fetch_state', 'analysis_state']`

**Step 3: Commit**

```bash
git add fetcher.py
git commit -m "feat: fetcher — SQLite schema and db init"
```

---

### Task 3: fetcher.py — Slack Fetch Logic

**Files:**
- Modify: `fetcher.py` (add user resolution, channel fetch, thread fetch, `run_fetch`)

**Step 1: Add user resolution + message storage helpers to `fetcher.py`**

Append after `init_db()`:

```python
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
    return msg.get("subtype") in SKIP_SUBTYPES or not msg.get("text", "").strip()


def store_messages(conn, channel_id, channel_name, messages, users):
    """Insert messages into DB, ignoring duplicates."""
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
```

**Step 2: Add thread fetch + channel fetch + `run_fetch` to `fetcher.py`**

Append after the helpers:

```python
# ── Thread replies ────────────────────────────────────────────────────────────

def fetch_threads(client, conn, channel_id, channel_name, users, thread_messages):
    """Fetch replies for messages that have them."""
    for msg in thread_messages:
        if not msg.get("reply_count"):
            continue
        replies = []
        cursor = None
        while True:
            resp = client.conversations_replies(
                channel=channel_id,
                ts=msg["ts"],
                cursor=cursor,
                limit=200,
            )
            # first message is the parent — skip it
            for r in resp["messages"][1:]:
                r["is_reply"] = True
                replies.append(r)
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
            time.sleep(config.RATE_LIMIT_DELAY)

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
        time.sleep(config.RATE_LIMIT_DELAY)


# ── Per-channel fetch ─────────────────────────────────────────────────────────

def fetch_channel(client, conn, channel_id, channel_name, users, backfill=False):
    """Fetch messages for one channel, with checkpoint/resume."""
    state = conn.execute(
        "SELECT * FROM fetch_state WHERE channel_id=?", (channel_id,)
    ).fetchone()

    # Decide oldest/newest bounds
    now_ts = datetime.now(tz=timezone.utc).timestamp()

    if backfill:
        if state and state["is_complete"]:
            print(f"  [{channel_name}] backfill already complete, skipping")
            return
        oldest = state["oldest_ts"] if state and state["oldest_ts"] else now_ts
        newest = None   # page backward from oldest
    else:
        if state and state["is_recent_complete"]:
            # incremental: only fetch newer than last_ts
            newest = None
            oldest = state["last_ts"] if state and state["last_ts"] else None
            if oldest is None:
                cutoff = now_ts - config.DAYS_BACK_DEFAULT * 86400
                oldest = cutoff
        else:
            # first run: 90-day window
            cutoff = now_ts - config.DAYS_BACK_DEFAULT * 86400
            oldest = cutoff
            newest = None

    cursor = state["last_cursor"] if (state and not backfill) else None
    page = 0
    total = 0
    thread_candidates = []

    while True:
        kwargs = dict(channel=channel_id, limit=200, cursor=cursor)
        if backfill:
            kwargs["latest"] = oldest   # go backwards
        else:
            if oldest:
                kwargs["oldest"] = oldest

        try:
            resp = client.conversations_history(**kwargs)
        except Exception as e:
            if "ratelimited" in str(e).lower():
                retry_after = int(getattr(e, "headers", {}).get("Retry-After", 60))
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

        cursor = resp.get("response_metadata", {}).get("next_cursor")
        has_more = resp.get("has_more", False)

        # Update checkpoint
        last_ts = float(msgs[0]["ts"]) if msgs else now_ts
        oldest_seen = float(msgs[-1]["ts"]) if msgs else oldest

        conn.execute("""
            INSERT INTO fetch_state
                (channel_id, last_cursor, last_ts, oldest_ts,
                 is_complete, is_recent_complete, message_count, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(channel_id) DO UPDATE SET
                last_cursor=excluded.last_cursor,
                last_ts=MAX(last_ts, excluded.last_ts),
                oldest_ts=MIN(COALESCE(oldest_ts, excluded.oldest_ts), excluded.oldest_ts),
                message_count=message_count+excluded.message_count,
                updated_at=excluded.updated_at
        """, (channel_id, cursor if has_more else None,
              last_ts, oldest_seen,
              1 if (backfill and not has_more) else 0,
              1 if (not backfill and not has_more) else 0,
              stored, datetime.now(tz=timezone.utc).isoformat()))
        conn.commit()

        if not has_more:
            break
        time.sleep(config.RATE_LIMIT_DELAY)

    # Fetch thread replies
    print(f"  [{channel_name}] fetching threads …")
    fetch_threads(client, conn, channel_id, channel_name, users, thread_candidates)

    # Update channels table
    conn.execute("""
        INSERT OR REPLACE INTO channels (id, name, is_private, fetched_at)
        VALUES (?, ?, ?, ?)
    """, (channel_id, channel_name, 0, datetime.now(tz=timezone.utc).isoformat()))
    conn.commit()
    print(f"  [{channel_name}] done — {total} messages stored")


# ── Entry point ───────────────────────────────────────────────────────────────

def run_fetch(backfill=False):
    """Fetch all TARGET_CHANNELS. Call with backfill=True for full history."""
    from slack_sdk import WebClient

    if not config.SLACK_TOKEN:
        raise ValueError("SLACK_TOKEN is empty — fill it in config.py")
    if not config.TARGET_CHANNELS:
        raise ValueError("TARGET_CHANNELS is empty — fill it in config.py")

    init_db()
    client = WebClient(token=config.SLACK_TOKEN)
    conn = get_db()
    mode = "BACKFILL" if backfill else "RECENT (90 days)"
    print(f"\n=== Fetching {len(config.TARGET_CHANNELS)} channels [{mode}] ===\n")

    users = load_users(client)

    # Resolve channel names → IDs
    channel_map = {}
    resp = client.conversations_list(
        types="public_channel,private_channel", limit=1000
    )
    for ch in resp.get("channels", []):
        channel_map[ch["name"]] = ch["id"]

    for name in config.TARGET_CHANNELS:
        channel_id = channel_map.get(name)
        if not channel_id:
            print(f"  WARNING: channel #{name} not found — skipping")
            continue
        print(f"\n→ #{name} ({channel_id})")
        fetch_channel(client, conn, channel_id, name, users, backfill=backfill)

    conn.close()
    print("\n=== Fetch complete ===")


if __name__ == "__main__":
    import sys
    run_fetch(backfill="--backfill" in sys.argv)
```

**Step 3: Dry-run import check (no token needed)**

```bash
python3 -c "import fetcher; print('fetcher imports ok')"
```
Expected: `fetcher imports ok`

**Step 4: Commit**

```bash
git add fetcher.py
git commit -m "feat: fetcher — Slack API fetch, checkpoint/resume, thread replies"
```

---

### Task 4: analyzer.py

**Files:**
- Create: `analyzer.py`

**Step 1: Create `analyzer.py`**

```python
# analyzer.py — Analyzes Slack messages using local Ollama LLM.
# Run standalone: python analyzer.py  (reads from SQLite, no Slack calls)

import sqlite3
import requests
import json
from datetime import datetime, timezone
import config


# ── Ollama interface ──────────────────────────────────────────────────────────

def ask(prompt):
    """Send a prompt to Ollama, return the response text."""
    payload = {
        "model": config.CHAT_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_ctx": config.CONTEXT_WINDOW,
        }
    }
    resp = requests.post(
        f"{config.OLLAMA_BASE}/api/generate",
        json=payload,
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()["response"].strip()


# ── Prompt templates ──────────────────────────────────────────────────────────

CHANNEL_PROMPT = """\
You are analyzing Slack messages from the #{channel} channel.
Below are the messages in chronological order.

{messages}

Write a structured analysis with EXACTLY these sections (use ## headings):

## Historical Context
Summarise the history and background visible in these messages.

## Key Points
List the most important topics, decisions, or outcomes.

## Future Plans
List any upcoming work, goals, or commitments mentioned.

## Identified Gaps
List problems, blockers, unanswered questions, or missing information.

## Recommendations
Concrete, actionable recommendations based on what you read.

Be concise. Use bullet points where appropriate.
"""

MERGE_PROMPT = """\
You are synthesising multiple partial analyses of the #{channel} Slack channel.
Each partial analysis covers a different time window. Combine them into ONE
coherent final analysis.

{partials}

Write the final synthesis with EXACTLY these sections (use ## headings):

## Historical Context
## Key Points
## Future Plans
## Identified Gaps
## Recommendations
"""

MASTER_PROMPT = """\
You are an executive analyst. Below are individual analyses for each Slack channel
in this organisation.

{channel_analyses}

Write a cross-channel executive summary with EXACTLY these sections (use ## headings):

## Cross-Channel Themes
Common themes, patterns, and topics that appear across multiple channels.

## Team Dependencies & Handoffs
Where teams depend on each other; handoffs that appear unclear or risky.

## Organization-Wide Gaps & Risks
Gaps in communication, knowledge, or execution visible across channels.

## Conflicting Priorities
Where channels reveal competing goals, conflicting timelines, or unclear ownership.

## Leadership Recommendations
Concrete, prioritised recommendations for leadership based on the full picture.
"""


# ── Chunking ──────────────────────────────────────────────────────────────────

def chunk_text(text, max_chars):
    """Split text into chunks no larger than max_chars, breaking on newlines."""
    chunks = []
    current = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > max_chars and current:
            chunks.append("".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


# ── Per-channel analysis ──────────────────────────────────────────────────────

def format_messages(rows):
    """Format DB message rows into readable text, with thread replies indented."""
    lines = []
    for row in rows:
        date = row["date"] or "unknown"
        user = row["user_name"] or row["user_id"] or "unknown"
        text = (row["text"] or "").strip()
        if not text:
            continue
        prefix = "  ↳" if row["is_reply"] else f"[{date}]"
        lines.append(f"{prefix} {user}: {text}")
    return "\n".join(lines)


def analyze_channel(channel_name, formatted_text):
    """Analyze one channel's messages. Handles chunking for large channels."""
    chunks = chunk_text(formatted_text, config.CONTEXT_WINDOW)
    print(f"  [{channel_name}] {len(chunks)} chunk(s) to analyze")

    if len(chunks) == 1:
        prompt = CHANNEL_PROMPT.format(channel=channel_name, messages=chunks[0])
        return ask(prompt)

    # Multi-chunk: analyze each, then synthesize
    partials = []
    for i, chunk in enumerate(chunks, 1):
        print(f"  [{channel_name}] chunk {i}/{len(chunks)} …")
        prompt = CHANNEL_PROMPT.format(channel=channel_name, messages=chunk)
        partials.append(f"=== Part {i} ===\n{ask(prompt)}")

    print(f"  [{channel_name}] synthesising …")
    merge_prompt = MERGE_PROMPT.format(
        channel=channel_name,
        partials="\n\n".join(partials)
    )
    return ask(merge_prompt)


# ── Master report ─────────────────────────────────────────────────────────────

def build_master(channel_analyses):
    """Build cross-channel executive summary from dict {channel_name: analysis}."""
    sections = []
    for name, analysis in channel_analyses.items():
        sections.append(f"=== #{name} ===\n{analysis}")
    combined = "\n\n".join(sections)
    prompt = MASTER_PROMPT.format(channel_analyses=combined)
    print("  Building master report …")
    return ask(prompt)


# ── Entry point (reads from SQLite) ──────────────────────────────────────────

def analyze_all():
    """
    Analyze all channels that have new messages since last analysis.
    Returns dict {channel_name: analysis_string}.
    """
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row

    # Get message counts per channel
    counts = conn.execute("""
        SELECT channel_name, channel_id, COUNT(*) as cnt
        FROM messages GROUP BY channel_id
    """).fetchall()

    results = {}
    for row in counts:
        ch_name = row["channel_name"]
        ch_id = row["channel_id"]
        msg_count = row["cnt"]

        if msg_count < config.MIN_MESSAGES:
            print(f"  [{ch_name}] skipping — only {msg_count} messages")
            continue

        # Check if re-analysis needed
        state = conn.execute(
            "SELECT message_count_at_analysis FROM analysis_state WHERE channel_id=?",
            (ch_id,)
        ).fetchone()

        if state and state["message_count_at_analysis"] == msg_count:
            print(f"  [{ch_name}] no new messages, skipping analysis")
            continue

        print(f"\n→ Analyzing #{ch_name} ({msg_count} messages) …")
        rows = conn.execute("""
            SELECT * FROM messages
            WHERE channel_id=?
            ORDER BY ts ASC
        """, (ch_id,)).fetchall()

        formatted = format_messages(rows)
        analysis = analyze_channel(ch_name, formatted)
        results[ch_name] = analysis

        # Update analysis_state
        conn.execute("""
            INSERT INTO analysis_state
                (channel_id, channel_name, message_count_at_analysis, analyzed_at, report_path)
            VALUES (?,?,?,?,?)
            ON CONFLICT(channel_id) DO UPDATE SET
                message_count_at_analysis=excluded.message_count_at_analysis,
                analyzed_at=excluded.analyzed_at,
                report_path=excluded.report_path
        """, (
            ch_id, ch_name, msg_count,
            datetime.now(tz=timezone.utc).isoformat(),
            f"{config.OUTPUT_DIR}/{ch_name}.md",
        ))
        conn.commit()

    conn.close()
    return results


if __name__ == "__main__":
    results = analyze_all()
    for ch, analysis in results.items():
        print(f"\n{'='*60}\n#{ch}\n{'='*60}\n{analysis}\n")
```

**Step 2: Test Ollama connectivity without running full analysis**

```bash
python3 -c "
import analyzer
result = analyzer.ask('Say hello in one word.')
print('Ollama ok:', result[:50])
"
```
Expected: `Ollama ok: Hello` (or similar single word)

**Step 3: Test chunking logic**

```bash
python3 -c "
import analyzer
text = 'line\n' * 1000
chunks = analyzer.chunk_text(text, 200)
print(f'{len(chunks)} chunks, first {len(chunks[0])} chars')
assert all(len(c) <= 210 for c in chunks), 'chunk too large'
print('chunk test passed')
"
```
Expected: `N chunks, first 200 chars` + `chunk test passed`

**Step 4: Commit**

```bash
git add analyzer.py
git commit -m "feat: analyzer — Ollama LLM chunking, per-channel and master analysis"
```

---

### Task 5: exporter.py

**Files:**
- Create: `exporter.py`

**Step 1: Create `exporter.py`**

```python
# exporter.py — Writes analysis results to Markdown and JSON files.
# Run standalone: python exporter.py  (writes test output to ./data/output/)

import os
import json
from datetime import datetime, timezone
import config


def _ensure_output_dir():
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)


def save_channel_report(channel_name, analysis, message_count):
    """Write per-channel report as .md and .json."""
    _ensure_output_dir()
    generated_at = datetime.now(tz=timezone.utc).isoformat()

    # Markdown
    md_path = os.path.join(config.OUTPUT_DIR, f"{channel_name}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# #{channel_name} — Slack Intelligence Report\n\n")
        f.write(f"_Generated: {generated_at}_  \n")
        f.write(f"_Messages analysed: {message_count}_\n\n---\n\n")
        f.write(analysis)
    print(f"  Wrote {md_path}")

    # JSON
    json_path = os.path.join(config.OUTPUT_DIR, f"{channel_name}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "channel": channel_name,
            "generated_at": generated_at,
            "message_count": message_count,
            "analysis": analysis,
        }, f, indent=2, ensure_ascii=False)
    print(f"  Wrote {json_path}")


def save_master_report(analysis, channel_names):
    """Write cross-channel master report as .md and .json."""
    _ensure_output_dir()
    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    generated_at = datetime.now(tz=timezone.utc).isoformat()

    md_path = os.path.join(config.OUTPUT_DIR, f"master_{today}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Slack Intelligence — Master Report ({today})\n\n")
        f.write(f"_Generated: {generated_at}_  \n")
        f.write(f"_Channels: {', '.join(f'#{c}' for c in channel_names)}_\n\n---\n\n")
        f.write(analysis)
    print(f"  Wrote {md_path}")

    json_path = os.path.join(config.OUTPUT_DIR, f"master_{today}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "channel": "master",
            "generated_at": generated_at,
            "channels": channel_names,
            "analysis": analysis,
        }, f, indent=2, ensure_ascii=False)
    print(f"  Wrote {json_path}")

    return md_path


if __name__ == "__main__":
    # Smoke test: write a dummy report
    save_channel_report("test-channel", "## Historical Context\nTest.", 42)
    save_master_report("## Cross-Channel Themes\nTest master.", ["test-channel"])
    print("exporter smoke test passed")
```

**Step 2: Run smoke test**

```bash
python3 exporter.py
```
Expected:
```
  Wrote ./data/output/test-channel.md
  Wrote ./data/output/test-channel.json
  Wrote ./data/output/master_YYYYMMDD.md
  Wrote ./data/output/master_YYYYMMDD.json
exporter smoke test passed
```

**Step 3: Verify JSON is valid**

```bash
python3 -c "
import json
with open('./data/output/test-channel.json') as f:
    d = json.load(f)
print('JSON ok:', list(d.keys()))
"
```
Expected: `JSON ok: ['channel', 'generated_at', 'message_count', 'analysis']`

**Step 4: Commit**

```bash
git add exporter.py
git commit -m "feat: exporter — markdown and JSON report writers"
```

---

### Task 6: main.py

**Files:**
- Create: `main.py`

**Step 1: Create `main.py`**

```python
# main.py — Orchestrator: fetch → analyze → export.
# Usage:
#   python main.py            # fetch last 90 days + analyze + export
#   python main.py --backfill # fetch full history (resumable)

import sys
import sqlite3
import requests
import config
import fetcher
import analyzer
import exporter


# ── Health check ──────────────────────────────────────────────────────────────

def health_check():
    """Verify Ollama is up, slack_sdk installed, ./data/ is writable."""
    errors = []

    # Ollama ping
    try:
        resp = requests.get(f"{config.OLLAMA_BASE}/api/tags", timeout=5)
        models = [m["name"] for m in resp.json().get("models", [])]
        match = any(config.CHAT_MODEL in m for m in models)
        if match:
            print(f"  ✓ Ollama running — {config.CHAT_MODEL} available")
        else:
            print(f"  ✓ Ollama running — WARNING: {config.CHAT_MODEL} not in {models}")
    except Exception as e:
        errors.append(f"Ollama not reachable at {config.OLLAMA_BASE}: {e}")

    # slack_sdk
    try:
        import slack_sdk
        print(f"  ✓ slack_sdk {slack_sdk.__version__} installed")
    except ImportError:
        errors.append("slack_sdk not installed — run: pip install slack_sdk")

    # ./data/ writable
    try:
        import os
        os.makedirs("./data/output", exist_ok=True)
        test_file = "./data/.write_test"
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
        print("  ✓ ./data/ is writable")
    except Exception as e:
        errors.append(f"./data/ not writable: {e}")

    # config filled in
    if not config.SLACK_TOKEN:
        errors.append("SLACK_TOKEN is empty — fill it in config.py")
    else:
        print("  ✓ SLACK_TOKEN set")

    if not config.TARGET_CHANNELS:
        errors.append("TARGET_CHANNELS is empty — fill it in config.py")
    else:
        print(f"  ✓ TARGET_CHANNELS: {config.TARGET_CHANNELS}")

    if errors:
        print("\n❌ Health check failed:")
        for e in errors:
            print(f"   • {e}")
        sys.exit(1)

    print("\n✓ All health checks passed\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    backfill = "--backfill" in sys.argv

    print("=" * 60)
    print("SLACK INTELLIGENCE AGENT")
    print("=" * 60)

    # 1. Health check
    print("\n[1/4] Health check …")
    health_check()

    # 2. Fetch
    print(f"\n[2/4] Fetching Slack data {'(BACKFILL)' if backfill else '(recent)'} …")
    fetcher.run_fetch(backfill=backfill)

    # 3. Analyze
    print("\n[3/4] Analyzing channels …")
    analyses = analyzer.analyze_all()

    if not analyses:
        print("  No channels needed re-analysis. Run with new data or --backfill.")
    else:
        # 4. Export per-channel
        print(f"\n[4/4] Exporting {len(analyses)} channel report(s) + master …")
        conn = sqlite3.connect(config.DB_PATH)
        for ch_name, analysis in analyses.items():
            count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE channel_name=?", (ch_name,)
            ).fetchone()[0]
            exporter.save_channel_report(ch_name, analysis, count)

        # Master report
        master = analyzer.build_master(analyses)
        exporter.save_master_report(master, list(analyses.keys()))
        conn.close()

    print("\n✓ Done. Run 'python dashboard.py' to view reports.")


if __name__ == "__main__":
    main()
```

**Step 2: Verify imports and health check (token not needed for this)**

```bash
python3 -c "import main; print('main imports ok')"
```
Expected: `main imports ok`

**Step 3: Commit**

```bash
git add main.py
git commit -m "feat: main — orchestrator with health check, fetch/analyze/export pipeline"
```

---

### Task 7: query.py

**Files:**
- Create: `query.py`

**Step 1: Create `query.py`**

```python
# query.py — Interactive local Q&A over Slack messages.
# Usage: python query.py
# Syntax: "your question"          → search all channels
#         "#channelname question"  → search specific channel

import sqlite3
import re
import config
import analyzer   # reuse ask()


HELP_TEXT = """
Slack Intelligence — Interactive Q&A
─────────────────────────────────────
Type a question to search all channels.
Prefix with #channel to scope: "#engineering what deployment issues came up?"
Type 'exit' to quit.
"""

RAG_PROMPT = """\
You are a Slack message analyst. Answer the user's question using ONLY the
messages provided below. If the answer is not in the messages, say so.

Question: {question}

Messages:
{context}

Answer:"""


def search_messages(conn, query, channel_name=None, limit=30):
    """Keyword search across messages. Returns list of Row objects."""
    words = re.findall(r'\w+', query.lower())
    if not words:
        return []

    # Build OR-based LIKE filter
    conditions = " OR ".join(["LOWER(text) LIKE ?" for _ in words])
    params = [f"%{w}%" for w in words]

    if channel_name:
        sql = f"""
            SELECT date, user_name, text, channel_name, is_reply
            FROM messages
            WHERE channel_name=? AND ({conditions})
            ORDER BY ts DESC LIMIT {limit}
        """
        params = [channel_name] + params
    else:
        sql = f"""
            SELECT date, user_name, text, channel_name, is_reply
            FROM messages
            WHERE {conditions}
            ORDER BY ts DESC LIMIT {limit}
        """

    conn.row_factory = sqlite3.Row
    return conn.execute(sql, params).fetchall()


def format_context(rows):
    """Format search results as readable context for LLM."""
    lines = []
    for r in rows:
        prefix = "  ↳" if r["is_reply"] else f"[{r['date']}] #{r['channel_name']}"
        lines.append(f"{prefix} {r['user_name']}: {r['text']}")
    return "\n".join(lines)


def parse_input(user_input):
    """Parse '#channel question' or plain question."""
    m = re.match(r"^#(\S+)\s+(.*)", user_input.strip())
    if m:
        return m.group(2).strip(), m.group(1)
    return user_input.strip(), None


def run_query():
    conn = sqlite3.connect(config.DB_PATH)
    print(HELP_TEXT)

    while True:
        try:
            user_input = input("Q> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user_input:
            continue
        if user_input.lower() == "exit":
            print("Bye.")
            break

        question, channel = parse_input(user_input)
        rows = search_messages(conn, question, channel_name=channel)

        if not rows:
            print("  No matching messages found.\n")
            continue

        print(f"  Found {len(rows)} messages", end="")
        if channel:
            print(f" in #{channel}", end="")
        print(". Asking Ollama …\n")

        context = format_context(rows)
        prompt = RAG_PROMPT.format(question=question, context=context)

        try:
            answer = analyzer.ask(prompt)
            print(f"A> {answer}\n")
        except Exception as e:
            print(f"  Error calling Ollama: {e}\n")

    conn.close()


if __name__ == "__main__":
    run_query()
```

**Step 2: Test keyword search logic in isolation**

```bash
python3 -c "
import sqlite3, query
conn = sqlite3.connect('./data/slack_intel.db')
# Insert a test message
conn.execute('''INSERT OR IGNORE INTO messages
    (id, channel_id, channel_name, ts, date, user_id, user_name, text, is_reply)
    VALUES (?,?,?,?,?,?,?,?,?)''',
    ('test_001', 'C001', 'general', 1000000, '2024-01-01',
     'U001', 'alice', 'we need to fix the deployment pipeline', 0))
conn.commit()
rows = query.search_messages(conn, 'deployment pipeline')
print(f'Found {len(rows)} rows')
assert len(rows) >= 1
print('search test passed')
conn.close()
"
```
Expected: `Found 1 rows` + `search test passed`

**Step 3: Test input parsing**

```bash
python3 -c "
import query
q, ch = query.parse_input('#engineering what broke in prod?')
assert ch == 'engineering'
assert 'broke' in q
q2, ch2 = query.parse_input('what is the roadmap?')
assert ch2 is None
assert 'roadmap' in q2
print('parse test passed')
"
```
Expected: `parse test passed`

**Step 4: Commit**

```bash
git add query.py
git commit -m "feat: query — hybrid keyword search + Ollama RAG interactive CLI"
```

---

### Task 8: dashboard.py

**Files:**
- Create: `dashboard.py`

**Step 1: Create `dashboard.py`**

```python
# dashboard.py — Local web dashboard for Slack Intelligence reports.
# Usage: python dashboard.py
# Opens: http://localhost:8080

import http.server
import json
import os
import re
import sqlite3
import glob
from datetime import datetime, timezone
import config


# ── Markdown → HTML renderer ──────────────────────────────────────────────────

def md_to_html(text):
    """Minimal markdown → HTML: headings, bold, lists, code blocks."""
    html = []
    in_ul = False
    in_code = False

    for line in text.splitlines():
        # Code blocks
        if line.strip().startswith("```"):
            if in_code:
                html.append("</code></pre>")
                in_code = False
            else:
                if in_ul:
                    html.append("</ul>")
                    in_ul = False
                html.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            html.append(line.replace("&", "&amp;").replace("<", "&lt;"))
            continue

        # Headings
        m = re.match(r'^(#{1,4})\s+(.*)', line)
        if m:
            if in_ul:
                html.append("</ul>")
                in_ul = False
            level = len(m.group(1))
            html.append(f"<h{level}>{m.group(2)}</h{level}>")
            continue

        # List items
        m = re.match(r'^[-*•]\s+(.*)', line)
        if m:
            if not in_ul:
                html.append("<ul>")
                in_ul = True
            item = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', m.group(1))
            html.append(f"<li>{item}</li>")
            continue

        # Close list
        if in_ul and line.strip() == "":
            html.append("</ul>")
            in_ul = False

        # Horizontal rule
        if re.match(r'^---+$', line.strip()):
            html.append("<hr>")
            continue

        # Bold inline
        line = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', line)
        line = re.sub(r'_(.+?)_', r'<em>\1</em>', line)

        if line.strip():
            html.append(f"<p>{line}</p>")
        else:
            html.append("<br>")

    if in_ul:
        html.append("</ul>")
    return "\n".join(html)


# ── HTML page templates ───────────────────────────────────────────────────────

CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       margin: 0; display: flex; height: 100vh; }
nav { width: 220px; min-width: 220px; background: #1a1d21; color: #d1d2d3;
      overflow-y: auto; padding: 16px 0; }
nav h2 { padding: 0 16px; font-size: 14px; color: #868686;
          text-transform: uppercase; letter-spacing: .08em; margin: 0 0 8px; }
nav a { display: block; padding: 6px 16px; color: #d1d2d3; text-decoration: none;
         font-size: 14px; border-radius: 4px; margin: 1px 8px; }
nav a:hover, nav a.active { background: #1164a3; color: #fff; }
nav .section { margin-top: 16px; }
main { flex: 1; overflow-y: auto; padding: 32px 40px; background: #fff; }
main h1 { color: #1a1d21; border-bottom: 2px solid #e0e0e0; padding-bottom: 8px; }
main h2 { color: #1164a3; margin-top: 28px; }
main h3 { color: #444; }
main ul { line-height: 1.8; }
main code, main pre { background: #f4f4f4; border-radius: 4px; }
main pre { padding: 12px; overflow-x: auto; }
main code { padding: 2px 5px; }
.meta { color: #888; font-size: 13px; margin-bottom: 24px; }
.status-bar { background: #f4f4f4; border-bottom: 1px solid #ddd;
               padding: 8px 16px; font-size: 12px; color: #555; }
"""

def page(title, body, channels, active=""):
    nav_links = '<div class="section"><h2>Channels</h2>'
    for ch in channels:
        cls = ' class="active"' if ch == active else ''
        nav_links += f'<a href="/channel/{ch}"{cls}>#{ch}</a>'
    nav_links += '</div>'
    nav_links += '<div class="section"><h2>Reports</h2>'
    nav_links += f'<a href="/master"{" class=\"active\"" if active == "__master__" else ""}>Master Report</a>'
    nav_links += '</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{title} — Slack Intel</title>
<style>{CSS}</style></head>
<body>
<nav>
  <h2 style="color:#fff;font-size:16px;padding:0 16px 12px">Slack Intel</h2>
  {nav_links}
</nav>
<main>
{body}
</main>
</body></html>"""


def index_body(channels, conn):
    rows = conn.execute("""
        SELECT channel_name, message_count_at_analysis, analyzed_at
        FROM analysis_state ORDER BY analyzed_at DESC
    """).fetchall()

    table = "<table style='border-collapse:collapse;width:100%'>"
    table += "<tr style='background:#f4f4f4'><th style='padding:8px;text-align:left'>Channel</th><th>Messages</th><th>Last Analyzed</th></tr>"
    for r in rows:
        table += (f"<tr style='border-top:1px solid #eee'>"
                  f"<td style='padding:8px'><a href='/channel/{r[0]}'>#{r[0]}</a></td>"
                  f"<td style='padding:8px;text-align:center'>{r[1]}</td>"
                  f"<td style='padding:8px;color:#888'>{r[2][:16] if r[2] else ''}</td></tr>")
    table += "</table>"

    total_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return f"<h1>Slack Intelligence Dashboard</h1><p class='meta'>Total messages: {total_msgs:,} | Channels analyzed: {len(rows)}</p>{table}"


# ── Request handler ───────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # silence default access log

    def send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def get_channels(self, conn):
        rows = conn.execute(
            "SELECT DISTINCT channel_name FROM analysis_state ORDER BY channel_name"
        ).fetchall()
        return [r[0] for r in rows]

    def do_GET(self):
        conn = sqlite3.connect(config.DB_PATH)
        path = self.path.split("?")[0]

        try:
            channels = self.get_channels(conn)

            if path == "/" or path == "":
                body = index_body(channels, conn)
                html = page("Dashboard", body, channels)
                self.send_html(html)

            elif path.startswith("/channel/"):
                ch = path[len("/channel/"):]
                md_path = os.path.join(config.OUTPUT_DIR, f"{ch}.md")
                if not os.path.exists(md_path):
                    self.send_html(page(ch, "<h2>Report not found</h2>", channels), 404)
                    return
                with open(md_path, encoding="utf-8") as f:
                    content = f.read()
                body = md_to_html(content)
                self.send_html(page(f"#{ch}", body, channels, active=ch))

            elif path == "/master":
                # Find latest master report
                masters = sorted(glob.glob(os.path.join(config.OUTPUT_DIR, "master_*.md")))
                if not masters:
                    self.send_html(page("Master", "<h2>No master report yet. Run main.py first.</h2>", channels))
                    return
                with open(masters[-1], encoding="utf-8") as f:
                    content = f.read()
                body = md_to_html(content)
                self.send_html(page("Master Report", body, channels, active="__master__"))

            elif path == "/api/status":
                total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                analyzed = conn.execute("SELECT COUNT(*) FROM analysis_state").fetchone()[0]
                last_run = conn.execute(
                    "SELECT MAX(analyzed_at) FROM analysis_state"
                ).fetchone()[0]
                self.send_json({
                    "total_messages": total,
                    "channels_analyzed": analyzed,
                    "last_run": last_run,
                })

            else:
                self.send_html("<h2>Not found</h2>", 404)

        finally:
            conn.close()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    addr = ("", config.DASHBOARD_PORT)
    server = http.server.HTTPServer(addr, Handler)
    print(f"Slack Intelligence Dashboard running at http://localhost:{config.DASHBOARD_PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
```

**Step 2: Test markdown renderer**

```bash
python3 -c "
import dashboard
html = dashboard.md_to_html('## Hello\n- item one\n- item two\n**bold**')
assert '<h2>' in html
assert '<ul>' in html
assert '<strong>' in html
print('md_to_html test passed')
"
```
Expected: `md_to_html test passed`

**Step 3: Test server starts**

```bash
python3 -c "
import threading, time, urllib.request, dashboard, http.server
server = http.server.HTTPServer(('', 18080), dashboard.Handler)
t = threading.Thread(target=server.serve_forever)
t.daemon = True
t.start()
time.sleep(0.5)
resp = urllib.request.urlopen('http://localhost:18080/api/status')
data = resp.read()
print('server ok, status bytes:', len(data))
server.shutdown()
"
```
Expected: `server ok, status bytes: N` (some positive number)

**Step 4: Commit**

```bash
git add dashboard.py
git commit -m "feat: dashboard — local http.server web UI with channel nav and master report"
```

---

### Task 9: Final Integration Verification

**Step 1: Confirm all files exist**

```bash
ls -1 *.py
```
Expected:
```
analyzer.py
config.py
dashboard.py
exporter.py
fetcher.py
main.py
query.py
```

**Step 2: Verify all modules import cleanly**

```bash
python3 -c "
import config, fetcher, analyzer, exporter, main, query, dashboard
print('All modules import ok')
"
```
Expected: `All modules import ok`

**Step 3: Verify Ollama health check passes**

```bash
python3 -c "import main; main.health_check()"
```
Expected: All checks pass (except token/channel — those you fill in)

**Step 4: Final commit**

```bash
git add .
git commit -m "chore: verify all modules import and integrate cleanly"
```

---

## Usage After Build

```bash
# 1. Fill in config.py:
#    SLACK_TOKEN = "xoxb-..."
#    TARGET_CHANNELS = ["general", "engineering", ...]

# 2. First run (last 90 days):
python main.py

# 3. Full history backfill (resumable):
python main.py --backfill

# 4. View dashboard:
python dashboard.py
# open http://localhost:8080

# 5. Interactive Q&A:
python query.py
```
