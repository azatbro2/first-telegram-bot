import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import dotenv

# --- НАСТРОЙКИ ---
# Бесплатный публичный HTTPS-прокси (для теста)
# ⚠️ такие прокси иногда "умирают", тогда надо будет заменить на другой

bot = Bot(token=TOKEN, proxy=PROXY_URL)
dp = Dispatcher()

TOKEN = dotenv.get_key(".env", "TOKEN")
PROXY_URL = dotenv.get_key(".env", "PROXY_URL")

@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("Бот запустился через прокси ✅")


@dp.message()
async def echo(message: types.Message):
    await message.answer(f"Эхо: {message.text}")


async def main():
    print("Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
