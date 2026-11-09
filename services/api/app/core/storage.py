"""Хранилище файлов в S3-совместимом сервисе (RustFS в dev, AWS S3 и аналоги в prod)."""

from typing import Any

import structlog
from botocore.exceptions import ClientError

log = structlog.get_logger("storage")


class ImageStorage:
    """Тонкая обёртка над aioboto3-клиентом. Остальной код не знает про boto и S3-API.

    Два клиента: `client` ходит в S3 по внутреннему адресу (сеть Docker), а `signing_client` нужен только для
    подписи ссылок под ПУБЛИЧНЫМ адресом, который откроет браузер. Подпись считается локально, в сеть клиент
    при этом не обращается.
    """

    def __init__(self, client: Any, signing_client: Any, bucket: str, url_ttl_seconds: int) -> None:
        self._client = client
        self._signing_client = signing_client
        self._bucket = bucket
        self._url_ttl = url_ttl_seconds

    async def ensure_bucket(self) -> None:
        """Создаёт бакет, если его нет (идемпотентно)."""
        try:
            await self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            await self._client.create_bucket(Bucket=self._bucket)
            log.info("bucket_created", bucket=self._bucket)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await self._client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
        )

    async def delete(self, key: str) -> None:
        await self._client.delete_object(Bucket=self._bucket, Key=key)

    async def exists(self, key: str) -> bool:
        try:
            await self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError:
            return False
        return True

    async def presigned_url(self, key: str) -> str:
        """Временная ссылка на чтение. Бакет остаётся приватным, ссылка живёт `image_url_ttl_seconds`."""
        url: str = await self._signing_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=self._url_ttl,
        )
        return url
