"""Shared test fixtures.

Every test gets its own on-disk SQLite file and its own app instance, so tests
cannot leak rows into each other or into the development database.

Note on isolation: this suite does NOT use pytest's tmp_path fixture. On this
machine the default pytest base temp directory is access-denied, which makes
every test that requests tmp_path error at setup before any test code runs.
Using an explicit per-test directory under the system temp dir avoids that
entirely and keeps the suite self-contained.
"""

import os
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


def make_tenant(client, name="Test", email=None):
    """Create a tenant through the real API so its subscription is real."""
    email = email or f"{uuid.uuid4().hex}@example.com"
    resp = client.post("/tenants", json={"name": name, "email": email})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], email


def key():
    return f"key-{uuid.uuid4().hex}"
