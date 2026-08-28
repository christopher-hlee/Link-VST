"""Environment configuration for the restock monitor."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "monitor.db"

# Auth
PASSWORD_HASH = os.environ.get("MONITOR_PASSWORD_HASH", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
API_KEY = os.environ.get("MONITOR_API_KEY", "")
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days
COOKIE_NAME = "monitor_session"

# Notifications
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "")

# Second channel for alert_level="critical". Free, and its high-priority
# notifications pierce Do Not Disturb, which Telegram's do not.
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Strategy credentials
TARGET_API_KEY = os.environ.get(
    "TARGET_API_KEY", "9f36aeafbe60771e321a7cc95a78140772ab3e96"
)
BESTBUY_API_KEY = os.environ.get("BESTBUY_API_KEY", "")

# Scheduler
TICK_SECONDS = int(os.environ.get("TICK_SECONDS", "15"))

# Polling defaults (seconds)
INTERVAL_SLOW = 900
INTERVAL_BASE = 300
INTERVAL_HOT = 45
JITTER_FRACTION = 0.20

# Optional Cloudflare Worker egress proxy — see worker/README.md. Only hosts
# listed in PROXY_HOSTS are routed through it; everything else goes direct.
PROXY_URL = os.environ.get("PROXY_URL", "").rstrip("/")
PROXY_KEY = os.environ.get("PROXY_KEY", "")
PROXY_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("PROXY_HOSTS", "").split(",")
    if h.strip()
}

# Politeness
PER_DOMAIN_CONCURRENCY = 2
PER_DOMAIN_MIN_GAP = 1.0     # seconds between requests to the same host
REQUEST_TIMEOUT = 20.0
MAX_CONCURRENT_CHECKS = 8

# A watch that fails this many times in a row raises a watch_failing event.
FAILURE_ALERT_THRESHOLD = 5

# Don't re-alert the same watch more often than this.
ALERT_COOLDOWN_SECONDS = 600

# Rows of check history kept per watch.
CHECK_HISTORY_LIMIT = 500

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
