"""Критерий этапа 1: SIGTERM не обрывает запросы в работе, процесс завершается быстро."""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings

API_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _env(settings: Settings) -> dict[str, str]:
    return {
        **os.environ,
        "ENV": "test",
        "POSTGRES_HOST": settings.postgres_host,
        "POSTGRES_PORT": str(settings.postgres_port),
        "POSTGRES_PASSWORD": settings.postgres_password.get_secret_value(),
        "REDIS_HOST": settings.redis_host,
        "REDIS_PORT": str(settings.redis_port),
        "REDIS_PASSWORD": settings.redis_password.get_secret_value(),
        "RABBITMQ_HOST": settings.rabbitmq_host,
        "RABBITMQ_PORT": str(settings.rabbitmq_port),
        "RABBITMQ_PASSWORD": settings.rabbitmq_password.get_secret_value(),
    }


async def _wait_until_live(client: httpx.AsyncClient, proc: "subprocess.Popen[str]") -> None:
    for _ in range(100):
        if proc.poll() is not None:
            raise AssertionError("uvicorn завершился при старте")
        try:
            if (await client.get("/health/live")).status_code == 200:
                return
        except httpx.TransportError:
            await asyncio.sleep(0.2)
    raise AssertionError("приложение не запустилось")


@pytest.mark.filterwarnings("ignore::UserWarning")
async def test_inflight_request_survives_sigterm(infra_settings: Settings) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:create_app",
            "--factory",
            "--port",
            str(port),
            "--timeout-graceful-shutdown",
            "10",
            "--no-access-log",
        ],
        cwd=API_ROOT,
        env=_env(infra_settings),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        async with httpx.AsyncClient(base_url=base_url) as client:
            await _wait_until_live(client, proc)

            slow = asyncio.create_task(
                client.get("/debug/sleep", params={"seconds": 3}, timeout=15)
            )
            await asyncio.sleep(0.5)  # запрос уже в работе

            stopped_at = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            response = await slow  # не должен оборваться
            exit_code = await asyncio.to_thread(proc.wait, 15)
            elapsed = time.monotonic() - stopped_at

        assert response.status_code == 200
        # uvicorn после graceful-остановки повторно шлёт себе SIGTERM: код -15 (143 в контейнере).
        assert exit_code in (0, -signal.SIGTERM)
        assert elapsed < 8, f"остановка заняла {elapsed:.1f} с"
        with pytest.raises(httpx.TransportError):  # после остановки новые запросы не принимаются
            httpx.get(f"{base_url}/health/live", timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
