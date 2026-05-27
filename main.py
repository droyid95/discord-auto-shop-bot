import asyncio
import logging

import disnake
from disnake.ext import commands

from cogs.shop import ShopCog
from config import load_config
from database.store import Store
from services.cryptopay import cryptopay_payment_watcher
from services.yoomoney import yoomoney_payment_watcher


def make_status(status_id: int) -> disnake.Status:
    statuses = {
        1: disnake.Status.online,
        2: disnake.Status.dnd,
        3: disnake.Status.idle,
        4: disnake.Status.invisible,
    }
    return statuses.get(status_id, disnake.Status.online)


def make_activity(config) -> disnake.BaseActivity | None:
    text = config.activity_text.strip()
    if not text:
        return None

    if config.activity_type == 2:
        return disnake.Activity(type=disnake.ActivityType.listening, name=text)
    if config.activity_type == 3:
        return disnake.Streaming(name=text, url=config.activity_stream_url)
    return disnake.Game(name=text)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    store = Store(config.database_path)
    await store.connect()
    await store.init_schema()

    intents = disnake.Intents.default()
    intents.members = True
    intents.message_content = True

    command_sync_flags = commands.CommandSyncFlags.default()
    bot = commands.InteractionBot(
        intents=intents,
        command_sync_flags=command_sync_flags,
        test_guilds=[config.guild_id] if config.guild_id else None,
    )
    bot.config = config
    bot.store = store

    @bot.event
    async def on_ready() -> None:
        await bot.change_presence(status=make_status(config.bot_status), activity=make_activity(config))
        logging.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")

    bot.add_cog(ShopCog(bot, store, config))
    payment_task = (
        asyncio.create_task(yoomoney_payment_watcher(bot, store, config))
        if config.yoomoney_enabled
        else None
    )
    crypto_task = (
        asyncio.create_task(cryptopay_payment_watcher(bot, store, config))
        if config.cryptopay_enabled
        else None
    )
    try:
        await bot.start(config.bot_token)
    finally:
        if payment_task is not None:
            payment_task.cancel()
            try:
                await payment_task
            except asyncio.CancelledError:
                pass
        if crypto_task is not None:
            crypto_task.cancel()
            try:
                await crypto_task
            except asyncio.CancelledError:
                pass
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
