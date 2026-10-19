"""Media helpers for memes, GIFs, and image embeds."""

from __future__ import annotations

import logging
import asyncio
import random
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

import config
from services import search, database as db, llm

log = logging.getLogger("nixi.media")


@dataclass
class MediaResult:
    title: str
    url: str
    source_url: str = ""
    attribution: str = ""
    standalone: bool = False


SAFE_ACTION_QUERIES = {
    "handshake": "funny handshake gif",
    "high_five": "funny high five gif",
    "fist_bump": "funny fist bump gif",
    "pat": "wholesome head pat gif",
    "punch": "funny cartoon punch gif",
    "bonk": "funny bonk gif",
    "slap": "funny cartoon slap gif",
    "hug": "wholesome hug gif",
    "kiss": "wholesome kiss gif",
    "kick": "funny cartoon kick gif",
    "boop": "cute boop gif",
    "poke": "funny poke gif",
    "cuddle": "wholesome cuddle gif",
    "tickle": "funny tickle gif",
    "wave": "friendly wave gif",
    "yeet": "funny cartoon yeet gif",
    "smack": "funny cartoon smack gif",
    "glare": "dramatic glare gif",
    "stare": "dramatic stare gif",
    "tea": "tea serving",
    "coffee": "coffee serving",
}


async def fetch_meme() -> MediaResult | None:
    """Select a previously unseen meme; skip rather than recycle old images."""
    try:
        from services.generated_memes import create_meme
        return await asyncio.wait_for(create_meme(), timeout=60)
    except asyncio.TimeoutError:
        log.warning("No new meme found within the search time budget")
        return None


async def fetch_captioned_gif(topic: str = "") -> MediaResult | None:
    """Render an original animated template only when paid captioning is enabled."""
    if (not config.IMGFLIP_API_KEY or not config.IMGFLIP_GIF_CAPTIONS_ENABLED
            or config.IMGFLIP_GIF_DAILY_LIMIT <= 0):
        return None
    if not isinstance(topic, str) or re.search(r"https?://|<@|@everyone|@here", topic, re.I):
        return None
    try:
        from services.generated_memes import create_meme
        return await asyncio.wait_for(create_meme(animated=True, topic=topic[:300]), timeout=60)
    except asyncio.TimeoutError:
        log.warning("No new captioned GIF within the generation time budget")
        return None


def _meme_url_key(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


async def fetch_cat() -> MediaResult | None:
    """Generate a standalone caption; no server conversation reaches Cataas."""
    from services.activities import commentary
    caption = await commentary("Invent a NEW generic smug cat reaction caption <=80 characters. No names, quotes, personal information or references to any actual conversation.")
    caption = " ".join(caption.split())[:80]
    if not caption:
        return None
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        response = await client.get("https://cataas.com/cat", params={"json": "true"})
        response.raise_for_status()
        data = response.json()
    cat_id = str(data.get("id") or data.get("_id") or "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", cat_id):
        return None
    return MediaResult(title=caption, url=f"https://cataas.com/cat/{cat_id}/says/{quote(caption, safe='')}")


async def fetch_gif(query: str, *, avoid_repeats: bool = False) -> MediaResult | None:
    """Fetch a GIPHY GIF, using legacy Tenor only when GIPHY is unset."""
    if config.GIPHY_API_KEY:
        params = {
            "api_key": config.GIPHY_API_KEY, "q": query[:50],
            "limit": 8 if avoid_repeats else 1,
            "rating": "pg" if config.MEDIA_SAFE_MODE else "r",
        }
        # Search beyond exhausted first-page results, without changing owner ranking.
        offsets = (0, 8, 16) if avoid_repeats else (0,)
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                for offset in offsets:
                    response = await client.get("https://api.giphy.com/v1/gifs/search",
                                                params={**params, "offset": offset})
                    response.raise_for_status()
                    results = response.json().get("data", [])
                    if not results:
                        return None
                    for item in results:
                        images = item.get("images", {})
                        rendition = images.get("downsized") or images.get("original") or {}
                        url = rendition.get("url") or images.get("original", {}).get("url")
                        if not url:
                            continue
                        gif_key = "giphy:" + str(item.get("id") or _meme_url_key(url))
                        if avoid_repeats and not await db.claim_reaction_gif(gif_key):
                            continue
                        return MediaResult(title=item.get("title") or query, url=url,
                                           source_url=item.get("url", ""), attribution="Powered By GIPHY")
        except httpx.HTTPStatusError as exc:
            # Never surface a provider URL containing its API key.
            raise RuntimeError(f"GIPHY request failed (HTTP {exc.response.status_code})") from None
        except httpx.RequestError:
            raise RuntimeError("Could not connect to GIPHY") from None
        return None
    if not config.TENOR_API_KEY:
        return None
    params = {
        "key": config.TENOR_API_KEY,
        "q": query,
        "limit": 8,
        "media_filter": "gif,tinygif",
        "contentfilter": "medium" if config.MEDIA_SAFE_MODE else "off",
    }
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.get("https://tenor.googleapis.com/v2/search", params=params)
        response.raise_for_status()
        data = response.json()
    results = data.get("results", [])
    if not results:
        return None
    random.shuffle(results)
    for item in results:
        formats = item.get("media_formats", {})
        rendition = formats.get("gif") or formats.get("tinygif") or {}
        url = rendition.get("url")
        if not url:
            continue
        gif_key = "tenor:" + str(item.get("id") or _meme_url_key(url))
        if avoid_repeats and not await db.claim_reaction_gif(gif_key):
            continue
        return MediaResult(title=item.get("content_description") or query, url=url, source_url=item.get("itemurl", ""))
    return None


async def fetch_image(query: str) -> MediaResult | None:
    """Fetch an image using the configured search provider."""
    results = await search.image_search(query, count=8)
    if results:
        item = random.choice(results)
        return MediaResult(title=item.title, url=item.image_url or item.url, source_url=item.url)
    return None


async def fetch_tea_or_coffee(kind: str) -> MediaResult | None:
    """Fetch the first serving GIF rather than substituting a still image."""
    return await fetch_gif(SAFE_ACTION_QUERIES[kind])


async def fetch_action_gif(action: str) -> MediaResult | None:
    """Search existing GIFs for a social action, never generate an avatar edit."""
    normalized = action.lower().strip()
    if not 1 <= len(normalized) <= 60 or re.search(r"https?://|<[^>]*>|@|[\r\n]", normalized, re.I):
        return None
    query = SAFE_ACTION_QUERIES.get(normalized, f"funny {normalized.replace('_', ' ')} gif")
    return await fetch_gif(query)
