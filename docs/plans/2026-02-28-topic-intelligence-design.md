# Topic Intelligence System — Design Document
**Date:** 2026-02-28
**Status:** Approved

---

## Overview

Extend Slack Intelligence Agent with a cross-channel topic digest feature. Given a topic keyword, the system searches all channels, synthesizes a structured briefing using Ollama, persists key notes for cumulative intelligence, and surfaces results in the dashboard. Three modes: interactive persistent digest, stateless fresh digest, and auto-digest from `main.py`.

---

## Confirmed Decisions

| Decision | Choice |
|----------|--------|
| Entry point (interactive) | Extend `query.py` with `/digest Topic X` and `/digest --fresh Topic X` |
| Entry point (automatic) | `main.py` step 5 — digest all `WATCHED_TOPICS` each run |
| Persistence | New `topic_notes` SQLite table |
| Cumulative intelligence | Prior notes injected as context into next LLM call for same topic |
| Fresh mode | `--fresh` flag skips prior notes entirely |
| Dashboard | New "Topics" sidebar section, history per topic |
| New files | None — all changes in existing modules |

---

## Architecture & Data Flow

```
query.py /digest          main.py (step 5)
     │                         │
     ▼                         ▼
SQLite LIKE search        SQLite LIKE search
(all channels or #one)    (per WATCHED_TOPICS)
     │                         │
     ▼                         ▼
topic_notes lookup        topic_notes lookup
(inject prior context)    (inject prior context)
     │                         │
     ▼                         ▼
Ollama synthesis          Ollama synthesis
(analyzer.build_topic_digest)
     │                         │
     ▼                         ▼
topic_notes INSERT        topic_notes INSERT
./data/output/topics/     ./data/output/topics/
     │                         │
     ▼                         ▼
print to terminal         silent (logged only)
```

---

## Data Model

### New table: `topic_notes`

```sql
CREATE TABLE IF NOT EXISTS topic_notes (
    topic       TEXT,
    created_at  TEXT,
    key_notes   TEXT,   -- full LLM briefing text
    PRIMARY KEY (topic, created_at)
);
```

### Output files

```
data/output/topics/
├── topic-x.md        -- latest digest for "Topic X"
├── topic-x.json      -- { topic, generated_at, key_notes, sources, message_count }
└── topic-x/
    ├── 20260228T1430.md    -- history snapshots (one per digest run)
    └── 20260228T1430.json
```

---

## Briefing Structure (all modes)

```markdown
## Key Updates
## Decisions Made
## Open Questions
## Next Steps
## Sources        ← list of contributing channels
```

---

## Module Changes

### config.py
Add:
```python
WATCHED_TOPICS = []   # e.g. ["API redesign", "auth service", "Q2 roadmap"]
```

### analyzer.py
Add `build_topic_digest(topic, messages, prior_notes=None)`:
- Formats messages as context (channel, date, user, text)
- If `prior_notes` provided: prepends as "Prior Intelligence" section in prompt
- Calls `ask()` with structured briefing prompt
- Returns briefing string

### query.py
Add command parsing in the main loop:
- `/digest <topic>` — persistent mode (load prior notes, synthesize, save)
- `/digest --fresh <topic>` — stateless mode (no prior notes loaded or saved)
- Cross-channel search: no `#channel` prefix = all channels
- `#channel /digest <topic>` = scoped to one channel
- After synthesis: INSERT into `topic_notes`, write `.md` + `.json` to `./data/output/topics/`

### main.py
Add step 5 after export:
```
[5/5] Topic digests …
```
- Iterate `config.WATCHED_TOPICS`
- Skip if empty
- Call same flow as `/digest` (persistent mode)
- Print one line per topic: `  ✓ Topic X → data/output/topics/topic-x.md`

### dashboard.py
- Add "Topics" section to sidebar (below channels)
- `GET /topics` — list all topics with last digest date
- `GET /topic/{name}` — render latest `topic-name.md` as HTML
- `GET /topic/{name}/history` — list all snapshots with links

---

## Prompt Design

### Topic digest prompt
```
You are analyzing Slack conversations to build a topic intelligence briefing.

Topic: {topic}
Channels searched: {channel_list}
Message count: {count}

{prior_notes_section}  ← only in persistent mode:
  "== Prior Intelligence ==\n{prior_notes}\n"

Messages:
{formatted_messages}

Produce a briefing with these sections:
## Key Updates
## Decisions Made
## Open Questions
## Next Steps
## Sources
```

---

## Error Handling

- **No messages found for topic:** print "No messages found for '{topic}'" — skip synthesis, no file written
- **Ollama unavailable:** same health check path as existing code — fail fast
- **`WATCHED_TOPICS` empty:** step 5 prints "  (no WATCHED_TOPICS configured — skipping)" and exits cleanly
- **Topic name → filename:** slugify: lowercase, spaces to hyphens, strip special chars

---

## Dependencies

No new dependencies. All changes use existing `sqlite3`, `requests`, stdlib.

---

## Run Order

```bash
# Interactive persistent digest
python query.py
> /digest API redesign

# Interactive fresh digest (no prior context)
python query.py
> /digest --fresh API redesign

# Scoped to one channel
python query.py
> #engineering /digest auth service

# Auto-digest via main.py (after setting WATCHED_TOPICS in config.py)
python main.py

# View topic digests
python dashboard.py   # → http://localhost:8080  (Topics section in sidebar)
```
