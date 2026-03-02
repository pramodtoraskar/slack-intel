# analyzer.py — Analyzes Slack messages using local Ollama LLM.
# Run standalone: python analyzer.py  (reads from SQLite, no Slack calls needed)

import sqlite3
import requests
import json
from datetime import datetime, timezone
import config


def ensure_topic_notes_table(conn):
    """Create topic_notes table if it does not exist. Safe to call multiple times."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS topic_notes (
            topic      TEXT,
            created_at TEXT,
            key_notes  TEXT,
            PRIMARY KEY (topic, created_at)
        )
    """)
    conn.commit()



# ── Ollama interface ──────────────────────────────────────────────────────────

def ask(prompt):
    """Send a prompt to Ollama, return the response text.

    Uses temperature=0.2 for consistent, factual outputs.
    Timeout is generous (300s) since long channel histories take time.
    """
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
    """Split text into chunks no larger than max_chars, breaking on newlines.

    Ensures no single LLM call exceeds the context window. Breaks at line
    boundaries to avoid splitting mid-message.
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    current = []
    current_len = 0

    for line in text.splitlines(keepends=True):
        if current and current_len + len(line) > max_chars:
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
    """Format DB message rows into readable text, with thread replies indented.

    Top-level messages: [YYYY-MM-DD] username: text
    Thread replies:       ↳ username: text
    """
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
    """Analyze one channel's messages. Handles chunking for large channels.

    Single chunk  → one ask() call.
    Multi-chunk   → analyze each chunk, then synthesize with MERGE_PROMPT.
    Returns the full analysis string (markdown with ## headings).
    """
    chunks = chunk_text(formatted_text, config.CONTEXT_WINDOW)
    print(f"  [{channel_name}] {len(chunks)} chunk(s) to analyze")

    if len(chunks) == 1:
        prompt = CHANNEL_PROMPT.format(channel=channel_name, messages=chunks[0])
        return ask(prompt)

    # Multi-chunk: analyze each chunk separately, then merge
    partials = []
    for i, chunk in enumerate(chunks, 1):
        print(f"  [{channel_name}] chunk {i}/{len(chunks)} …")
        prompt = CHANNEL_PROMPT.format(channel=channel_name, messages=chunk)
        partials.append(f"=== Part {i} ===\n{ask(prompt)}")

    print(f"  [{channel_name}] synthesising {len(chunks)} chunks …")
    merge_prompt = MERGE_PROMPT.format(
        channel=channel_name,
        partials="\n\n".join(partials)
    )
    return ask(merge_prompt)


# ── Master report ─────────────────────────────────────────────────────────────

def build_master(channel_analyses):
    """Build cross-channel executive summary.

    Args:
        channel_analyses: dict mapping channel_name -> analysis_string
    Returns:
        Master report string (markdown with ## headings)
    """
    if not channel_analyses:
        return "No channel analyses available to build master report."

    sections = []
    for name, analysis in channel_analyses.items():
        sections.append(f"=== #{name} ===\n{analysis}")
    combined = "\n\n".join(sections)
    prompt = MASTER_PROMPT.format(channel_analyses=combined)
    print(f"  Building master report from {len(channel_analyses)} channels …")
    return ask(prompt)


# ── Topic digest ──────────────────────────────────────────────────────────────

TOPIC_PROMPT = """You are analyzing Slack conversations to build a topic intelligence briefing.

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

    e.g. "API Redesign!" -> "api-redesign"
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
        print(f"  [topic:{topic}] chunk {i}/{len(chunks)} ...")
        p = _topic_prompt(topic, chunk, prior_notes=None,
                          channels=channels_str, message_count=len(messages))
        partials.append(f"=== Part {i} ===\n{ask(p)}")

    print(f"  [topic:{topic}] synthesising {len(chunks)} chunks ...")
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


# ── Entry point (reads from SQLite) ──────────────────────────────────────────

def analyze_all():
    """Analyze all channels that have new messages since last analysis.

    Compares current message count against analysis_state.message_count_at_analysis.
    Skips channels with fewer than MIN_MESSAGES messages.
    Updates analysis_state after each successful channel analysis.

    Returns:
        dict mapping channel_name -> analysis_string for re-analyzed channels only.
    """
    import os
    if not os.path.exists(config.DB_PATH):
        print("  DB not found — run main.py first to fetch Slack data.")
        return {}

    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row

    # Check schema exists
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "messages" not in tables:
        print("  DB exists but schema not initialised — run main.py first.")
        conn.close()
        return {}

    results = {}
    try:
        counts = conn.execute("""
            SELECT channel_name, channel_id, COUNT(*) as cnt
            FROM messages GROUP BY channel_id
        """).fetchall()

        for row in counts:
            ch_name = row["channel_name"]
            ch_id = row["channel_id"]
            msg_count = row["cnt"]

            if msg_count < config.MIN_MESSAGES:
                print(f"  [{ch_name}] skipping — only {msg_count} messages (< {config.MIN_MESSAGES})")
                continue

            state = conn.execute(
                "SELECT message_count_at_analysis FROM analysis_state WHERE channel_id=?",
                (ch_id,)
            ).fetchone()

            if state and state["message_count_at_analysis"] == msg_count:
                print(f"  [{ch_name}] no new messages since last analysis, skipping")
                continue

            print(f"\n→ Analyzing #{ch_name} ({msg_count} messages) …")

            rows = conn.execute("""
                SELECT date, user_id, user_name, text, thread_ts, is_reply
                FROM messages
                WHERE channel_id=?
                ORDER BY ts ASC
            """, (ch_id,)).fetchall()

            formatted = format_messages(rows)
            if not formatted.strip():
                print(f"  [{ch_name}] no non-empty messages after formatting, skipping")
                continue

            analysis = analyze_channel(ch_name, formatted)
            results[ch_name] = analysis

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

    finally:
        conn.close()

    return results


if __name__ == "__main__":
    results = analyze_all()
    if not results:
        print("No channels required analysis (either no data or all up-to-date).")
    else:
        for ch, analysis in results.items():
            print(f"\n{'='*60}\n#{ch}\n{'='*60}\n{analysis}\n")
