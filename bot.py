from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

PAYMENT_LINK =https://buy.stripe.com/dRmaEW0VMb684J77KnfAc00

@dp.message(Command("start"))
async def start(message: types.Message):

    telegram_id = message.from_user.id

    payment_url = f"{PAYMENT_LINK}?client_reference_id={telegram_id}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Kup VIP",
                    url=payment_url
                )
            ]
        ]
    )

    await message.answer(
        "Kup dostęp VIP:",
        reply_markup=kb
    )

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
