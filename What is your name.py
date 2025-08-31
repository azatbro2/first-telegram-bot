# -*- coding: utf-8 -*-
"""
Аукцион-картины для Telegram (aiogram 3.x), один файл.

Фишки:
- Каждый игрок добавляет 2 картины: как фото ИЛИ через WebApp-рисовалку.
- У лота есть реальная стоимость (100..1000, кратно 10). Стартовая цена ниже реальной.
- Торги с кнопками (+100/+200/+300/Пасс), 10-секундный таймер, сброс на ставке.
- Продаём, если время вышло с последней ставкой ИЛИ все кроме лидера нажали Пасс.
- В финале капитал = деньги + сумма реальных цен купленных лотов (минус 1500, если брал кредит).
- Кнопка «Рестарт» только в конце. При рестарте — полная очистка.

Важно про рисовалку: Telegram WebApp требует HTTPS.
Запусти локально сервер (этот файл его поднимет на :8080) и открой туннель:
  ngrok http 8080
Скопируй https-url из ngrok в DRAW_WEBAPP_URL ниже.
"""

import asyncio
import base64
import json
import logging
import random
from io import BytesIO
from typing import Dict, Any, List
import os
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    BufferedInputFile, WebAppInfo,
)

load_dotenv()
# ====== НАСТРОЙКИ ======
API_TOKEN = os.getenv("API_TOKEN")
START_MONEY = 3000
LOAN_PLUS = 1000
LOAN_PAYBACK = 1500

MAX_ARTS_PER_PLAYER = 2

# реальная стоимость лота
REAL_MIN = 100
REAL_MAX = 3500
# стартовая цена должна быть ниже реальной
START_OFFSETS = [100, 200, 300]  # насколько ниже реальной
# таймер торгов (сек)
BID_TIMER_SEC = 10

# URL рисовалки (HTTPS). Пример: "https://<твой_ngrok>.ngrok.io/draw"
DRAW_WEBAPP_URL = os.getenv("DRAW_WEBAPP_URL")  

# ====== ЛОГИ ======
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("auction")

bot = Bot(API_TOKEN, parse_mode=None)
dp = Dispatcher()

# ====== СОСТОЯНИЕ ИГРЫ ======
class Player:
    def __init__(self, uid: int, name: str, username: str | None):
        self.id = uid
        self.name = name
        self.username = username
        self.money = START_MONEY
        self.loan = False
        self.arts_created = 0  # сколько картин добавил (макс 2)

class Lot:
    def __init__(self, lot_id: int, author_id: int, title: str, file_id: str, real_value: int, start_price: int):
        self.id = lot_id
        self.author_id = author_id
        self.title = title
        self.file_id = file_id
        self.real_value = real_value
        self.start_price = start_price
        self.sold_to: int | None = None
        self.sold_price: int = 0

players: Dict[int, Player] = {}   # user_id -> Player
lots: List[Lot] = []              # все лоты
queue: List[int] = []             # очередь id лотов

# текущее торги
current: Dict[str, Any] = {
    "chat_id": None,
    "lot": None,           # Lot
    "active_ids": [],      # кто может ставить (не автор)
    "price": 0,
    "leader": None,        # user_id
    "passed": set(),       # user_id, пасанули в этом раунде
    "photo_msg_id": None,  # id сообщения с фото лота
    "timer_msg_id": None,  # id сообщения с таймером
    "timer_task": None,    # asyncio.Task таймера
    "auction_running": False,
}

# ====== УТИЛИТЫ ======
def round10(x: int) -> int:
    """Округление вниз до десятки: 234 -> 230."""
    return (x // 10) * 10

def make_bid_keyboard(price: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"+100 → {price+100}", callback_data=f"bid:{price+100}"),
            InlineKeyboardButton(text=f"+200 → {price+200}", callback_data=f"bid:{price+200}"),
            InlineKeyboardButton(text=f"+300 → {price+300}", callback_data=f"bid:{price+300}"),
        ],
        [InlineKeyboardButton(text="🚫 Пасс", callback_data="pass")]
    ])

def restart_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔄 Рестарт игры")]],
        resize_keyboard=True
    )

def draw_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text="🖌️ Нарисовать картину", web_app=WebAppInfo(url=DRAW_WEBAPP_URL))
        ]],
        resize_keyboard=True
    )

def ensure_player(u: types.User) -> Player:
    p = players.get(u.id)
    if not p:
        p = Player(u.id, u.first_name or str(u.id), u.username)
        players[u.id] = p
    return p

def everyone_ready() -> bool:
    if not players:
        return False
    return all(p.arts_created >= MAX_ARTS_PER_PLAYER for p in players.values())

def compute_capital(p: Player) -> int:
    value_sum = sum(l.real_value for l in lots if l.sold_to == p.id)
    cap = p.money + value_sum
    if p.loan:
        cap -= LOAN_PAYBACK
    return cap

# ====== КОМАНДЫ ======
@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    ensure_player(m.from_user)
    await m.answer(
        "🎨 Привет! Это аукцион картин.\n"
        "1) Вступай: /join\n"
        "2) Добавь 2 картины — пришли фото ИЛИ жми «🖌️ Нарисовать картину» ниже.\n"
        "Когда все добавят по 2 — аукцион стартует автоматически.",
        reply_markup=draw_kb()
    )

@dp.message(Command("join"))
async def cmd_join(m: types.Message):
    p = ensure_player(m.from_user)
    await m.answer(f"👤 {p.name} в игре! Баланс: {p.money} 💰")

@dp.message(Command("loan"))
async def cmd_loan(m: types.Message):
    p = ensure_player(m.from_user)
    if p.loan:
        await m.answer("🏦 Ты уже брал кредит.")
        return
    p.loan = True
    p.money += LOAN_PLUS
    await m.answer(f"🏦 Кредит +{LOAN_PLUS}. В конце спишется {LOAN_PAYBACK}.")

@dp.message(Command("status"))
async def cmd_status(m: types.Message):
    p = ensure_player(m.from_user)
    own = [l.id for l in lots if l.sold_to == p.id]
    await m.answer(
        f"👤 {p.name}\nБаланс: {p.money} 💰\nКредит: {'да' if p.loan else 'нет'}\n"
        f"Добавлено картин: {p.arts_created}/{MAX_ARTS_PER_PLAYER}\nКуплено: {own}"
    )

@dp.message(F.text == "🔄 Рестарт игры")
async def btn_restart(m: types.Message):
    await restart_game(m.chat.id)

@dp.message(Command("restart"))
async def cmd_restart(m: types.Message):
    await restart_game(m.chat.id)

async def restart_game(chat_id: int):
    # отменяем таймер, если шёл
    t = current.get("timer_task")
    if t and not t.done():
        t.cancel()
        with contextlib.suppress(Exception):
            await t
    # чистим всё
    players.clear()
    lots.clear()
    queue.clear()
    for k in list(current.keys()):
        current[k] = None
    current.update({"auction_running": False})
    await bot.send_message(chat_id, "♻️ Игра сброшена. /join чтобы начать заново.", reply_markup=ReplyKeyboardRemove())
# Команда /draw
@dp.message(Command("draw"))
async def draw_cmd(message: types.Message):
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(
                text="🎨 Открыть рисовалку",
                web_app=types.WebAppInfo(url="https://azatbro2.github.io/azat/")
            )]
        ]
    )
    await message.answer("Нажми кнопку ниже, чтобы начать рисовать:", reply_markup=keyboard)

# ====== ПРИЁМ ФОТО КАРТИН ======
@dp.message(F.photo)
async def on_photo(m: types.Message):
    p = ensure_player(m.from_user)
    if p.arts_created >= MAX_ARTS_PER_PLAYER:
        await m.answer("⚠️ У тебя уже 2 картины.")
        return

    # берём file_id последней (крупной) копии
    file_id = m.photo[-1].file_id

    # реальная цена и стартовая (ниже реальной)
    real = round10(random.randint(REAL_MIN, REAL_MAX))
    start = max(REAL_MIN, real - random.choice(START_OFFSETS))
    if start >= real:
        start = max(REAL_MIN, real - 100)
    start = round10(start)

    lot = Lot(len(lots)+1, p.id, f"Картина #{len(lots)+1}", file_id, real, start)
    lots.append(lot)
    p.arts_created += 1

    await m.answer(f"✅ Картина добавлена. (реальная стоимость скрыта, стартовая цена: {start})")


    # когда все по 2 — запускаем аукцион
    if everyone_ready() and not current.get("auction_running"):
        await m.answer("🔔 Все добавили по 2 картины. Через 2 сек начнём…", reply_markup=ReplyKeyboardRemove())
        await asyncio.sleep(2)
        await start_auction(m.chat.id)

# ====== ПРИЁМ ДАННЫХ ИЗ WEB APP (рисовалка) ======
@dp.message(F.web_app_data)
async def on_web_app_data(m: types.Message):
    """
    WebApp шлёт JSON строкой: {"title": "...", "png":"data:image/png;base64,...."}
    """
    p = ensure_player(m.from_user)
    if p.arts_created >= MAX_ARTS_PER_PLAYER:
        await m.answer("⚠️ У тебя уже 2 картины.")
        return

    try:
        data = json.loads(m.web_app_data.data)
        title = (data.get("title") or "").strip() or f"Картина #{len(lots)+1}"
        b64 = data.get("png", "")
        if not b64.startswith("data:image/png;base64,"):
            raise ValueError("wrong data url")
        raw = base64.b64decode(b64.split(",", 1)[1])
    except Exception:
        await m.answer("Не удалось принять рисунок 😕 Попробуй ещё раз.")
        return

    # загружаем картинку в Telegram, чтобы получить file_id
    img = BufferedInputFile(raw, filename="art.png")
    msg = await bot.send_photo(m.chat.id, img, caption="🖌️ Рисунок получен.")
    file_id = msg.photo[-1].file_id
    # чтобы не захламлять чат — можем удалить это служебное сообщение
    try:
        await bot.delete_message(m.chat.id, msg.message_id)
    except Exception:
        pass

    # цены
    real = round10(random.randint(REAL_MIN, REAL_MAX))
    start = max(REAL_MIN, real - random.choice(START_OFFSETS))
    if start >= real:
        start = max(REAL_MIN, real - 100)
    start = round10(start)

    lot = Lot(len(lots)+1, p.id, title, file_id, real, start)
    lots.append(lot)
    p.arts_created += 1
    await m.answer(f"✅ Рисунок сохранён как «{title}». Стартовая цена: {start}")

    if everyone_ready() and not current.get("auction_running"):
        await m.answer("🔔 Все добавили по 2 картины. Через 2 сек начнём…", reply_markup=ReplyKeyboardRemove())
        await asyncio.sleep(2)
        await start_auction(m.chat.id)

# ====== СТАРТ АУКЦИОНА ======
async def start_auction(chat_id: int):
    current["auction_running"] = True
    # формируем очередь и мешаем
    queue[:] = [l.id for l in lots]
    random.shuffle(queue)
    await next_lot(chat_id)

async def next_lot(chat_id: int):
    # отменяем старый таймер
    t = current.get("timer_task")
    if t and not t.done():
        t.cancel()
        with contextlib.suppress(Exception):
            await t

    if not queue:
        current["auction_running"] = False
        await show_results(chat_id)
        return

    lot_id = queue.pop(0)
    lot = next(l for l in lots if l.id == lot_id)

    active_ids = [pid for pid in players if pid != lot.author_id]
    if not active_ids:
        # никому продавать
        lot.sold_to = lot.author_id
        lot.sold_price = 0
        await bot.send_message(chat_id, f"⚠️ Лот №{lot.id} остался у автора (нет покупателей).")
        await next_lot(chat_id)
        return

    current.update({
        "chat_id": chat_id,
        "lot": lot,
        "active_ids": active_ids,
        "price": lot.start_price,
        "leader": None,
        "passed": set(),
        "photo_msg_id": None,
        "timer_msg_id": None,
        "timer_task": None,
    })

    # публикуем лот (без автора/названия)
    caption = f"🎨 ЛОТ №{lot.id}\n💰 Стартовая цена: {lot.start_price}\nНажимайте на ставки или «Пасс»."
    msg = await bot.send_photo(chat_id, lot.file_id, caption=caption, reply_markup=make_bid_keyboard(lot.start_price))
    current["photo_msg_id"] = msg.message_id

    # отдельное сообщение-таймер
    tmsg = await bot.send_message(chat_id, f"⏳ Осталось: {BID_TIMER_SEC} сек.")
    current["timer_msg_id"] = tmsg.message_id
    current["timer_task"] = asyncio.create_task(run_timer())

async def run_timer():
    """Отдельный таймер: тикает, редактирует сообщение, завершает лот при нуле."""
    try:
        sec = BID_TIMER_SEC
        while sec > 0:
            await asyncio.sleep(1)
            sec -= 1
            # может уже финализирован
            if not current["lot"]:
                return
            try:
                await bot.edit_message_text(f"⏳ Осталось: {sec} сек.", chat_id=current["chat_id"], message_id=current["timer_msg_id"])
            except Exception:
                pass

        # таймер истёк
        if current["leader"] is not None:
            await finalize_sale(reason="⏰ Время вышло")
        else:
            # никто не сделал ставки
            lot: Lot = current["lot"]
            lot.sold_to = lot.author_id
            lot.sold_price = 0
            await bot.edit_message_caption(
                chat_id=current["chat_id"], message_id=current["photo_msg_id"],
                caption=f"🎨 ЛОТ №{lot.id}\n❌ Никто не сделал ставку. Лот остался у автора.",
                reply_markup=None
            )
            await cleanup_after_lot()
            await next_lot(current["chat_id"])
    except asyncio.CancelledError:
        # сбросили таймер (кто-то поставил)
        return

async def cleanup_after_lot():
    # удалить таймер-сообщение
    try:
        if current["timer_msg_id"]:
            await bot.delete_message(current["chat_id"], current["timer_msg_id"])
    except Exception:
        pass
    current["timer_msg_id"] = None
    # остановить и забыть таймер
    t = current.get("timer_task")
    if t and not t.done():
        t.cancel()
        with contextlib.suppress(Exception):
            await t
    current["timer_task"] = None
    # обнулить контекст лота
    current["lot"] = None
    current["leader"] = None
    current["passed"] = set()
    current["active_ids"] = []

async def finalize_sale(reason: str):
    lot: Lot = current["lot"]
    leader_id = current["leader"]
    price = current["price"]
    buyer = players[leader_id]
    # списываем деньги
    buyer.money -= price
    lot.sold_to = leader_id
    lot.sold_price = price

    try:
        await bot.edit_message_caption(
            chat_id=current["chat_id"], message_id=current["photo_msg_id"],
            caption=f"✅ {reason} — ЛОТ №{lot.id} продан {buyer.name} за {price} 💰\n"
                    f"💎 Реальная стоимость будет раскрыта в финале.",
            reply_markup=None
        )
    except Exception:
        pass

    await cleanup_after_lot()
    await asyncio.sleep(1)
    await next_lot(current["chat_id"])

# ====== КОЛБЭКИ СТАВОК / ПАСС ======
@dp.callback_query(F.data.startswith("bid:"))
async def on_bid(c: types.CallbackQuery):
    if not current["lot"]:
        await c.answer("Сейчас нет активного лота.", show_alert=True)
        return
    uid = c.from_user.id
    if uid not in current["active_ids"]:
        await c.answer("Автор не может ставить на свой лот.", show_alert=True)
        return

    new_price = int(c.data.split(":")[1])
    # цены всегда «круглые»
    new_price = round10(new_price)

    p = players[uid]
    if p.money < new_price:
        await c.answer("Недостаточно монет 💸", show_alert=True)
        return
    if new_price <= current["price"]:
        await c.answer("Ставка должна быть больше текущей.", show_alert=True)
        return

    # принимаем ставку
    current["price"] = new_price
    current["leader"] = uid
    current["passed"] = set()  # все «Пассы» обнуляем

    # обновляем подпись лота и кнопки
    try:
        await bot.edit_message_caption(
            chat_id=current["chat_id"], message_id=current["photo_msg_id"],
            caption=f"🎨 ЛОТ №{current['lot'].id}\n📈 Текущая ставка: {current['price']} (от {p.name})",
            reply_markup=make_bid_keyboard(current["price"])
        )
    except Exception:
        pass

    # сброс таймера
    t = current.get("timer_task")
    if t and not t.done():
        t.cancel()
        with contextlib.suppress(Exception):
            await t
    # перезапускаем таймер заново на 10 сек
    tm = await bot.edit_message_text(f"⏳ Осталось: {BID_TIMER_SEC} сек.", chat_id=current["chat_id"], message_id=current["timer_msg_id"])
    current["timer_msg_id"] = tm.message_id
    current["timer_task"] = asyncio.create_task(run_timer())

    await c.answer("Ставка принята ✅")

@dp.callback_query(F.data == "pass")
async def on_pass(c: types.CallbackQuery):
    if not current["lot"]:
        await c.answer()
        return
    uid = c.from_user.id
    # учитывать только участников (не автора)
    if uid not in current["active_ids"]:
        await c.answer()
        return

    # записываем пасс
    current["passed"].add(uid)
    await c.answer("🚫 Пасс")

    # если есть лидер и ВСЕ КРОМЕ НЕГО пассанули — продаём сразу
    if current["leader"] is not None:
        others = set(current["active_ids"]) - {current["leader"]}
        if others.issubset(current["passed"]) and others:
            await finalize_sale(reason="🛎 Все пасс")
            return

    # если никто не ставил и все пассанули — лот к автору
    if current["leader"] is None and set(current["active_ids"]).issubset(current["passed"]):
        lot: Lot = current["lot"]
        lot.sold_to = lot.author_id
        lot.sold_price = 0
        try:
            await bot.edit_message_caption(
                chat_id=current["chat_id"], message_id=current["photo_msg_id"],
                caption=f"🎨 ЛОТ №{lot.id}\n❌ Все пасс. Лот остался у автора.",
                reply_markup=None
            )
        except Exception:
            pass
        await cleanup_after_lot()
        await next_lot(current["chat_id"])

# ====== ФИНАЛ ======
async def show_results(chat_id: int):
    # раскрываем авторов/названия/реальные стоимости
    lines = ["🏁 Аукцион завершён!\n"]
    for l in lots:
        author = players[l.author_id].name
        if l.sold_to:
            buyer = players[l.sold_to].name
            lines.append(f"🎨 Лот №{l.id} — «{l.title}» (автор: {author})\n"
                         f"   🏷 Продан {buyer} за {l.sold_price} 💰 | 💎 Реальная стоимость: {l.real_value}\n")
        else:
            lines.append(f"🎨 Лот №{l.id} — «{l.title}» (автор: {author})\n"
                         f"   ❌ Не продан | 💎 Реальная стоимость: {l.real_value}\n")

    # турнирная таблица
    rating = []
    for p in players.values():
        cap = compute_capital(p)
        value_sum = sum(l.real_value for l in lots if l.sold_to == p.id)
        rating.append((cap, p, value_sum))
    rating.sort(key=lambda x: x[0], reverse=True)

    lines.append("🏆 Итоги:")
    for i, (cap, p, value_sum) in enumerate(rating, start=1):
        lines.append(f"{i}. {p.name} — капитал: {cap} 💰 (баланс: {p.money}, картины: {value_sum}, кредит: {'да' if p.loan else 'нет'})")

    await bot.send_message(chat_id, "\n".join(lines), reply_markup=restart_kb())

# ====== ПРОСТОЙ ВЕБ-СЕРВЕР С РИСОВАЛКОЙ ======
# (Для реального Telegram добавь HTTPS через ngrok и пропиши DRAW_WEBAPP_URL)
import contextlib
from aiohttp import web

DRAW_HTML = """<!doctype html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Рисовалка</title>
    <style>
        body {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
            background: #1e1e1e;
            color: white;
            font-family: Arial, sans-serif;
        }
        canvas {
            border: 2px solid white;
            background: black;
            cursor: crosshair;
        }
        .controls {
            margin-top: 10px;
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            justify-content: center;
        }
        button {
            padding: 8px 15px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 16px;
            font-weight: bold;
        }
        .clear { background: crimson; color: white; }
        .save { background: limegreen; color: black; }
        .color-picker, .size-picker {
            padding: 5px;
        }
    </style>
</head>
<body>
    <h1>🎨 Рисовалка</h1>
    <canvas id="canvas" width="800" height="500"></canvas>
    <div class="controls">
        <input type="color" id="color" class="color-picker" value="#ffffff">
        <input type="range" id="size" class="size-picker" min="1" max="20" value="4">
        <button class="clear" onclick="clearCanvas()">Очистить</button>
        <button class="save" onclick="saveCanvas()">Сохранить</button>
    </div>

    <script>
        const canvas = document.getElementById("canvas");
        const ctx = canvas.getContext("2d");
        let painting = false;
        let color = document.getElementById("color").value;
        let size = document.getElementById("size").value;

        document.getElementById("color").addEventListener("change", (e) => {
            color = e.target.value;
        });

        document.getElementById("size").addEventListener("input", (e) => {
            size = e.target.value;
        });

        function startPosition(e) {
            painting = true;
            draw(e);
        }
        function endPosition() {
            painting = false;
            ctx.beginPath();
        }
        function draw(e) {
            if (!painting) return;
            ctx.lineWidth = size;
            ctx.lineCap = "round";
            ctx.strokeStyle = color;

            const rect = canvas.getBoundingClientRect();
            ctx.lineTo(e.clientX - rect.left, e.clientY - rect.top);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(e.clientX - rect.left, e.clientY - rect.top);
        }

        function clearCanvas() {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
        }

        function saveCanvas() {
            const link = document.createElement('a');
            link.download = 'drawing.png';
            link.href = canvas.toDataURL();
            link.click();
        }

        canvas.addEventListener("mousedown", startPosition);
        canvas.addEventListener("mouseup", endPosition);
        canvas.addEventListener("mousemove", draw);
    </script>
</body>
</html>
"""

async def handle_draw(request: web.Request):
    return web.Response(text=DRAW_HTML, content_type="text/html; charset=utf-8")

async def run_web_server():
    app = web.Application()
    app.router.add_get("/draw", handle_draw)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    log.info("WebApp served at https://azatbro2.github.io/azat/  (используй ngrok для HTTPS)")

# ====== ЗАПУСК ======
async def main():
    await run_web_server()         # поднимем рисовалку
    await dp.start_polling(bot)    # запустим бота (long polling)

if __name__ == "__main__":
    import contextlib
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("stopped")
