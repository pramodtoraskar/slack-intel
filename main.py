# main.py — Orchestrator: fetch → analyze → export.
# Usage:
#   python main.py            # fetch last 90 days + analyze + export
#   python main.py --backfill # fetch full history (resumable via checkpoint)

import sys
import sqlite3
import requests
import config
import fetcher
import analyzer
import exporter


# ── Health check ──────────────────────────────────────────────────────────────

def health_check():
    """Verify all prerequisites before starting any work.

    Checks:
      - Ollama is reachable and the configured model is available
      - slack_sdk is installed
      - ./data/ directory is writable
      - SLACK_TOKEN is set in config.py
      - TARGET_CHANNELS is set in config.py

    Exits with code 1 and clear instructions if any check fails.
    """
    errors = []

    # Ollama ping
    try:
        resp = requests.get(f"{config.OLLAMA_BASE}/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        model_found = any(config.CHAT_MODEL in m for m in models)
        if model_found:
            print(f"  ✓ Ollama running — {config.CHAT_MODEL} available")
        else:
            print(f"  ✓ Ollama running")
            print(f"  ⚠ WARNING: '{config.CHAT_MODEL}' not found in pulled models: {models}")
            print(f"    Run: ollama pull {config.CHAT_MODEL}")
    except Exception as e:
        errors.append(
            f"Ollama not reachable at {config.OLLAMA_BASE}: {e}\n"
            f"    → Start Ollama: ollama serve"
        )

    # slack_sdk
    try:
        import slack_sdk
        print(f"  ✓ slack_sdk {slack_sdk.__version__} installed")
    except ImportError:
        errors.append("slack_sdk not installed\n    → Run: pip install slack_sdk")

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

    # Config validation
    if not config.SLACK_TOKEN:
        errors.append(
            "SLACK_TOKEN is empty\n"
            "    → Open config.py and set SLACK_TOKEN = 'xoxb-...'"
        )
    else:
        masked = config.SLACK_TOKEN[:8] + "..." if len(config.SLACK_TOKEN) > 8 else "***"
        print(f"  ✓ SLACK_TOKEN set ({masked})")

    if not config.TARGET_CHANNELS:
        errors.append(
            "TARGET_CHANNELS is empty\n"
            "    → Open config.py and set TARGET_CHANNELS = ['channel1', 'channel2', ...]"
        )
    else:
        print(f"  ✓ TARGET_CHANNELS: {config.TARGET_CHANNELS}")

    if errors:
        print("\n❌ Health check failed — fix the issues above before running:\n")
        for i, err in enumerate(errors, 1):
            print(f"   {i}. {err}")
        sys.exit(1)

    print("\n  ✓ All health checks passed\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    backfill = "--backfill" in sys.argv

    print("=" * 60)
    print("  SLACK INTELLIGENCE AGENT")
    print("=" * 60)

    # Step 1: Health check
    print("\n[1/4] Health check …")
    health_check()

    # Step 2: Fetch
    mode_label = "BACKFILL (full history)" if backfill else f"RECENT ({config.DAYS_BACK_DEFAULT} days)"
    print(f"\n[2/4] Fetching Slack data [{mode_label}] …")
    fetcher.run_fetch(backfill=backfill)

    # Step 3: Analyze
    print("\n[3/4] Analyzing channels …")
    analyses = analyzer.analyze_all()

    if not analyses:
        print("\n  No channels required re-analysis.")
        print("  (Either no data has been fetched yet, or all channels are up-to-date.)")
        print("\n  Tip: if this is your first run, check that SLACK_TOKEN and")
        print("  TARGET_CHANNELS are set in config.py, then run again.")
        print("\n✓ Done.\n")
        return

    # Step 4: Export per-channel reports
    print(f"\n[4/4] Exporting {len(analyses)} channel report(s) …")
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        for ch_name, analysis in analyses.items():
            count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE channel_name=?", (ch_name,)
            ).fetchone()[0]
            exporter.save_channel_report(ch_name, analysis, count)
    finally:
        conn.close()

    # Build and export master report
    print("\n  Building master report …")
    master = analyzer.build_master(analyses)
    exporter.save_master_report(master, list(analyses.keys()))

    print("\n" + "=" * 60)
    print("  ✓ Done!")
    print(f"  Reports in: {config.OUTPUT_DIR}/")
    print("  View dashboard: python dashboard.py → http://localhost:8080")
    print("  Query your data: python query.py")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
