"""Страница «О нас»: редактируемые тексты и частые вопросы.

Таблицы site_pages (блоки текста) и faq_items (вопросы с порядком). Начальные вопросы добавляются
здесь же, чтобы страница не была пустой после первого запуска; администратор потом правит их сам.
Тексты блоков не вставляются: пока строки нет, сайт показывает текст по умолчанию из кода.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

INITIAL_FAQ = [
    (
        "Чем новая форсунка отличается от восстановленной?",
        "Новая форсунка произведена заводом и продаётся в заводской упаковке. Восстановленная разобрана, "
        "дефектована, в ней заменены изношенные детали, после чего она проверена на стенде. "
        "Восстановленная стоит дешевле, состояние указано в карточке товара.",
    ),
    (
        "Как узнать, подойдёт ли форсунка к моему двигателю?",
        "В магазине работает поиск по артикулу производителя (пробелы и дефисы не важны) и по названию "
        "двигателя. В карточке товара есть список двигателей, к которым подходит форсунка. "
        "Если не уверены, напишите или позвоните, мы подскажем.",
    ),
    (
        "Как оплатить заказ?",
        "После оформления заказа вы переходите на страницу оплаты. Если заказ не оплачен в течение "
        "60 минут, он закрывается, а товар возвращается на склад.",
    ),
    (
        "Что такое электромагнитная и пьезофорсунка?",
        "Это два вида привода клапана форсунки. Электромагнитный привод проще и распространён шире, "
        "пьезоэлектрический быстрее и точнее дозирует топливо. Привод указан в карточке товара.",
    ),
    (
        "Вы ремонтируете ТНВД?",
        "Да. Мы ремонтируем всю топливную аппаратуру дизельных двигателей: форсунки Common Rail, "
        "насос-форсунки и ТНВД (топливные насосы высокого давления).",
    ),
    (
        "Сколько стоит ремонт и как долго он продолжается?",
        "Примерные цены есть на странице «Ремонт». Точную стоимость и срок называет мастер после "
        "диагностики на стенде, ремонт начинается только после вашего согласия.",
    ),
    (
        "Как отправить форсунки или ТНВД на ремонт?",
        "Оставьте заявку на странице «Ремонт»: мастер свяжется с вами, подскажет, как привезти или "
        "отправить детали, и согласует стоимость.",
    ),
]


def upgrade() -> None:
    op.create_table(
        "site_pages",
        sa.Column("slug", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("slug"),
    )
    faq = op.create_table(
        "faq_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("question", sa.String(length=300), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_faq_items_position", "faq_items", ["position"])
    op.bulk_insert(
        faq,
        [
            {"question": q, "answer": a, "position": n}
            for n, (q, a) in enumerate(INITIAL_FAQ, start=1)
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_faq_items_position", table_name="faq_items")
    op.drop_table("faq_items")
    op.drop_table("site_pages")
