"""Central configuration. Everything is overridable by environment variable."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("OTC_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.getenv("OTC_DB", DATA_DIR / "otc.db"))
INBOX_DIR = DATA_DIR / "inbox"
REPORT_DIR = ROOT / "reports"

COMPANY = "Northfield Fluid Systems"          # fictional manufacturer used for the demo
PORTAL_URL = os.getenv("OTC_PORTAL_URL", "http://localhost:8501")

# --- AI extraction -------------------------------------------------------
# rules = deterministic parser only | llm = Claude first (falls back on failure) | auto = llm if key present
EXTRACTOR_MODE = os.getenv("OTC_EXTRACTOR", "auto")
LLM_MODEL = os.getenv("OTC_LLM_MODEL", "claude-sonnet-5-5")
LLM_CLARIFY_POLISH = os.getenv("OTC_LLM_POLISH", "0") == "1"

# --- Integrations: mock (default, runs offline) or live ------------------
BC_MODE = os.getenv("OTC_BC_MODE", "mock")
HUBSPOT_MODE = os.getenv("OTC_HUBSPOT_MODE", "mock")
MAGENTO_MODE = os.getenv("OTC_MAGENTO_MODE", "mock")

# --- Business rules ------------------------------------------------------
PRICE_TOLERANCE = 0.005        # 0.5% - quoted vs contract price
FUZZY_THRESHOLD = 0.80         # >= this => "did you mean?" exception, never auto-corrected
DEFAULT_LEAD_DAYS = 10         # used when the customer gives no need-by date

# --- Modeled assumptions (NOT measurements; editable in the dashboard) ---
ASSUMPTIONS = {
    "manual_min_base": 6.0,                  # rep minutes to key an order header into the ERP
    "manual_min_per_line": 1.5,              # rep minutes per order line
    "manual_min_per_customer_email": 5.0,    # one back-and-forth email per customer-facing issue
    "review_min_per_assisted_case": 4.0,     # rep minutes to review/resolve an exception case here
    "monitor_min_per_touchless_case": 0.3,   # spot-check time for an auto-processed case
}
