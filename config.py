import os
import secrets

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "hairbiz.db")

SQUARE_ACCESS_TOKEN = os.environ.get("SQUARE_ACCESS_TOKEN", "")
SQUARE_ENVIRONMENT = os.environ.get("SQUARE_ENVIRONMENT", "sandbox").strip().lower()
SQUARE_VERSION = os.environ.get("SQUARE_VERSION", "2024-01-18")
SQUARE_LOCATION_ID = os.environ.get("SQUARE_LOCATION_ID", "").strip()
SYNC_LOOKBACK_DAYS = int(os.environ.get("SYNC_LOOKBACK_DAYS", "730"))
SYNC_BOOKINGS = os.environ.get("SYNC_BOOKINGS", "true").strip().lower() in ("1", "true", "yes")

# Comma-separated, case-insensitive substrings. Any sold line item whose name contains
# one of these is excluded from service performance/trend/bundling analysis (e.g. retail
# product sales rung up alongside styling services). Leave blank to disable filtering.
EXCLUDED_ITEM_KEYWORDS = [
    k.strip().lower()
    for k in os.environ.get(
        "EXCLUDED_ITEM_KEYWORDS", "cream,hairspray,smoother,oil,shampoo"
    ).split(",")
    if k.strip()
]


def _get_or_create_secret_key():
    """Uses FLASK_SECRET_KEY if set; otherwise generates and persists a random one
    locally, so sessions survive a restart without ever committing a real secret to
    the repo (the previous hardcoded fallback was a known, public value)."""
    env_key = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    os.makedirs(DATA_DIR, exist_ok=True)
    key_path = os.path.join(DATA_DIR, ".flask_secret_key")
    if os.path.exists(key_path):
        with open(key_path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(key_path, "w") as f:
        f.write(key)
    return key


FLASK_SECRET_KEY = _get_or_create_secret_key()

SQUARE_API_BASE = (
    "https://connect.squareupsandbox.com"
    if SQUARE_ENVIRONMENT == "sandbox"
    else "https://connect.squareup.com"
)
