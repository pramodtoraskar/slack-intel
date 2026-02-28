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
