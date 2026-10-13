"""Media helpers for memes, GIFs, and image embeds."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from urllib.parse import quote_plus

import httpx

import config
from services import search

log = logging.getLogger("nixi.media")


@dataclass
class MediaResult:
    title: str
    url: str
    source_url: str = ""
    attribution: str = ""


SAFE_ACTION_QUERIES = {
    "punch": "funny cartoon punch gif",
    "bonk": "funny bonk gif",
    "slap": "funny cartoon slap gif",
    "hug": "wholesome hug gif",
    "glare": "dramatic glare gif",
    "stare": "dramatic stare gif",
    "tea": "tea serving",
    "coffee": "coffee serving",
}


async def fetch_meme() -> MediaResult | None:
    """Fetch a meme from a simple no-key API."""
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        response = await client.get("https://meme-api.com/gimme")
        response.raise_for_status()
        data = response.json()
    url = data.get("url") or data.get("preview", [None])[-1]
    if not url:
        return None
    return MediaResult(
        title=data.get("title", "A meme, presented on a silver tray."),
        url=url,
        source_url=data.get("postLink", ""),
    )


async def fetch_gif(query: str) -> MediaResult | None:
    """Fetch a GIPHY GIF, using legacy Tenor only when GIPHY is unset."""
    if config.GIPHY_API_KEY:
        params = {
            "api_key": config.GIPHY_API_KEY,
            "q": query[:50],
            "limit": 1,
            "rating": "pg" if config.MEDIA_SAFE_MODE else "r",
        }
        # HTTP exceptions include the request URL, which contains the API key.
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.get("https://api.giphy.com/v1/gifs/search", params=params)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise RuntimeError(f"GIPHY request failed (HTTP {status})") from None
        except httpx.RequestError:
            raise RuntimeError("Could not connect to GIPHY") from None
        results = data.get("data", [])
        if not results:
            log.info("GIPHY returned no results for query %r", params["q"])
            return None
        item = results[0]
        images = item.get("images", {})
        rendition = images.get("downsized") or images.get("original") or {}
        url = rendition.get("url")
        if not url:
            url = images.get("original", {}).get("url")
        if not url:
            return None
        return MediaResult(
            title=item.get("title") or query,
            url=url,
            source_url=item.get("url", ""),
            attribution="Powered By GIPHY",
        )
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
    item = random.choice(results)
    formats = item.get("media_formats", {})
    media = formats.get("gif") or formats.get("tinygif") or {}
    url = media.get("url")
    if not url:
        return None
    return MediaResult(
        title=item.get("content_description") or query,
        url=url,
        source_url=item.get("itemurl", ""),
    )


async def fetch_image(query: str) -> MediaResult | None:
    """Fetch an image using the configured search provider."""
    results = await search.image_search(query, count=8)
    if results:
        item = random.choice(results)
        return MediaResult(title=item.title, url=item.image_url or item.url, source_url=item.url)
    return MediaResult(title=query, url=search.unsplash_source_url(query))


async def fetch_tea_or_coffee(kind: str) -> MediaResult | None:
    """Fetch the first serving GIF rather than substituting a still image."""
    return await fetch_gif(SAFE_ACTION_QUERIES[kind])


async def fetch_action_gif(action: str) -> MediaResult | None:
    """Fetch a safe action GIF for targetted owner commands."""
    normalized = action.lower().strip()
    query = SAFE_ACTION_QUERIES.get(normalized, f"funny {quote_plus(normalized)} gif")
    return await fetch_gif(query)
