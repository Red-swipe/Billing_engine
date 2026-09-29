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
    APP_BASE_URL: str


settings = Settings(
    DATABASE_URL=os.getenv("DATABASE_URL", "sqlite:///./billing.db"),
    STRIPE_SECRET_KEY=os.getenv("STRIPE_SECRET_KEY", ""),
    STRIPE_WEBHOOK_SECRET=os.getenv("STRIPE_WEBHOOK_SECRET", ""),
    APP_BASE_URL=os.getenv("APP_BASE_URL", "http://localhost:8000"),
)
