import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# --- НАСТРОЙКИ ---
TOKEN = "7805909443:AAF0OxG7CxENDnEJuP2lMRi1r_33ghkGL54"

# Бесплатный публичный HTTPS-прокси (для теста)
# ⚠️ такие прокси иногда "умирают", тогда надо будет заменить на другой
PROXY_URL = "http://51.91.144.39:80"

bot = Bot(token=TOKEN, proxy=PROXY_URL)
dp = Dispatcher()


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
