from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

MONTHLY_LINK = "https://buy.stripe.com/dRmaEW0VMb684J77KnfAc00"
THREE_MONTH_LINK = "https://buy.stripe.com/dRm8wOgUKfmofnL5CffAc03"
SIX_MONTH_LINK = "https://buy.stripe.com/4gM3cu33U2zC2AZc0DfAc02"
LIFETIME_LINK = "https://buy.stripe.com/fZu9AS6g67TW8Zn9SvfAc01"


@dp.message(Command("start"))
async def start(message: types.Message):

    telegram_id = message.from_user.id

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔥 Lifetime: 1.800 PLN / Lebenslang",
                    url=f"{LIFETIME_LINK}?client_reference_id={telegram_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📅 Monatlich: 299 PLN / 1 Monat",
                    url=f"{MONTHLY_LINK}?client_reference_id={telegram_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📆 3 Monate: 799 PLN / 3 Monate",
                    url=f"{THREE_MONTH_LINK}?client_reference_id={telegram_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗓️ 6 Monate: 1.499 PLN / 6 Monate",
                    url=f"{SIX_MONTH_LINK}?client_reference_id={telegram_id}"
                )
            ]
        ]
    )

    await message.answer(
        f"Willkommen, {message.from_user.first_name}!\n\n"
        f"Um der VIP-Gruppe beizutreten, wählen Sie bitte einen der folgenden Pläne:",
        reply_markup=keyboard
    )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
