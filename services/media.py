"""Media helpers for memes, GIFs, and image embeds."""

from __future__ import annotations

import logging
import asyncio
import random
import re
import hashlib
import json
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

import config
from services import search, database as db, llm

log = logging.getLogger("nixi.media")
_gif_selection_lock = asyncio.Lock()
_gif_provider_backoff: tuple[str, float] = ('', 0)


class GifProviderError(RuntimeError):
    def __init__(self, message: str, retry_after: int = 30):
        super().__init__(message)
        self.retry_after = retry_after


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
    "hit": "funny cartoon hitting gif",
    "jump": "funny jumping gif",
    "dance": "funny dancing gif",
    "run": "funny running gif",
    "laugh": "laughing reaction gif",
    "clap": "clapping applause gif",
    "bow": "bowing politely gif",
    "cry": "crying reaction gif",
    "sleep": "sleeping gif",
    "nod": "nodding yes gif",
    "salute": "saluting gif",
    "shrug": "shrug reaction gif",
    "facepalm": "facepalm reaction gif",
    "spin": "spinning gif",
}

REACTION_CATEGORIES = {
    **{name: name for name in ('handshake', 'pat', 'punch', 'bonk', 'slap', 'hug', 'kiss',
        'kick', 'poke', 'cuddle', 'tickle', 'wave', 'yeet', 'stare', 'dance', 'run', 'laugh',
        'clap', 'cry', 'sleep', 'nod', 'salute', 'shrug', 'facepalm', 'spin')},
    'high_five': 'highfive', 'fist_bump': 'handshake', 'smack': 'slap', 'hit': 'punch',
    'glare': 'stare', 'boop': 'poke',
}
CONTEXTUAL_REACTIONS = ('stare', 'facepalm', 'shrug', 'clap', 'nod', 'laugh', 'dance',
                        'wave', 'smug', 'shocked', 'think', 'nope', 'confused', 'happy', 'blush', 'tableflip')


def _gif_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and bool(parts.hostname) and not parts.username
                and not parts.password and parts.path.lower().endswith('.gif'))
    except (TypeError, ValueError):
        return False


async def _choose_gif(candidates: list[tuple[str, MediaResult]], query: str, *, allow_reuse: bool = False) -> MediaResult | None:
    if not candidates:
        return None
    key = 'gif_last:' + hashlib.sha256(query.casefold().strip().encode()).hexdigest()
    async with _gif_selection_lock:
        try:
            recent = json.loads(await db.get_state('gif_recent') or '[]')
        except ValueError:
            recent = []
        if not isinstance(recent, list):
            recent = []
        async def remember(gif_key):
            await db.set_state(key, gif_key)
            await db.set_state('gif_recent', json.dumps(([v for v in recent if v != gif_key] + [gif_key])[-40:]))
        for gif_key, result in candidates:
            if await db.claim_reaction_gif(gif_key):
                await remember(gif_key)
                return result
        if allow_reuse:
            last = await db.get_state(key)
            pool = [item for item in candidates if item[0] != last] or candidates
            fresh = [item for item in pool if item[0] not in recent]
            gif_key, result = random.choice(fresh) if fresh else min(pool, key=lambda item: recent.index(item[0]) if item[0] in recent else -1)
            await remember(gif_key)
            return result
    return None


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


async def fetch_gif(query: str, *, avoid_repeats: bool = False, allow_reuse: bool = False) -> MediaResult | None:
    """Fetch a GIPHY GIF, using legacy Tenor only when GIPHY is unset."""
    if config.GIPHY_API_KEY:
        params = {
            "api_key": config.GIPHY_API_KEY, "q": query[:50],
            "limit": 8 if avoid_repeats else 1,
            "rating": "pg" if config.MEDIA_SAFE_MODE else "r",
        }
        # Search beyond exhausted first-page results, without changing owner ranking.
        offsets = (0, 8, 16) if avoid_repeats else (0,)
        seen_candidates = []
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                for offset in offsets:
                    response = await client.get("https://api.giphy.com/v1/gifs/search",
                                                params={**params, "offset": offset})
                    response.raise_for_status()
                    results = response.json().get("data", [])
                    if not results:
                        break
                    page = []
                    for item in results:
                        images = item.get("images", {})
                        rendition = images.get("downsized") or images.get("original") or {}
                        url = rendition.get("url") or images.get("original", {}).get("url")
                        if not url:
                            continue
                        gif_key = "giphy:" + str(item.get("id") or _meme_url_key(url))
                        page.append((gif_key, MediaResult(title=item.get("title") or query, url=url,
                                           source_url=item.get("url", ""), attribution="Powered By GIPHY")))
                    if page and not avoid_repeats:
                        return page[0][1]
                    selected = await _choose_gif(page, query)
                    if selected:
                        return selected
                    seen_candidates.extend(page)
        except httpx.HTTPStatusError as exc:
            # Never surface a provider URL containing its API key.
            log.warning("GIPHY request failed (HTTP %d)", exc.response.status_code)
            status = exc.response.status_code
            raise GifProviderError(f"GIPHY request failed (HTTP {status})",
                                   900 if status in {401, 403} else 60 if status == 429 else 30) from None
        except httpx.RequestError:
            raise GifProviderError("Could not connect to GIPHY") from None
        return await _choose_gif(seen_candidates, query, allow_reuse=allow_reuse) if allow_reuse else None
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
    candidates = []
    for item in results:
        formats = item.get("media_formats", {})
        rendition = formats.get("gif") or formats.get("tinygif") or {}
        url = rendition.get("url")
        if not url:
            continue
        gif_key = "tenor:" + str(item.get("id") or _meme_url_key(url))
        candidates.append((gif_key, MediaResult(title=item.get("content_description") or query, url=url, source_url=item.get("itemurl", ""))))
    if not avoid_repeats:
        return candidates[0][1] if candidates else None
    return await _choose_gif(candidates, query, allow_reuse=allow_reuse)


async def fetch_available_gif(query: str, *, allow_reuse: bool = True, fallback_action: str = '') -> MediaResult | None:
    """Use ranked GIFs, free action categories, then actual animated search results."""
    global _gif_provider_backoff
    provider = hashlib.sha256((config.GIPHY_API_KEY or config.TENOR_API_KEY or '').encode()).hexdigest()
    blocked, until = _gif_provider_backoff
    if provider != blocked or time.monotonic() >= until:
        try:
            result = await fetch_gif(query, avoid_repeats=True, allow_reuse=allow_reuse)
            if result:
                return result
        except (RuntimeError, httpx.HTTPError) as exc:
            if isinstance(exc, GifProviderError):
                _gif_provider_backoff = (provider, time.monotonic() + exc.retry_after)
            log.info('Trying alternative GIF sources after provider failure')
    from services.intent import _reaction_action
    category = (fallback_action if fallback_action in CONTEXTUAL_REACTIONS else
                REACTION_CATEGORIES.get(_reaction_action(query.casefold())))
    if category:
        try:
            async with httpx.AsyncClient(timeout=10, headers={'User-Agent': config.GIF_USER_AGENT}) as client:
                response = await client.get(f'https://nekos.best/api/v2/{category}', params={'amount': 8})
                response.raise_for_status()
                items = response.json().get('results', [])
            candidates = [('nekos:' + _meme_url_key(item['url']), MediaResult(
                title=f'{category} GIF', url=item['url'], attribution='nekos.best'))
                for item in items if isinstance(item, dict) and _gif_url(item.get('url', ''))]
            result = await _choose_gif(candidates, query, allow_reuse=allow_reuse)
            if result:
                return result
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            log.info('Action GIF fallback skipped (%s)', type(exc).__name__)
    try:
        # Unlike ordinary image search, never substitute a still image for a GIF.
        items = await search.image_search(query + ' animated gif', count=8)
        candidates = [('search:' + _meme_url_key(url), MediaResult(
            title=item.title, url=url, source_url=item.url))
            for item in items if _gif_url(url := item.image_url or item.url)]
        return await _choose_gif(candidates, query, allow_reuse=allow_reuse)
    except (httpx.HTTPError, RuntimeError, ValueError, TypeError) as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        log.info('Animated GIF search fallback skipped (%s, HTTP %s)', type(exc).__name__, status)
        return None


async def fetch_image(query: str) -> MediaResult | None:
    """Fetch an image using the configured search provider."""
    results = await search.image_search(query, count=8)
    if results:
        item = random.choice(results)
        return MediaResult(title=item.title, url=item.image_url or item.url, source_url=item.url)
    return None


async def fetch_tea_or_coffee(kind: str) -> MediaResult | None:
    """Rotate matching serving GIFs, never substitute a still image."""
    return await fetch_available_gif(SAFE_ACTION_QUERIES[kind])


async def fetch_action_gif(action: str) -> MediaResult | None:
    """Search existing GIFs for a social action, never generate an avatar edit."""
    normalized = action.lower().strip()
    if not 1 <= len(normalized) <= 60 or re.search(r"https?://|<[^>]*>|@|[\r\n]", normalized, re.I):
        return None
    query = SAFE_ACTION_QUERIES.get(normalized, f"funny {normalized.replace('_', ' ')} gif")
    return await fetch_available_gif(query)
