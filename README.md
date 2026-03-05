<p align="center">
  <img src="assets/logo.svg" width="100" height="100" alt="Slack Intel logo"/>
</p>

# Slack Intelligence Agent

**A fully local pipeline for fetching, analyzing, and querying your Slack history — no cloud LLM, no data leaves your machine.**

Slack Intelligence Agent pulls your Slack channel history into a local SQLite database, runs each channel through a locally-hosted Ollama LLM to produce structured analysis reports, and serves them via a lightweight web dashboard. An interactive CLI lets you ask natural-language questions against your own message history using retrieval-augmented generation (RAG) — all without sending a single message to an external AI service.

---

## Features

- **Incremental fetch** — only pulls new messages on each run; `--backfill` fetches full history (resumable)
- **Per-channel LLM analysis** — historical context, key points, future plans, identified gaps, recommendations
- **Cross-channel master report** — themes, dependencies, org-wide risks, conflicting priorities
- **Interactive Q&A** — keyword search + Ollama RAG; scope to a specific channel with `#channel question`
- **Local web dashboard** — `http://localhost:8080`, channel sidebar, rendered Markdown reports
- **Zero external AI calls** — Ollama runs entirely on your hardware
- **Resilient** — rate-limit retry, fetch checkpointing, skip-unchanged-channel analysis

---

## Slack Intelligence Agent vs Slack AI (Native)

| | **Slack Intelligence Agent** | **Slack AI (Salesforce Native)** |
|---|---|---|
| **Data residency** | Fully local — data never leaves your environment | Cloud-based; data sent to Salesforce/OpenAI infrastructure |
| **LLM** | Ollama-powered (llama3 / llama3.1) + SQLite | Built-in summarization backed by OpenAI |
| **Control** | Multi-agent Python pipeline you own and modify | Fixed to Slack's predefined use cases |
| **Coverage** | Analyzes 20+ channels with custom logic | Limited to thread recaps, search, and channel summaries |
| **Customization** | You own the schema, the queries, and the outputs | No customization of the intelligence layer |
| **Specialized use cases** | Adaptable to any team workflow or specific intelligence need | General-purpose only |
| **Setup complexity** | Requires Python, Ollama, and a Slack bot token | Zero setup — works out of the box |
| **Cost** | No subscription cost beyond local compute | Extra charge on top of Slack subscription |

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.9+ | Standard library only beyond two pip packages |
| [Ollama](https://ollama.com) | Running locally on `localhost:11434` |
| `llama3` model | `ollama pull llama3` (or set a different model in `config.py`) |
| Slack Bot Token | `xoxb-…` with `channels:history`, `channels:read`, `users:read` scopes |

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/your-username/slack-intel.git
cd slack-intel

# 2. Install dependencies
pip install slack_sdk requests

# 3. Configure
#    Open config.py and set:
#      SLACK_TOKEN = "xoxb-..."
#      TARGET_CHANNELS = ["general", "engineering", "product"]

# 4. Run the pipeline (fetch last 90 days → analyze → export reports)
python main.py

# 5. Open the dashboard
python dashboard.py
# → http://localhost:8080
```

---

## Configuration

All settings live in `config.py`. No environment variables required.

| Setting | Default | Description |
|---------|---------|-------------|
| `SLACK_TOKEN` | `""` | Bot token (`xoxb-…`) — required |
| `TARGET_CHANNELS` | `[]` | List of channel names to fetch — required |
| `OLLAMA_BASE` | `http://localhost:11434` | Ollama server URL |
| `CHAT_MODEL` | `llama3` | Model name (must be pulled locally) |
| `CONTEXT_WINDOW` | `6000` | Max characters per LLM call |
| `RATE_LIMIT_DELAY` | `1.2` | Seconds between Slack API calls |
| `DAYS_BACK_DEFAULT` | `90` | First-run fetch window (days) |
| `MIN_MESSAGES` | `10` | Skip analysis for channels below this count |
| `DB_PATH` | `./data/slack_intel.db` | SQLite database path |
| `OUTPUT_DIR` | `./data/output` | Report output directory |
| `DASHBOARD_PORT` | `8080` | Web dashboard port |

---

## Usage

### Run the full pipeline

```bash
python main.py              # fetch last 90 days + analyze + export
python main.py --backfill   # fetch full channel history (resumable)
```

`main.py` runs a health check first — it will tell you exactly what to fix if Ollama isn't running, your token is missing, or the model hasn't been pulled.

### View reports in the browser

```bash
python dashboard.py
# Open http://localhost:8080
```

The dashboard shows a sidebar of all analyzed channels. Click any channel to read its report. The master cross-channel report is always one click away.

### Ask questions about your Slack history

```bash
python query.py
```

```
> what decisions were made about the API redesign?
> #engineering who owns the auth service?
> exit
```

Prefix a query with `#channel-name` to scope it to a single channel. Type `exit` to quit.

---

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

---

## Output

After each run, reports land in `./data/output/`:

```
data/
├── slack_intel.db              # SQLite — messages, channels, fetch/analysis state
└── output/
    ├── general.md              # per-channel Markdown report
    ├── general.json            # per-channel JSON report
    ├── engineering.md
    ├── engineering.json
    ├── master_20260228.md      # cross-channel master report
    └── master_20260228.json
```

**Per-channel report sections:** Historical Context · Key Points · Future Plans · Identified Gaps · Recommendations

**Master report sections:** Cross-Channel Themes · Team Dependencies & Handoffs · Organization-Wide Gaps & Risks · Conflicting Priorities · Leadership Recommendations

---

## Architecture

```
main.py (orchestrator)
    │
    ├─► fetcher.py
    │       • Slack WebClient → channels.history + conversations.replies
    │       • Normal run: only fetches messages newer than last checkpoint
    │       • --backfill: pages backward from oldest stored ts, resumable
    │       • Resolves user_id → name (cached per run)
    │       • Writes to SQLite: messages, channels, fetch_state
    │
    ├─► analyzer.py
    │       • Reads SQLite only — no re-fetch
    │       • Skips channels where message count is unchanged
    │       • Chunks text → POST /api/generate → merges multi-chunk results
    │       • Produces per-channel analysis strings
    │
    ├─► exporter.py
    │       • Writes .md + .json per channel to ./data/output/
    │       • Writes master_{YYYYMMDD}.md + .json
    │
    └─► query.py (standalone CLI)
            • Keyword search → top 30 messages → Ollama RAG answer
            • "#channel question" scopes to one channel

dashboard.py (standalone server)
    • stdlib http.server only — no extra dependencies
    • Reads SQLite + ./data/output/ files
    • GET /              — channel sidebar
    • GET /channel/{name} — rendered channel report
    • GET /master        — rendered master report
    • GET /api/status    — JSON health/stats
```

---

## Project Structure

```
slack-intel/
├── config.py       — all settings and constants
├── fetcher.py      — Slack API → SQLite
├── analyzer.py     — Ollama LLM analysis
├── exporter.py     — Markdown + JSON report writers
├── main.py         — orchestrator
├── query.py        — interactive RAG CLI
├── dashboard.py    — local web server (localhost:8080)
└── data/           — created on first run
    ├── slack_intel.db
    └── output/
```

---

## Privacy

All message data is stored locally in `./data/slack_intel.db`. Analysis is performed by Ollama running on your own hardware. No Slack messages, user names, or report content are ever sent to an external API.

---

## Slack App Setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Under **OAuth & Permissions**, add these Bot Token Scopes:
   - `channels:history`
   - `channels:read`
   - `users:read`
3. **Install to Workspace** → copy the **Bot User OAuth Token** (`xoxb-…`)
4. Invite the bot to each target channel: `/invite @your-bot-name`
5. Paste the token into `config.py`

---

## License

MIT
