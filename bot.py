"""
Nixi — Personal Discord Butler Bot

Entry point. Initialises the bot, loads cogs, and starts the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord
from discord.ext import commands

import config
from services import database, emoji_filter

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("nixi")


# ── Bot Setup ────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True  # Read message text (privileged)
intents.members = True          # Resolve member info (privileged)
intents.guilds = True           # Guild/emoji access

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,  # We use slash commands; disable default help
)


@bot.event
async def on_ready() -> None:
    """Fires once when the bot has connected and is ready."""
    assert bot.user is not None
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("Nixi is at your service.")
    log.info("Logged in as %s (ID: %d)", bot.user, bot.user.id)
    log.info("Owner ID: %d", config.OWNER_ID)
    log.info("LLM model: %s", config.OLLAMA_MODEL)
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Initialise database
    await database.get_db()
    log.info("Database initialised at %s", config.DB_PATH)

    # Cache emoji ownership from audit logs
    await emoji_filter.cache_emojis(bot)
    log.info("Emoji ownership cache updated")

    # Load cogs
    cog_names = ["cogs.chat", "cogs.commands", "cogs.watcher"]
    for cog in cog_names:
        try:
            await bot.load_extension(cog)
            log.info("Loaded cog: %s", cog)
        except Exception:
            log.exception("Failed to load cog: %s", cog)

    log.info(
        "Serving in %d guild(s). Awaiting orders.",
        len(bot.guilds),
    )


@bot.event
async def on_command_error(
    ctx: commands.Context, error: commands.CommandError
) -> None:
    """Global error handler for prefix commands."""
    if isinstance(error, commands.CommandNotFound):
        return  # Silently ignore unknown prefix commands
    if isinstance(error, commands.NotOwner):
        await ctx.send(
            "That privilege is reserved for the master of the house, I'm afraid."
        )
        return
    log.exception("Unhandled command error", exc_info=error)


async def shutdown() -> None:
    """Graceful shutdown: close DB, then the bot."""
    log.info("Shutting down gracefully...")
    await database.close()
    await bot.close()


def main() -> None:
    """Entry point."""
    try:
        bot.run(config.DISCORD_TOKEN, log_handler=None)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    finally:
        # Ensure DB is closed
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(database.close())
            else:
                loop.run_until_complete(database.close())
        except Exception:
            pass


if __name__ == "__main__":
    main()
