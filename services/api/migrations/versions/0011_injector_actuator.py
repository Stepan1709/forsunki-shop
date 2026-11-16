"""Привод клапана форсунки: электромагнитная или пьезо.

Новый необязательный признак injectors.actuator. У существующих товаров он остаётся пустым: администратор
заполнит его сам, а в карточке пустое значение просто не показывается.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

actuator_enum = postgresql.ENUM(
    "electromagnetic", "piezo", name="injector_actuator", create_type=False
)


def upgrade() -> None:
    actuator_enum.create(op.get_bind(), checkfirst=True)
    op.add_column("injectors", sa.Column("actuator", actuator_enum, nullable=True))
    op.create_index("ix_injectors_actuator", "injectors", ["actuator"])


def downgrade() -> None:
    op.drop_index("ix_injectors_actuator", table_name="injectors")
    op.drop_column("injectors", "actuator")
    actuator_enum.drop(op.get_bind(), checkfirst=True)
