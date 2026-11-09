"""Платежи и журнал событий провайдера.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

PAYMENT_STATUSES = ("PENDING", "SUCCEEDED", "FAILED", "REFUND_REQUIRED", "REVIEW_REQUIRED")


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum(*PAYMENT_STATUSES, name="payment_status"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=100), nullable=True),
        sa.Column("pay_url", sa.Text(), nullable=True),
        sa.Column("amount_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_payments_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint("order_id", "attempt", name="uq_payments_order_attempt"),
        sa.UniqueConstraint("provider", "provider_payment_id", name="uq_payments_provider_payment"),
    )
    op.create_index(
        "uq_payments_one_success_per_order",
        "payments",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("status = 'SUCCEEDED'"),
    )
    op.create_table(
        "provider_events",
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("outcome", sa.String(length=60), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("provider", "event_id", name=op.f("pk_provider_events")),
    )


def downgrade() -> None:
    op.drop_table("provider_events")
    op.drop_index("uq_payments_one_success_per_order", table_name="payments")
    op.drop_table("payments")
    op.execute("DROP TYPE payment_status")
