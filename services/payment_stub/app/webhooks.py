"""Отправка webhook'ов магазину с подписью и повторами."""

import asyncio
import contextlib
import json
import time

import httpx
import structlog

from app.config import Settings
from app.signing import sign
from app.storage import Payment, Storage

log = structlog.get_logger("webhooks")

EVENT_TYPES = {"succeeded": "payment.succeeded", "failed": "payment.failed"}


def build_event(payment: Payment) -> dict[str, object]:
    """Тело события. event_id фиксируется при завершении платежа и не меняется при повторных отправках:
    получатель по нему отсекает дубли."""
    return {
        "event_id": payment.event_id,
        "type": EVENT_TYPES[payment.status],
        "payment_id": payment.id,
        "order_id": payment.order_id,
        "amount_kopecks": payment.amount_kopecks,
        "currency": payment.currency,
        "occurred_at": payment.finalized_at,
    }


async def deliver_once(settings: Settings, client: httpx.AsyncClient, payment: Payment) -> bool:
    body = json.dumps(build_event(payment), ensure_ascii=False).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Stub-Signature": sign(
            settings.payment_webhook_secret.get_secret_value(), int(time.time()), body
        ),
    }
    if payment.correlation_id:
        headers["X-Request-ID"] = payment.correlation_id
    try:
        response = await client.post(settings.webhook_url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("webhook_failed", payment_id=payment.id, error=type(exc).__name__)
        return False
    ok = response.is_success  # любой 2xx: событие принято (в том числе «дубль»)
    log.info("webhook_sent", payment_id=payment.id, status=response.status_code, ok=ok)
    return ok


async def deliver_with_retries(
    settings: Settings, client: httpx.AsyncClient, storage: Storage, payment: Payment
) -> None:
    """Повторяет отправку с растущей паузой, пока магазин не ответит 2xx или не кончатся попытки."""
    for attempt in range(settings.webhook_max_attempts):
        await storage.bump_attempts(payment.id)
        if await deliver_once(settings, client, payment):
            await storage.mark_delivered(payment.id)
            return
        await asyncio.sleep(settings.webhook_retry_base_seconds * 2**attempt)
    log.error("webhook_gave_up", payment_id=payment.id)


async def redeliver_loop(
    settings: Settings, client: httpx.AsyncClient, storage: Storage, stop: asyncio.Event
) -> None:
    """Подбирает события, которые не удалось доставить (например, магазин был недоступен или заглушка перезапускалась)."""
    while not stop.is_set():
        try:
            for payment in await storage.undelivered(settings.webhook_max_attempts):
                await storage.bump_attempts(payment.id)
                if await deliver_once(settings, client, payment):
                    await storage.mark_delivered(payment.id)
        except Exception:
            log.exception("redeliver_failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), settings.redeliver_interval_seconds)
