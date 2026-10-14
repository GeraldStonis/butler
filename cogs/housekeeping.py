"""Scheduled household tasks: morning news, dead-chat nudges, and stickers."""

from __future__ import annotations

import logging
import random
import time
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands, tasks

import config
from services import actions, database as db, memory, media, llm, music, activities, engagement
from services.response_text import filter_links

log = logging.getLogger("nixi.housekeeping")

DEAD_CHAT_EXAMPLES = (
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
    "ded chat. {member}, your last contribution was an online status. Raise the bar.",
    "ded chat. {member}, spectacular commitment to the mute-button lifestyle. Drop an opinion.",
    "ded chat. {member}, the server has become your digital waiting room. Entertain the queue.",
    "ded chat. {member}, posting 'real' every three days is not a personality. Give us a take.",
    "ded chat. {member}, all those reaction GIFs and no original thought? Start a topic.",
    "ded chat. {member}, the drawing room is quieter than your comeback folder. Open it.",
    "ded chat. {member}, you have main-character notifications and background-extra dialogue. Speak.",
    "ded chat. {member}, your keyboard called. It would like a job. Give us a sentence.",
    "ded chat. {member}, this chat has less movement than your allegedly productive afternoon. Revive it.",
    "ded chat. {member}, lurking with this much confidence should require a permit. Say something.",
    "ded chat. {member}, surely the group can survive one of your questionable takes. Test it.",
    "ded chat. {member}, the last topic died of natural causes. Bring a new one, not a eulogy.",
    "ded chat. {member}, I have dusted the entire estate waiting for you to finish typing. Begin.",
    "ded chat. {member}, even the pinned messages are carrying harder than you. Drop a topic.",
    "ded chat. {member}, the yap factory has laid everyone off. Apply for the first shift.",
    "ded chat. {member}, you cannot farm aura from an empty chat. Supply the content.",
    "ded chat. {member}, Discord did not install itself so you could supervise silence. Talk.",
    "ded chat. {member}, this server is serving loading-screen energy. Give it a plot.",
    "ded chat. {member}, your notifications are on a hunger strike. Feed them a conversation.",
    "ded chat. {member}, I see the online dot. Now show us the alleged personality. Start a topic.",
)


def dead_chat_delay_seconds(row: dict) -> int:
    count = max(0, int(row.get("dead_ping_count") or 0)) if row.get("last_message_is_bot") else 0
    return max(1, config.DEAD_CHAT_IDLE_SECONDS) * (2 ** count)


def dead_chat_due(row: dict) -> bool:
    return (int(row.get("dead_ping_count") or 0) < 5
            and float(row.get("idle_seconds") or 0) >= dead_chat_delay_seconds(row))


async def generate_dead_chat_line(previous: str | None, count: int) -> str:
    for _ in range(2):
        text = await llm.generate_response([
            {"role": "system", "content": config.load_persona()},
            {"role": "system", "content": (
                "Generate a NEW playful roast inviting a quiet Discord chat to revive itself. "
                "One or two short, sassy sentences, preferably saying 'ded chat'. Tease lurking "
                "or a weak take, not identity or vulnerabilities. No threats or personal claims. "
                "Do not include names, mentions, URLs, or placeholders; the application adds "
                "one chosen member's mention. These are ONLY style examples, never copy them:\n"
                + "\n".join(line.replace('{member}, ', '') for line in random.sample(DEAD_CHAT_EXAMPLES, 3))
                + "\nDo not repeat the previous nudge. Output only the new message."
            )},
            {"role": "user", "content": json.dumps({"previous_nudge": previous, "nudge_number": count + 1})},
        ], temperature=0.95, max_tokens=600, failure_reply="")
        text = filter_links(text)
        text = re.sub(r"<@[^>]*>|@(?:everyone|here)|\{member\}", "", text).strip()
        examples = {line.replace('{member}, ', '').casefold() for line in DEAD_CHAT_EXAMPLES}
        if text and len(text) <= 500 and text.casefold() != (previous or '').casefold() and text.casefold() not in examples:
            return text
    return ""


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
        for task in (self._maybe_send_morning_news, self._maybe_nudge_dead_chat, self._maybe_send_random_meme, self._maybe_send_music):
            try:
                await task()
            except Exception:
                log.exception("Scheduled task failed: %s", task.__name__)

    @housekeeping_loop.before_loop
    async def before_housekeeping(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=60)
    async def sticker_spam_loop(self) -> None:
        try:
            await self._maybe_send_random_sticker()
        except Exception:
            log.exception("Sticker task failed; the next interval will retry")

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
        sent = await channel.send(briefing)
        await memory.log_delivery(sent)
        await db.set_state(state_key, today_key)

    async def _maybe_nudge_dead_chat(self) -> None:
        if not config.DEAD_CHAT_ENABLED or engagement.dead_chat_quiet_now():
            return

        candidates = await db.get_channel_idle_candidates(config.DEAD_CHAT_CHANNEL_IDS or None)
        for row in candidates:
            if not dead_chat_due(row):
                continue

            channel = self.bot.get_channel(int(row["channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                continue
            if config.DEAD_CHAT_CHANNEL_IDS and channel.id not in config.DEAD_CHAT_CHANNEL_IDS:
                continue
            if not row.get("last_user_id") or not await engagement.can_post(channel.id):
                continue
            retry_key = f"engagement_retry:{channel.id}"
            try:
                retry_at = float(await db.get_state(retry_key) or 0)
            except ValueError:
                retry_at = 0
            if time.time() < retry_at:
                continue

            async with memory.channel_lock(channel.id):
                fresh = await db.get_channel_idle_candidates([channel.id])
                if not fresh or not dead_chat_due(fresh[0]):
                    continue
                games = getattr(self.bot, "get_cog", lambda _: None)("ActivitiesCog")
                if games and channel.id in games.active:
                    continue
                line_key = f"dead_chat_last_line:{channel.id}"
                previous = await db.get_state(line_key)
                member = await self._pick_random_member(channel)
                if member is None:
                    continue
                # Provider failures must not cause a fresh LLM request every tick.
                await db.set_state(retry_key, str(time.time() + 180))
                line = await generate_dead_chat_line(previous, int(fresh[0].get("dead_ping_count") or 0))
                if not line:
                    continue
                latest = await db.get_channel_idle_candidates([channel.id])
                if not latest or latest[0]["last_message_at"] != fresh[0]["last_message_at"]:
                    continue
                if engagement.dead_chat_quiet_now() or not await engagement.can_post(channel.id):
                    continue
                sent = await channel.send(
                    f"{member.mention} {line}",
                    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=member.id)], replied_user=False),
                )
                await db.mark_dead_chat_ping(channel.id)
                await db.set_state(line_key, line)
                await db.set_state(retry_key, "0")
                await memory.log_delivery(sent)
                await engagement.mark_sent(channel.id)
                await db.set_state(f"dead_chat_target:{channel.id}", str(member.id))

    async def _pick_random_member(self, channel: discord.TextChannel) -> discord.Member | None:
        opted_out = await db.get_dead_chat_opt_outs(channel.guild.id)
        previous = await db.get_state(f"dead_chat_target:{channel.id}")
        eligible = [
            member
            for member in channel.members
            if not member.bot and member.id != config.OWNER_ID and member.id not in opted_out
        ]
        varied = [m for m in eligible if str(m.id) != previous]
        candidates = varied or eligible
        return random.choice(candidates) if candidates else None

    async def _maybe_send_random_meme(self) -> None:
        if not config.MEME_DROP_ENABLED or not config.MEME_DROP_CHANNEL_ID:
            return
        channel = self.bot.get_channel(config.MEME_DROP_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            log.warning("Meme drop channel not found: %d", config.MEME_DROP_CHANNEL_ID)
            return
        if not await engagement.can_post(channel.id):
            return
        games = getattr(self.bot, "get_cog", lambda _: None)("ActivitiesCog")
        if games and channel.id in games.active:
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
        if not await engagement.can_post(channel.id):
            return
        sent = await channel.send(embed=actions._media_embed(result), allowed_mentions=discord.AllowedMentions.none())
        await memory.log_delivery(sent)
        await db.set_state(last_key, result.url)
        await db.record_bot_activity(channel.guild.id, channel.id)
        await engagement.mark_sent(channel.id)

    async def _maybe_send_music(self) -> None:
        if not config.MUSIC_DROP_ENABLED or not config.MUSIC_CHANNEL_IDS:
            return
        rows = await db.get_channel_idle_candidates(config.MUSIC_CHANNEL_IDS)
        for row in rows:
            channel = self.bot.get_channel(int(row["channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                continue
            games = getattr(self.bot, "get_cog", lambda _: None)("ActivitiesCog")
            if games and channel.id in games.active:
                continue
            if not await engagement.can_post(channel.id):
                continue
            song = await music.idle_song(row)
            if not song:
                continue
            line = await activities.commentary(
                "The chat has been quiet. You decide to listen to a song. "
                "Write a casual short announcement; 'imma listen to a song' is a style example, not a fixed reply."
            )
            if not line:
                continue
            async with memory.channel_lock(channel.id):
                fresh = await db.get_channel_idle_candidates([channel.id])
                if not fresh or fresh[0]["last_message_at"] != row["last_message_at"]:
                    continue
                if games and channel.id in games.active:
                    continue
                if not await engagement.can_post(channel.id):
                    continue
                # Ordinary URLs let Discord create a native playable YouTube preview.
                sent = await channel.send(f"{line}\nhttps://www.youtube.com/watch?v={song['youtubeId']}",
                                   allowed_mentions=discord.AllowedMentions.none())
                await memory.log_delivery(sent)
                await db.record_bot_activity(channel.guild.id, channel.id)
                await engagement.mark_sent(channel.id)

    async def _maybe_send_random_sticker(self) -> None:
        if not config.STICKER_SPAM_ENABLED or not config.STICKER_SPAM_CHANNEL_ID:
            return
        if engagement.quiet_now() or engagement.spam_quiet_now(config.STICKER_SPAM_CHANNEL_ID):
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
        if engagement.quiet_now() or engagement.spam_quiet_now(channel.id):
            return
        try:
            await channel.send(stickers=[sticker])
        except discord.HTTPException:
            log.exception("Failed to send sticker %s to #%s", sticker.name, channel.name)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HousekeepingCog(bot))
