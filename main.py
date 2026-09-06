import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from openai import OpenAI
from aiohttp import web

BOT_TOKEN = "8855775486:AAGQB5GpvgW8UFDuhzAVI_ASUzzg8P9o5-M"
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
    await bot.delete_webhook(drop_pending_updates=True)
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
