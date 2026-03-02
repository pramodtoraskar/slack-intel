# Topic Intelligence System — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add cross-channel topic digests — `/digest Topic X` in query.py, auto-digest via main.py step 5, and a Topics section in the dashboard; synthesized notes persist in SQLite for cumulative LLM intelligence.

**Architecture:** A new `topic_notes` SQLite table stores briefings per topic over time. `analyzer.build_topic_digest()` does the synthesis (injecting prior notes for persistent mode). `query.py` exposes `/digest` and `/digest --fresh` commands. `main.py` auto-runs digests for `WATCHED_TOPICS`. `dashboard.py` gains a Topics sidebar section and two new routes.

**Tech Stack:** Python stdlib, sqlite3, requests (Ollama), http.server — no new dependencies.

---

### Task 1: Set up pytest and test fixtures

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

**Step 1: Install pytest**

```bash
pip install pytest
```

Expected: `Successfully installed pytest-...`

**Step 2: Create `tests/__init__.py`**

```python
```
(empty file)

**Step 3: Create `tests/conftest.py`**

```python
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
```

**Step 4: Verify pytest collects (zero tests is fine)**

```bash
pytest tests/ -v
```

Expected: `no tests ran` or `0 passed` — no errors.

**Step 5: Commit**

```bash
git add tests/
git commit -m "test: bootstrap pytest fixtures"
```

---

### Task 2: Add `topic_notes` table migration to `analyzer.py`

**Files:**
- Modify: `analyzer.py` (add `ensure_topic_notes_table` function after line 8, the imports block)
- Create: `tests/test_analyzer_topic_notes.py`

**Step 1: Write the failing test**

```python
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
```

**Step 2: Run to confirm failure**

```bash
pytest tests/test_analyzer_topic_notes.py -v
```

Expected: `AttributeError: module 'analyzer' has no attribute 'ensure_topic_notes_table'`

**Step 3: Add `ensure_topic_notes_table` to `analyzer.py`**

Add this function after the imports block (after `import config` on line 8), before the `ask()` function:

```python
def ensure_topic_notes_table(conn):
    """Create topic_notes table if it doesn't exist. Safe to call multiple times."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS topic_notes (
            topic      TEXT,
            created_at TEXT,
            key_notes  TEXT,
            PRIMARY KEY (topic, created_at)
        )
    """)
    conn.commit()
```

**Step 4: Run tests to confirm pass**

```bash
pytest tests/test_analyzer_topic_notes.py -v
```

Expected: `3 passed`

**Step 5: Commit**

```bash
git add analyzer.py tests/test_analyzer_topic_notes.py
git commit -m "feat: topic_notes table migration in analyzer"
```

---

### Task 3: Add `WATCHED_TOPICS` to `config.py`

**Files:**
- Modify: `config.py`

**Step 1: Add one line to `config.py`** after the `DASHBOARD_PORT` line:

```python
WATCHED_TOPICS = []   # e.g. ["API redesign", "auth service", "Q2 roadmap"]
```

**Step 2: Verify import still works**

```bash
python -c "import config; print(config.WATCHED_TOPICS)"
```

Expected: `[]`

**Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add WATCHED_TOPICS to config"
```

---

### Task 4: Add `build_topic_digest()` to `analyzer.py`

**Files:**
- Modify: `analyzer.py` (add prompt constant + function before `analyze_all`)
- Create: `tests/test_build_topic_digest.py`

**Step 1: Write the failing tests**

```python
# tests/test_build_topic_digest.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import analyzer


def test_topic_prompt_includes_topic():
    prompt = analyzer._topic_prompt("auth service", "msg1\nmsg2", prior_notes=None)
    assert "auth service" in prompt


def test_topic_prompt_no_prior_notes():
    prompt = analyzer._topic_prompt("auth service", "msg1\nmsg2", prior_notes=None)
    assert "Prior Intelligence" not in prompt


def test_topic_prompt_with_prior_notes():
    prompt = analyzer._topic_prompt(
        "auth service", "msg1\nmsg2",
        prior_notes="old notes here"
    )
    assert "Prior Intelligence" in prompt
    assert "old notes here" in prompt


def test_topic_prompt_includes_messages():
    prompt = analyzer._topic_prompt("X", "alpha\nbeta", prior_notes=None)
    assert "alpha" in prompt
    assert "beta" in prompt


def test_slugify():
    assert analyzer._slugify("API Redesign!") == "api-redesign"
    assert analyzer._slugify("auth service") == "auth-service"
    assert analyzer._slugify("Q2  roadmap") == "q2-roadmap"
```

**Step 2: Run to confirm failure**

```bash
pytest tests/test_build_topic_digest.py -v
```

Expected: `AttributeError: module 'analyzer' has no attribute '_topic_prompt'`

**Step 3: Add the prompt template, `_slugify`, `_topic_prompt`, and `build_topic_digest` to `analyzer.py`**

Add these immediately before the `analyze_all` function (after the `build_master` function):

```python
# ── Topic digest ──────────────────────────────────────────────────────────────

TOPIC_PROMPT = """\
You are analyzing Slack conversations to build a topic intelligence briefing.

Topic: {topic}
Channels searched: {channels}
Message count: {message_count}
{prior_section}
Messages:
{messages}

Produce a briefing with EXACTLY these sections (use ## headings):

## Key Updates
The most important recent developments related to this topic.

## Decisions Made
Any decisions, agreements, or conclusions reached.

## Open Questions
Unresolved questions, blockers, or items awaiting action.

## Next Steps
Upcoming actions, commitments, or plans mentioned.

## Sources
List the channels that contributed messages to this briefing.

Be concise. Use bullet points where appropriate.
"""


def _slugify(text):
    """Convert topic name to a safe filename slug.

    e.g. "API Redesign!" → "api-redesign"
    """
    import re as _re
    slug = text.lower().strip()
    slug = _re.sub(r'[^\w\s-]', '', slug)
    slug = _re.sub(r'[\s_]+', '-', slug)
    slug = _re.sub(r'-+', '-', slug)
    return slug.strip('-')


def _topic_prompt(topic, formatted_messages, prior_notes, channels="all channels",
                  message_count=0):
    """Build the topic digest prompt.

    Injects prior_notes as a 'Prior Intelligence' section when provided.
    """
    prior_section = ""
    if prior_notes:
        prior_section = f"\n== Prior Intelligence ==\n{prior_notes}\n"

    return TOPIC_PROMPT.format(
        topic=topic,
        channels=channels,
        message_count=message_count,
        prior_section=prior_section,
        messages=formatted_messages,
    )


def build_topic_digest(topic, messages, prior_notes=None):
    """Synthesize a topic intelligence briefing from a list of message rows.

    Args:
        topic:       Topic keyword string (used in prompt + filename slug).
        messages:    List of sqlite3.Row objects with (date, user_name, text,
                     channel_name, is_reply) columns.
        prior_notes: Prior briefing text to inject as context (persistent mode).
                     Pass None for fresh/stateless mode.

    Returns:
        Tuple of (briefing_str, sources_list) where sources_list is the
        distinct channel names that contributed messages.
    """
    if not messages:
        return None, []

    sources = sorted({r["channel_name"] for r in messages if r["channel_name"]})
    formatted = format_messages(messages)
    channels_str = ", ".join(f"#{s}" for s in sources) if sources else "all channels"

    # Chunk if needed
    chunks = chunk_text(formatted, config.CONTEXT_WINDOW)
    print(f"  [topic:{topic}] {len(messages)} messages, {len(chunks)} chunk(s), "
          f"sources: {channels_str}")

    if len(chunks) == 1:
        prompt = _topic_prompt(topic, chunks[0], prior_notes,
                               channels=channels_str,
                               message_count=len(messages))
        return ask(prompt), sources

    # Multi-chunk: analyze each chunk then merge
    partials = []
    for i, chunk in enumerate(chunks, 1):
        print(f"  [topic:{topic}] chunk {i}/{len(chunks)} …")
        p = _topic_prompt(topic, chunk, prior_notes=None,
                          channels=channels_str, message_count=len(messages))
        partials.append(f"=== Part {i} ===\n{ask(p)}")

    print(f"  [topic:{topic}] synthesising {len(chunks)} chunks …")
    merge = (
        f"Synthesise these partial topic briefings for '{topic}' into ONE coherent "
        f"briefing with sections: "
        f"## Key Updates, ## Decisions Made, ## Open Questions, "
        f"## Next Steps, ## Sources\n\n"
        + "\n\n".join(partials)
    )
    if prior_notes:
        merge = f"== Prior Intelligence ==\n{prior_notes}\n\n" + merge

    return ask(merge), sources
```

**Step 4: Run tests to confirm pass**

```bash
pytest tests/test_build_topic_digest.py -v
```

Expected: `5 passed`

**Step 5: Run all tests**

```bash
pytest tests/ -v
```

Expected: all green.

**Step 6: Commit**

```bash
git add analyzer.py tests/test_build_topic_digest.py
git commit -m "feat: analyzer — build_topic_digest with persistent and fresh modes"
```

---

### Task 5: Add `save_topic_report()` to `exporter.py`

**Files:**
- Modify: `exporter.py`
- Create: `tests/test_exporter_topic.py`

**Step 1: Write the failing test**

```python
# tests/test_exporter_topic.py
import os, json, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
import exporter


def test_save_topic_report_creates_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    briefing = "## Key Updates\n- thing happened\n## Sources\n- #engineering"
    md_path, json_path = exporter.save_topic_report(
        "api-redesign", "API Redesign", briefing, sources=["engineering"], message_count=5
    )
    assert os.path.exists(md_path)
    assert os.path.exists(json_path)


def test_save_topic_report_json_structure(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    exporter.save_topic_report(
        "auth-service", "auth service", "briefing text",
        sources=["eng", "general"], message_count=12
    )
    json_path = os.path.join(str(tmp_path), "topics", "auth-service.json")
    with open(json_path) as f:
        d = json.load(f)
    assert d["topic"] == "auth service"
    assert d["slug"] == "auth-service"
    assert d["message_count"] == 12
    assert "eng" in d["sources"]
    assert "briefing text" in d["key_notes"]


def test_save_topic_report_writes_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", str(tmp_path))
    exporter.save_topic_report(
        "roadmap", "roadmap", "notes", sources=[], message_count=3
    )
    snapshot_dir = os.path.join(str(tmp_path), "topics", "roadmap")
    snapshots = os.listdir(snapshot_dir)
    assert len(snapshots) == 2  # .md + .json snapshot
```

**Step 2: Run to confirm failure**

```bash
pytest tests/test_exporter_topic.py -v
```

Expected: `AttributeError: module 'exporter' has no attribute 'save_topic_report'`

**Step 3: Add `save_topic_report` to `exporter.py`** — insert before the `if __name__ == "__main__":` block:

```python
def save_topic_report(slug, topic, briefing, sources, message_count):
    """Write topic digest as latest + snapshot .md and .json.

    Latest:   OUTPUT_DIR/topics/{slug}.md  and .json  (always overwritten)
    Snapshot: OUTPUT_DIR/topics/{slug}/{timestamp}.md and .json (history)

    Args:
        slug:          URL-safe filename slug (from analyzer._slugify)
        topic:         Human-readable topic name
        briefing:      Full LLM briefing text (markdown)
        sources:       List of channel name strings that contributed
        message_count: Number of messages searched

    Returns:
        Tuple of (md_path, json_path) for the latest files.
    """
    topics_dir = os.path.join(config.OUTPUT_DIR, "topics")
    snapshot_dir = os.path.join(topics_dir, slug)
    os.makedirs(topics_dir, exist_ok=True)
    os.makedirs(snapshot_dir, exist_ok=True)

    generated_at = datetime.now(tz=timezone.utc).isoformat()
    ts_label = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M")
    sources_str = ", ".join(f"#{s}" for s in sources) if sources else "all channels"

    payload = {
        "topic": topic,
        "slug": slug,
        "generated_at": generated_at,
        "key_notes": briefing,
        "sources": sources,
        "message_count": message_count,
    }

    # Latest files (overwritten each run)
    md_path = os.path.join(topics_dir, f"{slug}.md")
    json_path = os.path.join(topics_dir, f"{slug}.json")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Topic Digest: {topic}\n\n")
        f.write(f"_Generated: {generated_at}_  \n")
        f.write(f"_Sources: {sources_str}_  \n")
        f.write(f"_Messages searched: {message_count:,}_\n\n")
        f.write("---\n\n")
        f.write(briefing)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    # Snapshot files (history)
    snap_md = os.path.join(snapshot_dir, f"{ts_label}.md")
    snap_json = os.path.join(snapshot_dir, f"{ts_label}.json")

    with open(snap_md, "w", encoding="utf-8") as f:
        f.write(f"# Topic Digest: {topic} ({ts_label})\n\n")
        f.write(f"_Generated: {generated_at}_  \n")
        f.write(f"_Sources: {sources_str}_\n\n---\n\n")
        f.write(briefing)

    with open(snap_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    print(f"  Wrote {md_path}")
    return md_path, json_path
```

**Step 4: Run tests to confirm pass**

```bash
pytest tests/test_exporter_topic.py -v
```

Expected: `3 passed`

**Step 5: Run all tests**

```bash
pytest tests/ -v
```

Expected: all green.

**Step 6: Commit**

```bash
git add exporter.py tests/test_exporter_topic.py
git commit -m "feat: exporter — save_topic_report with latest + snapshot files"
```

---

### Task 6: Add digest helpers to `query.py`

**Files:**
- Modify: `query.py`
- Create: `tests/test_query_digest.py`

**Step 1: Write the failing tests**

```python
# tests/test_query_digest.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import query


def test_parse_digest_command():
    topic, channel, mode = query.parse_digest_input("/digest API redesign")
    assert topic == "API redesign"
    assert channel is None
    assert mode == "persistent"


def test_parse_digest_fresh():
    topic, channel, mode = query.parse_digest_input("/digest --fresh auth service")
    assert topic == "auth service"
    assert channel is None
    assert mode == "fresh"


def test_parse_digest_with_channel():
    topic, channel, mode = query.parse_digest_input("#engineering /digest auth service")
    assert topic == "auth service"
    assert channel == "engineering"
    assert mode == "persistent"


def test_is_digest_command():
    assert query.is_digest_command("/digest anything") is True
    assert query.is_digest_command("/digest --fresh X") is True
    assert query.is_digest_command("#ch /digest X") is True
    assert query.is_digest_command("what is X?") is False
    assert query.is_digest_command("#ch some question") is False


def test_parse_digest_strips_whitespace():
    topic, _, _ = query.parse_digest_input("  /digest   my topic  ")
    assert topic == "my topic"
```

**Step 2: Run to confirm failure**

```bash
pytest tests/test_query_digest.py -v
```

Expected: `AttributeError: module 'query' has no attribute 'parse_digest_input'`

**Step 3: Add helpers to `query.py`** — insert after the `parse_input` function (after line 107):

```python
def is_digest_command(user_input):
    """Return True if the input is a /digest command (any form)."""
    stripped = user_input.strip()
    # Plain: /digest ...
    if re.match(r'^/digest\b', stripped):
        return True
    # Channel-scoped: #channel /digest ...
    if re.match(r'^#\S+\s+/digest\b', stripped):
        return True
    return False


def parse_digest_input(user_input):
    """Parse a /digest command into (topic, channel_or_None, mode).

    Supported forms:
      /digest Topic X                 → ("Topic X", None, "persistent")
      /digest --fresh Topic X         → ("Topic X", None, "fresh")
      #channel /digest Topic X        → ("Topic X", "channel", "persistent")
      #channel /digest --fresh Topic X → ("Topic X", "channel", "fresh")

    Returns:
        (topic_str, channel_name_or_None, mode_str)
        mode_str is "persistent" or "fresh"
    """
    s = user_input.strip()
    channel = None

    # Strip leading #channel if present
    m = re.match(r'^#(\S+)\s+(.*)', s)
    if m:
        channel = m.group(1).lower()
        s = m.group(2).strip()

    # Must start with /digest
    m = re.match(r'^/digest\s+(.*)', s)
    if not m:
        return "", channel, "persistent"

    rest = m.group(1).strip()

    # Check for --fresh flag
    mode = "persistent"
    if rest.startswith("--fresh"):
        mode = "fresh"
        rest = rest[len("--fresh"):].strip()

    return rest.strip(), channel, mode
```

**Step 4: Run tests to confirm pass**

```bash
pytest tests/test_query_digest.py -v
```

Expected: `5 passed`

**Step 5: Run all tests**

```bash
pytest tests/ -v
```

Expected: all green.

**Step 6: Commit**

```bash
git add query.py tests/test_query_digest.py
git commit -m "feat: query — digest command parsing helpers"
```

---

### Task 7: Wire `/digest` into `query.py` main loop

**Files:**
- Modify: `query.py` (update `HELP_TEXT`, add `run_digest()`, wire into `run_query` loop)

**Step 1: Update `HELP_TEXT`** — replace the existing `HELP_TEXT` constant:

```python
HELP_TEXT = """
╔══════════════════════════════════════════════════════╗
║     Slack Intelligence — Interactive Q&A             ║
╠══════════════════════════════════════════════════════╣
║  Type a question to search all channels.             ║
║  #channel question     scope to one channel.         ║
║  /digest Topic X       cross-channel topic digest.   ║
║  /digest --fresh X     digest without prior context. ║
║  #ch /digest Topic X   channel-scoped digest.        ║
║  Type 'exit' to quit.                                ║
╚══════════════════════════════════════════════════════╝
"""
```

**Step 2: Add `run_digest()` function** — add before `run_query()`:

```python
def run_digest(conn, topic, channel=None, fresh=False):
    """Execute a topic digest and save results.

    Searches for messages matching the topic keyword, synthesizes a briefing
    via Ollama, persists key notes in topic_notes, and saves reports.

    Args:
        conn:    Open sqlite3 connection (row_factory=sqlite3.Row set).
        topic:   Topic keyword string.
        channel: Optional channel name to scope the search.
        fresh:   If True, ignore prior notes (stateless mode).
    """
    import analyzer
    import exporter
    from datetime import datetime, timezone

    print(f"\n  Searching for topic: '{topic}'"
          + (f" in #{channel}" if channel else " across all channels") + " …\n")

    rows = search_messages(conn, topic, channel_name=channel, limit=50)
    if not rows:
        print(f"  No messages found matching '{topic}'.\n")
        return

    words = re.findall(r'\w+', topic.lower())
    rows = score_and_rank(rows, words)

    # Load prior notes unless fresh mode
    prior_notes = None
    if not fresh:
        analyzer.ensure_topic_notes_table(conn)
        row = conn.execute(
            "SELECT key_notes FROM topic_notes WHERE topic=? ORDER BY created_at DESC LIMIT 1",
            (topic.lower(),)
        ).fetchone()
        if row:
            prior_notes = row["key_notes"]
            print(f"  Injecting prior intelligence ({len(prior_notes):,} chars) …\n")

    print(f"  Found {len(rows)} relevant messages. Synthesizing with Ollama …\n")

    briefing, sources = analyzer.build_topic_digest(topic, rows, prior_notes=prior_notes)

    if not briefing:
        print(f"  Could not generate digest for '{topic}'.\n")
        return

    # Print to terminal
    print(f"\n{'='*60}")
    print(f"  TOPIC DIGEST: {topic.upper()}")
    print(f"{'='*60}\n")
    print(briefing)
    print()

    # Persist notes (persistent mode only)
    slug = analyzer._slugify(topic)
    if not fresh:
        analyzer.ensure_topic_notes_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO topic_notes (topic, created_at, key_notes) VALUES (?,?,?)",
            (topic.lower(), datetime.now(tz=timezone.utc).isoformat(), briefing)
        )
        conn.commit()

    # Save report files
    exporter.save_topic_report(slug, topic, briefing, sources=sources,
                               message_count=len(rows))
    print(f"\n  Report saved → data/output/topics/{slug}.md")
    print(f"  View in dashboard → http://localhost:{__import__('config').DASHBOARD_PORT}/topic/{slug}\n")
```

**Step 3: Wire into `run_query` loop** — in the `while True:` loop, add a branch for digest commands after the `exit` check (before `question, channel = parse_input(...)`). Replace the block from line ~144 to ~176 with:

```python
            # Digest command
            if is_digest_command(user_input):
                topic, ch, mode = parse_digest_input(user_input)
                if not topic:
                    print("  Usage: /digest <topic>  or  /digest --fresh <topic>\n")
                    continue
                try:
                    run_digest(conn, topic, channel=ch, fresh=(mode == "fresh"))
                except Exception as e:
                    print(f"  Error running digest: {e}\n")
                continue

            question, channel = parse_input(user_input)
```

**Step 4: Smoke-test the module imports cleanly**

```bash
python -c "import query; print('ok')"
```

Expected: `ok`

**Step 5: Run all tests**

```bash
pytest tests/ -v
```

Expected: all green.

**Step 6: Commit**

```bash
git add query.py
git commit -m "feat: query — /digest and /digest --fresh commands wired into loop"
```

---

### Task 8: Add auto-digest step 5 to `main.py`

**Files:**
- Modify: `main.py`

**Step 1: Add the auto-digest step** — after the master report block (after the `exporter.save_master_report(...)` call), add:

```python
    # Step 5: Auto-digest watched topics
    if config.WATCHED_TOPICS:
        print(f"\n[5/5] Auto-digest for {len(config.WATCHED_TOPICS)} watched topic(s) …")
        import query as _query
        with sqlite3.connect(config.DB_PATH) as dconn:
            dconn.row_factory = sqlite3.Row
            for topic in config.WATCHED_TOPICS:
                try:
                    _query.run_digest(dconn, topic)
                    slug = analyzer._slugify(topic)
                    print(f"  ✓ {topic} → {config.OUTPUT_DIR}/topics/{slug}.md")
                except Exception as e:
                    print(f"  ⚠ Skipped '{topic}': {e}")
    else:
        print("\n[5/5] Topic digests — skipped (WATCHED_TOPICS is empty in config.py)")
        print("  Tip: set WATCHED_TOPICS = ['your topic'] in config.py to auto-digest")
```

**Step 2: Update the final print block** — change `[4/4]` references to clarify 5-step pipeline. In the `print` near line 109, change:

```python
    mode_label = "BACKFILL (full history)" if backfill else f"RECENT ({config.DAYS_BACK_DEFAULT} days)"
    print(f"\n[2/5] Fetching Slack data [{mode_label}] …")
```

And update the other step labels: `[3/4]` → `[3/5]`, `[4/4]` → `[4/5]`.

**Step 3: Verify imports**

```bash
python -c "import main; print('ok')"
```

Expected: `ok`

**Step 4: Run all tests**

```bash
pytest tests/ -v
```

Expected: all green.

**Step 5: Commit**

```bash
git add main.py
git commit -m "feat: main — step 5 auto-digest for WATCHED_TOPICS"
```

---

### Task 9: Add Topics section to `dashboard.py`

**Files:**
- Modify: `dashboard.py`
- Create: `tests/test_dashboard_topics.py`

**Step 1: Write failing tests**

```python
# tests/test_dashboard_topics.py
import os, sys, json, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import dashboard


def test_get_topics_empty(tmp_path):
    topics_dir = tmp_path / "topics"
    topics_dir.mkdir()
    result = dashboard._get_topics(str(tmp_path))
    assert result == []


def test_get_topics_finds_json(tmp_path):
    topics_dir = tmp_path / "topics"
    topics_dir.mkdir()
    (topics_dir / "api-redesign.json").write_text(
        '{"topic":"API Redesign","slug":"api-redesign","generated_at":"2026-02-28T10:00:00+00:00","sources":["eng"],"message_count":5,"key_notes":"notes"}'
    )
    result = dashboard._get_topics(str(tmp_path))
    assert len(result) == 1
    assert result[0]["slug"] == "api-redesign"
    assert result[0]["topic"] == "API Redesign"


def test_slugify_roundtrip():
    import analyzer
    slug = analyzer._slugify("API Redesign")
    assert slug == "api-redesign"
```

**Step 2: Run to confirm failure**

```bash
pytest tests/test_dashboard_topics.py -v
```

Expected: `AttributeError: module 'dashboard' has no attribute '_get_topics'`

**Step 3: Add `_get_topics` helper to `dashboard.py`** — add after the `_nav_html` function:

```python
def _get_topics(output_dir):
    """Return list of topic metadata dicts from OUTPUT_DIR/topics/*.json.

    Each dict has: slug, topic, generated_at, sources, message_count.
    Returns empty list if topics directory doesn't exist.
    """
    topics_dir = os.path.join(output_dir, "topics")
    if not os.path.isdir(topics_dir):
        return []
    results = []
    for path in sorted(glob.glob(os.path.join(topics_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            results.append({
                "slug": d.get("slug", os.path.basename(path).replace(".json", "")),
                "topic": d.get("topic", ""),
                "generated_at": d.get("generated_at", "")[:16].replace("T", " "),
                "sources": d.get("sources", []),
                "message_count": d.get("message_count", 0),
            })
        except Exception:
            pass
    return results
```

**Step 4: Update `_nav_html` to include Topics section** — replace the existing `_nav_html` function:

```python
def _nav_html(channels, topics=None, active=""):
    topics = topics or []
    ch_links = ""
    for ch in channels:
        cls = ' class="active"' if ch == active else ""
        ch_safe = _html.escape(ch)
        ch_links += f'<a href="/channel/{ch_safe}"{cls}>#{ch_safe}</a>\n'

    topic_links = ""
    for t in topics:
        slug = t["slug"]
        cls = ' class="active"' if active == f"__topic__{slug}" else ""
        t_safe = _html.escape(t["topic"])
        topic_links += f'<a href="/topic/{slug}"{cls}>◆ {t_safe}</a>\n'

    topic_section = ""
    if topics:
        topic_section = f"""
  <div class="section-label">Topics</div>
  {topic_links}"""

    master_cls = ' class="active"' if active == "__master__" else ""
    return f"""
<nav>
  <div class="brand">📊 Slack Intel</div>
  <div class="section-label">Reports</div>
  <a href="/master"{master_cls}>⭐ Master Report</a>
  {topic_section}
  <div class="section-label">Channels</div>
  {ch_links}
</nav>"""
```

**Step 5: Update `page()` to accept topics** — replace the existing `page` function:

```python
def page(title, body, channels, topics=None, active=""):
    nav = _nav_html(channels, topics=topics, active=active)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — Slack Intel</title>
  <style>{CSS}</style>
</head>
<body>
{nav}
<main>
{body}
</main>
</body>
</html>"""
```

**Step 6: Add `/topic/{slug}` route to `do_GET`** — add this block inside `do_GET` after the `/master` route and before the `/api/status` route:

```python
            elif path.startswith("/topic/"):
                slug = path[len("/topic/"):]
                if not re.match(r'^[\w\-]+$', slug):
                    self.send_html(page("Error", "<h2>Invalid topic.</h2>", channels), 400)
                    return
                topics = _get_topics(config.OUTPUT_DIR)
                md_path = os.path.join(config.OUTPUT_DIR, "topics", f"{slug}.md")
                if not os.path.exists(md_path):
                    body = (f"<h2>No digest for '{slug}'</h2>"
                            "<p>Run <code>python query.py</code> and use "
                            "<code>/digest your topic</code>.</p>")
                    self.send_html(page(slug, body, channels, topics=topics), 404)
                    return
                with open(md_path, encoding="utf-8") as f:
                    content = f.read()
                body = md_to_html(content)
                self.send_html(page(slug, body, channels, topics=topics,
                                    active=f"__topic__{slug}"))
```

**Step 7: Pass topics into all existing `page()` calls in `do_GET`** — update the three existing calls that don't yet pass topics:

For the `/` route:
```python
                topics = _get_topics(config.OUTPUT_DIR)
                body = index_body(channels, conn)
                self.send_html(page("Dashboard", body, channels, topics=topics))
```

For `/channel/{name}`:
```python
                topics = _get_topics(config.OUTPUT_DIR)
                # ... existing code ...
                self.send_html(page(f"#{ch}", body, channels, topics=topics, active=ch))
```

For `/master`:
```python
                topics = _get_topics(config.OUTPUT_DIR)
                # ... existing code ...
                self.send_html(page("Master Report", body, channels, topics=topics, active="__master__"))
```

**Step 8: Run tests**

```bash
pytest tests/test_dashboard_topics.py -v
pytest tests/ -v
```

Expected: all green.

**Step 9: Commit**

```bash
git add dashboard.py tests/test_dashboard_topics.py
git commit -m "feat: dashboard — Topics sidebar section and /topic/{slug} route"
```

---

### Task 10: Update README with new feature

**Files:**
- Modify: `README.md`

**Step 1: Add a "Topic Digests" section** to `README.md` after the Usage section:

```markdown
## Topic Digests

Gather cross-channel intelligence on any topic with a single command.

### Interactive digest (in query.py)

```
> /digest API redesign
> /digest --fresh auth service
> #engineering /digest Q2 roadmap
```

- **`/digest Topic X`** — searches all channels, synthesizes a briefing, and stores key notes in SQLite. Future digests on the same topic inject prior notes as context, building cumulative intelligence over time.
- **`/digest --fresh Topic X`** — same synthesis without prior context; useful for an unbiased fresh read.
- **`#channel /digest Topic X`** — scopes the search to a single channel.

### Auto-digest via main.py

Add topics to `config.py`:

```python
WATCHED_TOPICS = ["API redesign", "auth service", "Q2 roadmap"]
```

Each `python main.py` run automatically generates digests for all watched topics as step 5.

### Briefing sections

Every digest produces: **Key Updates · Decisions Made · Open Questions · Next Steps · Sources**

Reports are saved to `./data/output/topics/` and visible in the dashboard sidebar under **Topics**.
```

**Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README — Topic Digests feature section"
```

---

### Task 11: Final verification

**Step 1: Run full test suite**

```bash
pytest tests/ -v
```

Expected: all green, no warnings.

**Step 2: Verify all modules import cleanly**

```bash
python -c "import config, analyzer, exporter, query, dashboard, main; print('all imports ok')"
```

Expected: `all imports ok`

**Step 3: Commit if any last fixes needed, then push**

```bash
git push origin claude/xenodochial-lehmann
```
