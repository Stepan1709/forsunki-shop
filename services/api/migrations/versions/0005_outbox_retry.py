"""Outbox: отложенные повторы для событий без получателя.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column(
        "outbox",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column("outbox", sa.Column("last_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox", "last_error")
    op.drop_column("outbox", "next_attempt_at")
    op.drop_column("outbox", "attempts")
