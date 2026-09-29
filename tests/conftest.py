"""Shared test fixtures.

Every test gets its own on-disk SQLite file and its own app instance, so tests
cannot leak rows into each other or into the development database.

Note on isolation: this suite does NOT use pytest's tmp_path fixture. On this
machine the default pytest base temp directory is access-denied, which makes
every test that requests tmp_path error at setup before any test code runs.
Using an explicit per-test directory under the system temp dir avoids that
entirely and keeps the suite self-contained.
"""

import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

TEST_TENANT_EMAIL = "test@example.com"


@pytest.fixture
def db_dir():
    """A unique writable directory for this test's database."""
    path = Path(tempfile.gettempdir()) / "billing_engine_tests" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    yield path
    # Best-effort cleanup; a locked file must not fail the suite.
    for child in path.glob("*"):
        try:
            child.unlink()
        except OSError:
            pass
    try:
        path.rmdir()
    except OSError:
        pass


@pytest.fixture
def db_engine(db_dir):
    """A throwaway engine per test."""
    engine = create_engine(
        f"sqlite:///{db_dir / 'test.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    yield engine
    engine.dispose()


@pytest.fixture
def client(db_engine):
    """A TestClient bound to the throwaway database, with plans seeded."""
    from app.database import Base, get_db
    from app.main import app
    from app.models import Plan

    Base.metadata.create_all(db_engine)
    TestingSession = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)

    with TestingSession() as session:
        session.add(
            Plan(name="Free", api_calls_limit=1_000, tokens_limit=100_000, price_cents=0)
        )
        session.add(
            Plan(
                name="Pro",
                api_calls_limit=50_000,
                tokens_limit=5_000_000,
                price_cents=2_000,
            )
        )
        session.commit()

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture
def session(db_engine):
    """Direct ORM access to the same throwaway DB, for arranging state."""
    from app.database import Base

    Base.metadata.create_all(db_engine)
    TestingSession = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    with TestingSession() as s:
        yield s


def _install_settings(monkeypatch, **overrides):
    """Install a modified copy of the app settings into every module that reads it.

    `settings` is a frozen dataclass instance, so it cannot be mutated in place.
    Modules do `from app.config import settings`, binding the object directly, so
    patching `app.config.settings` alone would not reach them. We therefore
    rebind the name in each consumer module. monkeypatch reverts all of it.
    """
    from dataclasses import replace

    from app import config as config_module
    from app.routes import checkout as checkout_module
    from app.services import stripe_service as stripe_module

    new_settings = replace(config_module.settings, **overrides)
    for module in (stripe_module, checkout_module):
        monkeypatch.setattr(module, "settings", new_settings, raising=False)
    return new_settings


@pytest.fixture
def stripe_settings(monkeypatch):
    """Fake-but-well-formed Stripe test values.

    No network call is made with these: Stripe API calls are mocked in the
    Stage 3 tests, so the suite needs no real credentials and no network.
    """
    return _install_settings(
        monkeypatch,
        STRIPE_SECRET_KEY="sk_test_FAKE_FOR_UNIT_TESTS",
        STRIPE_WEBHOOK_SECRET="whsec_FAKE_FOR_UNIT_TESTS",
        STRIPE_PRO_PRICE_ID="price_FAKE_FOR_UNIT_TESTS",
    )


@pytest.fixture
def no_stripe_config(monkeypatch):
    """Settings with no Stripe credentials, to exercise the unconfigured path."""
    return _install_settings(
        monkeypatch, STRIPE_SECRET_KEY="", STRIPE_WEBHOOK_SECRET="", STRIPE_PRO_PRICE_ID=""
    )


def make_tenant(client, name="Test", email=None):
    """Create a tenant through the real API so its subscription is real."""
    email = email or f"{uuid.uuid4().hex}@example.com"
    resp = client.post("/tenants", json={"name": name, "email": email})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], email


def key():
    return f"key-{uuid.uuid4().hex}"
