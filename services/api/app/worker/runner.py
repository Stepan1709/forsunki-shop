"""Воркер: consumer'ы RabbitMQ и outbox relay в одном процессе (тот же образ, что и api).

Запуск: `python -m app.worker`. Остановка по SIGTERM/SIGINT проходит в таком порядке:
  1. consumer'ы перестают брать новые сообщения (cancel) и дожидаются текущих (с таймаутом);
  2. relay доводит до конца текущую пачку и останавливается;
  3. закрываются канал публикации и соединение с брокером, затем пул БД.
Сначала останавливается приём работы, потом закрываются клиенты, которыми эта работа выполняется.
"""

import asyncio
import contextlib
import signal
from contextlib import AsyncExitStack
from pathlib import Path

import aio_pika
import structlog
from aio_pika.abc import AbstractRobustConnection
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.messaging.consumer import Consumer, Subscription
from app.core.messaging.publisher import Publisher
from app.worker.relay import OutboxRelay
from app.worker.subscriptions import build_subscriptions

log = structlog.get_logger("worker")

_HEARTBEAT_SECONDS = 5.0


class Worker:
    def __init__(self, settings: Settings, subscriptions: list[Subscription]) -> None:
        self._settings = settings
        self._subscriptions = subscriptions
        self._stack: AsyncExitStack | None = None
        self._connection: AbstractRobustConnection | None = None
        self._consumers: list[Consumer] = []
        self._relay_stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        s = self._settings
        async with AsyncExitStack() as stack:
            engine = create_async_engine(s.database_url, pool_size=s.db_pool_size)
            stack.push_async_callback(engine.dispose)
            session_factory = async_sessionmaker(engine, expire_on_commit=False)

            connection = self._connection = await aio_pika.connect_robust(
                s.rabbitmq_url, client_properties={"connection_name": s.service_name}
            )
            stack.push_async_callback(connection.close)

            publisher = await Publisher.open(connection)
            stack.push_async_callback(publisher.close)

            for subscription in self._subscriptions:
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
            self._tasks.append(asyncio.create_task(relay.run(self._relay_stop), name="relay"))
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
        # 2. Relay доводит текущую пачку до конца.
        self._relay_stop.set()
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
    worker = Worker(settings, build_subscriptions(settings))
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
