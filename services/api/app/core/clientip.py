"""Реальный IP клиента за прокси.

Лимиты по IP (вход, регистрация, форма ремонта) работают по адресу клиента. За nginx и сайтом `web` адрес соединения это адрес
ближайшего прокси, и лимит сработал бы на всех пользователей сразу. Настоящий адрес лежит в заголовке X-Forwarded-For.

Но заголовок пишет кто угодно, поэтому ему верим только если соединение пришло от доверенного прокси (`trusted_proxies`), и идём по цепочке
справа налево, пока адреса доверенные: первый недоверенный и есть клиент. Подделанные клиентом записи остаются слева и игнорируются.
nginx дополнительно ПЕРЕЗАПИСЫВАЕТ заголовок адресом соединения, а не дописывает к присланному.

Модуль повторяется в `web` (три сервиса это независимые проекты без общего кода).
"""

import ipaddress
from collections.abc import Iterable
from functools import cache

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(values: Iterable[str]) -> list[Network]:
    return [ipaddress.ip_network(v, strict=False) for v in values]


@cache
def networks_for(values: tuple[str, ...]) -> list[Network]:
    """Разбор настройки один раз на набор значений: вызывается на каждый запрос."""
    return parse_networks(values)


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def resolve_client_ip(peer: str | None, forwarded_for: str | None, trusted: list[Network]) -> str:
    """Адрес клиента по адресу соединения и заголовку X-Forwarded-For."""
    if not peer:
        return "unknown"
    peer_ip = _ip(peer)
    if peer_ip is None or not forwarded_for or not any(peer_ip in net for net in trusted):
        return peer
    chain = [part for part in (p.strip() for p in forwarded_for.split(",")) if part]
    candidate = peer
    for entry in reversed(chain):
        parsed = _ip(entry)
        if parsed is None:
            return peer  # мусор в заголовке: ему не верим целиком
        candidate = str(parsed)
        if not any(parsed in net for net in trusted):
            return candidate
    return candidate  # вся цепочка доверенная: клиент это самый левый адрес
