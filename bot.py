import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

REQUIRED_ENV_VARS = (
    "BOT_TOKEN",
    "PAYMENT_LINK_MONTHLY",
    "PAYMENT_LINK_3_MONTHS",
    "PAYMENT_LINK_6_MONTHS",
    "PAYMENT_LINK_LIFETIME",
)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    payment_link_monthly: str
    payment_link_3_months: str
    payment_link_6_months: str
    payment_link_lifetime: str


dp = Dispatcher()
settings: Settings | None = None


def validate_payment_link(env_name: str, value: str) -> str:
    parsed_url = urlparse(value)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise RuntimeError(
            f"Invalid value for {env_name}: expected an absolute HTTPS URL."
        )
    return value


def load_settings() -> Settings:
    missing_vars = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing_vars:
        missing_list = ", ".join(missing_vars)
        raise RuntimeError(f"Missing required environment variable(s): {missing_list}")

    return Settings(
        bot_token=os.environ["BOT_TOKEN"],
        payment_link_monthly=validate_payment_link(
            "PAYMENT_LINK_MONTHLY", os.environ["PAYMENT_LINK_MONTHLY"]
        ),
        payment_link_3_months=validate_payment_link(
            "PAYMENT_LINK_3_MONTHS", os.environ["PAYMENT_LINK_3_MONTHS"]
        ),
        payment_link_6_months=validate_payment_link(
            "PAYMENT_LINK_6_MONTHS", os.environ["PAYMENT_LINK_6_MONTHS"]
        ),
        payment_link_lifetime=validate_payment_link(
            "PAYMENT_LINK_LIFETIME", os.environ["PAYMENT_LINK_LIFETIME"]
        ),
    )


def get_settings() -> Settings:
    if settings is None:
        raise RuntimeError("Application settings were not loaded.")
    return settings


def build_stripe_link(payment_link: str, client_reference_id: int) -> str:
    parsed_url = urlparse(payment_link)
    query_params = [
        (key, value)
        for key, value in parse_qsl(parsed_url.query, keep_blank_values=True)
        if key != "client_reference_id"
    ]
    query_params.append(("client_reference_id", str(client_reference_id)))
    return urlunparse(parsed_url._replace(query=urlencode(query_params)))


@dp.message(Command("start"))
async def start(message: types.Message):
    if message.from_user is None:
        logger.warning("Received /start without from_user; message ignored.")
        return

    app_settings = get_settings()
    telegram_id = message.from_user.id

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔥 Lifetime: 300 € / Lebenslang",
                    url=build_stripe_link(
                        app_settings.payment_link_lifetime, telegram_id
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📅 Monatlich: 29 € / Monat",
                    url=build_stripe_link(app_settings.payment_link_monthly, telegram_id),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📆 3 Monate: 59 € / 3 Monate",
                    url=build_stripe_link(
                        app_settings.payment_link_3_months, telegram_id
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗓️ 6 Monate: 99 € / 6 Monate",
                    url=build_stripe_link(app_settings.payment_link_6_months, telegram_id),
                )
            ],
        ]
    )

    await message.answer(
        f"Willkommen, {message.from_user.first_name}!\n\n"
        "Um der exklusiven VIP-Gruppe beizutreten, wählen Sie bitte eines der folgenden Abonnements:",
        reply_markup=keyboard,
    )


async def main():
    global settings

    try:
        settings = load_settings()
    except RuntimeError as error:
        logger.error("%s", error)
        sys.exit(1)

    bot = Bot(token=settings.bot_token)
    logger.info("Starting Telegram bot in long polling mode.")

    try:
        await dp.start_polling(bot)
    except Exception:
        logger.exception("Bot stopped because of an unexpected error.")
        raise
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
