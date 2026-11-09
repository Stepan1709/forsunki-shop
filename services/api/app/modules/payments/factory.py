import httpx

from app.core.config import Settings
from app.modules.payments.gateway import PaymentGateway
from app.modules.payments.stub_gateway import StubPaymentGateway


def build_gateway(settings: Settings, http: httpx.AsyncClient) -> PaymentGateway:
    """Выбор провайдера по настройке `payment_provider`. Подключение настоящей кассы: новая реализация
    PaymentGateway и ещё одна ветка здесь; остальной код не меняется."""
    if settings.payment_provider == "stub":
        return StubPaymentGateway(
            http,
            base_url=settings.payment_provider_url,
            api_key=settings.payment_api_key.get_secret_value(),
            webhook_secret=settings.payment_webhook_secret.get_secret_value(),
            tolerance_seconds=settings.payment_webhook_tolerance_seconds,
            timeout=settings.payment_request_timeout_seconds,
        )
    raise ValueError(f"неизвестный платёжный провайдер: {settings.payment_provider}")
