"""
Emoji service for Nixi.

Two responsibilities:
1. Provide the LLM with a list of available server emojis so it can use them
   naturally in its responses.
2. Post-process LLM output:
   a. Strip any hallucinated custom emoji (IDs that don't exist in the guild).
   b. Guarantee at least one real server emoji appears in every response.

The owner-only restriction has been removed — Nixi can now use ANY custom
emoji in the server, not just those uploaded by the owner.
"""

from __future__ import annotations

import logging
import random
import re
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from discord.ext.commands import Bot

log = logging.getLogger("nixi.emoji")

# Regex matching Discord custom emoji syntax: <:name:id>, <a:name:id>
# and hallucinated emojis like <:name:> without IDs
CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+)(?::(\d+))?>")


# ── Startup Cache ────────────────────────────────────────────────────────────

async def cache_emojis(bot: Bot) -> None:
    """
    Log available emojis at startup.  The old audit-log approach is no longer
    needed since we now allow ALL server emojis, but we keep this function
    for the startup log line and forward-compat.
    """
    total = 0
    for guild in bot.guilds:
        count = len(guild.emojis)
        total += count
        log.info(
            "Guild %s (%d) has %d custom emojis available",
            guild.name, guild.id, count,
        )
    log.info("Total custom emojis across all guilds: %d", total)


# ── LLM Context Helper ──────────────────────────────────────────────────────

def get_emoji_context(guild: discord.Guild | None) -> str | None:
    """
    Build a system-prompt block listing all custom emojis in the guild
    so the LLM can weave them into its replies naturally using their names.
    """
    if not guild or not guild.emojis:
        return None

    # Just give the names: :name1:, :name2:
    emoji_strs = [f":{e.name}:" for e in guild.emojis]
    emoji_list = ", ".join(emoji_strs)

    return (
        "EMOJI DIRECTIVE (OBEY STRICTLY):\n"
        "The following are custom emojis available in this Discord server. "
        "You MUST use at least one of these in EVERY reply. Prefer these over "
        "standard Unicode emojis — they are the server's personality. "
        "Use them naturally: to punctuate a joke, react to something, express "
        "emotion, or even send an emoji-only reply when the vibe calls for it. "
        "Use the exact name surrounded by colons.\n\n"
        f"Available emojis: {emoji_list}\n\n"
        "Guidelines:\n"
        "- Use 1-3 emojis per message typically; more for emphasis or comedy\n"
        "- Emoji-only replies (just 1-3 emojis, no text) are allowed when "
        "a reaction speaks louder than words\n"
        "- Place emojis where they feel natural — end of a sentence, inline, "
        "or standalone\n"
        "- NEVER use default/unicode emojis when a server emoji fits the mood"
    )


# ── Post-Processing Filter ──────────────────────────────────────────────────

def _get_guild_emoji_ids(guild: discord.Guild) -> set[int]:
    """Return a set of all custom emoji IDs in the guild."""
    return {e.id for e in guild.emojis}


def _pick_random_emoji(guild: discord.Guild) -> str:
    """Return a random custom emoji string from the guild."""
    emoji = random.choice(guild.emojis)
    prefix = "a" if emoji.animated else ""
    return f"<{prefix}:{emoji.name}:{emoji.id}>"


async def filter_response(text: str, guild: discord.Guild | None) -> str:
    """
    Post-process an LLM response:
    1. Resolve @username mentions to real Discord <@user_id> tags.
    2. Replace :emoji_name: with Discord's <a:name:id> format.
    3. Strip any hallucinated custom emojis.
    4. If no real custom emoji survived, append a random one.

    In DMs (guild is None), strip all custom emoji since they won't render.
    """
    if guild:
        # Resolve @username to real <@user_id> mentions
        sorted_members = sorted(guild.members, key=lambda m: len(m.display_name), reverse=True)
        for member in sorted_members:
            pattern = re.compile(r"@" + re.escape(member.display_name) + r"(?!\w)", re.IGNORECASE)
            text = pattern.sub(f"<@{member.id}>", text)
            if member.name != member.display_name:
                pattern2 = re.compile(r"@" + re.escape(member.name) + r"(?!\w)", re.IGNORECASE)
                text = pattern2.sub(f"<@{member.id}>", text)
    if not guild:
        # In DMs, strip all custom emoji formats
        text = re.sub(r":(\w+):", "", text)
        return CUSTOM_EMOJI_RE.sub("", text).strip()

    if not guild.emojis:
        text = re.sub(r":(\w+):", "", text)
        return CUSTOM_EMOJI_RE.sub("", text).strip()

    emoji_map = {e.name: e for e in guild.emojis}
    valid_ids = _get_guild_emoji_ids(guild)
    kept_count = 0

    # 1. Strip any hallucinated <a:name:id> syntaxes the LLM might still output
    def _strip_invalid_id(match: re.Match) -> str:
        nonlocal kept_count
        emoji_id_str = match.group(2)
        if emoji_id_str and int(emoji_id_str) in valid_ids:
            kept_count += 1
            return match.group(0)
        return ""
    text = CUSTOM_EMOJI_RE.sub(_strip_invalid_id, text)

    # 2. Replace :name: with the correct <a:name:id> Discord syntax
    def _replace_name(match: re.Match) -> str:
        nonlocal kept_count
        name = match.group(1)
        if name in emoji_map:
            e = emoji_map[name]
            kept_count += 1
            prefix = "a" if e.animated else ""
            return f"<{prefix}:{e.name}:{e.id}>"
        # Leave non-custom emojis alone (e.g. standard ones the LLM typed)
        return match.group(0)
        
    text = re.sub(r":(\w+):", _replace_name, text)

    # Clean up any double spaces
    filtered = re.sub(r"  +", " ", text).strip()

    # Guarantee at least one server emoji in the response
    if kept_count == 0 and filtered:
        random_emoji = _pick_random_emoji(guild)
        # Append it naturally — with a space before it
        filtered = f"{filtered} {random_emoji}"

    return filtered
