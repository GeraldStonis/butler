"""Media helpers for memes, GIFs, and image embeds."""

from __future__ import annotations

import logging
import asyncio
import hashlib
import random
from dataclasses import dataclass
from urllib.parse import quote_plus, urlsplit, urlunsplit

import httpx

import config
from services import search, database as db

log = logging.getLogger("nixi.media")


@dataclass
class MediaResult:
    title: str
    url: str
    source_url: str = ""
    attribution: str = ""
    standalone: bool = False


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
    """Select a previously unseen meme; skip rather than recycle old images."""
    try:
        return await asyncio.wait_for(_fetch_unique_meme(), timeout=20)
    except asyncio.TimeoutError:
        log.warning("No new meme found within the search time budget")
        return None


def _meme_url_key(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


async def _fetch_unique_meme() -> MediaResult | None:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for _ in range(2):
            response = await client.get("https://meme-api.com/gimme/8")
            response.raise_for_status()
            candidates = response.json().get("memes", [])
            random.shuffle(candidates)
            for item in candidates:
                if config.MEDIA_SAFE_MODE and (item.get("nsfw") or item.get("spoiler")):
                    continue
                previews = item.get("preview") or []
                url = item.get("url") or (previews[-1] if previews else None)
                if not url:
                    continue
                image_key = _meme_url_key(url)
                post_url = item.get("postLink") or ""
                post_key = _meme_url_key(post_url) if post_url else None
                if await db.meme_is_known(image_key, post_key):
                    continue
                fingerprint = hashlib.sha256()
                try:
                    async with client.stream("GET", url) as image_response:
                        image_response.raise_for_status()
                        if not image_response.headers.get("content-type", "").startswith("image/"):
                            continue
                        size = 0
                        async for chunk in image_response.aiter_bytes():
                            size += len(chunk)
                            if size > 8 * 1024 * 1024:
                                break
                            fingerprint.update(chunk)
                        if not size or size > 8 * 1024 * 1024:
                            continue
                except httpx.HTTPError:
                    continue
                if await db.claim_meme(fingerprint.hexdigest(), image_key, post_key):
                    return MediaResult(title=item.get("title") or "A meme.", url=url, source_url=post_url)
    return None


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
