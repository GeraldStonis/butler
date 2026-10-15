"""Execution for owner-only natural language actions."""

from __future__ import annotations

import logging

import discord

import config
from services import intent, media, search, memory, avatar_edits
from services.response_text import filter_links, requests_links

log = logging.getLogger("nixi.actions")


def _media_embed(result: media.MediaResult) -> discord.Embed:
    embed = discord.Embed()
    embed.set_image(url=result.url)
    if result.attribution:
        embed.set_footer(text=result.attribution)
    return embed


async def build_news_briefing(topic: str | None = None, *, include_links: bool = False) -> str:
    """Fetch and summarize a compact news briefing."""
    query = topic or ", ".join(config.MORNING_NEWS_TOPICS)
    results = await search.news_search(query, count=8)
    return await search.summarize_results(query, results, mode="news", include_links=include_links)


async def execute_owner_intent(
    message: discord.Message,
    owner_intent: intent.OwnerIntent,
) -> bool:
    """
    Execute an owner-only intent.

    Returns True when an action was handled and normal chat should stop.
    """
    name = owner_intent.name
    if message.author.id != config.OWNER_ID:
        return False
    if name == "chat":
        return False

    try:
        if name == "avatar_edit":
            if not message.guild:
                await message.reply("Avatar caricatures are available in server chat only.", mention_author=False)
                return True
            member_id = owner_intent.target_user_id or message.author.id
            member = message.guild.get_member(member_id)
            if member is None or member.bot:
                await message.reply("Choose a member of this server for the caricature.", mention_author=False)
                return True
            if not await avatar_edits.permitted(message.guild.id, member.id):
                await message.reply("That member has disabled avatar edits, roasting, or media.", mention_author=False)
                return True
            if not avatar_edits.ready():
                await message.reply("Avatar editing needs its Cloudflare configuration enabled before I can render an image.", mention_author=False)
                return True
            scene = owner_intent.action
            if owner_intent.query or scene not in avatar_edits.SCENES:
                scene = await avatar_edits.prepare_owner_scene(owner_intent.query or message.content)
                if scene is None:
                    await message.reply("I couldn't prepare that as a nonsexual, all-ages cartoon scene. No image was generated.", mention_author=False)
                    return True
            image = await avatar_edits.generate(message.guild.id, member, scene, owner_requested=True)
            if image:
                sent = await avatar_edits.send_reply(message, image, target_member_id=member.id)
                if sent is not None:
                    await memory.log_message(message.channel.id, message.guild.id, sent.author.id,
                                             "Nixi", f"[AI-edited avatar caricature delivered for member ID {member.id}]", is_bot=True)
                else:
                    await message.reply("The image wasn't delivered: preferences may have changed, or Discord rejected the attachment. Check Attach Files and Embed Links permissions.", mention_author=False)
            else:
                await message.reply("Avatar editing is unavailable, cooling down, or out of budget. No image was posted.", mention_author=False)
            return True

        if name == "cat":
            result = await media.fetch_cat()
            if result:
                await message.reply(embed=_media_embed(result), mention_author=False)
            else:
                await message.reply("Cat service returned no image. Try again shortly.", mention_author=False)
            return True

        if name == "web_search":
            query = owner_intent.query or message.content
            results = await search.web_search(query, count=6)
            await message.reply(
                await search.summarize_results(query, results, include_links=requests_links(message.content)),
                mention_author=False,
            )
            return True

        if name == "news":
            await message.reply(
                await build_news_briefing(owner_intent.query or None, include_links=requests_links(message.content)),
                mention_author=False,
            )
            return True

        if name == "image_search":
            query = owner_intent.query or message.content
            result = await media.fetch_image(query)
            if not result:
                await message.reply("I found no image fit for the household gallery.", mention_author=False)
                return True
            await message.reply(embed=_media_embed(result), mention_author=False)
            return True

        if name == "gif_search":
            query = owner_intent.query or message.content
            result = await media.fetch_gif(query)
            if not result:
                await message.reply(
                    f"GIPHY returned no GIFs for '{filter_links(query)}'. Try a simpler search.",
                    mention_author=False,
                )
                return True
            await message.reply(embed=_media_embed(result), mention_author=False)
            return True

        if name == "meme":
            result = await media.fetch_meme()
            if not result:
                await message.reply("No new meme available right now. I'll skip the reruns.", mention_author=False)
                return True
            sent = await message.reply(filter_links(result.title[:2000], requests_links(message.content)) or None, embed=_media_embed(result), mention_author=False)
            await memory.log_message(
                channel_id=message.channel.id,
                guild_id=message.guild.id if message.guild else None,
                author_id=sent.author.id,
                author_name="Nixi",
                content=f"[Meme delivered as a Discord image embed: {result.title[:500]}]",
                is_bot=True,
            )
            return True

        if name in {"tea", "coffee"}:
            result = await media.fetch_tea_or_coffee(name)
            if not result:
                await message.reply(f"GIPHY returned no {name}-serving GIFs. Try me again in a moment.", mention_author=False)
                return True
            label = "Tea service" if name == "tea" else "Coffee service"
            await message.reply(f"{label}, as requested.", embed=_media_embed(result), mention_author=False)
            return True

        if name == "action_gif":
            result = await media.fetch_action_gif(owner_intent.action)
            target = f"<@{owner_intent.target_user_id}>" if owner_intent.target_user_id else "the intended party"
            if not result:
                await message.reply(
                    "I could not find a suitable reaction GIF. For now, consider this a stern eyebrow.",
                    mention_author=False,
                )
                return True
            await message.channel.send(
                f"{target}, the Young Master has requested a little slapstick diplomacy.",
                embed=_media_embed(result),
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                    users=[discord.Object(id=owner_intent.target_user_id)] if owner_intent.target_user_id else [], replied_user=False),
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
