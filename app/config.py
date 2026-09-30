import os
from dataclasses import dataclass
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
)
