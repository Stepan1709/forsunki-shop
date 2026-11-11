"""Рисует SVG-бейдж покрытия по файлам coverage.xml (строки и ветки всех сервисов вместе).

Запуск: `python3 scripts/coverage_badge.py services/api/coverage.xml services/web/coverage.xml ... > coverage.svg`.
Без внешних сервисов (Codecov и т.п.): CI кладёт результат в служебную ветку `badges`, а README ссылается на файл в ней.
"""

import re
import sys
from pathlib import Path

# Порог цвета: чем ниже покрытие, тем «тревожнее» бейдж.
COLORS = ((90, "#4c1"), (80, "#97ca00"), (70, "#dfb317"), (0, "#e05d44"))


def read_counts(path: Path) -> tuple[int, int]:
    """(покрыто, всего) по строкам и веткам из корневого элемента coverage.xml."""
    head = path.read_text(encoding="utf-8")[:4000]

    def attr(name: str) -> int:
        found = re.search(rf'{name}="(\d+)"', head)
        if not found:
            raise SystemExit(f"{path}: нет атрибута {name}")
        return int(found.group(1))

    covered = attr("lines-covered") + attr("branches-covered")
    total = attr("lines-valid") + attr("branches-valid")
    return covered, total


def percent(paths: list[Path]) -> float:
    covered = total = 0
    for path in paths:
        c, t = read_counts(path)
        covered += c
        total += t
    return 100 * covered / total if total else 0.0


def color_for(value: float) -> str:
    return next(color for limit, color in COLORS if value >= limit)


def badge(value: float) -> str:
    text = f"{value:.0f}%"
    label_w, value_w = 62, 8 * len(text) + 12
    width = label_w + value_w
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="20" role="img" aria-label="coverage: {text}">
<linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient>
<clipPath id="r"><rect width="{width}" height="20" rx="3" fill="#fff"/></clipPath>
<g clip-path="url(#r)"><rect width="{label_w}" height="20" fill="#555"/><rect x="{label_w}" width="{value_w}" height="20" fill="{color_for(value)}"/><rect width="{width}" height="20" fill="url(#s)"/></g>
<g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
<text x="{label_w / 2}" y="14">coverage</text><text x="{label_w + value_w / 2}" y="14">{text}</text></g>
</svg>
"""


def main() -> None:
    paths = [Path(arg) for arg in sys.argv[1:]]
    if not paths:
        raise SystemExit("использование: coverage_badge.py coverage.xml [coverage.xml ...]")
    sys.stdout.write(badge(percent(paths)))


if __name__ == "__main__":
    main()
