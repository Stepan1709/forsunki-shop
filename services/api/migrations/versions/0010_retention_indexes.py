"""Индексы для чистки старых строк: outbox, inbox, idempotency_keys.

Без индексов по времени каждый проход чистки сканировал бы всю таблицу. Для outbox индекс частичный (только отправленные):
неотправленных мало, и они уже покрыты ix_outbox_unsent.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-12
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_outbox_sent_at", "outbox", ["sent_at"], postgresql_where="sent_at IS NOT NULL"
    )
    op.create_index("ix_processed_messages_processed_at", "processed_messages", ["processed_at"])
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_created_at", table_name="idempotency_keys")
    op.drop_index("ix_processed_messages_processed_at", table_name="processed_messages")
    op.drop_index("ix_outbox_sent_at", table_name="outbox")
