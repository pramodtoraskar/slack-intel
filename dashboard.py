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
import html as _html
import config


# ── Markdown → HTML renderer ──────────────────────────────────────────────────

def md_to_html(text):
    """Convert markdown to HTML.

    Handles: # headings (h1-h4), **bold**, _italic_, - bullet lists,
    ``` code blocks, --- horizontal rules, blank lines as <br>.
    """
    html = []
    in_ul = False
    in_code = False

    for line in text.splitlines():
        # Code blocks (``` fence)
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
            # Escape HTML entities inside code blocks
            html.append(
                line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            continue

        # Headings (# H1 through #### H4)
        m = re.match(r'^(#{1,4})\s+(.*)', line)
        if m:
            if in_ul:
                html.append("</ul>")
                in_ul = False
            level = len(m.group(1))
            content = _inline(m.group(2))
            html.append(f"<h{level}>{content}</h{level}>")
            continue

        # Horizontal rule ---
        if re.match(r'^-{3,}$', line.strip()):
            if in_ul:
                html.append("</ul>")
                in_ul = False
            html.append("<hr>")
            continue

        # Unordered list items (-, *, •)
        m = re.match(r'^[-*•]\s+(.*)', line)
        if m:
            if not in_ul:
                html.append("<ul>")
                in_ul = True
            html.append(f"<li>{_inline(m.group(1))}</li>")
            continue

        # Close list on blank or non-list line
        if in_ul and not line.strip():
            html.append("</ul>")
            in_ul = False

        # Blank line
        if not line.strip():
            html.append("<br>")
            continue

        # Paragraph
        html.append(f"<p>{_inline(line)}</p>")

    if in_ul:
        html.append("</ul>")
    if in_code:
        html.append("</code></pre>")

    return "\n".join(html)


def _inline(text):
    """Apply inline markdown: **bold**, _italic_, `code`.
    
    Escapes raw HTML first to prevent XSS from untrusted markdown content.
    """
    text = _html.escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'_(.+?)_', r'<em>\1</em>', text)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    return text


# ── HTML page template ────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       margin: 0; display: flex; height: 100vh; background: #f8f9fa; color: #1a1d21; }
nav { width: 230px; min-width: 230px; background: #1a1d21; color: #d1d2d3;
      overflow-y: auto; display: flex; flex-direction: column; }
nav .brand { padding: 18px 16px 12px; font-size: 16px; font-weight: 700;
             color: #fff; border-bottom: 1px solid #2d3139; }
nav .section-label { padding: 12px 16px 4px; font-size: 11px; color: #868686;
                     text-transform: uppercase; letter-spacing: .08em; }
nav a { display: block; padding: 6px 16px; color: #d1d2d3; text-decoration: none;
        font-size: 14px; border-radius: 4px; margin: 1px 8px; white-space: nowrap;
        overflow: hidden; text-overflow: ellipsis; }
nav a:hover { background: #2d3139; }
nav a.active { background: #1164a3; color: #fff; }
main { flex: 1; overflow-y: auto; padding: 32px 48px; }
main h1 { color: #1a1d21; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; }
main h2 { color: #1164a3; margin-top: 32px; border-left: 4px solid #1164a3;
          padding-left: 12px; }
main h3, main h4 { color: #444; }
main p { line-height: 1.7; color: #333; }
main ul { line-height: 1.9; }
main li { margin-bottom: 4px; }
main code { background: #f0f0f0; border-radius: 3px; padding: 2px 5px;
            font-size: 0.9em; }
main pre { background: #f4f4f4; border: 1px solid #ddd; border-radius: 6px;
           padding: 14px; overflow-x: auto; }
main pre code { background: none; padding: 0; }
main hr { border: none; border-top: 1px solid #e0e0e0; margin: 24px 0; }
.meta { color: #888; font-size: 13px; margin-bottom: 24px; }
.stat-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;
             margin: 24px 0; }
.stat-card { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
             padding: 20px; text-align: center; }
.stat-card .num { font-size: 28px; font-weight: 700; color: #1164a3; }
.stat-card .label { font-size: 13px; color: #888; margin-top: 4px; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
th { background: #f4f4f4; padding: 10px 14px; text-align: left;
     font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: #666; }
td { padding: 10px 14px; border-top: 1px solid #f0f0f0; font-size: 14px; }
tr:hover td { background: #f9f9f9; }
a.channel-link { color: #1164a3; text-decoration: none; font-weight: 500; }
a.channel-link:hover { text-decoration: underline; }
.badge { display: inline-block; background: #e8f0fe; color: #1164a3;
         border-radius: 12px; padding: 2px 10px; font-size: 12px; }
.empty { text-align: center; padding: 64px; color: #888; }
.empty h2 { color: #ccc; font-size: 48px; margin: 0 0 16px; }
"""


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


# ── Page bodies ───────────────────────────────────────────────────────────────

def index_body(channels, conn):
    """Dashboard home — stats + channel table."""
    total_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    analyzed = conn.execute("SELECT COUNT(*) FROM analysis_state").fetchone()[0]
    last_run = conn.execute("SELECT MAX(analyzed_at) FROM analysis_state").fetchone()[0]
    last_run_str = last_run[:16].replace("T", " ") + " UTC" if last_run else "never"

    stats = f"""
<div class="stat-grid">
  <div class="stat-card"><div class="num">{total_msgs:,}</div>
    <div class="label">Total Messages</div></div>
  <div class="stat-card"><div class="num">{analyzed}</div>
    <div class="label">Channels Analyzed</div></div>
  <div class="stat-card"><div class="num">{last_run_str}</div>
    <div class="label">Last Analysis</div></div>
</div>"""

    rows = conn.execute("""
        SELECT a.channel_name, a.message_count_at_analysis, a.analyzed_at
        FROM analysis_state a ORDER BY a.analyzed_at DESC
    """).fetchall()

    if not rows:
        return """<h1>Slack Intelligence Dashboard</h1>
<div class="empty"><h2>📭</h2>
<p>No data yet. Run <code>python main.py</code> to fetch and analyze your Slack channels.</p>
</div>"""

    table_rows = ""
    for r in rows:
        analyzed_at = (r[2] or "")[:16].replace("T", " ")
        ch_s = _html.escape(str(r[0]))
        table_rows += f"""
<tr>
  <td><a href="/channel/{ch_s}" class="channel-link">#{ch_s}</a></td>
  <td style="text-align:center"><span class="badge">{r[1]:,}</span></td>
  <td style="color:#888">{analyzed_at} UTC</td>
  <td><a href="/channel/{ch_s}" class="channel-link">View →</a></td>
</tr>"""

    table = f"""
<table>
  <thead><tr><th>Channel</th><th>Messages</th><th>Last Analyzed</th><th></th></tr></thead>
  <tbody>{table_rows}</tbody>
</table>"""

    return f"<h1>Slack Intelligence Dashboard</h1>{stats}{table}"


# ── Request handler ───────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # Print minimal access log
        print(f"  {self.command} {self.path}")

    def _get_channels(self, conn):
        rows = conn.execute(
            "SELECT DISTINCT channel_name FROM analysis_state ORDER BY channel_name"
        ).fetchall()
        return [r[0] for r in rows]

    def send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not os.path.exists(config.DB_PATH):
            self.send_html(page(
                "Setup Required",
                "<h1>Setup Required</h1><p>Run <code>python main.py</code> first.</p>",
                [], topics=[]
            ))
            return

        conn = sqlite3.connect(config.DB_PATH)
        conn.row_factory = sqlite3.Row
        path = self.path.split("?")[0].rstrip("/") or "/"

        try:
            channels = self._get_channels(conn)

            if path in ("/", ""):
                topics = _get_topics(config.OUTPUT_DIR)
                body = index_body(channels, conn)
                self.send_html(page("Dashboard", body, channels, topics=topics))

            elif path.startswith("/channel/"):
                ch = path[len("/channel/"):]
                # Sanitize: only allow alphanumeric, hyphens, underscores
                if not re.match(r'^[\w\-]+$', ch):
                    self.send_html(page("Error", "<h2>Invalid channel name.</h2>", channels), 400)
                    return
                topics = _get_topics(config.OUTPUT_DIR)
                md_path = os.path.join(config.OUTPUT_DIR, f"{ch}.md")
                if not os.path.exists(md_path):
                    body = f"<h2>No report for #{ch}</h2><p>Run <code>python main.py</code> to generate reports.</p>"
                    self.send_html(page(f"#{ch}", body, channels, topics=topics), 404)
                    return
                with open(md_path, encoding="utf-8") as f:
                    content = f.read()
                body = md_to_html(content)
                self.send_html(page(f"#{ch}", body, channels, topics=topics, active=ch))

            elif path == "/master":
                topics = _get_topics(config.OUTPUT_DIR)
                masters = sorted(glob.glob(os.path.join(config.OUTPUT_DIR, "master_*.md")))
                if not masters:
                    body = "<h2>No master report yet</h2><p>Run <code>python main.py</code> first.</p>"
                    self.send_html(page("Master Report", body, channels, topics=topics))
                    return
                with open(masters[-1], encoding="utf-8") as f:
                    content = f.read()
                body = md_to_html(content)
                self.send_html(page("Master Report", body, channels, topics=topics, active="__master__"))

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
                    "channels": channels,
                })

            else:
                self.send_html(page("Not Found", "<h2>404 — Page not found</h2>", channels), 404)

        except Exception as e:
            import traceback
            error_html = f"<h2>Internal Error</h2><pre>{traceback.format_exc()}</pre>"
            self.send_html(page("Error", error_html, []), 500)
        finally:
            conn.close()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    addr = ("", config.DASHBOARD_PORT)
    server = http.server.HTTPServer(addr, Handler)
    print(f"\n  Slack Intelligence Dashboard")
    print(f"  ─────────────────────────────")
    print(f"  Open: http://localhost:{config.DASHBOARD_PORT}")
    print(f"  DB:   {config.DB_PATH}")
    print(f"  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
