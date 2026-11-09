"""Базовая ревизия без таблиц.

Нужна, чтобы цепочка миграций и контейнер `migrate` работали с этапа 1.
Первые таблицы появятся с модулями (users на этапе 2, outbox/inbox на этапе 4).

Revision ID: 0001
Revises:
Create Date: 2026-11-09
"""

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
