"""Базовая ревизия без таблиц.

Нужна, чтобы цепочка миграций и контейнер `migrate` работали с самого начала.
Первые таблицы добавляют следующие миграции.

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
