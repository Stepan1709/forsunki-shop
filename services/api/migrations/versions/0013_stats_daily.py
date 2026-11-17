"""Дневные итоги просмотров для страницы «Статистика».

Таблицы site_stats_daily (просмотры страниц по дням) и product_views_daily (просмотры карточек по дням
и товарам). Регистрации и покупки считаются из существующих users и orders.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_stats_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("page_views", sa.BigInteger(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("day"),
    )
    op.create_table(
        "product_views_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("injector_id", sa.Integer(), nullable=False),
        sa.Column("views", sa.BigInteger(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["injector_id"], ["injectors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("day", "injector_id"),
    )
    # Топ товаров за период группирует по товару: индекс по injector_id ускоряет и удаление строк при
    # удалении товара (внешний ключ без индекса заставил бы сканировать всю таблицу).
    op.create_index("ix_product_views_daily_injector_id", "product_views_daily", ["injector_id"])


def downgrade() -> None:
    op.drop_index("ix_product_views_daily_injector_id", table_name="product_views_daily")
    op.drop_table("product_views_daily")
    op.drop_table("site_stats_daily")
