"""Central configuration. Secrets come from .env only - never hardcode keys."""
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "reconciliation.db"
DOCS_DIR = DATA_DIR / "documents"

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")


def llm_enabled() -> bool:
    return bool(OPENROUTER_API_KEY)


def _d(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default))


@dataclass(frozen=True)
class Tolerances:
    price_pct: Decimal = field(default_factory=lambda: _d("TOL_PRICE_PCT", "0.5"))   # % vs PO unit price
    total_abs: Decimal = field(default_factory=lambda: _d("TOL_TOTAL_ABS", "1"))     # INR, invoice totals
    tax_abs: Decimal = field(default_factory=lambda: _d("TOL_TAX_ABS", "0.05"))      # INR, per tax component per line
    line_abs: Decimal = field(default_factory=lambda: _d("TOL_LINE_ABS", "0.05"))    # INR, line arithmetic
    dup_amount_pct: Decimal = Decimal("1")      # potential duplicate: amount within 1%
    dup_days: int = 3                           # potential duplicate: dates within 3 days
    dup_number_similarity: float = 0.8          # potential duplicate: invoice-no similarity (ratio, not money)
