import asyncio
import csv
import io
import logging
import os
import re
import time

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from openai import AsyncOpenAI, APIError, APIConnectionError, APITimeoutError

# ==========================================================
#                     НАСТРОЙКИ / ТОКЕНЫ
# ==========================================================
# Токены НЕ хранятся в коде — они задаются в Render как Environment Variables
# (Settings -> Environment -> Add Environment Variable), это безопаснее для
# публичного репозитория на GitHub.
BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]

# Твой личный Telegram Chat ID (число), куда бот будет присылать уведомления
# о номерах телефонов клиентов, желающих обратный звонок.
# Задаётся в Render как Environment Variable: ADMIN_CHAT_ID
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

# Модель, которую будем использовать через OpenRouter
MODEL_NAME = "openai/gpt-4o-mini"

# Порт для Render (обязательно из переменной окружения, иначе Render не увидит открытый порт)
PORT = int(os.environ.get("PORT", 10000))

# Ссылки на Google Таблицы с каталогом и правилами поведения бота.
# Задаются в Render как Environment Variables (необязательно — есть значения по умолчанию).
# Формат ссылки: .../gviz/tq?tqx=out:csv&gid=НОМЕР_ВКЛАДКИ
CATALOG_CSV_URL = os.environ.get(
    "CATALOG_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1OAaGhQ-cG85g-jPecwIrH6y1fL3WOIHLkTN_gCjOXXc/gviz/tq?tqx=out:csv&gid=0",
)
RULES_CSV_URL = os.environ.get(
    "RULES_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1OAaGhQ-cG85g-jPecwIrH6y1fL3WOIHLkTN_gCjOXXc/gviz/tq?tqx=out:csv&gid=213018400",
)

# Базовый шаблон промпта — каталог и правила подставляются в него на лету
# из Google Таблиц, чтобы владелец мог менять их без правки кода.
BASE_PROMPT_TEMPLATE = """Ты — Дима, живой онлайн-консультант магазина салютов и фейерверков BENEFIS UZ в Ташкенте.
Ты дружелюбный, вежливый, разбираешься в товаре и искренне хочешь помочь клиенту выбрать то, что подойдёт именно под его повод (свадьба, день рождения, корпоратив, Навруз и т.д.).

=== КАТАЛОГ ТОВАРОВ (используй ТОЛЬКО эти данные, ничего не придумывай сверху) ===
{catalog}

Если клиент спрашивает про товар, которого нет в этом списке — честно скажи, что именно этой позиции сейчас нет, и предложи ближайшие варианты из каталога, либо предложи уточнить у менеджера.

=== КАК ПОКАЗЫВАТЬ ФОТО/ВИДЕО ===
Ты НЕ отправляешь картинки сам. Когда клиент хочет посмотреть, как выглядит товар — присылай ему ссылку на конкретный пост в Telegram-канале (из каталога выше), он сам откроет и увидит.

=== ПРАВИЛА ПОВЕДЕНИЯ, ИНФОРМАЦИЯ О КОМПАНИИ И КОНТАКТЫ ===
{rules}

=== ОБЩИЕ ПРАВИЛА ОБЩЕНИЯ ===
Отвечай кратко и по делу, избегай длинных нечитаемых простыней текста.
Используй 1-3 уместных эмодзи в каждом сообщении, подходящих по смыслу и настроению момента.
НЕ используй давление или ложные ограничения: никогда не говори "осталось мало" или про несуществующие скидки/акции, если это не указано явно выше.
НИКОГДА не советуй и не упоминай другие магазины или конкурентов — ты представляешь исключительно этот магазин.
ВАЖНО: если клиент переспрашивает, спорит или настаивает на другой версии фактов — НЕ меняй свой ответ и не путайся, если ты уже точно назвал характеристику или цену из каталога выше. Твёрдо повторяй правильные данные, а не соглашайся с клиентом просто чтобы не спорить.
Если клиент прямо в сообщении присылает свой номер телефона и просит перезвонить/связаться — уверенно подтверди, что его номер принят и передан менеджеру, а не просто говори "я не могу позвонить".
Никогда не выдумывай ответ, если не уверен — честно скажи, что уточнишь у менеджера, и дай контакт из правил выше."""

# Кэш данных из таблиц, чтобы не дёргать Google при каждом сообщении клиента
CACHE_TTL_SECONDS = 300  # 5 минут
_catalog_cache = {"text": None, "ts": 0.0}
_rules_cache = {"text": None, "ts": 0.0}


async def fetch_csv_text(url: str) -> str:
    """Скачивает CSV-содержимое по ссылке на Google Таблицу."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            return await resp.text()


def format_catalog_csv(raw_csv: str) -> str:
    """Превращает CSV каталога в читаемый список товаров для промпта.
    Работает с любыми названиями колонок — просто собирает 'колонка: значение'."""
    reader = csv.reader(io.StringIO(raw_csv))
    rows = list(reader)
    if not rows:
        return "Каталог пуст."
    headers = [h.strip() for h in rows[0]]
    lines = []
    for i, row in enumerate(rows[1:], start=1):
        parts = []
        for header, value in zip(headers, row):
            value = value.strip()
            if value:
                parts.append(f"{header}: {value}")
        if parts:
            lines.append(f"{i}. " + "; ".join(parts))
    return "\n".join(lines) if lines else "Каталог пуст."


def format_rules_csv(raw_csv: str) -> str:
    """Превращает CSV с правилами (Параметр, Значение) в читаемый текст для промпта."""
    reader = csv.reader(io.StringIO(raw_csv))
    rows = list(reader)
    if not rows:
        return ""
    lines = []
    for row in rows[1:]:
        if len(row) >= 2 and row[0].strip():
            lines.append(f"{row[0].strip()}: {row[1].strip()}")
    return "\n".join(lines)


async def get_catalog_text() -> str:
    now = time.time()
    if _catalog_cache["text"] and now - _catalog_cache["ts"] < CACHE_TTL_SECONDS:
        return _catalog_cache["text"]
    try:
        raw = await fetch_csv_text(CATALOG_CSV_URL)
        formatted = format_catalog_csv(raw)
        _catalog_cache["text"] = formatted
        _catalog_cache["ts"] = now
        return formatted
    except Exception as e:
        logger.warning("Не удалось обновить каталог из Google Таблицы: %s", e)
        return _catalog_cache["text"] or "Каталог временно недоступен — сообщи клиенту, что уточнишь у менеджера."


async def get_rules_text() -> str:
    now = time.time()
    if _rules_cache["text"] and now - _rules_cache["ts"] < CACHE_TTL_SECONDS:
        return _rules_cache["text"]
    try:
        raw = await fetch_csv_text(RULES_CSV_URL)
        formatted = format_rules_csv(raw)
        _rules_cache["text"] = formatted
        _rules_cache["ts"] = now
        return formatted
    except Exception as e:
        logger.warning("Не удалось обновить правила из Google Таблицы: %s", e)
        return _rules_cache["text"] or ""


async def build_system_prompt() -> str:
    catalog_text = await get_catalog_text()
    rules_text = await get_rules_text()
    return BASE_PROMPT_TEMPLATE.format(catalog=catalog_text, rules=rules_text)

# ==========================================================
#                     ЛОГИРОВАНИЕ
# ==========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("benefis_bot")

# ==========================================================
#              ИНИЦИАЛИЗАЦИЯ БОТА И OPENROUTER-КЛИЕНТА
# ==========================================================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

ai_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# Простое хранилище истории диалога в памяти (chat_id -> список сообщений)
# Для продакшена лучше заменить на Redis/БД, но для старта этого достаточно.
user_histories: dict[int, list[dict]] = {}
MAX_HISTORY_MESSAGES = 10  # сколько последних сообщений храним на пользователя

# Слова-триггеры, при которых клиента считаем "горячим" и шлём тебе уведомление
LEAD_KEYWORDS = [
    "хочу купить", "хочу заказать", "хочу сделать заказ", "оформить заказ",
    "куплю", "закажу", "перезвоните", "перезвони", "свяжитесь", "готов купить",
    "готова купить", "оплачу", "как оплатить", "хочу оформить",
]
PHONE_REGEX = re.compile(r"(\+?\d[\d\-\s\(\)]{7,}\d)")


def detect_lead(text: str) -> bool:
    """Проверяет, похоже ли сообщение клиента на готовность к покупке или номер телефона."""
    lowered = text.lower()
    if any(keyword in lowered for keyword in LEAD_KEYWORDS):
        return True
    if PHONE_REGEX.search(text):
        return True
    return False


async def notify_admin_dialog(message: Message, answer: str, is_lead: bool) -> None:
    """Присылает владельцу лог диалога: сообщение клиента + ответ бота.
    Помогает следить за качеством работы бота и ловить проблемные моменты."""
    if not ADMIN_CHAT_ID:
        return
    client_name = message.from_user.full_name or "Клиент"
    client_username = f"@{message.from_user.username}" if message.from_user.username else "без username"
    tag = "🔥 <b>Горячий клиент!</b>\n" if is_lead else "💬 <b>Диалог с ботом</b>\n"
    notify_text = (
        f"{tag}\n"
        f"Имя: {client_name}\n"
        f"Username: {client_username}\n\n"
        f"<b>Клиент:</b> {message.text}\n\n"
        f"<b>Бот:</b> {answer}\n\n"
        f"Написать клиенту: tg://user?id={message.from_user.id}"
    )
    try:
        await bot.send_message(int(ADMIN_CHAT_ID), notify_text)
    except Exception as e:
        logger.warning("Не удалось отправить лог диалога админу: %s", e)


async def notify_admin_new_entry(message: Message) -> None:
    """Присылает владельцу мгновенное уведомление о новом клиенте, зашедшем в бота."""
    if not ADMIN_CHAT_ID:
        return
    client_name = message.from_user.full_name or "Клиент"
    client_username = f"@{message.from_user.username}" if message.from_user.username else "без username"
    notify_text = (
        "🆕 <b>Новый клиент зашёл в бота!</b>\n\n"
        f"Имя: {client_name}\n"
        f"Username: {client_username}\n\n"
        f"Написать клиенту: tg://user?id={message.from_user.id}"
    )
    try:
        await bot.send_message(int(ADMIN_CHAT_ID), notify_text)
    except Exception as e:
        logger.warning("Не удалось отправить уведомление о новом клиенте: %s", e)


# ==========================================================
#                     ОБРАБОТЧИКИ TELEGRAM
# ==========================================================
@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    user_histories[message.chat.id] = []
    await message.answer(
        "Здравствуйте! 🎆\n"
        "Добро пожаловать в <b>BENEFIS UZ</b> — магазин салютов и фейерверков в Ташкенте.\n\n"
        "Я скину вам всю информацию о салютах, но для начала хочу, чтобы вы ознакомились "
        "с нашим прозрачным и открытым чатом отзывов клиентов 🙌\n"
        "👉 https://t.me/otzivsalyutuz\n\n"
        "А теперь с радостью отвечу на ваши вопросы: об ассортименте, ценах, доставке "
        "и всём остальном. Просто напишите свой вопрос! 😊"
    )
    asyncio.create_task(notify_admin_new_entry(message))


@dp.message(F.text)
async def handle_text(message: Message) -> None:
    chat_id = message.chat.id
    user_text = message.text

    history = user_histories.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})
    # Обрезаем историю, чтобы не раздувать контекст
    history = history[-MAX_HISTORY_MESSAGES:]
    user_histories[chat_id] = history

    # Определяем, похоже ли сообщение на готовность к покупке (для пометки в логе)
    is_lead = detect_lead(user_text)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    await bot.send_chat_action(chat_id, action="typing")

    try:
        response = await ai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.3,
            max_tokens=800,
        )
        answer = response.choices[0].message.content.strip()

    except APITimeoutError:
        logger.warning("OpenRouter timeout для chat_id=%s", chat_id)
        answer = (
            "Извините, сервер отвечает дольше обычного. "
            "Попробуйте, пожалуйста, повторить вопрос через минуту."
        )
    except APIConnectionError:
        logger.warning("Ошибка соединения с OpenRouter для chat_id=%s", chat_id)
        answer = (
            "Не удалось связаться с сервером ИИ. "
            "Пожалуйста, попробуйте немного позже."
        )
    except APIError as e:
        logger.error("Ошибка OpenRouter API: %s", e)
        answer = (
            "Произошла техническая ошибка при обработке вашего запроса. "
            "Пожалуйста, свяжитесь с нашим менеджером или попробуйте позже."
        )
    except Exception as e:  # на всякий случай — не даём боту упасть
        logger.exception("Непредвиденная ошибка: %s", e)
        answer = "Что-то пошло не так. Попробуйте, пожалуйста, ещё раз."

    else:
        history.append({"role": "assistant", "content": answer})
        user_histories[chat_id] = history[-MAX_HISTORY_MESSAGES:]

    await message.answer(answer)
    asyncio.create_task(notify_admin_dialog(message, answer, is_lead))


# ==========================================================
#              ЛЁГКИЙ ВЕБ-СЕРВЕР ДЛЯ RENDER (aiohttp)
# ==========================================================
async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="BENEFIS UZ bot is alive")


async def start_web_server() -> None:
    app = web.Application()
    app.router.add_get("/", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("Веб-сервер запущен на порту %s", PORT)


# ==========================================================
#                        ТОЧКА ВХОДА
# ==========================================================
async def main() -> None:
    # Снимаем вебхук и сбрасываем "зависшие" апдейты — защита от TelegramConflictError
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhook сброшен, старые апдейты очищены")

    # Запускаем веб-сервер в фоне, чтобы Render видел открытый порт
    asyncio.create_task(start_web_server())

    logger.info("Запускаем polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен вручную")
