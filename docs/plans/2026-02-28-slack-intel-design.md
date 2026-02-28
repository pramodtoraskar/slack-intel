# Slack Intelligence Agent — Design Document
**Date:** 2026-02-28
**Status:** Approved

---

## Overview

A fully local, multi-agent pipeline that fetches Slack channel history, analyzes
conversations with a local Ollama LLM, produces per-channel and cross-channel
reports, and enables interactive Q&A — all data stays on the machine.

---

## Confirmed Decisions

| Decision | Choice |
|----------|--------|
| `query.py` search mode | Hybrid: keyword retrieval → Ollama RAG answer |
| Initial fetch scope | Two-phase: 90 days default, `--backfill` flag for full history |
| Bot messages | Skip all (`bot_message` subtype) |
| Channel targeting | Manual list in `config.py` |
| Re-runs | Incremental: fetch new messages only, re-analyze changed channels only |
| Incremental state tracking | Approach A: SQLite `analysis_state` table |
| Cross-channel report UI | Local web dashboard (`dashboard.py`, `http.server`, port 8080) |

---

## Architecture & Data Flow

```
main.py (orchestrator)
    │
    ├─► fetcher.py
    │       • WebClient → channels.history + conversations.replies
    │       • Normal run: only fetches messages newer than last_ts
    │       • --backfill flag: fetches from oldest cursor backward
    │       • Resolves user_id → name (cached in memory per run)
    │       • Writes to SQLite: messages, channels, fetch_state
    │
    ├─► analyzer.py
    │       • Reads from SQLite only (no re-fetch)
    │       • Checks analysis_state: skip if message_count unchanged
    │       • Chunks text → POST /api/generate → merge if multi-chunk
    │       • Returns analysis string per channel
    │
    ├─► exporter.py
    │       • Writes .md + .json per channel to ./data/output/
    │       • Writes master_{YYYYMMDD}.md + .json
    │
    └─► query.py (standalone CLI, reads SQLite directly)
            • Keyword search → top 30 messages → Ollama RAG answer
            • "#channel question" scopes to one channel

dashboard.py (standalone server)
    • python dashboard.py → localhost:8080
    • Reads SQLite + ./data/output/ files
    • No extra dependencies (stdlib http.server only)
```

---

## Project Structure

```
slack-intel/
├── config.py       — all settings and constants
├── fetcher.py      — Slack API → SQLite
├── analyzer.py     — Ollama LLM analysis
├── exporter.py     — Markdown + JSON output
├── main.py         — orchestrator (fetch → analyze → export)
├── query.py        — interactive CLI Q&A
├── dashboard.py    — local web server at localhost:8080
└── data/
    ├── slack_intel.db
    └── output/
        ├── {channel_name}.md
        ├── {channel_name}.json
        ├── master_{YYYYMMDD}.md
        └── master_{YYYYMMDD}.json
```

---

## Data Model (SQLite)

```sql
CREATE TABLE channels (
    id TEXT PRIMARY KEY,
    name TEXT,
    is_private INTEGER,
    fetched_at TEXT
);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,          -- channel_id + ts
    channel_id TEXT,
    channel_name TEXT,
    ts REAL,
    date TEXT,                    -- YYYY-MM-DD
    user_id TEXT,
    user_name TEXT,
    text TEXT,
    thread_ts TEXT,
    is_reply INTEGER,
    raw_json TEXT
);

CREATE TABLE fetch_state (
    channel_id TEXT PRIMARY KEY,
    last_cursor TEXT,             -- Slack pagination cursor
    last_ts REAL,                 -- newest ts fetched (incremental)
    oldest_ts REAL,               -- oldest ts fetched (backfill)
    is_complete INTEGER,          -- 1 = full history done
    is_recent_complete INTEGER,   -- 1 = 90-day window done
    message_count INTEGER,
    updated_at TEXT
);

CREATE TABLE analysis_state (
    channel_id TEXT PRIMARY KEY,
    channel_name TEXT,
    message_count_at_analysis INTEGER,
    analyzed_at TEXT,
    report_path TEXT
);
```

---

## Module Interfaces

### config.py
Pure constants, no logic.
```python
SLACK_TOKEN = ""
TARGET_CHANNELS = []
OLLAMA_BASE = "http://localhost:11434"
CHAT_MODEL = "llama3"
CONTEXT_WINDOW = 6000
RATE_LIMIT_DELAY = 1.2
DAYS_BACK_DEFAULT = 90
DB_PATH = "./data/slack_intel.db"
OUTPUT_DIR = "./data/output"
DASHBOARD_PORT = 8080
```

### fetcher.py
Entry point: `run_fetch(backfill=False)`
- Normal mode: fetches messages newer than `last_ts` (or last 90 days on first run)
- Backfill mode: pages backward from `oldest_ts` already stored, resumable
- Checkpoints every page to `fetch_state`
- Fetches thread replies for any message with `reply_count > 0`
- Skips subtypes: `channel_join`, `channel_leave`, `bot_message`
- Rate limiting: 1.2s between calls, auto-retry on 429 with `Retry-After`

### analyzer.py
Entry point: `analyze_all()` → `dict[channel_name, analysis_str]`
- Per channel: compare `messages COUNT(*)` vs `analysis_state.message_count_at_analysis`
- If unchanged: skip. If greater: re-analyze.
- Chunking: split formatted text at CONTEXT_WINDOW character boundary
- Single chunk → one `ask()` call
- Multi-chunk → analyze each chunk separately → synthesize with merge prompt
- Uses HTTP POST to `/api/generate` (not ollama Python library)
- `ask(prompt)`: temperature=0.2, num_ctx from config

**Per-channel prompt sections:**
```
## Historical Context
## Key Points
## Future Plans
## Identified Gaps
## Recommendations
```

**Master report prompt sections:**
```
## Cross-Channel Themes
## Team Dependencies & Handoffs
## Organization-Wide Gaps & Risks
## Conflicting Priorities
## Leadership Recommendations
```

### exporter.py
Stateless writers:
- `save_channel_report(channel_name, analysis, message_count)` → `.md` + `.json`
- `save_master_report(analysis, channel_names)` → `master_{YYYYMMDD}.md` + `.json`
- JSON format: `{ channel, generated_at, analysis, message_count }`

### main.py
Parses `--backfill` CLI flag.
1. `health_check()` — Ollama ping, slack_sdk import check, ./data/ writable
2. `run_fetch(backfill)` — fetcher.py
3. `analyze_all()` — analyzer.py (reads SQLite, skips unchanged channels)
4. Save reports — exporter.py
5. Print clear progress at each step

### query.py
Standalone interactive CLI:
```
loop:
  input → parse "#channel query" or plain query
  → SQLite LIKE search on text column
  → top 30 messages as context (ordered by relevance score)
  → POST /api/generate with RAG prompt ("answer only from these messages")
  → print answer → repeat until "exit"
```

### dashboard.py
Standalone server: `python dashboard.py` → `localhost:8080`
- `GET /` — sidebar of all analyzed channels + link to master report
- `GET /channel/{name}` — renders channel `.md` report as HTML
- `GET /master` — renders latest `master_{YYYYMMDD}.md` as HTML
- `GET /api/status` — JSON: channels analyzed, message counts, last run time
- Markdown → HTML: minimal regex renderer (headings, bold, lists, code blocks)
- No extra dependencies — stdlib `http.server` only

---

## Error Handling

- **Rate limiting (429):** Read `Retry-After` header, sleep, retry automatically
- **Ollama unavailable:** health_check() fails fast with clear message before any work
- **Interrupted fetch:** fetch_state checkpoint allows resume from last cursor
- **Interrupted analysis:** analysis_state not updated until full analysis completes
- **Channel < 10 messages:** skip analysis, log skip reason
- **Slack token missing:** config validation at startup, fail fast with instructions

---

## Dependencies

```
pip install slack_sdk requests
# stdlib only beyond that: sqlite3, http.server, json, datetime, re, argparse
```

---

## Run Order

```bash
python main.py              # fetch (90 days) + analyze + export
python main.py --backfill   # fetch full history (resumable)
python dashboard.py         # open localhost:8080 in browser
python query.py             # interactive Q&A
```
