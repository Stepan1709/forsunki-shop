"""Заявки на ремонт.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

REPAIR_STATUSES = ("NEW", "IN_PROGRESS", "DONE", "REJECTED")


def upgrade() -> None:
    op.create_table(
        "repair_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.Enum(*REPAIR_STATUSES, name="repair_status"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("vehicle", sa.String(length=200), nullable=False),
        sa.Column("injector", sa.String(length=200), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repair_requests")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_repair_requests_idempotency_key")),
    )


def downgrade() -> None:
    op.drop_table("repair_requests")
    op.execute("DROP TYPE repair_status")
