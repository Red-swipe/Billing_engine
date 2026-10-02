"""Scope usage-event idempotency to each tenant.

Revision ID: 20261002_0002
Revises: 20260930_0001
"""

from alembic import op


revision = "20261002_0002"
down_revision = "20260930_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("usage_events") as batch_op:
        batch_op.drop_constraint(
            "uq_usage_events_idempotency_key", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_usage_events_tenant_idempotency_key",
            ["tenant_id", "idempotency_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("usage_events") as batch_op:
        batch_op.drop_constraint(
            "uq_usage_events_tenant_idempotency_key", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_usage_events_idempotency_key", ["idempotency_key"]
        )
