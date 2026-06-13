import asyncio
import os
from aiogram import Bot

BOT_TOKEN = os.getenv("BOT_TOKEN")

async def main():
    bot = Bot(BOT_TOKEN)
    me = await bot.get_me()
    print("BOT:", me.username)

asyncio.run(main())
