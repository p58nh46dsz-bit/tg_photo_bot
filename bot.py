import asyncio
import re
import os
import logging
import httpx
from typing import Optional
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
YADISK_TOKEN = os.getenv("YADISK_TOKEN", "")
YADISK_FOLDER = os.getenv("YADISK_FOLDER", "/photos")  # папка на Яндекс Диске

# Паттерн даты: MM-DD-YY или DD-MM-YY и т.п.
DATE_PATTERN = re.compile(r"\b(\d{2}-\d{2}-\d{2})\b")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


async def get_photo_from_yadisk(date_str: str) -> Optional[bytes]:
    """
    Листает папку на Яндекс Диске и ищет файл, имя которого начинается с date_str.
    Возвращает байты файла или None если не найден.
    """
    headers = {"Authorization": f"OAuth {YADISK_TOKEN}"}

    async with httpx.AsyncClient() as client:
        # Получаем список файлов в папке
        resp = await client.get(
            "https://cloud-api.yandex.net/v1/disk/resources",
            headers=headers,
            params={"path": YADISK_FOLDER, "limit": 1000},
        )
        if resp.status_code != 200:
            return None

        items = resp.json().get("_embedded", {}).get("items", [])

        # Ищем файл, имя которого начинается с даты (без учёта расширения)
        matched = None
        for item in items:
            name = item.get("name", "")
            name_without_ext = name.rsplit(".", 1)[0]
            if name_without_ext == date_str and item.get("type") == "file":
                matched = item
                break

        if not matched:
            return None

        # Скачиваем найденный файл
        dl_resp = await client.get(
            "https://cloud-api.yandex.net/v1/disk/resources/download",
            headers=headers,
            params={"path": f"{YADISK_FOLDER}/{matched['name']}"},
        )
        if dl_resp.status_code != 200:
            return None

        download_url = dl_resp.json().get("href")
        if not download_url:
            return None

        file_resp = await client.get(download_url, follow_redirects=True)
        if file_resp.status_code == 200:
            return file_resp.content

    return None


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Отправь дату в формате ДД-ММ-ГГ (например 12-23-26), "
        "и я пришлю фото за этот день."
    )


@dp.message()
async def handle_message(message: types.Message):
    if not message.text:
        return

    match = DATE_PATTERN.search(message.text)
    if not match:
        await message.answer("Не нашёл дату. Пришли в формате ДД-ММ-ГГ, например: 12-23-26")
        return

    date_str = match.group(1)
    await message.answer(f"Ищу фото за {date_str}...")

    photo_bytes = await get_photo_from_yadisk(date_str)

    if photo_bytes is None:
        await message.answer(f"Фото за {date_str} не найдено.")
        return

    photo = BufferedInputFile(photo_bytes, filename=f"{date_str}.jpg")
    await message.answer_photo(photo, caption=f"Фото за {date_str}")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
