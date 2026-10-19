"""Natural language actions with explicit public and owner-only permissions."""

from __future__ import annotations

import logging

import discord

import config
from services import intent, media, music, search, memory, avatar_edits, conversation, database as db
from services.response_text import filter_links, requests_links

log = logging.getLogger("nixi.actions")

GUEST_ACTIONS = frozenset({
    "cat", "web_search", "news", "image_search", "gif_search", "meme",
    "tea", "coffee", "action_gif", "song",
})


def _media_embed(result: media.MediaResult) -> discord.Embed:
    embed = discord.Embed()
    embed.set_image(url=result.url)
    if result.attribution:
        embed.set_footer(text=result.attribution)
    return embed


async def reply_discreetly(message: discord.Message, kind: str, reason: str) -> None:
    log.info("Continuing %s as conversation (%s)", kind, reason)
    text = await conversation.discreet_reply(message.content, is_owner=message.author.id == config.OWNER_ID)
    sent = await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    await memory.log_delivery(sent)


async def build_news_briefing(topic: str | None = None, *, include_links: bool = False) -> str:
    """Fetch and summarize a compact news briefing."""
    query = topic or ", ".join(config.MORNING_NEWS_TOPICS)
    results = await search.news_search(query, count=8)
    return await search.summarize_results(query, results, mode="news", include_links=include_links)


async def execute_owner_intent(
    message: discord.Message,
    owner_intent: intent.OwnerIntent,
) -> bool:
    """Owner entry point; never trusts a display name or classifier result."""
    if message.author.id != config.OWNER_ID:
        return False
    return await _execute_intent(message, owner_intent)


async def execute_guest_intent(message: discord.Message, guest_intent: intent.OwnerIntent) -> bool:
    """Guests can use public tools, never paid edits or administrative tools."""
    if guest_intent.name not in GUEST_ACTIONS:
        return False
    return await _execute_intent(message, guest_intent)


async def _execute_intent(message: discord.Message, owner_intent: intent.OwnerIntent) -> bool:
    name = owner_intent.name
    is_owner = message.author.id == config.OWNER_ID
    if not is_owner and name not in GUEST_ACTIONS:
        return False
    if name == "chat":
        return False

    try:
        if name == "song":
            song = await music.select_song(message.channel.id, owner_intent.query)
            if not song:
                await reply_discreetly(message, name, "no_matching_song")
                return True
            label = " - ".join(s for field in ("title", "artist")
                               if isinstance(s := song.get(field), str) and s.strip())
            label = discord.utils.escape_markdown(filter_links(label))[:250]
            url = f"https://www.youtube.com/watch?v={song['youtubeId']}"
            sent = await message.reply(f"{label}\n{url}".strip(), mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())
            await memory.log_delivery(sent)
            return True

        if name == "avatar_edit":
            if not message.guild:
                await reply_discreetly(message, name, "server_only")
                return True
            member_id = owner_intent.target_user_id or message.author.id
            member = message.guild.get_member(member_id)
            if member is None or member.bot:
                await reply_discreetly(message, name, "unresolved_target")
                return True
            if not await avatar_edits.permitted(message.guild.id, member.id):
                await reply_discreetly(message, name, "member_boundary")
                return True
            if not avatar_edits.ready():
                await reply_discreetly(message, name, "not_configured")
                return True
            scene = owner_intent.action
            if owner_intent.query or scene not in avatar_edits.SCENES:
                scene = await avatar_edits.prepare_owner_scene(owner_intent.query or message.content)
                if scene is None:
                    await reply_discreetly(message, name, "scene_not_prepared")
                    return True
            image = await avatar_edits.generate(message.guild.id, member, scene, owner_requested=True)
            if image:
                sent = await avatar_edits.send_reply(message, image, target_member_id=member.id)
                if sent is not None:
                    await memory.log_message(message.channel.id, message.guild.id, sent.author.id,
                                             "Nixi", f"[AI-edited avatar caricature delivered for member ID {member.id}]", is_bot=True)
                else:
                    await reply_discreetly(message, name, "attachment_not_delivered")
            else:
                await reply_discreetly(message, name, "no_image")
            return True

        if name == "cat":
            result = await media.fetch_cat()
            if result:
                await message.reply(embed=_media_embed(result), mention_author=False)
            else:
                await reply_discreetly(message, name, "no_image")
            return True

        if name == "web_search":
            query = owner_intent.query or message.content
            results = await search.web_search(query, count=6)
            text = await search.summarize_results(query, results, include_links=requests_links(message.content))
            if text:
                await message.reply(text, mention_author=False)
            else:
                await reply_discreetly(message, name, "no_verified_results")
            return True

        if name == "news":
            text = await build_news_briefing(owner_intent.query or None, include_links=requests_links(message.content))
            if text:
                await message.reply(text, mention_author=False)
            else:
                await reply_discreetly(message, name, "no_verified_results")
            return True

        if name == "image_search":
            query = owner_intent.query or message.content
            result = await media.fetch_image(query)
            if not result:
                await reply_discreetly(message, name, "no_image")
                return True
            await message.reply(embed=_media_embed(result), mention_author=False)
            return True

        if name == "gif_search":
            query = owner_intent.query or message.content
            result = await media.fetch_gif(query)
            if not result:
                await reply_discreetly(message, name, "no_gif")
                return True
            await message.reply(embed=_media_embed(result), mention_author=False)
            return True

        if name in {"meme", "captioned_gif"}:
            if name == "captioned_gif":
                if not config.IMGFLIP_API_KEY:
                    await reply_discreetly(message, name, "not_configured")
                    return True
                if not config.IMGFLIP_GIF_CAPTIONS_ENABLED:
                    await reply_discreetly(message, name, "paid_generation_disabled")
                    return True
                result = await media.fetch_captioned_gif(owner_intent.query or message.content)
            else:
                result = await media.fetch_meme()
            if not result:
                await reply_discreetly(message, name, "no_new_media")
                return True
            sent = await message.reply(filter_links(result.title[:2000], requests_links(message.content)) or None, embed=_media_embed(result), mention_author=False)
            await memory.log_message(
                channel_id=message.channel.id,
                guild_id=message.guild.id if message.guild else None,
                author_id=sent.author.id,
                author_name="Nixi",
                content=f"[{'Captioned GIF' if name == 'captioned_gif' else 'Meme'} delivered as a Discord image embed: {result.title[:500]}]",
                is_bot=True,
            )
            return True

        if name in {"tea", "coffee"}:
            result = await media.fetch_tea_or_coffee(name)
            if not result:
                await reply_discreetly(message, name, "no_gif")
                return True
            label = "Tea service" if name == "tea" else "Coffee service"
            await message.reply(f"{label}, as requested.", embed=_media_embed(result), mention_author=False)
            return True

        if name == "action_gif":
            target_id = owner_intent.target_user_id
            member = message.guild.get_member(target_id) if message.guild and target_id else None
            mentioned_ids = {user.id for user in getattr(message, "mentions", [])}
            if member is not None and (member.bot or member.id != target_id or member.id not in mentioned_ids):
                member = None
            if not owner_intent.action:
                await reply_discreetly(message, name, "unresolved_action")
                return True
            preferences = await db.get_member_preferences(message.guild.id, member.id) if member else {}
            if member and (not preferences["media"] or not is_owner and not preferences["roasting"]):
                await reply_discreetly(message, name, "member_boundary")
                return True
            result = await media.fetch_action_gif(owner_intent.action)
            if not result:
                await reply_discreetly(message, name, "no_gif")
                return True
            # Recheck comfort settings after waiting on the GIF provider.
            preferences = await db.get_member_preferences(message.guild.id, member.id) if member else {}
            if member and (not preferences["media"] or not is_owner and not preferences["roasting"]):
                await reply_discreetly(message, name, "member_boundary")
                return True
            sent = await message.channel.send(
                f"<@{member.id}>" if member else None,
                embed=_media_embed(result),
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                    users=[discord.Object(id=member.id)] if member and preferences["pings"] else [], replied_user=False),
            )
            await memory.log_delivery(sent)
            return True

    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(exc, "status", None)
        log.error("Natural language action failed: %s (%s, HTTP %s)", name, type(exc).__name__, status)
        await reply_discreetly(message, name, "exception")
        return True

    return False
