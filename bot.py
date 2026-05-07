"""
Свадебный квест-бот: игра "Быки и коровы" со словами
=====================================================
Запуск (разработка):  python3 bot.py           — режим polling
Запуск (продакшн):    USE_WEBHOOK=1 python3 bot.py — режим webhook

Слова:       отредактируй words.txt (каждое слово с новой строки)
Результаты:  отправь команду /results в боте
Игроки:      отправь команду /players — список активных игр
Сброс:       отправь команду /newteam — начать с новой командой
"""

import asyncio
import logging
import os
import random
import sqlite3
from datetime import datetime, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ── Настройка логирования ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Конфигурация ───────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORDS_FILE = os.path.join(BASE_DIR, "words.txt")
DB_FILE = os.path.join(BASE_DIR, "database.db")

# Режим работы: webhook (продакшн) или polling (разработка)
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "").lower() in ("1", "true", "yes")
# Порт для HTTP-сервера в режиме webhook
PORT = int(os.environ.get("PORT", "8080"))
# Домен берётся из переменной окружения, которую Replit задаёт автоматически
REPLIT_DOMAINS = os.environ.get("REPLIT_DOMAINS", "")


# ── База данных (SQLite) ───────────────────────────────────────────────────────

def init_db():
    """Создаёт таблицу результатов, если её ещё нет."""
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            team_name   TEXT NOT NULL,
            word        TEXT NOT NULL,
            attempts    INTEGER NOT NULL,
            started_at  TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            seconds     INTEGER NOT NULL
        )
    """)
    con.commit()
    con.close()


def save_result(team_name: str, word: str, attempts: int,
                started_at: datetime, finished_at: datetime):
    """Сохраняет результат команды в базу данных."""
    seconds = int((finished_at - started_at).total_seconds())
    con = sqlite3.connect(DB_FILE)
    con.execute(
        "INSERT INTO results (team_name, word, attempts, started_at, finished_at, seconds) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            team_name,
            word,
            attempts,
            started_at.isoformat(),
            finished_at.isoformat(),
            seconds,
        ),
    )
    con.commit()
    con.close()


def get_results() -> list[dict]:
    """Возвращает все результаты, отсортированные по времени прохождения."""
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT team_name, word, attempts, seconds "
        "FROM results ORDER BY seconds ASC"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ── Загрузка слов ─────────────────────────────────────────────────────────────

def load_words() -> list[str]:
    """Читает слова из words.txt и возвращает их в верхнем регистре."""
    with open(WORDS_FILE, encoding="utf-8") as f:
        words = [line.strip().upper() for line in f if line.strip()]
    if not words:
        raise ValueError("Файл words.txt пуст — добавь хотя бы одно слово.")
    return words


# ── Логика "Быки и коровы" ────────────────────────────────────────────────────

def check_guess(secret: str, guess: str) -> tuple[int, int]:
    """
    Сравнивает попытку с загаданным словом.
    Возвращает (быки, коровы):
      бык  — буква стоит на правильном месте
      корова — буква есть в слове, но стоит не там
    """
    bulls = 0

    # Считаем быков
    secret_remaining = []
    guess_remaining = []
    for s_ch, g_ch in zip(secret, guess):
        if s_ch == g_ch:
            bulls += 1
        else:
            secret_remaining.append(s_ch)
            guess_remaining.append(g_ch)

    # Считаем коров из оставшихся букв
    cows = 0
    for g_ch in guess_remaining:
        if g_ch in secret_remaining:
            cows += 1
            secret_remaining.remove(g_ch)

    return bulls, cows


def format_seconds(seconds: int) -> str:
    """Форматирует секунды в строку вида 'X мин Y сек'."""
    m, s = divmod(seconds, 60)
    if m:
        return f"{m} мин {s} сек"
    return f"{s} сек"


# ── Состояния FSM (конечный автомат) ──────────────────────────────────────────

class GameState(StatesGroup):
    waiting_for_team = State()   # Ожидаем название команды
    playing = State()            # Команда угадывает слово


# ── Глобальный словарь активных игр ───────────────────────────────────────────
# Ключ: user_id (int)
# Значение: dict с полями team_name, started_at, attempts, word_len

active_games: dict[int, dict] = {}


# ── Обработчики бота ──────────────────────────────────────────────────────────

router_dp = Dispatcher(storage=MemoryStorage())


@router_dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Обрабатывает команду /start."""
    data = await state.get_data()

    # Если команда уже играет — продолжаем
    if data.get("team_name") and data.get("secret_word"):
        team = data["team_name"]
        word = data["secret_word"]
        attempts = data.get("attempts", 0)
        await message.answer(
            f"👋 С возвращением, команда *{team}*!\n\n"
            f"Вы продолжаете игру. Загаданное слово состоит из *{len(word)} букв*.\n"
            f"Попыток сделано: *{attempts}*\n\n"
            "Введи слово-попытку:",
            parse_mode="Markdown",
        )
        return

    # Иначе — начинаем заново
    await state.clear()
    await state.set_state(GameState.waiting_for_team)
    await message.answer(
        "🎉 Добро пожаловать на свадебный квест!\n\n"
        "Введите название вашей команды:"
    )


@router_dp.message(GameState.waiting_for_team)
async def handle_team_name(message: Message, state: FSMContext):
    """Получает название команды и начинает игру."""
    team_name = message.text.strip()
    if not team_name:
        await message.answer("Пожалуйста, введи название команды.")
        return

    words = load_words()
    secret = random.choice(words)

    started = datetime.now(timezone.utc)
    await state.update_data(
        team_name=team_name,
        secret_word=secret,
        attempts=0,
        started_at=started.isoformat(),
    )
    await state.set_state(GameState.playing)

    active_games[message.from_user.id] = {
        "team_name": team_name,
        "started_at": started,
        "attempts": 0,
        "word_len": len(secret),
    }

    await message.answer(
        f"✅ Команда *{team_name}* зарегистрирована!\n\n"
        f"🎯 Загадано слово из *{len(secret)} букв*.\n\n"
        "Правила:\n"
        "🐂 *Бык* — буква на правильном месте\n"
        "🐄 *Корова* — буква есть в слове, но стоит не там\n\n"
        "Введи первое слово-попытку:",
        parse_mode="Markdown",
    )


@router_dp.message(GameState.playing)
async def handle_guess(message: Message, state: FSMContext):
    """Обрабатывает попытку угадать слово."""
    data = await state.get_data()
    secret = data["secret_word"]
    team_name = data["team_name"]
    attempts = data.get("attempts", 0)
    started_at = datetime.fromisoformat(data["started_at"])

    guess = message.text.strip().upper()

    if len(guess) != len(secret):
        await message.answer(
            f"⚠️ Слово должно содержать *{len(secret)} букв*. "
            f"Ты ввёл слово из {len(guess)} букв. Попробуй ещё раз.",
            parse_mode="Markdown",
        )
        return

    if not guess.isalpha():
        await message.answer("⚠️ Введи слово, состоящее только из букв.")
        return

    attempts += 1
    await state.update_data(attempts=attempts)

    if message.from_user.id in active_games:
        active_games[message.from_user.id]["attempts"] = attempts

    bulls, cows = check_guess(secret, guess)

    if bulls == len(secret):
        finished_at = datetime.now(timezone.utc)
        elapsed = int((finished_at - started_at).total_seconds())

        save_result(team_name, secret, attempts, started_at, finished_at)
        active_games.pop(message.from_user.id, None)
        await state.clear()

        await message.answer(
            f"🎉 *Поздравляем, команда {team_name}!*\n\n"
            f"Вы угадали слово *{secret}*!\n\n"
            f"⏱ Время прохождения: *{format_seconds(elapsed)}*\n"
            f"🔢 Количество попыток: *{attempts}*\n\n"
            "Ваш результат сохранён. Молодцы! 🏆",
            parse_mode="Markdown",
        )
        return

    await message.answer(
        f"*{guess}* → 🐂 {bulls} бык(а/ов), 🐄 {cows} корова(ы/ов)\n"
        f"Попытка №{attempts}. Продолжай!",
        parse_mode="Markdown",
    )


@router_dp.message(Command("newteam"))
async def cmd_newteam(message: Message, state: FSMContext):
    """/newteam — сбросить текущую игру и ввести новое название команды."""
    data = await state.get_data()
    old_team = data.get("team_name")

    await state.clear()
    active_games.pop(message.from_user.id, None)

    note = f"Игра команды *{old_team}* сброшена.\n\n" if old_team else ""
    await state.set_state(GameState.waiting_for_team)
    await message.answer(
        f"{note}🔄 Введите новое название команды, чтобы начать заново:",
        parse_mode="Markdown",
    )


@router_dp.message(Command("hint"))
async def cmd_hint(message: Message, state: FSMContext):
    """/hint — открывает одну случайную букву. Только одна подсказка за игру."""
    data = await state.get_data()
    secret = data.get("secret_word")

    if not secret:
        await message.answer("Сначала начни игру с помощью /start.")
        return

    if data.get("hint_used"):
        mask = data.get("hint_mask", "*" * len(secret))
        await message.answer(
            f"💡 Подсказка уже была использована:\n\n`{mask}`\n\n"
            "Больше подсказок нет — угадывай! 😉",
            parse_mode="Markdown",
        )
        return

    pos = random.randrange(len(secret))
    mask = " ".join(secret[i] if i == pos else "*" for i in range(len(secret)))

    await state.update_data(hint_used=True, hint_mask=mask)
    await message.answer(
        f"💡 *Подсказка:*\n\n`{mask}`\n\n"
        f"Буква *{secret[pos]}* стоит на позиции *{pos + 1}*.\n"
        "Это единственная подсказка — используй её с умом! 🎯",
        parse_mode="Markdown",
    )


@router_dp.message(Command("players"))
async def cmd_players(message: Message):
    """/players — список команд, чья игра ещё не завершена."""
    if not active_games:
        await message.answer("Сейчас нет активных игр.")
        return

    now = datetime.now(timezone.utc)
    lines = ["🎮 *Активные игры*\n"]
    for entry in active_games.values():
        elapsed = int((now - entry["started_at"]).total_seconds())
        lines.append(
            f"👥 *{entry['team_name']}*\n"
            f"   ⏱ в игре {format_seconds(elapsed)} | 🔢 {entry['attempts']} попыток"
        )
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router_dp.message(Command("clearallresults"))
async def cmd_clearallresults(message: Message):
    """/clearallresults — удаляет все записи из таблицы результатов."""
    con = sqlite3.connect(DB_FILE)
    con.execute("DELETE FROM results")
    con.commit()
    con.close()
    await message.answer("🗑 Все результаты удалены. Таблица очищена.")


@router_dp.message(Command("results"))
async def cmd_results(message: Message):
    """/results — таблица результатов всех команд."""
    rows = get_results()
    if not rows:
        await message.answer("Пока нет ни одного результата.")
        return

    lines = ["🏆 *Таблица результатов*\n"]
    for i, row in enumerate(rows, start=1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(
            f"{medal} *{row['team_name']}*\n"
            f"   ⏱ {format_seconds(row['seconds'])} | 🔢 {row['attempts']} попыток | слово: {row['word']}"
        )
    await message.answer("\n".join(lines), parse_mode="Markdown")


# ── Запуск бота ───────────────────────────────────────────────────────────────

async def health_handler(request: web.Request) -> web.Response:
    """Health check endpoint для деплоя."""
    return web.Response(text="ok")


async def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Переменная TELEGRAM_BOT_TOKEN не задана!")

    init_db()
    logger.info("База данных инициализирована.")

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )

    if USE_WEBHOOK:
        # ── Режим Webhook (продакшн) ───────────────────────────────────────────
        # Telegram присылает обновления на наш URL — не нужен постоянный polling.
        # Работает с autoscale-деплоем на Replit.

        # Берём первый домен из списка (основной публичный адрес)
        domain = REPLIT_DOMAINS.split(",")[0].strip()
        if not domain:
            raise RuntimeError("REPLIT_DOMAINS не задан — не могу зарегистрировать webhook.")

        webhook_path = "/webhook"
        webhook_url = f"https://{domain}{webhook_path}"

        # Регистрируем webhook в Telegram
        await bot.set_webhook(webhook_url, allowed_updates=router_dp.resolve_used_update_types())
        logger.info(f"Webhook зарегистрирован: {webhook_url}")

        # Создаём aiohttp-приложение
        app = web.Application()
        app.router.add_get("/api/healthz", health_handler)

        # SimpleRequestHandler принимает POST от Telegram и передаёт dispatcher'у
        SimpleRequestHandler(dispatcher=router_dp, bot=bot).register(app, path=webhook_path)
        setup_application(app, router_dp, bot=bot)

        logger.info(f"HTTP-сервер запущен на порту {PORT}")
        web.run_app(app, host="0.0.0.0", port=PORT)

    else:
        # ── Режим Polling (разработка) ────────────────────────────────────────
        # Бот сам опрашивает Telegram. Удобно для локальной разработки.
        # Сначала снимаем webhook, если он был зарегистрирован ранее.
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Бот запущен в режиме polling...")
        try:
            await router_dp.start_polling(bot, allowed_updates=router_dp.resolve_used_update_types())
        finally:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
