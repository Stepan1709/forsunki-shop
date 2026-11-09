"""Отправка писем: интерфейс `Mailer` и SMTP-реализация.

Бизнес-код зависит только от интерфейса: в dev письма принимает Mailpit, в проде будет SMTP-сервис провайдера.
Ошибки отправки сразу переводятся на язык очереди сообщений:
  TemporaryError - сервер недоступен или занят (повторить позже);
  PermanentError - сервер отказал окончательно (адрес не существует, письмо отвергнуто): повтор не поможет.
"""

from email.message import EmailMessage
from typing import Protocol

import aiosmtplib

from app.core.messaging.errors import PermanentError, TemporaryError


class Mailer(Protocol):
    async def send(self, message: EmailMessage) -> None: ...


class SmtpMailer:
    """Один SMTP-сеанс на письмо: писем мало, а долгоживущее соединение пришлось бы переоткрывать при обрывах."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout

    async def send(self, message: EmailMessage) -> None:
        try:
            await aiosmtplib.send(
                message, hostname=self._host, port=self._port, timeout=self._timeout
            )
        except (
            aiosmtplib.SMTPRecipientsRefused,
            aiosmtplib.SMTPSenderRefused,
            aiosmtplib.SMTPDataError,
        ) as exc:
            # Отказ по существу письма. Код 5xx окончательный, 4xx (например, «попробуйте позже») временный.
            if isinstance(exc, aiosmtplib.SMTPRecipientsRefused) and exc.recipients:
                code = int(
                    exc.recipients[0].code
                )  # у «всех адресатов отказано» код лежит в первом отказе
            else:
                code = int(getattr(exc, "code", 0) or 0)
            if 500 <= code < 600:
                raise PermanentError(f"SMTP отклонил письмо: {code}") from exc
            raise TemporaryError(f"SMTP временно отклонил письмо: {code}") from exc
        except (aiosmtplib.SMTPException, OSError, TimeoutError) as exc:
            raise TemporaryError(f"SMTP недоступен: {type(exc).__name__}") from exc
