"""Подпись webhook. Формат заголовка: `t=<unix-время>,v1=<hex HMAC-SHA256>`.

Подписывается строка `<время>.<тело запроса>`. Время входит в подпись, поэтому перехваченное событие нельзя
отправить повторно позже: получатель отвергает слишком старые подписи. Алгоритм общий с api, там его проверяет
`StubPaymentGateway.verify_webhook`; одинаковость проверяется общим тестовым вектором в обоих наборах тестов.
"""

import hashlib
import hmac


def sign(secret: str, timestamp: int, body: bytes) -> str:
    payload = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"
