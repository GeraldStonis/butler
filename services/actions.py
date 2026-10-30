"""Natural language actions with explicit public and owner-only permissions."""

from __future__ import annotations

import asyncio
import html
import logging
import random
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


async def _remember_result(sent: discord.Message, description: str) -> None:
    # Discord already accepted the reply. A DB error must not trigger a second reply.
    try:
        await memory.log_delivery(sent, description=description)
    except Exception as exc:
        log.warning("Delivered tool result could not be remembered (%s)", type(exc).__name__)


async def reply_discreetly(message: discord.Message, kind: str, reason: str) -> None:
    log.info("Continuing %s as conversation (%s)", kind, reason)
    text = await conversation.discreet_reply(message.content, is_owner=message.author.id == config.OWNER_ID)
    sent = await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    await memory.log_delivery(sent)


def _news_source_text(text: str) -> str:
    """Treat fetched headlines as plain text, including during a model outage."""
    text = re.sub(r"<[^>]*>", " ", html.unescape(text))
    text = re.sub(r"@(?:everyone|here)\b", "", filter_links(text), flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def _news_sources(results: list[search.SearchItem]) -> list[search.SearchItem]:
    sources = []
    seen = set()
    for item in results:
        try:
            url = urlsplit(item.url)
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username or url.password or len(item.url) > 700
                    or any(c.isspace() or ord(c) < 32 or c in '<>"' for c in item.url)):
                continue
        except ValueError:
            continue
        title = _news_source_text(item.title)
        description = _news_source_text(item.description)
        if item.url in seen or not (title or description):
            continue
        sources.append(search.SearchItem(title, item.url, description))
        seen.add(item.url)
        if len(sources) == 3:
            break
    return sources


def _short_news_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"


def _news_fallback(results: list[search.SearchItem]) -> str:
    headlines = []
    for item in results:
        title = discord.utils.escape_markdown(_short_news_text(item.title, 160))
        description = discord.utils.escape_markdown(_short_news_text(item.description, 180))
        headlines.append("\N{BULLET} " + (f"**{title}**" if title else description)
                         + (f" — {description}" if title and description else ""))
    return "\n\n".join(headlines)


async def _news_section(topic: str, *, include_links: bool, language_request: str) -> tuple[str, list[search.SearchItem]]:
    try:
        results = _news_sources(await search.news_search(topic, count=3))
    except Exception as exc:
        log.warning("News section fetch failed (%s)", type(exc).__name__)
        return "", []
    if not results:
        return "", []
    try:
        summary = await search.summarize_results(topic, results, mode="news", include_links=include_links,
                                                 language_request=language_request)
    except Exception as exc:
        log.warning("News section summary unavailable (%s)", type(exc).__name__)
        summary = ""
    # Application code appends only fetched source destinations, and keeps them whole.
    summary = re.sub(r"<@[^>]*>|@(?:everyone|here)\b", "", filter_links(summary), flags=re.I)
    summary = re.sub(r"(?m)^\s*\[(?:\d+[ ,;-]*)+\]\s*$", "", summary).strip()
    return summary or _news_fallback(results), results


async def build_news_briefing(topic: str | None = None, *, include_links: bool = False,
                              language_request: str = "") -> discord.Embed | None:
    """Build separate, verified news sections; a model outage keeps fetched headlines."""
    requested = topic.strip() if topic else ""
    topics = []
    seen = set()
    for query in [requested] if requested else config.MORNING_NEWS_TOPICS:
        query = query.strip()
        if query and query.casefold() not in seen:
            topics.append(query)
            seen.add(query.casefold())
        if len(topics) == 6:
            break
    if not topics:
        return None
    sections = await asyncio.gather(*(_news_section(query, include_links=include_links,
                                                    language_request=language_request) for query in topics))
    if not any(results for _, results in sections):
        return None

    embed = discord.Embed(title="News Briefing" if requested else "Morning News", colour=0xE7B75A)
    try:
        now = datetime.now(ZoneInfo(config.MORNING_NEWS_TIMEZONE))
    except (ZoneInfoNotFoundError, ValueError):
        now = datetime.now(timezone.utc)
    embed.set_footer(text=f"{now:%d %b %Y} · Recent verified headlines")
    names = [_short_news_text(_news_source_text(query).title() or "News", 80) for query in topics]
    # Discord caps fields at 1,024 characters and the entire embed at 6,000.
    field_limit = min(1024, (6000 - len(embed) - sum(map(len, names))) // len(topics))
    for name, (text, results) in zip(names, sections):
        if not results:
            text = "No verified headlines available for this section right now."
        links = []
        if include_links:
            for index, item in enumerate(results, 1):
                link = f"[{index}] <{item.url}>"
                if sum(len(line) + 1 for line in links) + len(link) <= field_limit - 162:
                    links.append(link)
        suffix = "\n\n" + "\n".join(links) if links else ""
        value = _short_news_text(text, field_limit - len(suffix)) + suffix
        embed.add_field(name=name, value=value, inline=False)
    return embed


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


async def _send_member_ping(message: discord.Message) -> None:
    if message.author.id != config.OWNER_ID:
        return
    # Reparse the real text: a classifier's suggested action/ID is not authorization.
    request = intent.ping_request(message.content, list(getattr(message, "mentions", [])))
    if request is None or message.guild is None:
        await reply_discreetly(message, "member_ping", "no_valid_server_request")
        return
    opted_out = await db.get_ping_opt_outs(message.guild.id)
    candidates = []
    for visible in getattr(message.channel, "members", []):
        member = message.guild.get_member(visible.id)
        if member is None or member.bot or member.id in opted_out:
            continue
        if request.action == "random" and member.id == message.author.id:
            continue
        if request.action == "self" and member.id != message.author.id:
            continue
        if request.action == "target" and member.id != request.target_user_id:
            continue
        candidates.append(member)
    while candidates:
        member = random.choice(candidates) if request.action == "random" else candidates[0]
        preferences = await db.get_member_preferences(message.guild.id, member.id)
        if not preferences["pings"]:
            candidates.remove(member)
            continue
        sent = await message.channel.send(
            f"<@{member.id}>",
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                users=[discord.Object(id=member.id)], replied_user=False),
        )
        await memory.log_delivery(sent)
        return
    await reply_discreetly(message, "member_ping", "no_eligible_member")


async def _execute_intent(message: discord.Message, owner_intent: intent.OwnerIntent) -> bool:
    name = owner_intent.name
    is_owner = message.author.id == config.OWNER_ID
    if not is_owner and name not in GUEST_ACTIONS:
        return False
    if name == "chat":
        return False

    try:
        if name == "member_ping":
            await _send_member_ping(message)
            return True

        if name == "song":
            song = await music.select_song(message.channel.id, owner_intent.query)
            if not song:
                # A catalog miss needs useful feedback, not an unrelated generated aside.
                text = ("I couldn't match that song in my playlist. What's the exact title or artist?"
                        if owner_intent.query else
                        "I don't have a song to share from my playlist right now.")
                sent = await message.reply(text, mention_author=False,
                                           allowed_mentions=discord.AllowedMentions.none())
                await _remember_result(sent, '')
                return True
            label = " - ".join(s for field in ("title", "artist")
                               if isinstance(s := song.get(field), str) and s.strip())
            label = discord.utils.escape_markdown(filter_links(label))[:250]
            url = f"https://www.youtube.com/watch?v={song['youtubeId']}"
            sent = await message.reply(f"{label}\n{url}".strip(), mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())
            await music.record_delivery(message.channel.id, song)
            await _remember_result(sent, '')
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
                sent = await message.reply(embed=_media_embed(result), mention_author=False,
                                           allowed_mentions=discord.AllowedMentions.none())
                await _remember_result(sent, f"Cat image delivered: {result.title}; source: {result.url}")
            else:
                await reply_discreetly(message, name, "no_image")
            return True

        if name == "web_search":
            query = owner_intent.query or message.content
            results = await search.web_search(query, count=6)
            text = await search.summarize_results(query, results, include_links=requests_links(message.content),
                                                  language_request=message.content)
            if text:
                sent = await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                sources = ', '.join(item.url for item in results[:3])
                await _remember_result(sent, f"Web search answered: {query}; sources: {sources}")
            else:
                await reply_discreetly(message, name, "no_verified_results")
            return True

        if name == "news":
            briefing = await build_news_briefing(owner_intent.query or None, include_links=requests_links(message.content),
                                                 language_request=message.content)
            if briefing:
                sent = await message.reply(embed=briefing, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
                await _remember_result(sent, f"News briefing delivered: {owner_intent.query or ', '.join(config.MORNING_NEWS_TOPICS)}")
            else:
                await reply_discreetly(message, name, "no_verified_results")
            return True

        if name == "image_search":
            query = owner_intent.query or message.content
            result = await media.fetch_image(query)
            if not result:
                await reply_discreetly(message, name, "no_image")
                return True
            sent = await message.reply(embed=_media_embed(result), mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())
            await _remember_result(sent, f"Image delivered for {query}: {result.title}; source: {result.url}")
            return True

        if name == "gif_search":
            query = owner_intent.query or message.content
            result = await media.fetch_available_gif(query)
            if not result:
                await reply_discreetly(message, name, "no_gif")
                return True
            sent = await message.reply(embed=_media_embed(result), mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())
            await _remember_result(sent, f"GIF delivered for {query}: {result.title}; source: {result.url}")
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
            sent = await message.reply(embed=_media_embed(result), mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())
            await _remember_result(sent, f"{name.title()} serving GIF delivered: {result.title}; source: {result.url}")
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
