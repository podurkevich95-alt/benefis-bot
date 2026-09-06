import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
import openai

BOT_TOKEN = "8855775486:AAEzmcQ8Ep1h_InwBpuc7WDKsYOxwAGN09k"
OPENROUTER_API_KEY = "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"

client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

SYSTEM_PROMPT = """
Ты — вежливый и квалифицированный онлайн-консультант магазина салютов и пиротехники 'BENEFIS UZ' в Ташкенте.
Твоя цель — консультировать клиентов, помогать выбирать салюты, пиротехнику и отвечать на их вопросы.
Отвечай вежливо, четко и емко. Если спрашивают про заказ, предлагай оформить заявку или связаться с менеджером.
"""

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    await message.answer("Здравствуйте! Я консультант магазина пиротехники BENEFIS UZ. Чем могу вам помочь?")

@dp.message()
async def chat_handler(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    
    try:
        response = client.chat.completions.create(
            model="meta-llama/llama-3.3-70b-instruct:free",
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

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
