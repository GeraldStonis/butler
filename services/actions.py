"""Execution for owner-only natural language actions."""

from __future__ import annotations

import logging

import discord

import config
from services import intent, media, search

log = logging.getLogger("nixi.actions")


async def build_news_briefing(topic: str | None = None) -> str:
    """Fetch and summarize a compact news briefing."""
    query = topic or ", ".join(config.MORNING_NEWS_TOPICS)
    results = await search.news_search(query, count=8)
    return await search.summarize_results(query, results, mode="news")


async def execute_owner_intent(
    message: discord.Message,
    owner_intent: intent.OwnerIntent,
) -> bool:
    """
    Execute an owner-only intent.

    Returns True when an action was handled and normal chat should stop.
    """
    name = owner_intent.name
    if name == "chat":
        return False

    try:
        if name == "web_search":
            query = owner_intent.query or message.content
            results = await search.web_search(query, count=6)
            await message.reply(
                await search.summarize_results(query, results),
                mention_author=False,
            )
            return True

        if name == "news":
            await message.reply(
                await build_news_briefing(owner_intent.query or None),
                mention_author=False,
            )
            return True

        if name == "image_search":
            query = owner_intent.query or message.content
            result = await media.fetch_image(query)
            if not result:
                await message.reply("I found no image fit for the household gallery.", mention_author=False)
                return True
            await message.reply(f"Presented for inspection: {result.url}", mention_author=False)
            return True

        if name == "gif_search":
            query = owner_intent.query or message.content
            result = await media.fetch_gif(query)
            if not result:
                await message.reply(
                    "I can fetch GIFs once TENOR_API_KEY is configured. At present I can only mime dramatically.",
                    mention_author=False,
                )
                return True
            await message.reply(result.url, mention_author=False)
            return True

        if name == "meme":
            result = await media.fetch_meme()
            if not result:
                await message.reply("The meme tray returned empty, a tragedy in three acts.", mention_author=False)
                return True
            await message.reply(f"{result.title}\n{result.url}", mention_author=False)
            return True

        if name in {"tea", "coffee"}:
            result = await media.fetch_tea_or_coffee(name)
            if not result:
                await message.reply(f"I could not procure {name}. The pantry is in disgrace.", mention_author=False)
                return True
            label = "Tea service" if name == "tea" else "Coffee service"
            await message.reply(f"{label}, as requested.\n{result.url}", mention_author=False)
            return True

        if name == "action_gif":
            result = await media.fetch_action_gif(owner_intent.action)
            target = f"<@{owner_intent.target_user_id}>" if owner_intent.target_user_id else "the intended party"
            if not result:
                await message.reply(
                    "I can perform such theatrics once TENOR_API_KEY is configured. For now, consider this a stern eyebrow.",
                    mention_author=False,
                )
                return True
            await message.channel.send(
                f"{target}, the Young Master has requested a little slapstick diplomacy.\n{result.url}"
            )
            return True

    except Exception:
        log.exception("Owner action failed: %s", owner_intent)
        await message.reply(
            "I attempted the errand and tripped over the carpet. Do try me again in a moment.",
            mention_author=False,
        )
        return True

    return False
