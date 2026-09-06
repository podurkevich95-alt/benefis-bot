import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from openai import OpenAI
from aiohttp import web

BOT_TOKEN = "8644196168:AAFzI44-Gu_2QiezfOfG_Z15rek6PsAIMDY"
OPENROUTER_API_KEY = "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"

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

SYSTEM_PROMPT = "Ты — вежливый и квалифицированный онлайн-консультант магазина салютов и пиротехники BENEFIS UZ в Ташкенте."

@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer("Здравствуйте! Я консультант магазина пиротехники BENEFIS UZ. Чем могу вам помочь?")

@dp.message()
async def handle_message(message: types.Message):
    try:
        response = client.chat.completions.create(
            model="openai/gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message.text}
            ]
        )
        answer = response.choices[0].message.content
        await message.answer(answer)
    except Exception as e:
        logging.error(f"Error: {e}")
        await message.answer("Извините, произошла ошибка при обработке запроса. Попробуйте еще раз позже.")

async def handle_ping(request):
    return web.Response(text="Bot is alive")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    logging.basicConfig(level=logging.INFO)
    
    # сброс старых зависших обновлений
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Одновременный запуск веб-сервера для Render (чтобы открылся порт) и телеграм-бота
    await asyncio.gather(
        start_web_server(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
