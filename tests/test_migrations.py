"""Focused verification that Alembic, not create_all, can build the schema."""

import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker


ROOT = Path(__file__).resolve().parents[1]


def upgrade_database(db_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")


def test_fresh_database_is_created_by_alembic_and_has_required_schema(db_dir, monkeypatch):
    db_path = db_dir / "fresh.db"
    upgrade_database(db_path, monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        database_inspector = inspect(engine)
        expected_tables = {
            "plans",
            "tenants",
            "subscriptions",
            "usage_events",
            "stripe_events",
        }
        assert expected_tables.issubset(database_inspector.get_table_names())

        usage_columns = {
            column["name"] for column in database_inspector.get_columns("usage_events")
        }
        assert {
            "tenant_id",
            "created_at",
            "response_status_code",
        }.issubset(usage_columns)

        stripe_columns = {
            column["name"] for column in database_inspector.get_columns("stripe_events")
        }
        assert {"stripe_event_id", "processed_at"}.issubset(stripe_columns)

        indexes = {
            index["name"]: index["column_names"]
            for index in database_inspector.get_indexes("usage_events")
        }
        assert indexes["ix_usage_events_tenant_created_at"] == [
            "tenant_id",
            "created_at",
        ]
        unique_constraints = {
            constraint["name"]: constraint["column_names"]
            for constraint in database_inspector.get_unique_constraints("usage_events")
        }
        assert unique_constraints == {
            "uq_usage_events_tenant_idempotency_key": [
                "tenant_id",
                "idempotency_key",
            ]
        }
    finally:
        engine.dispose()


def test_application_can_use_schema_created_by_alembic(db_dir, monkeypatch):
    db_path = db_dir / "application.db"
    upgrade_database(db_path, monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    from app.models import Plan

    with TestingSession() as session:
        session.add(
            Plan(
                name="Free",
                api_calls_limit=1_000,
                tokens_limit=100_000,
                price_cents=0,
            )
        )
        session.commit()

    from app.database import get_db
    from app.main import app

    def override_get_db():
        with TestingSession() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            response = client.post(
                "/tenants",
                json={
                    "name": "Migrated Tenant",
                    "email": f"{uuid.uuid4().hex}@example.com",
                },
            )
        assert response.status_code == 201, response.text
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
