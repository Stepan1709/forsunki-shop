"""Реальный IP клиента за прокси.

Лимиты по IP (вход, регистрация, форма ремонта) работают по адресу клиента. За nginx и сайтом `web` адрес соединения это адрес
ближайшего прокси, и лимит сработал бы на всех пользователей сразу. Настоящий адрес лежит в заголовке X-Forwarded-For.

Но заголовок пишет кто угодно, поэтому ему верим только если соединение пришло от доверенного прокси (`trusted_proxies`), и идём по цепочке
справа налево, пока адреса доверенные: первый недоверенный и есть клиент. Подделанные клиентом записи остаются слева и игнорируются.
nginx дополнительно ПЕРЕЗАПИСЫВАЕТ заголовок адресом соединения, а не дописывает к присланному.

Модуль повторяется в `api` (сервисы это независимые проекты без общего кода). В `web` добавлен ещё middleware, который запоминает адрес клиента
для запросов к `api`: тот же правило применяется там ещё раз, уже к адресу сайта.
"""

import ipaddress
from collections.abc import Iterable
from contextvars import ContextVar
from functools import cache

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

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


_client_ip: ContextVar[str | None] = ContextVar("client_ip", default=None)


def current_client_ip() -> str | None:
    """Адрес клиента текущего запроса (для передачи в api заголовком X-Forwarded-For)."""
    return _client_ip.get()


class ClientIpMiddleware:
    """Определяет настоящий адрес клиента запроса к сайту и кладёт его в contextvar.

    Нужен, чтобы `api` считал лимиты входа и регистрации по адресу пользователя, а не по адресу сайта.
    """

    def __init__(self, app: ASGIApp, trusted_proxies: list[str]) -> None:
        self.app = app
        self._trusted = parse_networks(trusted_proxies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            client = scope.get("client")
            ip = resolve_client_ip(
                client[0] if client else None,
                Headers(scope=scope).get("x-forwarded-for"),
                self._trusted,
            )
            token = _client_ip.set(ip)
            try:
                await self.app(scope, receive, send)
            finally:
                _client_ip.reset(token)
            return
        await self.app(scope, receive, send)
