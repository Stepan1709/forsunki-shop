import httpx
from fastapi import FastAPI

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64


async def put_image(
    client: httpx.AsyncClient,
    injector_id: int,
    data: bytes,
    content_type: str,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.put(
        f"/catalog/injectors/{injector_id}/image",
        files={"file": ("photo", data, content_type)},
        headers=headers,
    )


async def test_admin_uploads_image_and_card_gets_working_url(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog: dict[str, int],
    admin_headers: dict[str, str],
) -> None:
    injector_id = catalog["bosch_new"]

    response = await put_image(client, injector_id, PNG, "image/png", admin_headers)

    assert response.status_code == 200, response.text
    url = response.json()["image_url"]
    assert url
    assert "/injectors/" in url
    # Ссылка рабочая: по ней действительно отдаётся загруженный файл.
    async with httpx.AsyncClient() as plain:
        downloaded = await plain.get(url)
    assert downloaded.status_code == 200
    assert downloaded.content == PNG


async def test_replacing_image_deletes_the_old_object(
    app: FastAPI,
    client: httpx.AsyncClient,
    catalog: dict[str, int],
    admin_headers: dict[str, str],
) -> None:
    injector_id = catalog["bosch_new"]
    first = (await put_image(client, injector_id, PNG, "image/png", admin_headers)).json()
    second = (await put_image(client, injector_id, JPEG, "image/jpeg", admin_headers)).json()

    async with httpx.AsyncClient() as plain:
        old = await plain.get(first["image_url"])
        new = await plain.get(second["image_url"])

    assert old.status_code == 404  # прежний файл удалён, место не копится
    assert new.status_code == 200


async def test_card_shows_new_image_immediately_after_upload(
    client: httpx.AsyncClient, catalog: dict[str, int], admin_headers: dict[str, str]
) -> None:
    url = f"/catalog/injectors/{catalog['bosch_new']}"
    assert (await client.get(url)).json()["image_url"] is None  # карточка попала в кэш

    await put_image(client, catalog["bosch_new"], WEBP, "image/webp", admin_headers)

    after = await client.get(url)
    # Загрузка сбросила кэш (иначе осталась бы карточка без картинки) и сразу прогрела его новыми данными.
    assert after.headers["x-cache"] == "HIT"
    assert after.json()["image_url"] is not None


async def test_upload_requires_admin(
    client: httpx.AsyncClient, catalog: dict[str, int], customer_headers: dict[str, str]
) -> None:
    injector_id = catalog["bosch_new"]

    anonymous = await put_image(client, injector_id, PNG, "image/png")
    customer = await put_image(client, injector_id, PNG, "image/png", customer_headers)

    assert anonymous.status_code == 401
    assert customer.status_code == 403


async def test_upload_rejects_wrong_type_fake_content_and_big_files(
    client: httpx.AsyncClient, catalog: dict[str, int], admin_headers: dict[str, str]
) -> None:
    injector_id = catalog["bosch_new"]

    wrong_type = await put_image(client, injector_id, b"text", "text/plain", admin_headers)
    fake_png = await put_image(
        client, injector_id, b"<html>evil</html>", "image/png", admin_headers
    )
    too_big = await put_image(
        client, injector_id, PNG + b"\x00" * (2 * 1024 * 1024), "image/png", admin_headers
    )

    assert wrong_type.status_code == 415
    assert fake_png.status_code == 415  # Content-Type заявлен png, а байты не png
    assert too_big.status_code == 413


async def test_upload_to_missing_injector_is_404(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await put_image(client, 999999, PNG, "image/png", admin_headers)

    assert response.status_code == 404
