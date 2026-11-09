import enum
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class InjectorType(enum.StrEnum):
    COMMON_RAIL = "common_rail"
    PUMP_INJECTOR = "pump_injector"  # насос-форсунка


class InjectorCondition(enum.StrEnum):
    NEW = "new"
    REMANUFACTURED = "remanufactured"  # восстановленная


def _enum(enum_cls: type[enum.StrEnum], name: str) -> Enum:
    # values_callable: в БД хранятся значения ('common_rail'), а не имена ('COMMON_RAIL').
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


# Связь «многие ко многим»: одна форсунка подходит к нескольким двигателям и наоборот.
# Составной первичный ключ не даёт записать одну и ту же пару дважды.
injector_engines = Table(
    "injector_engines",
    Base.metadata,
    Column("injector_id", ForeignKey("injectors.id", ondelete="CASCADE"), primary_key=True),
    Column("engine_id", ForeignKey("engines.id", ondelete="CASCADE"), primary_key=True),
    # Обратный поиск «какие форсунки подходят к двигателю X» идёт по engine_id.
    Index("ix_injector_engines_engine_id", "engine_id"),
)


class Manufacturer(Base):
    __tablename__ = "manufacturers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True)  # для URL и фильтра: "bosch"

    def __str__(self) -> str:  # так запись называется в выпадающих списках админки
        return self.name


class Engine(Base):
    __tablename__ = "engines"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Полное название для поиска пользователем: "Mercedes-Benz OM651 2.1 CDI".
    name: Mapped[str] = mapped_column(String(200), unique=True)

    __table_args__ = (
        # Триграммный индекс ускоряет поиск подстроки (ILIKE '%om651%'), который обычный B-tree не умеет.
        Index(
            "ix_engines_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    def __str__(self) -> str:
        return self.name


class Injector(Base):
    __tablename__ = "injectors"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Артикул как его пишут люди: "0 445 110 183". Показываем его.
    oem_number: Mapped[str] = mapped_column(String(64))
    # Тот же артикул без пробелов/дефисов в верхнем регистре: "0445110183". По нему ищем, чтобы запрос
    # "0445-110 183" находил товар. Заполняется в сервисе при сохранении (см. normalize_oem).
    oem_normalized: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    manufacturer_id: Mapped[int] = mapped_column(
        ForeignKey("manufacturers.id", ondelete="RESTRICT")
    )
    type: Mapped[InjectorType] = mapped_column(_enum(InjectorType, "injector_type"))
    condition: Mapped[InjectorCondition] = mapped_column(
        _enum(InjectorCondition, "injector_condition")
    )
    # Деньги только целыми копейками: float даёт ошибки округления (0.1 + 0.2 != 0.3).
    price_kopecks: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    image_key: Mapped[str | None] = mapped_column(String(255), default=None)  # ключ объекта в S3
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    manufacturer: Mapped[Manufacturer] = relationship(lazy="joined")
    # selectin: одним дополнительным запросом на всю страницу, а не по запросу на каждую форсунку (N+1).
    engines: Mapped[list[Engine]] = relationship(
        secondary=injector_engines, lazy="selectin", order_by=Engine.name
    )

    __table_args__ = (
        # Один и тот же артикул может быть и новым, и восстановленным: это разные товары.
        UniqueConstraint("oem_normalized", "condition", name="uq_injectors_oem_condition"),
        # Защита на уровне БД: даже ошибка в коде не запишет отрицательный остаток или цену.
        CheckConstraint("stock >= 0", name="stock_non_negative"),
        CheckConstraint("price_kopecks >= 0", name="price_non_negative"),
        Index("ix_injectors_manufacturer_id", "manufacturer_id"),
        Index("ix_injectors_type", "type"),
        Index("ix_injectors_condition", "condition"),
        Index(
            "ix_injectors_oem_normalized_trgm",
            "oem_normalized",
            postgresql_using="gin",
            postgresql_ops={"oem_normalized": "gin_trgm_ops"},
        ),
    )

    def __str__(self) -> str:
        return f"{self.oem_number} ({self.title})"
