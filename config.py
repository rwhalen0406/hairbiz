import os

from dotenv import load_dotenv

load_dotenv()

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

FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "hairbiz.db")

SQUARE_API_BASE = (
    "https://connect.squareupsandbox.com"
    if SQUARE_ENVIRONMENT == "sandbox"
    else "https://connect.squareup.com"
)
