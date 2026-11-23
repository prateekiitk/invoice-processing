"""Central configuration: AP policy knobs + runtime settings.

Every threshold the decision engine uses lives here so the policy is reviewable
in one place (and explainable in a demo: "this is the rule that fired").
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SAMPLES_DIR = ROOT / "samples"
RUNTIME_DIR = Path(os.getenv("AP_RUNTIME_DIR", ROOT / "runtime"))
UPLOADS_DIR = RUNTIME_DIR / "uploads"
DB_PATH = RUNTIME_DIR / "ap_autopilot.db"


def _load_dotenv() -> None:
    """Tiny .env loader so setup needs no extra dependency."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


@dataclass(frozen=True)
class Buyer:
    name: str = "Halcyon Foods Pvt Ltd"
    gstin: str = "29AAHCH4821K1ZA"

    @property
    def state_code(self) -> str:
        return self.gstin[:2]


@dataclass(frozen=True)
class Policy:
    # --- Matching tolerances -------------------------------------------------
    unit_price_tolerance_pct: float = 2.0       # invoice unit price may exceed PO by <= 2%
    amount_tolerance_abs: float = 1000.0        # ...and the resulting line overcharge must be <= Rs 1,000
    rounding_tolerance: float = 1.00            # arithmetic slack for paise rounding
    tax_rate_tolerance_pct: float = 0.25        # tax charged vs expected rate (percentage points)

    # --- Approval matrix -----------------------------------------------------
    auto_approve_limit: float = 1_000_000.0     # invoices above Rs 10 lakh always need a human approver
    auto_approve_inferred_po: bool = False      # PO inferred (not quoted on invoice) => reviewer confirms

    # --- Duplicate detection -------------------------------------------------
    near_duplicate_window_days: int = 30        # same vendor + same amount within N days => suspected dup

    # --- Document sanity -----------------------------------------------------
    max_invoice_age_days: int = 365

    # --- PO inference --------------------------------------------------------
    po_inference_min_score: float = 0.70
    po_inference_min_margin: float = 0.15

    # --- Scanned invoices ----------------------------------------------------
    ocr_min_confidence: float = 0.80            # any OCR line below this => a person checks the reading


@dataclass(frozen=True)
class Settings:
    # Small pause between stages so a human can follow the live run view. 0 disables it.
    stage_delay_ms: int = field(default_factory=lambda: int(os.getenv("STAGE_DELAY_MS", "350")))


BUYER = Buyer()
POLICY = Policy()


def settings() -> Settings:
    return Settings()


def ensure_dirs() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
