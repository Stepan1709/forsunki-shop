"""Outbox: контекст трассы события (traceparent).

Событие уходит из БД позже и другим процессом (relay), поэтому контекст трассы запроса, который его породил, хранится в строке
outbox и кладётся в заголовок сообщения при публикации.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox", sa.Column("traceparent", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox", "traceparent")
