import asyncio
import re
import os
import logging
import httpx
from typing import Optional
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
YADISK_TOKEN = os.getenv("YADISK_TOKEN", "")
YADISK_FOLDER = os.getenv("YADISK_FOLDER", "/photo556")

DATE_PATTERN = re.compile(r"\b(\d{2}-\d{2}-\d{2})\b")
MONTH_PATTERN = re.compile(r"^\s*(\d{2}-\d{2})\s*$")

# Состояния пользователей
pending_delete = {}      # {user_id: filename}
pending_upload = {}      # {user_id: file_bytes}
pending_date = {}        # {user_id: "search" | "delete" | "month"}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Главное меню
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📸 Найти фото по дате"), KeyboardButton(text="📅 Все фото за месяц")],
        [KeyboardButton(text="⬆️ Загрузить фото"),     KeyboardButton(text="🗑 Удалить фото")],
    ],
    resize_keyboard=True
)


async def list_files_in_folder(client: httpx.AsyncClient) -> list:
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
    headers = {"Authorization": f"OAuth {YADISK_TOKEN}"}
    dl_resp = await client.get(
        "https://cloud-api.yandex.net/v1/disk/resources/download",
        headers=headers,
        params={"path": f"{YADISK_FOLDER}/{filename}"},
    )
    logger.info(f"Download URL status: {dl_resp.status_code} for {filename}")
    if dl_resp.status_code != 200:
        logger.error(f"Download URL error: {dl_resp.text}")
        return None
    download_url = dl_resp.json().get("href")
    if not download_url:
        return None
    # Используем отдельный клиент без авторизации для прямого скачивания
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as dl_client:
        file_resp = await dl_client.get(download_url)
    logger.info(f"File download status: {file_resp.status_code}, size: {len(file_resp.content)}")
    return file_resp.content if file_resp.status_code == 200 else None


async def get_photo_from_yadisk(date_str: str) -> Optional[tuple]:
    async with httpx.AsyncClient() as client:
        items = await list_files_in_folder(client)
        for item in items:
            name = item.get("name", "")
            if name.rsplit(".", 1)[0] == date_str and item.get("type") == "file":
                data = await download_file(client, name)
                if data:
                    return data, name
    return None


async def get_photos_by_month(month_str: str) -> list:
    results = []
    async with httpx.AsyncClient() as client:
        items = await list_files_in_folder(client)
        matched = [i for i in items if i.get("type") == "file" and f"-{month_str}" in i.get("name", "")]
        for item in matched:
            data = await download_file(client, item["name"])
            if data:
                results.append((data, item["name"]))
    return results


async def delete_photo_from_yadisk(filename: str) -> bool:
    headers = {"Authorization": f"OAuth {YADISK_TOKEN}"}
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            "https://cloud-api.yandex.net/v1/disk/resources",
            headers=headers,
            params={"path": f"{YADISK_FOLDER}/{filename}", "permanently": "true"},
        )
        return resp.status_code in (204, 202)


async def upload_photo_to_yadisk(file_bytes: bytes, filename: str) -> bool:
    headers = {"Authorization": f"OAuth {YADISK_TOKEN}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://cloud-api.yandex.net/v1/disk/resources/upload",
            headers=headers,
            params={"path": f"{YADISK_FOLDER}/{filename}", "overwrite": "true"},
        )
        if resp.status_code != 200:
            return False
        upload_url = resp.json().get("href")
        if not upload_url:
            return False
        upload_resp = await client.put(upload_url, content=file_bytes, headers={"Content-Type": "image/jpeg"})
        return upload_resp.status_code in (201, 200)


@dp.message(lambda m: m.text == "/debug")
async def cmd_debug(message: types.Message):
    async with httpx.AsyncClient() as client:
        items = await list_files_in_folder(client)
    if not items:
        await message.answer("❌ Папка пустая или токен не работает")
        return
    names = "\n".join(i["name"] for i in items if i.get("type") == "file")
    await message.answer(f"📁 Файлы в папке:\n{names}\n\nПуть: {YADISK_FOLDER}")


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Выбери действие:",
        reply_markup=MAIN_KEYBOARD
    )


@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    text = message.text.strip() if message.text else ""

    # ── Ожидаем дату для загружаемого фото ──
    if user_id in pending_upload:
        if message.text and text.lower() in ("отмена", "cancel"):
            del pending_upload[user_id]
            await message.answer("Отменено.", reply_markup=MAIN_KEYBOARD)
            return
        date_match = DATE_PATTERN.search(text)
        if date_match:
            date_str = date_match.group(1)
            file_bytes = pending_upload.pop(user_id)
            await message.answer("⏳ Загружаю...", reply_markup=ReplyKeyboardRemove())
            success = await upload_photo_to_yadisk(file_bytes, f"{date_str}.jpg")
            if success:
                await message.answer(f"✅ Фото сохранено как <b>{date_str}.jpg</b>", parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
            else:
                await message.answer("❌ Не удалось загрузить фото.", reply_markup=MAIN_KEYBOARD)
        else:
            await message.answer("Напиши дату в формате <b>ДД-ММ-ГГ</b>\nНапример: <b>08-06-26</b>\n\nИли напиши <b>отмена</b>.", parse_mode="HTML")
        return

    # ── Ожидаем подтверждение удаления ──
    if user_id in pending_delete:
        filename = pending_delete[user_id]
        if text.lower() in ("да", "y", "д"):
            del pending_delete[user_id]
            await message.answer("⏳ Удаляю...", reply_markup=ReplyKeyboardRemove())
            success = await delete_photo_from_yadisk(filename)
            if success:
                await message.answer(f"✅ Фото <b>{filename}</b> удалено.", parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
            else:
                await message.answer("❌ Не удалось удалить.", reply_markup=MAIN_KEYBOARD)
        elif text.lower() in ("нет", "n", "н"):
            del pending_delete[user_id]
            await message.answer("Отменено.", reply_markup=MAIN_KEYBOARD)
        else:
            await message.answer("Ответь <b>да</b> или <b>нет</b>.", parse_mode="HTML")
        return

    # ── Ожидаем дату (после нажатия кнопки) ──
    if user_id in pending_date:
        mode = pending_date[user_id]
        if text.lower() in ("отмена", "cancel"):
            del pending_date[user_id]
            await message.answer("Отменено.", reply_markup=MAIN_KEYBOARD)
            return

        if mode == "search":
            date_match = DATE_PATTERN.search(text)
            if not date_match:
                await message.answer("Напиши дату в формате <b>ДД-ММ-ГГ</b>, например: <b>08-06-26</b>", parse_mode="HTML")
                return
            del pending_date[user_id]
            date_str = date_match.group(1)
            await message.answer(f"🔍 Ищу фото за {date_str}...", reply_markup=ReplyKeyboardRemove())
            result = await get_photo_from_yadisk(date_str)
            if result is None:
                await message.answer(f"Фото за <b>{date_str}</b> не найдено.", parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
            else:
                photo_bytes, filename = result
                await message.answer_photo(BufferedInputFile(photo_bytes, filename=filename), caption=f"📸 {date_str}", reply_markup=MAIN_KEYBOARD)
            return

        if mode == "month":
            month_match = MONTH_PATTERN.match(text) or re.search(r"\b(\d{2}-\d{2})\b", text)
            if not month_match:
                await message.answer("Напиши месяц в формате <b>ММ-ГГ</b>, например: <b>06-26</b>", parse_mode="HTML")
                return
            del pending_date[user_id]
            month_str = month_match.group(1)
            await message.answer(f"🔍 Ищу все фото за {month_str}...", reply_markup=ReplyKeyboardRemove())
            results = await get_photos_by_month(month_str)
            if not results:
                await message.answer(f"Фото за <b>{month_str}</b> не найдено.", parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
            else:
                await message.answer(f"📅 Найдено: <b>{len(results)}</b> фото", parse_mode="HTML")
                for photo_bytes, filename in results:
                    label = filename.rsplit(".", 1)[0]
                    await message.answer_photo(BufferedInputFile(photo_bytes, filename=filename), caption=f"📸 {label}")
                await message.answer("Готово!", reply_markup=MAIN_KEYBOARD)
            return

        if mode == "delete":
            date_match = DATE_PATTERN.search(text)
            if not date_match:
                await message.answer("Напиши дату в формате <b>ДД-ММ-ГГ</b>, например: <b>08-06-26</b>", parse_mode="HTML")
                return
            del pending_date[user_id]
            date_str = date_match.group(1)
            result = await get_photo_from_yadisk(date_str)
            if result is None:
                await message.answer(f"Фото за <b>{date_str}</b> не найдено.", parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
                return
            _, filename = result
            pending_delete[user_id] = filename
            kb = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="да"), KeyboardButton(text="нет")]],
                resize_keyboard=True
            )
            await message.answer(f"Удалить фото за <b>{date_str}</b>?", parse_mode="HTML", reply_markup=kb)
            return

    # ── Кнопки главного меню ──
    if text == "📸 Найти фото по дате":
        pending_date[user_id] = "search"
        await message.answer("Напиши дату в формате <b>ДД-ММ-ГГ</b>\nНапример: <b>08-06-26</b>", parse_mode="HTML")
        return

    if text == "📅 Все фото за месяц":
        pending_date[user_id] = "month"
        await message.answer("Напиши месяц в формате <b>ММ-ГГ</b>\nНапример: <b>06-26</b>", parse_mode="HTML")
        return

    if text == "⬆️ Загрузить фото":
        await message.answer("📎 Пришли фото — я спрошу дату.")
        return

    if text == "🗑 Удалить фото":
        pending_date[user_id] = "delete"
        await message.answer("Напиши дату фото которое удалить <b>ДД-ММ-ГГ</b>\nНапример: <b>08-06-26</b>", parse_mode="HTML")
        return

    # ── Пользователь прислал фото ──
    if message.photo:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(file_url)
            file_bytes = resp.content
        pending_upload[user_id] = file_bytes
        await message.answer("📅 Напиши дату для этого фото <b>ДД-ММ-ГГ</b>\nНапример: <b>08-06-26</b>\n\nИли напиши <b>отмена</b>.", parse_mode="HTML")
        return

    await message.answer("Выбери действие:", reply_markup=MAIN_KEYBOARD)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
