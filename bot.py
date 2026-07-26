import asyncio
import logging
import os
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
    "STRIPE_WEBHOOK_SECRET",
)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    payment_link_monthly: str
    payment_link_3_months: str
    payment_link_6_months: str
    payment_link_lifetime: str
    stripe_webhook_secret: str
    telegram_group_id: int | str
    access_check_interval_seconds: int = 300


dp = Dispatcher()
settings: Settings | None = None


def set_settings(app_settings: Settings) -> None:
    global settings
    settings = app_settings


def validate_payment_link(env_name: str, value: str) -> str:
    parsed_url = urlparse(value)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise RuntimeError(
            f"Invalid value for {env_name}: expected an absolute HTTPS URL."
        )
    return value


def parse_telegram_group_id(value: str) -> int | str:
    value = value.strip()
    if not value:
        raise RuntimeError("Invalid value for TELEGRAM_GROUP_ID: value cannot be empty.")

    try:
        return int(value)
    except ValueError:
        if value.startswith("@") and len(value) > 1:
            return value
        raise RuntimeError(
            "Invalid value for TELEGRAM_GROUP_ID: expected numeric chat ID or @username."
        )


def parse_positive_int(env_name: str, value: str, default: int) -> int:
    if not value:
        return default

    try:
        parsed_value = int(value)
    except ValueError as error:
        raise RuntimeError(f"Invalid value for {env_name}: expected an integer.") from error

    if parsed_value < 60 or parsed_value > 86_400:
        raise RuntimeError(
            f"Invalid value for {env_name}: expected an integer from 60 to 86400."
        )

    return parsed_value


def load_telegram_group_id() -> int | str:
    telegram_group_id = os.getenv("TELEGRAM_GROUP_ID")
    if telegram_group_id:
        return parse_telegram_group_id(telegram_group_id)

    legacy_group_id = os.getenv("GROUP_ID")
    if legacy_group_id:
        logger.warning(
            "GROUP_ID is deprecated; use TELEGRAM_GROUP_ID for the VIP group."
        )
        return parse_telegram_group_id(legacy_group_id)

    raise RuntimeError(
        "Missing required environment variable(s): TELEGRAM_GROUP_ID or GROUP_ID"
    )


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
        stripe_webhook_secret=os.environ["STRIPE_WEBHOOK_SECRET"],
        telegram_group_id=load_telegram_group_id(),
        access_check_interval_seconds=parse_positive_int(
            "ACCESS_CHECK_INTERVAL_SECONDS",
            os.getenv("ACCESS_CHECK_INTERVAL_SECONDS", ""),
            default=300,
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


def create_bot(app_settings: Settings) -> Bot:
    return Bot(token=app_settings.bot_token)


async def run_polling(bot: Bot) -> None:
    logger.info("Starting Telegram bot in long polling mode.")
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


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
    try:
        app_settings = load_settings()
    except RuntimeError as error:
        logger.error("%s", error)
        raise SystemExit(1) from error

    set_settings(app_settings)
    bot = create_bot(app_settings)

    try:
        await run_polling(bot)
    except Exception:
        logger.exception("Bot stopped because of an unexpected error.")
        raise
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
