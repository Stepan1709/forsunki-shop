"""Столбчатые графики в виде SVG, которые рисует сервер: без JavaScript и без внешних библиотек.

Функции возвращают готовую строку SVG для вставки в шаблон (`| safe`). Все подписи проходят через `html.escape`,
числа форматируются здесь же, поэтому пользовательских данных в разметку не попадает. Цвета берутся из `currentColor`
и CSS-переменных страницы, значит график одинаково читается в светлой и тёмной темах.
"""

from collections.abc import Sequence
from html import escape

NBSP = " "

# Размеры области рисования в условных единицах viewBox: график масштабируется под ширину контейнера.
WIDTH = 720
HEIGHT = 240
LEFT, RIGHT, TOP, BOTTOM = 56, 12, 12, 34


def nice_ceiling(value: float) -> int:
    """Округляет максимум вверх до «круглого» числа для оси: 7 -> 8, 130 -> 150, 4200 -> 5000."""
    if value <= 4:
        return 4  # на пустом графике ось всё равно рисуется (0..4)
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        candidate = step * magnitude
        if candidate >= value:
            return int(candidate)
    return int(10 * magnitude)


def format_number(value: float) -> str:
    return f"{round(value):,}".replace(",", NBSP)


def bar_chart(
    labels: Sequence[str],
    values: Sequence[int],
    *,
    title: str,
    unit: str = "",
    value_format: str = "number",
) -> str:
    """Столбики по периодам. `labels` и `values` одной длины; подпись оси показывается не у каждого столбика.

    value_format="money": значения в копейках, на оси и во всплывающей подсказке показываются рубли.
    """
    if len(labels) != len(values):
        raise ValueError("labels и values должны быть одной длины")

    def show(v: float) -> str:
        if value_format == "money":
            return format_number(v / 100) + f"{NBSP}₽"
        return format_number(v) + (f"{NBSP}{unit}" if unit else "")

    count = len(values)
    peak = nice_ceiling(max(values, default=0))
    plot_w = WIDTH - LEFT - RIGHT
    plot_h = HEIGHT - TOP - BOTTOM
    slot = plot_w / max(count, 1)
    bar_w = max(min(slot * 0.7, 40), 1)

    parts: list[str] = [
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{escape(title)}" '
        'class="h-auto w-full text-slate-500 dark:text-slate-400" xmlns="http://www.w3.org/2000/svg">',
        f"<title>{escape(title)}</title>",
    ]

    # Горизонтальные линии и подписи оси Y: 0, 1/4, 1/2, 3/4 и максимум.
    for step in range(5):
        value = peak * step / 4
        y = TOP + plot_h - plot_h * step / 4
        parts.append(
            f'<line x1="{LEFT}" y1="{y:.1f}" x2="{WIDTH - RIGHT}" y2="{y:.1f}" '
            'stroke="currentColor" stroke-opacity="0.2" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{LEFT - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="11" '
            f'fill="currentColor">{escape(_axis_label(value, value_format))}</text>'
        )

    # Подпись оси X: не чаще чем раз в несколько столбиков, иначе при 90 днях она слипается.
    every = max(1, -(-count // 12))
    for i, (label, value) in enumerate(zip(labels, values, strict=True)):
        height = plot_h * value / peak if peak else 0
        x = LEFT + slot * i + (slot - bar_w) / 2
        y = TOP + plot_h - height
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(height, 0):.1f}" rx="2" '
            f'class="fill-brand-600" fill="#e0762b"><title>{escape(label)}: {escape(show(value))}</title></rect>'
        )
        if i % every == 0:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{HEIGHT - 12}" text-anchor="middle" font-size="11" '
                f'fill="currentColor">{escape(label)}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _axis_label(value: float, value_format: str) -> str:
    if value_format == "money":
        rubles = value / 100
        return _compact(rubles) + f"{NBSP}₽"
    return _compact(value)


def _compact(value: float) -> str:
    """Короткая подпись оси: 1500 -> 1,5 тыс.; 2 000 000 -> 2 млн."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:g}{NBSP}млн".replace(".", ",")
    if value >= 10_000:
        return f"{value / 1000:g}{NBSP}тыс.".replace(".", ",")
    return format_number(value)
