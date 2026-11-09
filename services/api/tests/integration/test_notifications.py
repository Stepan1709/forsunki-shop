"""Письма и заявки на ремонт. Реальные Postgres, RabbitMQ, Redis и SMTP-сервер Mailpit."""

import asyncio
import uuid
from email.message import EmailMessage
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.mail import Mailer, SmtpMailer
from app.core.messaging.consumer import Handler
from app.core.messaging.errors import PermanentError, TemporaryError
from app.core.outbox import OutboxMessage
from app.modules.repair.models import RepairRequest
from app.worker.notification_handlers import (
    make_order_mail_handler,
    make_repair_mail_handler,
    make_welcome_handler,
)
from tests.integration.conftest import Mailbox, create_user
from tests.integration.test_messaging import Harness, make_spec, send, wait_until
from tests.integration.test_payments import make_order


def smtp(settings: Settings) -> SmtpMailer:
    return SmtpMailer(settings.smtp_host, settings.smtp_port, settings.smtp_timeout_seconds)


async def wait_for_mail(mailbox: Mailbox, address: str, count: int = 1) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def ready() -> bool:
        found[:] = mailbox.to(address)
        return len(found) >= count

    await wait_until(ready)
    return found


REPAIR_BODY: dict[str, Any] = {
    "name": "Иван Петров",
    "phone": "+7 900 123-45-67",
    "email": "ivan@example.com",
    "vehicle": "Mercedes Sprinter 2.1 CDI, OM651",
    "injector": "Bosch 0 445 110 183",
    "description": "Двигатель троит на холодную, дымит чёрным.",
}


def repair_headers(key: str | None = None) -> dict[str, str]:
    return {"Idempotency-Key": key or str(uuid.uuid4())}


# ------------------------------------------------------------------------------------ приветствие
async def test_welcome_email_is_sent_once_even_if_event_is_delivered_twice(
    app: FastAPI, harness: Harness, publisher: Any, infra_settings: Settings, mailbox: Mailbox
) -> None:
    spec = make_spec()
    handler = make_welcome_handler(infra_settings, smtp(infra_settings))
    message_id = uuid.uuid4()
    async with harness.running(spec, handler):
        for _ in range(2):  # тот же message_id дважды: брокер доставляет «минимум один раз»
            await send(publisher, spec, {"email": "new@example.com"}, message_id)
        await wait_until(lambda: len(harness.calls) >= 1)
        await asyncio.sleep(0.5)  # время на возможную вторую (ошибочную) отправку
    letters = mailbox.to("new@example.com")
    assert len(letters) == 1
    assert letters[0]["Subject"] == "Добро пожаловать в Forsunki"
    assert len(harness.calls) == 1  # второй раз обработчик вообще не вызывался (inbox)


async def test_welcome_without_email_goes_to_dlq(
    harness: Harness, publisher: Any, infra_settings: Settings, mailbox: Mailbox
) -> None:
    handler = make_welcome_handler(infra_settings, smtp(infra_settings))
    spec = make_spec()
    async with harness.running(spec, handler):
        await send(publisher, spec, {"user_id": "x"})
        await wait_until(lambda: len(harness.calls) >= 1)
        await asyncio.sleep(0.3)
    assert mailbox.messages() == []


# ------------------------------------------------------------------------------------ заказы
@pytest.mark.parametrize(("paid", "needle"), [(True, "оплачен"), (False, "не был оплачен")])
async def test_order_mail_contains_number_and_total(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog: dict[str, int],
    harness: Harness,
    publisher: Any,
    infra_settings: Settings,
    mailbox: Mailbox,
    paid: bool,
    needle: str,
) -> None:
    user_id, headers = await create_user(app, f"buyer-{paid}@example.com")
    order = await make_order(app, client, headers, catalog["bosch_new"], 2)
    spec = make_spec()
    handler = make_order_mail_handler(infra_settings, smtp(infra_settings), paid=paid)
    async with harness.running(spec, handler):
        await send(
            publisher,
            spec,
            {
                "order_id": order["id"],
                "user_id": str(user_id),
                "total_kopecks": order["total_kopecks"],
            },
        )
        letters = await wait_for_mail(mailbox, f"buyer-{paid}@example.com")
    body = mailbox.full(letters[0]["ID"])
    number = order["id"].replace("-", "")[:8].upper()
    assert number in letters[0]["Subject"]
    assert needle in body["Text"]
    assert "90 000,00 ₽" in body["Text"]  # 2 x 45 000
    assert "<html" in body["HTML"]  # есть и HTML-версия


async def test_order_mail_for_deleted_user_is_skipped(
    app: FastAPI,
    harness: Harness,
    publisher: Any,
    infra_settings: Settings,
    mailbox: Mailbox,
) -> None:
    spec = make_spec()
    handler = make_order_mail_handler(infra_settings, smtp(infra_settings), paid=True)
    async with harness.running(spec, handler):
        await send(
            publisher,
            spec,
            {"order_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()), "total_kopecks": 100},
        )
        await wait_until(lambda: len(harness.calls) >= 1)
        await asyncio.sleep(0.3)
    assert mailbox.messages() == []


# ------------------------------------------------------------------------------------ заявки: API
async def test_repair_request_is_saved_and_event_goes_to_outbox(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    response = await client.post("/repair-requests", json=REPAIR_BODY, headers=repair_headers())
    assert response.status_code == 201
    assert response.json()["status"] == "NEW"
    async with app.state.resources.session_factory() as session:
        saved = (await session.execute(select(RepairRequest))).scalar_one()
        events = (
            (
                await session.execute(
                    select(OutboxMessage).where(OutboxMessage.routing_key == "repair.requested")
                )
            )
            .scalars()
            .all()
        )
    assert str(saved.id) == response.json()["id"]
    assert saved.name == "Иван Петров"
    assert len(events) == 1
    # В событии только id: контакты клиента через брокер не ходят.
    assert events[0].payload == {"request_id": str(saved.id)}


async def test_repeated_idempotency_key_returns_same_request(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    headers = repair_headers()
    first = await client.post("/repair-requests", json=REPAIR_BODY, headers=headers)
    second = await client.post("/repair-requests", json=REPAIR_BODY, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()
    async with app.state.resources.session_factory() as session:
        requests = (
            await session.execute(select(func.count()).select_from(RepairRequest))
        ).scalar_one()
        events = (
            await session.execute(select(func.count()).select_from(OutboxMessage))
        ).scalar_one()
    assert requests == 1
    assert events == 1


async def test_parallel_requests_with_same_key_create_one_request(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    headers = repair_headers()
    responses = await asyncio.gather(
        *(client.post("/repair-requests", json=REPAIR_BODY, headers=headers) for _ in range(4))
    )
    assert sorted(r.status_code for r in responses).count(201) == 1
    assert {r.json()["id"] for r in responses} == {responses[0].json()["id"]}
    async with app.state.resources.session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(RepairRequest))
        ).scalar_one() == 1


async def test_same_key_with_other_body_is_rejected(client: httpx.AsyncClient) -> None:
    headers = repair_headers()
    await client.post("/repair-requests", json=REPAIR_BODY, headers=headers)
    other = {**REPAIR_BODY, "description": "Совсем другая неисправность, течёт обратка."}
    response = await client.post("/repair-requests", json=other, headers=headers)
    assert response.status_code == 422


async def test_idempotency_key_is_required(client: httpx.AsyncClient) -> None:
    response = await client.post("/repair-requests", json=REPAIR_BODY)
    assert response.status_code == 400


@pytest.mark.parametrize(
    "patch",
    [
        {"phone": None, "email": None},  # нет способа связаться
        {"phone": "abc"},
        {"email": "not-an-email"},
        {"name": "И"},
        {"description": "коротко"},
        {"vehicle": ""},
    ],
)
async def test_invalid_repair_request_is_rejected(
    client: httpx.AsyncClient, patch: dict[str, Any]
) -> None:
    response = await client.post(
        "/repair-requests", json={**REPAIR_BODY, **patch}, headers=repair_headers()
    )
    assert response.status_code == 422


async def test_phone_only_request_is_accepted(client: httpx.AsyncClient) -> None:
    body = {**REPAIR_BODY, "email": None}
    response = await client.post("/repair-requests", json=body, headers=repair_headers())
    assert response.status_code == 201


async def test_honeypot_looks_successful_but_saves_nothing(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    body = {**REPAIR_BODY, "website": "http://spam.example"}
    response = await client.post("/repair-requests", json=body, headers=repair_headers())
    assert response.status_code == 201  # бот не должен понять, что его заметили
    async with app.state.resources.session_factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(RepairRequest))
        ).scalar_one() == 0
        assert (
            await session.execute(select(func.count()).select_from(OutboxMessage))
        ).scalar_one() == 0


async def test_repair_requests_are_rate_limited_per_ip(client: httpx.AsyncClient) -> None:
    codes = [
        (
            await client.post("/repair-requests", json=REPAIR_BODY, headers=repair_headers())
        ).status_code
        for _ in range(6)
    ]
    assert codes == [201] * 5 + [429]


# ------------------------------------------------------------------------------------ заявки: письма
async def _submit(app: FastAPI, client: httpx.AsyncClient, body: dict[str, Any]) -> str:
    response = await client.post("/repair-requests", json=body, headers=repair_headers())
    assert response.status_code == 201
    return str(response.json()["id"])


async def test_master_and_customer_get_their_own_emails(
    app: FastAPI,
    client: httpx.AsyncClient,
    harness: Harness,
    publisher: Any,
    infra_settings: Settings,
    mailbox: Mailbox,
) -> None:
    request_id = await _submit(app, client, REPAIR_BODY)
    master_spec, customer_spec = make_spec(), make_spec()
    master = make_repair_mail_handler(infra_settings, smtp(infra_settings), to_master=True)
    customer = make_repair_mail_handler(infra_settings, smtp(infra_settings), to_master=False)
    async with harness.running(master_spec, master), harness.running(customer_spec, customer):
        await send(publisher, master_spec, {"request_id": request_id})
        await send(publisher, customer_spec, {"request_id": request_id})
        to_master = await wait_for_mail(mailbox, infra_settings.repair_notify_email)
        to_customer = await wait_for_mail(mailbox, "ivan@example.com")
    master_text = mailbox.full(to_master[0]["ID"])["Text"]
    assert "Иван Петров" in master_text
    assert "+7 900 123-45-67" in master_text
    assert "дымит чёрным" in master_text
    assert "принята" in to_customer[0]["Subject"]


async def test_customer_without_email_gets_no_confirmation(
    app: FastAPI,
    client: httpx.AsyncClient,
    harness: Harness,
    publisher: Any,
    infra_settings: Settings,
    mailbox: Mailbox,
) -> None:
    request_id = await _submit(app, client, {**REPAIR_BODY, "email": None})
    spec = make_spec()
    handler = make_repair_mail_handler(infra_settings, smtp(infra_settings), to_master=False)
    async with harness.running(spec, handler):
        await send(publisher, spec, {"request_id": request_id})
        await wait_until(lambda: len(harness.calls) >= 1)
        await asyncio.sleep(0.3)
    assert mailbox.messages() == []


async def test_html_in_user_text_is_escaped(
    app: FastAPI,
    client: httpx.AsyncClient,
    harness: Harness,
    publisher: Any,
    infra_settings: Settings,
    mailbox: Mailbox,
) -> None:
    request_id = await _submit(app, client, {**REPAIR_BODY, "name": "<script>alert(1)</script>"})
    spec = make_spec()
    handler = make_repair_mail_handler(infra_settings, smtp(infra_settings), to_master=True)
    async with harness.running(spec, handler):
        await send(publisher, spec, {"request_id": request_id})
        letters = await wait_for_mail(mailbox, infra_settings.repair_notify_email)
    html = mailbox.full(letters[0]["ID"])["HTML"]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


async def test_unknown_request_id_is_permanent_error(
    app: FastAPI, harness: Harness, publisher: Any, infra_settings: Settings, mailbox: Mailbox
) -> None:
    spec = make_spec()
    handler = make_repair_mail_handler(infra_settings, smtp(infra_settings), to_master=True)
    async with harness.running(spec, handler):
        await send(publisher, spec, {"request_id": str(uuid.uuid4())})
        await wait_until(lambda: len(harness.calls) >= 1)
        await asyncio.sleep(0.3)
    assert mailbox.messages() == []  # сообщение ушло в DLQ, письма нет


# ------------------------------------------------------------------------------------ сбои SMTP
class FlakyMailer:
    """Первые `failures` попыток падают как недоступный SMTP, затем отправляет по-настоящему."""

    def __init__(self, real: Mailer, failures: int) -> None:
        self._real = real
        self.failures = failures
        self.attempts = 0

    async def send(self, message: EmailMessage) -> None:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise TemporaryError("SMTP недоступен")
        await self._real.send(message)


async def test_temporary_smtp_failure_is_retried_and_one_email_is_sent(
    harness: Harness, publisher: Any, infra_settings: Settings, mailbox: Mailbox
) -> None:
    flaky = FlakyMailer(smtp(infra_settings), failures=2)
    handler: Handler = make_welcome_handler(infra_settings, flaky)
    spec = make_spec(delays=(0.2, 0.2, 0.2))
    async with harness.running(spec, handler):
        await send(publisher, spec, {"email": "retry@example.com"})
        await wait_for_mail(mailbox, "retry@example.com")
        await asyncio.sleep(0.5)
    assert flaky.attempts == 3
    assert len(mailbox.to("retry@example.com")) == 1


async def test_unreachable_smtp_is_a_temporary_error(infra_settings: Settings) -> None:
    mailer = SmtpMailer("127.0.0.1", 1, timeout=1)  # порт 1 никто не слушает
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "a@example.com", "b@example.com", "x"
    message.set_content("x")
    with pytest.raises(TemporaryError):
        await mailer.send(message)


async def test_message_without_event_fields_is_permanent_error(infra_settings: Settings) -> None:
    from app.core.messaging.consumer import MessageContext

    ctx = MessageContext(
        session=None,  # type: ignore[arg-type]
        message_id=uuid.uuid4(),
        event_type="repair.requested",
        correlation_id=None,
        data={},
        retry_count=0,
    )
    handler = make_repair_mail_handler(infra_settings, smtp(infra_settings), to_master=True)
    with pytest.raises(PermanentError):
        await handler(ctx)
