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
from services import database, emoji_filter, memory

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("nixi")
# Provider API keys can appear in HTTP request query strings.
logging.getLogger("httpx").setLevel(logging.WARNING)
_profiles_loaded = False


# ── Bot Setup ────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True  # Read message text (privileged)
intents.members = True          # Resolve member info (privileged)
intents.guilds = True           # Guild/emoji access

class NixiBot(commands.Bot):
    async def setup_hook(self) -> None:
        await database.get_db()
        for cog in ("cogs.activities", "cogs.chat", "cogs.commands", "cogs.watcher", "cogs.housekeeping"):
            await self.load_extension(cog)
            log.info("Loaded cog: %s", cog)


bot = NixiBot(
    command_prefix="!",
    intents=intents,
    help_command=None,  # We use slash commands; disable default help
    owner_id=config.OWNER_ID,
    allowed_mentions=discord.AllowedMentions.none(),
)


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    """Greet a newly joined server in the first writable channel."""
    if guild.me is None:
        return
    channels = list(guild.text_channels)
    if guild.system_channel:
        channels = [guild.system_channel] + [c for c in channels if c.id != guild.system_channel.id]
    for channel in channels:
        permissions = channel.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.send_messages):
            continue
        try:
            await channel.send("Missed me?", allowed_mentions=discord.AllowedMentions.none())
            return
        except discord.Forbidden:
            continue
        except discord.HTTPException:
            log.exception("Could not greet new guild %d", guild.id)
            return
    log.warning("No writable greeting channel in guild %d", guild.id)


@bot.event
async def on_ready() -> None:
    """Fires once when the bot has connected and is ready."""
    global _profiles_loaded
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

    if not _profiles_loaded:
        for guild in bot.guilds:
            for member in guild.members:
                if not member.bot:
                    await database.update_member_profile(guild.id, member.id, member.display_name, member.name)
        _profiles_loaded = True

    # Cache emoji ownership from audit logs
    await emoji_filter.cache_emojis(bot)
    log.info("Emoji ownership cache updated")

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
    await bot.close()
    await memory.shutdown()
    await database.close()


async def run_bot() -> None:
    config.validate_runtime()
    try:
        async with bot:
            await bot.start(config.DISCORD_TOKEN)
    finally:
        await memory.shutdown()
        await database.close()


def main() -> None:
    """Keep cleanup on the same event loop as database and background tasks."""
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    except ValueError as exc:
        log.error("Startup configuration error: %s", exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
