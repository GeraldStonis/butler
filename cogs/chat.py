"""
Chat Cog — Core conversation handler for Nixi.

Listens to on_message and decides whether to respond based on triggers:
- Bot @-mentioned → DIRECT_ADDRESS
- Reply to bot's message → REPLY_CHAIN
- DM → DIRECT_ADDRESS
- Boss name mentioned → BOSS_MENTIONED
- Otherwise → log only, do not respond
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

import config
from services import llm, memory, emoji_filter

log = logging.getLogger("nixi.chat")


class ChatCog(commands.Cog):
    """Handles all organic conversation triggers."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Pre-compile boss name patterns (case-insensitive)
        self._boss_patterns: list[str] = [
            name.lower() for name in config.OWNER_NAMES
        ]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Never respond to ourselves
        if message.author.id == self.bot.user.id:  # type: ignore[union-attr]
            return

        # Determine the display content (embeds have no .content sometimes)
        content = message.content or ""

        # Log every message we can see (for context building),
        # but only non-bot messages or messages from tracked bots
        # (watcher cog handles bot messages separately for alerts)
        guild_id = message.guild.id if message.guild else None
        await memory.log_message(
            channel_id=message.channel.id,
            guild_id=guild_id,
            author_id=message.author.id,
            author_name=message.author.display_name,
            content=content[:2000] if content else "[embed/attachment]",
            is_bot=message.author.bot,
        )

        # Don't generate responses to other bots
        # (watcher cog handles bot alert logic)
        if message.author.bot:
            return

        # ── Determine trigger & mode ────────────────────────────────────
        mode = self._detect_mode(message)
        if mode is None:
            return  # Not a trigger message; we logged it, that's enough

        log.info(
            "Triggered [%s] by %s in #%s: %.80s",
            mode,
            message.author.display_name,
            getattr(message.channel, "name", "DM"),
            content,
        )

        # Show typing indicator while we think
        async with message.channel.typing():
            # Build context and generate response
            context = await memory.build_context(
                channel_id=message.channel.id,
                current_message=content,
                current_author=message.author.display_name,
                mode=mode,
                guild_id=guild_id,
                guild=message.guild,
            )

            reply_text = await llm.generate_response(context)

            # Filter emojis
            guild = message.guild
            reply_text = await emoji_filter.filter_response(reply_text, guild)

            # Ensure we don't exceed Discord's 2000-char limit
            if len(reply_text) > 2000:
                reply_text = reply_text[:1997] + "..."

        # Send the reply
        if not reply_text:
            return

        sent = await message.reply(reply_text, mention_author=False)

        # Log our own reply
        await memory.log_message(
            channel_id=message.channel.id,
            guild_id=guild_id,
            author_id=self.bot.user.id,  # type: ignore[union-attr]
            author_name="Nixi",
            content=reply_text,
            is_bot=True,
        )

        # Background: extract any long-term memories from this exchange
        asyncio.create_task(
            memory.extract_and_store_memories(
                channel_id=message.channel.id,
                user_message=content,
                user_name=message.author.display_name,
                bot_reply=reply_text,
            )
        )

    def _detect_mode(self, message: discord.Message) -> str | None:
        """
        Determine the conversation mode based on the message context.

        Returns the mode string, or None if the bot should not respond.
        """
        content_lower = (message.content or "").lower()

        # DMs always get a response
        if isinstance(message.channel, discord.DMChannel):
            return "DIRECT_ADDRESS"

        # Bot is @-mentioned
        if self.bot.user in message.mentions:  # type: ignore[operator]
            return "DIRECT_ADDRESS"

        # Message is a reply to one of the bot's own messages
        if message.reference and message.reference.message_id:
            # Check if the referenced message was sent by us
            # We use the cached message if available
            ref = message.reference.resolved
            if isinstance(ref, discord.Message):
                if ref.author.id == self.bot.user.id:  # type: ignore[union-attr]
                    return "REPLY_CHAIN"
            else:
                # If not cached, we can't easily check without an API call.
                # We'll try to fetch it.
                return self._check_reply_sync(message)

        # Boss name mentioned in the message
        for name in self._boss_patterns:
            if name in content_lower:
                return "BOSS_MENTIONED"

        return None

    def _check_reply_sync(self, message: discord.Message) -> str | None:
        """
        Fallback check for reply chains when the referenced message
        isn't cached. Returns mode or None.

        Since we can't await here, we check the reference resolved field.
        If it's a DeletedReferencedMessage or None, we skip.
        """
        # If the reference couldn't be resolved, check boss names as fallback
        content_lower = (message.content or "").lower()
        for name in self._boss_patterns:
            if name in content_lower:
                return "BOSS_MENTIONED"
        return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChatCog(bot))
