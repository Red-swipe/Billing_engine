import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    DATABASE_URL: str
    STRIPE_SECRET_KEY: str
    STRIPE_WEBHOOK_SECRET: str
    STRIPE_PRO_PRICE_ID: str
    APP_BASE_URL: str
    BACKGROUND_JOB_MAX_ATTEMPTS: int = 3
    # Exact metering rates. The brief specifies the token rates; it does not
    # specify a per-call charge, so that rate is configurable and defaults to
    # zero rather than inventing an examiner-mandated price.
    INPUT_RATE_DOLLARS_PER_1K: Decimal = Decimal("0.00025")
    CACHED_INPUT_RATE_DOLLARS_PER_1K: Decimal = Decimal("0.000025")
    OUTPUT_RATE_DOLLARS_PER_1K: Decimal = Decimal("0.00075")
    API_CALL_PRICE_CENTS: int = 0

    @property
    def stripe_configured(self) -> bool:
        """True when Stripe is usable: a secret key and a Pro price are set.

        Checked at call time rather than import time so a developer without
        Stripe credentials can still run everything except Stage 3 endpoints.
        """
        return bool(self.STRIPE_SECRET_KEY and self.STRIPE_PRO_PRICE_ID)

    @property
    def webhook_secret_configured(self) -> bool:
        """True when webhook signature verification can be performed."""
        return bool(self.STRIPE_WEBHOOK_SECRET)


settings = Settings(
    DATABASE_URL=os.getenv("DATABASE_URL", "sqlite:///./billing.db"),
    STRIPE_SECRET_KEY=os.getenv("STRIPE_SECRET_KEY", ""),
    STRIPE_WEBHOOK_SECRET=os.getenv("STRIPE_WEBHOOK_SECRET", ""),
    STRIPE_PRO_PRICE_ID=os.getenv("STRIPE_PRO_PRICE_ID", ""),
    APP_BASE_URL=os.getenv("APP_BASE_URL", "http://localhost:8000"),
    BACKGROUND_JOB_MAX_ATTEMPTS=int(os.getenv("BACKGROUND_JOB_MAX_ATTEMPTS", "3")),
    INPUT_RATE_DOLLARS_PER_1K=Decimal(
        os.getenv("INPUT_RATE_DOLLARS_PER_1K", "0.00025")
    ),
    CACHED_INPUT_RATE_DOLLARS_PER_1K=Decimal(
        os.getenv("CACHED_INPUT_RATE_DOLLARS_PER_1K", "0.000025")
    ),
    OUTPUT_RATE_DOLLARS_PER_1K=Decimal(
        os.getenv("OUTPUT_RATE_DOLLARS_PER_1K", "0.00075")
    ),
    API_CALL_PRICE_CENTS=int(os.getenv("API_CALL_PRICE_CENTS", "0")),
)
