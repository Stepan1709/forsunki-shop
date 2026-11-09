"""Outbox и inbox для надёжного обмена сообщениями.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-09
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("routing_key", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
    )
    op.create_index(
        "ix_outbox_unsent",
        "outbox",
        ["created_at"],
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.create_table(
        "processed_messages",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("consumer", sa.String(length=100), nullable=False),
        sa.Column(
            "processed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("message_id", "consumer", name=op.f("pk_processed_messages")),
    )


def downgrade() -> None:
    op.drop_table("processed_messages")
    op.drop_index("ix_outbox_unsent", table_name="outbox")
    op.drop_table("outbox")
