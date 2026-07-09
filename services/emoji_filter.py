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

# Regex matching Discord custom emoji syntax: <:name:id> or <a:name:id>
CUSTOM_EMOJI_RE = re.compile(r"<a?:(\w+):(\d+)>")


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
    so the LLM can weave them into its replies naturally.

    Returns None if there's no guild or no custom emojis.
    """
    if not guild or not guild.emojis:
        return None

    # Build a compact list: <:name:id> for static, <a:name:id> for animated
    emoji_strs = []
    for e in guild.emojis:
        if e.animated:
            emoji_strs.append(f"<a:{e.name}:{e.id}>")
        else:
            emoji_strs.append(f"<:{e.name}:{e.id}>")

    emoji_list = ", ".join(emoji_strs)

    return (
        "EMOJI DIRECTIVE (OBEY STRICTLY):\n"
        "The following are custom emojis available in this Discord server. "
        "You MUST use at least one of these in EVERY reply. Prefer these over "
        "standard Unicode emojis — they are the server's personality. "
        "Use them naturally: to punctuate a joke, react to something, express "
        "emotion, or even send an emoji-only reply when the vibe calls for it. "
        "Copy-paste the EXACT syntax shown (including the angle brackets, "
        "colons, and ID numbers) — do NOT invent emoji names or IDs.\n\n"
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
    1. Strip any custom emoji the LLM hallucinated (ID not in guild).
    2. If no real custom emoji survived, append a random one.

    In DMs (guild is None), strip all custom emoji since they won't render.
    """
    if not guild:
        # In DMs, strip all custom emoji (they won't render anyway)
        return CUSTOM_EMOJI_RE.sub("", text).strip()

    if not guild.emojis:
        # Guild has no custom emojis — nothing to inject
        return CUSTOM_EMOJI_RE.sub("", text).strip()

    valid_ids = _get_guild_emoji_ids(guild)
    kept_count = 0

    def _replace(match: re.Match) -> str:
        nonlocal kept_count
        emoji_id = int(match.group(2))
        if emoji_id in valid_ids:
            kept_count += 1
            return match.group(0)  # keep valid emoji
        return ""  # strip hallucinated emoji

    filtered = CUSTOM_EMOJI_RE.sub(_replace, text)
    # Clean up any double spaces left by removal
    filtered = re.sub(r"  +", " ", filtered).strip()

    # Guarantee at least one server emoji in the response
    if kept_count == 0 and filtered:
        random_emoji = _pick_random_emoji(guild)
        # Append it naturally — with a space before it
        filtered = f"{filtered} {random_emoji}"

    return filtered
