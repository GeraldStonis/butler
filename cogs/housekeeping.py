"""Scheduled household tasks: morning news and dead-chat nudges."""

from __future__ import annotations

import logging
import random
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands, tasks

import config
from services import actions, database as db

log = logging.getLogger("nixi.housekeeping")


class HousekeepingCog(commands.Cog):
    """Runs optional scheduled server tasks."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.housekeeping_loop.start()

    def cog_unload(self) -> None:
        self.housekeeping_loop.cancel()

    @tasks.loop(seconds=60)
    async def housekeeping_loop(self) -> None:
        await self._maybe_send_morning_news()
        await self._maybe_nudge_dead_chat()

    @housekeeping_loop.before_loop
    async def before_housekeeping(self) -> None:
        await self.bot.wait_until_ready()

    async def _maybe_send_morning_news(self) -> None:
        if not config.MORNING_NEWS_ENABLED or not config.MORNING_NEWS_CHANNEL_ID:
            return

        try:
            tz = ZoneInfo(config.MORNING_NEWS_TIMEZONE)
        except ZoneInfoNotFoundError:
            log.warning("Unknown MORNING_NEWS_TIMEZONE: %s", config.MORNING_NEWS_TIMEZONE)
            return

        now = datetime.now(tz)
        today_key = now.strftime("%Y-%m-%d")
        state_key = f"morning_news_sent:{config.MORNING_NEWS_CHANNEL_ID}"
        if await db.get_state(state_key) == today_key:
            return

        try:
            hour, minute = [int(part) for part in config.MORNING_NEWS_TIME.split(":", 1)]
        except ValueError:
            log.warning("Invalid MORNING_NEWS_TIME: %s", config.MORNING_NEWS_TIME)
            return

        if now.hour != hour or now.minute != minute:
            return

        channel = self.bot.get_channel(config.MORNING_NEWS_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            log.warning("Morning news channel not found: %d", config.MORNING_NEWS_CHANNEL_ID)
            return

        briefing = await actions.build_news_briefing()
        await channel.send(f"Morning briefing, M'Lord. The world has once again filed paperwork.\n\n{briefing}")
        await db.set_state(state_key, today_key)

    async def _maybe_nudge_dead_chat(self) -> None:
        if not config.DEAD_CHAT_ENABLED:
            return

        candidates = await db.get_channel_idle_candidates(config.DEAD_CHAT_CHANNEL_IDS or None)
        for row in candidates:
            idle = float(row.get("idle_seconds") or 0)
            cooldown = row.get("cooldown_seconds")
            cooldown = float(cooldown) if cooldown is not None else None
            if idle < config.DEAD_CHAT_IDLE_SECONDS:
                continue
            if cooldown is not None and cooldown < config.DEAD_CHAT_COOLDOWN_SECONDS:
                continue

            channel = self.bot.get_channel(int(row["channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                continue
            if config.DEAD_CHAT_CHANNEL_IDS and channel.id not in config.DEAD_CHAT_CHANNEL_IDS:
                continue

            member = self._pick_random_member(channel)
            if not member:
                continue

            await channel.send(
                f"ded chat. {member.mention}, kindly administer conversational CPR."
            )
            await db.mark_dead_chat_ping(channel.id)

    def _pick_random_member(self, channel: discord.TextChannel) -> discord.Member | None:
        eligible = [
            member
            for member in channel.members
            if not member.bot and member.id != config.OWNER_ID
        ]
        return random.choice(eligible) if eligible else None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HousekeepingCog(bot))
