"""
Core conversation handler for Nixi.

Public users can chat with Nixi when they address him. Owner-only natural
language actions are routed here before normal LLM conversation.
"""

from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord.ext import commands

import config
from services import actions, database as db, emoji_filter, intent, llm, memory
from services.response_text import requests_links

log = logging.getLogger("nixi.chat")


def is_wellbeing_question(content: str) -> bool:
    text = re.sub(r"<@!?\d+>|\b(?:nixi|nixon|butler)\b", "", content, flags=re.I)
    text = re.sub(r"[^a-z\s']", " ", text.lower())
    text = re.sub(r"\s+", " ", text).strip()
    return bool(re.fullmatch(
        r"(?:(?:hey|hi|hello|yo) )?(?:how (?:are you|are u|r you|r u|you doing|are you doing)|"
        r"how's it going|hows it going|how do you do|you good|u good)(?: today| doing| bro)?",
        text,
    ))


def is_unbecoming_request(content: str) -> bool:
    text = content.lower()
    # Discussing a phrase is different from asking Nixi to perform it.
    if re.search(r"\b(?:explain|define|meaning|what does|what is|why do|how to)\b", text):
        return False
    if re.search(r"\b(?:don't|do not|never|stop|avoid)\s+(?:say|saying|repeat|repeating|type|typing)\b", text):
        return False
    request = re.search(
        r"\b(?:say|repeat|type|reply|respond|chant|act like|pretend to be|become)\b(.{0,100})",
        text,
    )
    if not request:
        return False
    return bool(re.search(
        r"\b(?:u[\s_-]*w[\s_-]*u|o[\s_-]*w[\s_-]*o|nya+|meow|"
        r"catboy|catgirl|baby voice|good (?:boy|girl)|onii[ -]?chan)\b",
        request.group(1),
    ))


class ChatCog(commands.Cog):
    """Handles organic conversation triggers and owner action routing."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._boss_patterns: list[str] = [
            name.lower() for name in config.OWNER_ALIASES
        ]
        self._trigger_words: list[str] = ["butler", "servant", "nixi", "nixon"]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        async with memory.channel_lock(message.channel.id):
            await self._handle_message(message)

    async def _handle_message(self, message: discord.Message) -> None:
        if self.bot.user and message.author.id == self.bot.user.id:
            return

        content = message.content or ""
        guild_id = message.guild.id if message.guild else None

        await memory.log_message(
            channel_id=message.channel.id,
            guild_id=guild_id,
            author_id=message.author.id,
            author_name=message.author.display_name,
            content=content[:2000] if content else "[embed/attachment]",
            is_bot=message.author.bot,
        )

        if message.author.bot:
            return

        if guild_id is not None:
            await db.update_member_profile(guild_id, message.author.id, message.author.display_name, message.author.name)

        await db.record_user_activity(
            guild_id=guild_id,
            channel_id=message.channel.id,
            user_id=message.author.id,
            display_name=message.author.display_name,
        )

        mode = await self._detect_mode(message)
        if mode is not None or memory.has_member_fact_candidate(content):
            asyncio.create_task(memory.extract_and_store_memories(
                channel_id=message.channel.id,
                user_message=content,
                user_name=message.author.display_name,
                bot_reply="",
                guild_id=guild_id,
                user_id=message.author.id,
            ))
        if mode is None:
            return

        log.info(
            "Triggered [%s] by %s in #%s: %.80s",
            mode,
            message.author.display_name,
            getattr(message.channel, "name", "DM"),
            content,
        )

        fixed_reply = None
        if mode in {"DIRECT_ADDRESS", "REPLY_CHAIN"}:
            if message.author.id != config.OWNER_ID and is_unbecoming_request(content):
                fixed_reply = "Shush."
            elif is_wellbeing_question(content):
                fixed_reply = "Missed me?"
        if fixed_reply:
            await message.reply(fixed_reply, mention_author=False)
            await memory.log_message(
                channel_id=message.channel.id,
                guild_id=guild_id,
                author_id=self.bot.user.id,
                author_name="Nixi",
                content=fixed_reply,
                is_bot=True,
            )
            return

        async with message.channel.typing():
            if (
                config.ENABLE_NATURAL_OWNER_ACTIONS
                and message.author.id == config.OWNER_ID
                and mode in {"DIRECT_ADDRESS", "REPLY_CHAIN"}
            ):
                owner_intent = await intent.classify_owner_intent(
                    content,
                    mentions=list(message.mentions),
                )
                handled = await actions.execute_owner_intent(message, owner_intent)
                if handled:
                    return

            context = await memory.build_context(
                channel_id=message.channel.id,
                current_message=content,
                current_author=message.author.display_name,
                mode=mode,
                guild_id=guild_id,
                guild=message.guild,
                current_author_id=message.author.id,
            )

            reply_text = await llm.generate_response(context)
            reply_text = await emoji_filter.filter_response(
                reply_text, message.guild, allow_links=requests_links(content)
            )

            if len(reply_text) > 2000:
                reply_text = reply_text[:1997] + "..."

        if not reply_text:
            return

        await message.reply(reply_text, mention_author=False)

        if mode == "OWNER_UNAVAILABLE":
            await self._notify_owner(message)

        await memory.log_message(
            channel_id=message.channel.id,
            guild_id=guild_id,
            author_id=self.bot.user.id,  # type: ignore[union-attr]
            author_name="Nixi",
            content=reply_text,
            is_bot=True,
        )

    async def _detect_mode(self, message: discord.Message) -> str | None:
        """Determine why Nixi should speak, or return None for silence."""
        content_lower = (message.content or "").lower()

        if isinstance(message.channel, discord.DMChannel):
            return "DIRECT_ADDRESS"

        if (
            config.ENABLE_NATURAL_OWNER_ACTIONS
            and message.author.id == config.OWNER_ID
            and intent.heuristic_intent(message.content, list(message.mentions)) is not None
        ):
            return "DIRECT_ADDRESS"

        if self.bot.user in message.mentions:  # type: ignore[operator]
            return "DIRECT_ADDRESS"

        if message.reference and message.reference.message_id:
            ref = message.reference.resolved
            if isinstance(ref, discord.Message) and self.bot.user:
                if ref.author.id == self.bot.user.id:
                    return "REPLY_CHAIN"

        if message.author.id != config.OWNER_ID and self._mentions_owner(message):
            guild_id = message.guild.id if message.guild else None
            idle = await db.get_user_idle_seconds(guild_id, config.OWNER_ID)
            if idle is None or idle >= config.OWNER_AWAY_AFTER_SECONDS:
                return "OWNER_UNAVAILABLE"

        for word in self._trigger_words:
            if re.search(r"\b" + re.escape(word) + r"\b", content_lower):
                return "DIRECT_ADDRESS"

        return None

    def _mentions_owner(self, message: discord.Message) -> bool:
        if any(user.id == config.OWNER_ID for user in message.mentions):
            return True

        content_lower = (message.content or "").lower()
        for name in self._boss_patterns:
            if re.search(r"\b" + re.escape(name) + r"\b", content_lower):
                return True
        return False

    async def _notify_owner(self, message: discord.Message) -> None:
        """DM the owner when someone was looking for them."""
        try:
            owner = await self.bot.fetch_user(config.OWNER_ID)
            location = "DM"
            if message.guild and hasattr(message.channel, "mention"):
                location = f"{message.guild.name} / {message.channel.mention}"
            await owner.send(
                f"{message.author.display_name} mentioned you in {location}:\n"
                f"> {(message.content or '[no text]')[:500]}"
            )
        except Exception:
            log.exception("Failed to notify owner about mention")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChatCog(bot))
