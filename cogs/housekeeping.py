"""Scheduled household tasks: morning news, dead-chat nudges, and stickers."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands, tasks

import config
from services import actions, database as db, memory, media

log = logging.getLogger("nixi.housekeeping")

DEAD_CHAT_LINES = (
    "ded chat. {member}, all that aura and not one conversation. Say something.",
    "ded chat. {member}, the typing indicator has more personality today. Defend your reputation.",
    "ded chat. {member}, lurking is not a competitive sport. Drop a take.",
    "ded chat. {member}, bro joined the server just to become furniture. Start a conversation.",
    "ded chat. {member}, your yap license is collecting dust. Put it to work.",
    "ded chat. {member}, surely you have one opinion worse than this silence. Let's hear it.",
    "ded chat. {member}, the chat is buffering and you're apparently the connection. Fix it.",
    "ded chat. {member}, premium lurker, free-trial conversationalist. Give us a topic.",
    "ded chat. {member}, imagine getting out-yapped by a butler. Couldn't be you. Prove it.",
    "ded chat. {member}, everyone suddenly has a life? Suspicious. Start some harmless discourse.",
    "ded chat. {member}, zero messages, maximum screen time. Make that ratio less embarrassing.",
    "ded chat. {member}, the group project needs you to contribute one sentence. Begin.",
)


def dead_chat_delay_seconds(row: dict) -> int:
    count = max(0, int(row.get("dead_ping_count") or 0)) if row.get("last_message_is_bot") else 0
    return max(1, config.DEAD_CHAT_IDLE_SECONDS) * (2 ** count)


class HousekeepingCog(commands.Cog):
    """Runs optional scheduled server tasks."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.housekeeping_loop.start()
        self.sticker_spam_loop.change_interval(
            seconds=max(10, config.STICKER_SPAM_INTERVAL_SECONDS)
        )
        self.sticker_spam_loop.start()

    def cog_unload(self) -> None:
        self.housekeeping_loop.cancel()
        self.sticker_spam_loop.cancel()

    @tasks.loop(seconds=15)
    async def housekeeping_loop(self) -> None:
        for task in (self._maybe_send_morning_news, self._maybe_nudge_dead_chat, self._maybe_send_random_meme):
            try:
                await task()
            except Exception:
                log.exception("Scheduled task failed: %s", task.__name__)

    @housekeeping_loop.before_loop
    async def before_housekeeping(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=60)
    async def sticker_spam_loop(self) -> None:
        await self._maybe_send_random_sticker()

    @sticker_spam_loop.before_loop
    async def before_sticker_spam(self) -> None:
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
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("Invalid time")
        except ValueError:
            log.warning("Invalid MORNING_NEWS_TIME: %s", config.MORNING_NEWS_TIME)
            return

        if (now.hour, now.minute) < (hour, minute):
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
            if idle < dead_chat_delay_seconds(row):
                continue

            channel = self.bot.get_channel(int(row["channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                continue
            if config.DEAD_CHAT_CHANNEL_IDS and channel.id not in config.DEAD_CHAT_CHANNEL_IDS:
                continue

            async with memory.channel_lock(channel.id):
                fresh = await db.get_channel_idle_candidates([channel.id])
                if not fresh or float(fresh[0].get("idle_seconds") or 0) < dead_chat_delay_seconds(fresh[0]):
                    continue
                member = self._pick_random_member(channel)
                if not member:
                    continue
                line_key = f"dead_chat_last_line:{channel.id}"
                previous = await db.get_state(line_key)
                template = random.choice([line for line in DEAD_CHAT_LINES if line != previous])
                await channel.send(
                    template.format(member=member.mention),
                    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
                )
                await db.mark_dead_chat_ping(channel.id)
                await db.set_state(line_key, template)

    def _pick_random_member(self, channel: discord.TextChannel) -> discord.Member | None:
        eligible = [
            member
            for member in channel.members
            if not member.bot and member.id != config.OWNER_ID
        ]
        return random.choice(eligible) if eligible else None

    async def _maybe_send_random_meme(self) -> None:
        if not config.MEME_DROP_ENABLED or not config.MEME_DROP_CHANNEL_ID:
            return
        channel = self.bot.get_channel(config.MEME_DROP_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            log.warning("Meme drop channel not found: %d", config.MEME_DROP_CHANNEL_ID)
            return
        state_key = f"meme_drop_next:{channel.id}"
        stored = await db.get_state(state_key)
        try:
            due = float(stored) if stored is not None else None
        except ValueError:
            due = None
        now = time.time()
        if due is not None and now < due:
            return
        # Reserve the next slot before fetching, so failures don't retry every tick.
        delay = random.uniform(0.8, 1.2) * config.MEME_DROP_INTERVAL_SECONDS
        await db.set_state(state_key, str(now + delay))
        if due is None:
            return
        result = await media.fetch_meme()
        if not result:
            return
        last_key = f"meme_drop_last:{channel.id}"
        if await db.get_state(last_key) == result.url:
            return
        await channel.send(embed=actions._media_embed(result), allowed_mentions=discord.AllowedMentions.none())
        await db.set_state(last_key, result.url)
        await db.record_bot_activity(channel.guild.id, channel.id)

    async def _maybe_send_random_sticker(self) -> None:
        if not config.STICKER_SPAM_ENABLED or not config.STICKER_SPAM_CHANNEL_ID:
            return

        channel = self.bot.get_channel(config.STICKER_SPAM_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel) or not channel.guild:
            log.warning("Sticker spam channel not found: %d", config.STICKER_SPAM_CHANNEL_ID)
            return

        stickers = [sticker for sticker in channel.guild.stickers if sticker.available]
        if not stickers:
            log.warning("Guild %s has no stickers to spam", channel.guild.name)
            return

        sticker = random.choice(stickers)
        try:
            await channel.send(stickers=[sticker])
        except discord.HTTPException:
            log.exception("Failed to send sticker %s to #%s", sticker.name, channel.name)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HousekeepingCog(bot))
