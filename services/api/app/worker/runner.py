"""Воркер: consumer'ы RabbitMQ, outbox relay и страховочный обход истечения заказов в одном процессе
(тот же образ, что и api).

Запуск: `python -m app.worker`. Остановка по SIGTERM/SIGINT проходит в таком порядке:
  1. consumer'ы перестают брать новые сообщения (cancel) и дожидаются текущих (с таймаутом);
  2. relay и обход доводят текущую пачку до конца и останавливаются;
  3. закрываются канал публикации, соединение с брокером, HTTP-клиент и пул БД.
Сначала останавливается приём работы, потом закрываются клиенты, которыми эта работа выполняется.
"""

import asyncio
import contextlib
import signal
from contextlib import AsyncExitStack
from pathlib import Path

import aio_pika
import httpx
import structlog
from aio_pika.abc import AbstractRobustConnection
from prometheus_client import start_http_server
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.mail import SmtpMailer
from app.core.messaging.consumer import Consumer, Subscription
from app.core.messaging.publisher import Publisher
from app.core.messaging.topology import DelayQueueSpec, declare_delay_queue, declare_exchanges
from app.core.tracing import setup_tracing, shutdown_tracing
from app.modules.payments.factory import build_gateway
from app.worker.relay import OutboxRelay
from app.worker.retention import RetentionCleaner
from app.worker.stats import StatsCollector
from app.worker.subscriptions import build_delay_queues, build_subscriptions
from app.worker.sweeper import ExpirySweeper

log = structlog.get_logger("worker")

_HEARTBEAT_SECONDS = 5.0


class Worker:
    """`subscriptions` и `delay_queues` по умолчанию собираются из настроек; тесты передают свои."""

    def __init__(
        self,
        settings: Settings,
        subscriptions: list[Subscription] | None = None,
        delay_queues: list[DelayQueueSpec] | None = None,
        *,
        run_sweeper: bool = False,
        run_retention: bool = False,
    ) -> None:
        self._settings = settings
        self._subscriptions = subscriptions
        self._delay_queues = delay_queues
        self._run_sweeper = run_sweeper
        self._run_retention = run_retention
        self._stack: AsyncExitStack | None = None
        self._connection: AbstractRobustConnection | None = None
        self._consumers: list[Consumer] = []
        self._stop_background = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        s = self._settings
        async with AsyncExitStack() as stack:
            setup_tracing(s)
            stack.callback(
                shutdown_tracing
            )  # первым в стеке, значит закроется последним: спаны успевают уйти
            engine = create_async_engine(s.database_url, pool_size=s.db_pool_size)
            stack.push_async_callback(engine.dispose)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)

            # Клиент для платёжного провайдера: создаётся один раз и закрывается вместе с остальными ресурсами.
            http = httpx.AsyncClient(timeout=s.payment_request_timeout_seconds)
            stack.push_async_callback(http.aclose)

            connection = self._connection = await aio_pika.connect_robust(
                s.rabbitmq_url, client_properties={"connection_name": s.service_name}
            )
            stack.push_async_callback(connection.close)

            publisher = await Publisher.open(connection)
            stack.push_async_callback(publisher.close)

            subscriptions = self._subscriptions
            if subscriptions is None:
                subscriptions = build_subscriptions(
                    s,
                    build_gateway(s, http),
                    SmtpMailer(s.smtp_host, s.smtp_port, s.smtp_timeout_seconds),
                )
            delay_queues = self._delay_queues
            if delay_queues is None:
                delay_queues = build_delay_queues(s)

            # Очереди с задержкой объявляются ДО consumer'ов и вместе с ними: сообщение, вернувшееся из такой очереди
            # в обмен, не имеющий подходящей очереди, было бы потеряно (dead-lettering не бывает mandatory).
            delay_channel = await connection.channel()
            stack.push_async_callback(delay_channel.close)
            events, _ = await declare_exchanges(delay_channel)
            for spec in delay_queues:
                await declare_delay_queue(delay_channel, spec, events)

            for subscription in subscriptions:
                consumer = Consumer(
                    subscription,
                    connection,
                    session_factory,
                    publisher,
                    default_prefetch=s.consumer_prefetch,
                    shutdown_timeout=s.worker_shutdown_timeout_seconds,
                )
                await consumer.start()
                self._consumers.append(consumer)

            relay = OutboxRelay(
                session_factory,
                publisher,
                batch_size=s.relay_batch_size,
                poll_interval=s.relay_poll_interval_seconds,
            )
            self._tasks.append(asyncio.create_task(relay.run(self._stop_background), name="relay"))
            if self._run_sweeper:
                sweeper = ExpirySweeper(session_factory, s)
                self._tasks.append(
                    asyncio.create_task(sweeper.run(self._stop_background), name="sweeper")
                )
            if self._run_retention:
                cleaner = RetentionCleaner(session_factory, s)
                self._tasks.append(
                    asyncio.create_task(cleaner.run(self._stop_background), name="retention")
                )
            if s.worker_metrics_port is not None:
                # Воркер не веб-сервис, поэтому метрики отдаёт отдельный маленький HTTP-сервер в своём потоке.
                server, _thread = start_http_server(s.worker_metrics_port)
                stack.callback(server.shutdown)
                stats = StatsCollector(session_factory, s.metrics_refresh_seconds)
                self._tasks.append(
                    asyncio.create_task(stats.run(self._stop_background), name="stats")
                )
            if s.worker_heartbeat_path:
                self._tasks.append(
                    asyncio.create_task(
                        self._heartbeat(Path(s.worker_heartbeat_path)), name="heartbeat"
                    )
                )
            # Успешный старт: закрытием ресурсов дальше управляет stop(), а не выход из этого блока.
            self._stack = stack.pop_all()
        log.info("worker_started", consumers=[c.name for c in self._consumers])

    async def stop(self) -> None:
        log.info("worker_stopping")
        # 1. Перестаём принимать работу и ждём текущие сообщения.
        await asyncio.gather(*(c.stop() for c in self._consumers))
        # 2. Фоновые циклы (relay, обход) доводят текущую пачку до конца.
        self._stop_background.set()
        for task in self._tasks:
            if task.get_name() == "heartbeat":
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        # 3. Закрываем клиентов в обратном порядке создания.
        if self._stack is not None:
            await self._stack.aclose()
        log.info("worker_stopped")

    @staticmethod
    async def _heartbeat(path: Path) -> None:
        """Обновляет файл раз в несколько секунд: по его возрасту healthcheck узнаёт, что цикл жив."""
        while True:
            await asyncio.to_thread(path.touch)
            await asyncio.sleep(_HEARTBEAT_SECONDS)


async def _amain(settings: Settings) -> None:
    worker = Worker(settings, run_sweeper=True, run_retention=True)
    await worker.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    await stop_event.wait()
    await worker.stop()


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain(settings))


if __name__ == "__main__":
    main()
