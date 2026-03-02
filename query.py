# query.py — Interactive local Q&A over Slack messages.
# Usage: python query.py
# Syntax:
#   "your question"               → search all channels
#   "#channelname your question"  → scope search to one channel

import sqlite3
import re
import os
import config
import analyzer   # reuse ask()

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

RAG_PROMPT = """\
You are a helpful assistant. Answer the user's question using ONLY the Slack \
messages provided below. If the answer cannot be found in the messages, say so \
clearly — do not guess or make things up.

Question: {question}

Relevant Slack messages:
{context}

Answer:"""


def search_messages(conn, query, channel_name=None, limit=30):
    """Keyword search across the messages table.

    Splits the query into words and builds an OR-based LIKE filter.
    Scoped to channel_name if provided.
    Returns up to `limit` rows ordered by timestamp descending (most recent first).
    """
    words = re.findall(r'\w+', query.lower())
    if not words:
        return []

    # Score: count how many keywords match per row (higher = more relevant)
    # We use a simple union of LIKE conditions for now
    conditions = " OR ".join(["LOWER(m.text) LIKE ?" for _ in words])
    params = [f"%{w}%" for w in words]

    if channel_name:
        sql = f"""
            SELECT m.date, m.user_name, m.text, m.channel_name, m.is_reply, m.ts
            FROM messages m
            WHERE m.channel_name = ?
              AND ({conditions})
            ORDER BY m.ts DESC
            LIMIT ?
        """
        params = [channel_name] + params + [limit]
    else:
        sql = f"""
            SELECT m.date, m.user_name, m.text, m.channel_name, m.is_reply, m.ts
            FROM messages m
            WHERE {conditions}
            ORDER BY m.ts DESC
            LIMIT ?
        """
        params = params + [limit]

    return conn.execute(sql, params).fetchall()


def score_and_rank(rows, words):
    """Re-rank rows by keyword hit count (descending).

    The LIKE query returns rows with ANY keyword match.
    This re-ranks them so rows matching MORE keywords appear first.
    """
    def score(row):
        text_lower = (row["text"] or "").lower()
        return sum(1 for w in words if w in text_lower)

    return sorted(rows, key=score, reverse=True)


def format_context(rows):
    """Format search results as readable context for the LLM."""
    lines = []
    for r in rows:
        ch = r["channel_name"] or "unknown"
        prefix = f"  ↳" if r["is_reply"] else f"[{r['date']}] #{ch}"
        user = r["user_name"] or "unknown"
        lines.append(f"{prefix} {user}: {r['text']}")
    return "\n".join(lines)


def parse_input(user_input):
    """Parse '#channel question' or plain question.

    Returns (question_str, channel_name_or_None).
    """
    m = re.match(r"^#(\S+)\s+(.*)", user_input.strip())
    if m:
        return m.group(2).strip(), m.group(1).lower()
    return user_input.strip(), None

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
      /digest Topic X                  -> ("Topic X", None, "persistent")
      /digest --fresh Topic X          -> ("Topic X", None, "fresh")
      #channel /digest Topic X         -> ("Topic X", "channel", "persistent")
      #channel /digest --fresh Topic X -> ("Topic X", "channel", "fresh")

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
          + (f" in #{channel}" if channel else " across all channels") + " ...\n")

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
            print(f"  Injecting prior intelligence ({len(prior_notes):,} chars) ...\n")

    print(f"  Found {len(rows)} relevant messages. Synthesizing with Ollama ...\n")

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
    print(f"\n  Report saved -> data/output/topics/{slug}.md")
    print(f"  View in dashboard -> http://localhost:{__import__('config').DASHBOARD_PORT}/topic/{slug}\n")


def run_query():
    """Main interactive Q&A loop."""
    if not os.path.exists(config.DB_PATH):
        print("No database found. Run 'python main.py' first to fetch Slack data.")
        return

    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row

    # Quick sanity check
    try:
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    except Exception:
        print("Database schema not initialised. Run 'python main.py' first.")
        conn.close()
        return

    print(HELP_TEXT)
    print(f"  Loaded database: {count:,} messages available\n")

    try:
        while True:
            try:
                user_input = input("Q> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye.")
                break

            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "q"):
                print("Bye.")
                break

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
            if not question:
                continue

            words = re.findall(r'\w+', question.lower())
            rows = search_messages(conn, question, channel_name=channel)

            if not rows:
                print("  No matching messages found.\n")
                continue

            # Re-rank by keyword density before sending to LLM
            rows = score_and_rank(rows, words)

            scope_msg = f" in #{channel}" if channel else " across all channels"
            print(f"  Found {len(rows)} relevant messages{scope_msg}. Asking Ollama …\n")

            context = format_context(rows)

            # Guard: truncate context if it would exceed the LLM context window.
            # Reserve ~500 chars for the prompt template and question text.
            max_context_chars = config.CONTEXT_WINDOW - 500
            if len(context) > max_context_chars:
                context = context[:max_context_chars]
                print(f"  (Context truncated to {max_context_chars:,} chars to fit context window)\n")

            prompt = RAG_PROMPT.format(question=question, context=context)

            try:
                answer = analyzer.ask(prompt)
                print(f"A> {answer}\n")
            except Exception as e:
                print(f"  Error calling Ollama: {e}\n")
                print("  (Is Ollama running? Try: ollama serve)\n")

    finally:
        conn.close()


if __name__ == "__main__":
    run_query()
