# exporter.py — Writes analysis results to Markdown and JSON files.
# Run standalone: python exporter.py  (writes test output to ./data/output/)

import os
import json
from datetime import datetime, timezone
import config


def _ensure_output_dir():
    """Create ./data/output/ if it doesn't exist."""
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)


def save_channel_report(channel_name, analysis, message_count):
    """Write per-channel analysis as .md and .json.

    Files are written to config.OUTPUT_DIR/{channel_name}.md and .json.
    Existing files are overwritten (most recent analysis wins).

    Args:
        channel_name:  Slack channel name (no # prefix)
        analysis:      Full analysis string from analyzer.analyze_channel()
        message_count: Number of messages analyzed
    """
    _ensure_output_dir()
    generated_at = datetime.now(tz=timezone.utc).isoformat()

    # Markdown
    md_path = os.path.join(config.OUTPUT_DIR, f"{channel_name}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# #{channel_name} — Slack Intelligence Report\n\n")
        f.write(f"_Generated: {generated_at}_  \n")
        f.write(f"_Messages analysed: {message_count:,}_\n\n")
        f.write("---\n\n")
        f.write(analysis)
    print(f"  Wrote {md_path}")

    # JSON
    json_path = os.path.join(config.OUTPUT_DIR, f"{channel_name}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "channel": channel_name,
            "generated_at": generated_at,
            "analysis": analysis,
            "message_count": message_count,
        }, f, indent=2, ensure_ascii=False)
    print(f"  Wrote {json_path}")

    return md_path, json_path


def save_master_report(analysis, channel_names):
    """Write cross-channel master report as .md and .json.

    Filename includes today's date: master_YYYYMMDD.md / .json.
    Multiple runs on the same day overwrite the file.

    Args:
        analysis:      Full master report string from analyzer.build_master()
        channel_names: List of channel names included in the report

    Returns:
        Tuple of (md_path, json_path)
    """
    _ensure_output_dir()
    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    generated_at = datetime.now(tz=timezone.utc).isoformat()

    # Markdown
    md_path = os.path.join(config.OUTPUT_DIR, f"master_{today}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Slack Intelligence — Master Report ({today})\n\n")
        f.write(f"_Generated: {generated_at}_  \n")
        channels_str = ", ".join(f"#{c}" for c in channel_names)
        f.write(f"_Channels: {channels_str}_\n\n")
        f.write("---\n\n")
        f.write(analysis)
    print(f"  Wrote {md_path}")

    # JSON
    json_path = os.path.join(config.OUTPUT_DIR, f"master_{today}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "channel": "master",
            "generated_at": generated_at,
            "channels": channel_names,
            "analysis": analysis,
        }, f, indent=2, ensure_ascii=False)
    print(f"  Wrote {json_path}")

    return md_path, json_path



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

if __name__ == "__main__":
    # Smoke test: write dummy reports to verify output directory and file creation
    test_analysis = """## Historical Context
This is a test report.

## Key Points
- Point one
- Point two

## Future Plans
- Plan one

## Identified Gaps
- Gap one

## Recommendations
- Do the thing
"""
    ch_md, ch_json = save_channel_report("test-channel", test_analysis, 42)
    master_md, master_json = save_master_report(
        "## Cross-Channel Themes\nTest master.", ["test-channel", "general"]
    )

    # Verify files are valid
    import json as _json
    with open(ch_json) as f:
        d = _json.load(f)
    assert d["channel"] == "test-channel"
    assert d["message_count"] == 42
    assert "Historical Context" in d["analysis"]
    print(f"\n✓ exporter smoke test passed")
    print(f"  channel report: {ch_md}, {ch_json}")
    print(f"  master report:  {master_md}, {master_json}")
