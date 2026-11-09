"""Каталог: производители, двигатели, форсунки, применимость.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Расширение с триграммным поиском. Нужны права суперпользователя БД, в нашем Postgres они есть.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "manufacturers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_manufacturers")),
        sa.UniqueConstraint("name", name=op.f("uq_manufacturers_name")),
        sa.UniqueConstraint("slug", name=op.f("uq_manufacturers_slug")),
    )
    op.create_table(
        "engines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_engines")),
        sa.UniqueConstraint("name", name=op.f("uq_engines_name")),
    )
    op.create_index(
        "ix_engines_name_trgm",
        "engines",
        ["name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_table(
        "injectors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("oem_number", sa.String(length=64), nullable=False),
        sa.Column("oem_normalized", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("manufacturer_id", sa.Integer(), nullable=False),
        sa.Column(
            "type", sa.Enum("common_rail", "pump_injector", name="injector_type"), nullable=False
        ),
        sa.Column(
            "condition",
            sa.Enum("new", "remanufactured", name="injector_condition"),
            nullable=False,
        ),
        sa.Column("price_kopecks", sa.Integer(), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False),
        sa.Column("image_key", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("stock >= 0", name=op.f("ck_injectors_stock_non_negative")),
        sa.CheckConstraint("price_kopecks >= 0", name=op.f("ck_injectors_price_non_negative")),
        sa.ForeignKeyConstraint(
            ["manufacturer_id"],
            ["manufacturers.id"],
            name=op.f("fk_injectors_manufacturer_id_manufacturers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_injectors")),
        sa.UniqueConstraint("oem_normalized", "condition", name="uq_injectors_oem_condition"),
    )
    op.create_index("ix_injectors_manufacturer_id", "injectors", ["manufacturer_id"])
    op.create_index("ix_injectors_type", "injectors", ["type"])
    op.create_index("ix_injectors_condition", "injectors", ["condition"])
    op.create_index(
        "ix_injectors_oem_normalized_trgm",
        "injectors",
        ["oem_normalized"],
        postgresql_using="gin",
        postgresql_ops={"oem_normalized": "gin_trgm_ops"},
    )
    op.create_table(
        "injector_engines",
        sa.Column("injector_id", sa.Integer(), nullable=False),
        sa.Column("engine_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["injector_id"],
            ["injectors.id"],
            name=op.f("fk_injector_engines_injector_id_injectors"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["engine_id"],
            ["engines.id"],
            name=op.f("fk_injector_engines_engine_id_engines"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("injector_id", "engine_id", name=op.f("pk_injector_engines")),
    )
    op.create_index("ix_injector_engines_engine_id", "injector_engines", ["engine_id"])


def downgrade() -> None:
    op.drop_index("ix_injector_engines_engine_id", table_name="injector_engines")
    op.drop_table("injector_engines")
    op.drop_table("injectors")
    op.drop_table("engines")
    op.drop_table("manufacturers")
    # Типы enum не удаляются вместе с таблицей (см. комментарий в миграции 0002).
    op.execute("DROP TYPE injector_condition")
    op.execute("DROP TYPE injector_type")
    # Расширение pg_trgm намеренно НЕ удаляем: оно общее для БД, им могут пользоваться и другие объекты.
