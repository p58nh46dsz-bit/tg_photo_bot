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
YADISK_FOLDER = os.getenv("YADISK_FOLDER", "/photo556")

DATE_PATTERN = re.compile(r"\b(\d{2}-\d{2}-\d{2})\b")
MONTH_PATTERN = re.compile(r"^\s*(\d{2}-\d{2})\s*$")
DELETE_PATTERN = re.compile(r"удалить\s+(\d{2}-\d{2}-\d{2})", re.IGNORECASE)

# Ожидание подтверждения удаления: {user_id: filename}
pending_delete = {}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


async def list_files_in_folder(client: httpx.AsyncClient) -> list:
    """Возвращает список файлов в папке на Яндекс Диске."""
    headers = {"Authorization": f"OAuth {YADISK_TOKEN}"}
    resp = await client.get(
        "https://cloud-api.yandex.net/v1/disk/resources",
        headers=headers,
        params={"path": YADISK_FOLDER, "limit": 1000},
    )
    if resp.status_code != 200:
        return []
    return resp.json().get("_embedded", {}).get("items", [])


async def download_file(client: httpx.AsyncClient, filename: str) -> Optional[bytes]:
    """Скачивает файл с Яндекс Диска по имени файла."""
    headers = {"Authorization": f"OAuth {YADISK_TOKEN}"}
    dl_resp = await client.get(
        "https://cloud-api.yandex.net/v1/disk/resources/download",
        headers=headers,
        params={"path": f"{YADISK_FOLDER}/{filename}"},
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


async def get_photo_from_yadisk(date_str: str) -> Optional[tuple]:
    """Ищет файл по дате. Возвращает (bytes, filename) или None."""
    async with httpx.AsyncClient() as client:
        items = await list_files_in_folder(client)
        for item in items:
            name = item.get("name", "")
            name_without_ext = name.rsplit(".", 1)[0]
            if name_without_ext == date_str and item.get("type") == "file":
                data = await download_file(client, name)
                if data:
                    return data, name
    return None


async def get_photos_by_month(month_str: str) -> list:
    """Ищет все файлы за месяц (ММ-ГГ). Возвращает список (bytes, filename)."""
    results = []
    async with httpx.AsyncClient() as client:
        items = await list_files_in_folder(client)
        matched = [
            item for item in items
            if item.get("type") == "file" and f"-{month_str}" in item.get("name", "")
        ]
        for item in matched:
            data = await download_file(client, item["name"])
            if data:
                results.append((data, item["name"]))
    return results


async def delete_photo_from_yadisk(filename: str) -> bool:
    """Удаляет файл с Яндекс Диска. Возвращает True если успешно."""
    headers = {"Authorization": f"OAuth {YADISK_TOKEN}"}
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            "https://cloud-api.yandex.net/v1/disk/resources",
            headers=headers,
            params={"path": f"{YADISK_FOLDER}/{filename}", "permanently": "true"},
        )
        return resp.status_code in (204, 202)


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет!\n\n"
        "Доступные команды:\n"
        "📸 <b>08-06-26</b> — фото за конкретную дату\n"
        "📅 <b>06-26</b> — все фото за месяц (ММ-ГГ)\n"
        "🗑 <b>удалить 08-06-26</b> — удалить фото по дате",
        parse_mode="HTML"
    )


@dp.message()
async def handle_message(message: types.Message):
    if not message.text:
        return

    text = message.text.strip()
    user_id = message.from_user.id

    # Ожидаем подтверждение удаления
    if user_id in pending_delete:
        filename = pending_delete[user_id]
        if text.lower() in ("да", "yes", "y", "д"):
            del pending_delete[user_id]
            await message.answer("⏳ Удаляю...")
            success = await delete_photo_from_yadisk(filename)
            if success:
                await message.answer(f"✅ Фото <b>{filename}</b> удалено.", parse_mode="HTML")
            else:
                await message.answer("❌ Не удалось удалить. Возможно токен не имеет права на запись.")
        elif text.lower() in ("нет", "no", "n", "н"):
            del pending_delete[user_id]
            await message.answer("Отменено.")
        else:
            await message.answer("Ответь <b>да</b> или <b>нет</b>.", parse_mode="HTML")
        return

    # Команда удаления
    delete_match = DELETE_PATTERN.search(text)
    if delete_match:
        date_str = delete_match.group(1)
        result = await get_photo_from_yadisk(date_str)
        if result is None:
            await message.answer(f"Фото за <b>{date_str}</b> не найдено.", parse_mode="HTML")
            return
        _, filename = result
        pending_delete[user_id] = filename
        await message.answer(
            f"Удалить фото за <b>{date_str}</b>?\n\nОтветь <b>да</b> или <b>нет</b>.",
            parse_mode="HTML"
        )
        return

    # Поиск по конкретной дате ДД-ММ-ГГ
    date_match = DATE_PATTERN.search(text)
    if date_match:
        date_str = date_match.group(1)
        await message.answer(f"🔍 Ищу фото за {date_str}...")
        result = await get_photo_from_yadisk(date_str)
        if result is None:
            await message.answer(f"Фото за <b>{date_str}</b> не найдено.", parse_mode="HTML")
            return
        photo_bytes, filename = result
        photo = BufferedInputFile(photo_bytes, filename=filename)
        await message.answer_photo(photo, caption=f"📸 {date_str}")
        return

    # Поиск по месяцу ММ-ГГ
    month_match = MONTH_PATTERN.match(text)
    if month_match:
        month_str = month_match.group(1)
        await message.answer(f"🔍 Ищу все фото за {month_str}...")
        results = await get_photos_by_month(month_str)
        if not results:
            await message.answer(f"Фото за <b>{month_str}</b> не найдено.", parse_mode="HTML")
            return
        await message.answer(f"📅 Найдено фото: <b>{len(results)}</b>", parse_mode="HTML")
        for photo_bytes, filename in results:
            date_str = filename.rsplit(".", 1)[0]
            photo = BufferedInputFile(photo_bytes, filename=filename)
            await message.answer_photo(photo, caption=f"📸 {date_str}")
        return

    await message.answer(
        "Не понял запрос.\n\n"
        "📸 <b>08-06-26</b> — фото за дату\n"
        "📅 <b>06-26</b> — все фото за месяц\n"
        "🗑 <b>удалить 08-06-26</b> — удалить фото",
        parse_mode="HTML"
    )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
