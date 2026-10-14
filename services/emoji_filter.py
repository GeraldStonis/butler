"""Emoji helper for Nixi."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord
from services.response_text import filter_links

if TYPE_CHECKING:
    from discord.ext.commands import Bot

log = logging.getLogger("nixi.emoji")

CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+)(?::(\d+))?>")
SHORT_EMOJI_RE = re.compile(r"(?<![\w<:]):(\w+):")


async def cache_emojis(bot: Bot) -> None:
    """Log available custom emojis at startup."""
    total = 0
    for guild in bot.guilds:
        count = len(guild.emojis)
        total += count
        log.info(
            "Guild %s (%d) has %d custom emojis available",
            guild.name, guild.id, count,
        )
    log.info("Total custom emojis across all guilds: %d", total)


def get_emoji_context(guild: discord.Guild | None) -> str | None:
    """Build a system-prompt block listing custom emojis in the guild."""
    if not guild or not guild.emojis:
        return None

    emoji_list = ", ".join(f":{e.name}:" for e in guild.emojis)
    return (
        "EMOJI GUIDANCE:\n"
        "The following custom emojis are available in this Discord server. "
        "Use one when it naturally improves the reply, especially for jokes, "
        "reactions, or casual banter. Do not force emojis into serious or "
        "factual replies. Use the exact name surrounded by colons.\n\n"
        f"Available emojis: {emoji_list}"
    )


def _get_guild_emoji_ids(guild: discord.Guild) -> set[int]:
    return {e.id for e in guild.emojis}


async def filter_response(text: str, guild: discord.Guild | None, *, allow_links: bool = False) -> str:
    """
    Post-process an LLM response.

    - Resolve @display-name mentions to real Discord mention tags.
    - Replace :emoji_name: with Discord's <a:name:id> custom emoji format.
    - Strip hallucinated custom emoji IDs.
    """
    text = filter_links(text, allow_links)
    if guild:
        sorted_members = sorted(guild.members, key=lambda m: len(m.display_name), reverse=True)
        for member in sorted_members:
            pattern = re.compile(r"@" + re.escape(member.display_name) + r"(?!\w)", re.IGNORECASE)
            text = pattern.sub(f"<@{member.id}>", text)
            if member.name != member.display_name:
                pattern2 = re.compile(r"@" + re.escape(member.name) + r"(?!\w)", re.IGNORECASE)
                text = pattern2.sub(f"<@{member.id}>", text)

    if not guild or not guild.emojis:
        text = CUSTOM_EMOJI_RE.sub("", text)
        return SHORT_EMOJI_RE.sub("", text).strip()

    emoji_map = {e.name: e for e in guild.emojis}
    valid_ids = _get_guild_emoji_ids(guild)

    def _strip_invalid_id(match: re.Match) -> str:
        emoji_id_str = match.group(2)
        if emoji_id_str and int(emoji_id_str) in valid_ids:
            return match.group(0)
        return ""

    text = CUSTOM_EMOJI_RE.sub(_strip_invalid_id, text)

    def _replace_name(match: re.Match) -> str:
        name = match.group(1)
        if name in emoji_map:
            e = emoji_map[name]
            prefix = "a" if e.animated else ""
            return f"<{prefix}:{e.name}:{e.id}>"
        return match.group(0)

    text = SHORT_EMOJI_RE.sub(_replace_name, text)
    return re.sub(r"  +", " ", text).strip()
