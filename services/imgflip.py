"""Imgflip rendering with free image endpoints and opt-in, bounded paid GIFs."""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
import config
from services import database as db

log = logging.getLogger("nixi.imgflip")
_catalog = {}
_retry_until = {}
_gif_lock = asyncio.Lock()
SAFE_IMAGE_IDS = {
    "181913649", "87743020", "112126428", "222403160", "217743513",
    "124822590", "135256802", "97984", "4087833", "91538330",
    "309868304", "129242436", "188390779", "161865971", "101470",
    "102156234", "61579", "61520", "61544",
}
SAFE_GIF_IDS = {
    "222516354", "304768047", "367782046", "306216142", "171218846",
    "324040475", "511357979", "308128486", "171219241", "386198399",
    "345987437", "347580183", "258651081", "446086102", "499084427",
    "308448403", "489932982", "205604035",
}


async def available(*, animated: bool = False) -> bool:
    if not config.IMGFLIP_API_KEY or time.monotonic() < _retry_until.get(animated, 0):
        return False
    if not animated:
        return True
    if not config.IMGFLIP_GIF_CAPTIONS_ENABLED or config.IMGFLIP_GIF_DAILY_LIMIT <= 0:
        return False
    day = datetime.now(timezone.utc).date().isoformat()
    try:
        used = int(await db.get_state(f"imgflip_gif_daily:{day}") or 0)
    except ValueError:
        return False
    return used < config.IMGFLIP_GIF_DAILY_LIMIT


def _failure(animated: bool, detail: str) -> None:
    # Provider response bodies can echo input or credentials; never log them.
    log.warning("Skipped Imgflip %s captioning (%s)", "GIF" if animated else "image", detail)
    _retry_until[animated] = time.monotonic() + 900


async def templates(client: httpx.AsyncClient, *, animated: bool = False) -> list[dict]:
    if not await available(animated=animated):
        return []
    cached_at, cached = _catalog.get(animated, (0, []))
    if cached and time.monotonic() < cached_at + 86400:
        return cached
    try:
        response = await client.get("https://api.imgflip.com/get_memes",
                                    params={"type": "gif" if animated else "image"},
                                    follow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) and payload.get("success") is True else None
        items = data.get("memes") if isinstance(data, dict) else None
        if not isinstance(items, list):
            _failure(animated, "invalid template response")
            return []
        allowed = SAFE_GIF_IDS if animated else SAFE_IMAGE_IDS
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            template_id, name, boxes = str(item.get("id", "")), item.get("name"), item.get("box_count")
            if (not re.fullmatch(r"[0-9]{1,15}", template_id) or not isinstance(name, str)
                    or not isinstance(boxes, int) or not 1 <= boxes <= 3
                    or config.MEDIA_SAFE_MODE and template_id not in allowed):
                continue
            result.append({"id": template_id, "name": name[:120], "lines": boxes})
        _catalog[animated] = (time.monotonic(), result)
        return result
    except httpx.HTTPStatusError as exc:
        _failure(animated, f"HTTP {exc.response.status_code}")
    except (httpx.RequestError, ValueError, TypeError) as exc:
        _failure(animated, type(exc).__name__)
    return []


async def caption(client: httpx.AsyncClient, template_id: str, captions: list[str], *, animated: bool = False) -> str | None:
    if animated:
        async with _gif_lock:
            if not await available(animated=True):
                return None
            # Reserve before the paid request, including failed/cancelled attempts.
            day = datetime.now(timezone.utc).date().isoformat()
            key = f"imgflip_gif_daily:{day}"
            used = int(await db.get_state(key) or 0)
            await db.set_state(key, str(used + 1))
            return await _caption(client, template_id, captions, animated=True)
    if not await available():
        return None
    return await _caption(client, template_id, captions, animated=False)


async def _caption(client: httpx.AsyncClient, template_id: str, captions: list[str], *, animated: bool) -> str | None:
    form = {"template_id": template_id}
    form.update({f"boxes[{index}][text]": text for index, text in enumerate(captions)})
    try:
        endpoint = "caption_gif" if animated else "caption_image"
        response = await client.post(f"https://api.imgflip.com/{endpoint}", data=form,
                                     headers={"Authorization": f"Bearer {config.IMGFLIP_API_KEY}"},
                                     follow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) and payload.get("success") is True else None
        url = data.get("url") if isinstance(data, dict) else None
        parts = urlsplit(url) if isinstance(url, str) else None
        if (not parts or len(url) > 2048 or parts.scheme != "https" or parts.hostname != "i.imgflip.com"
                or parts.username or parts.password or parts.port not in {None, 443}
                or not parts.path.lower().endswith(".gif" if animated else (".jpg", ".jpeg", ".png", ".webp"))):
            _failure(animated, "invalid caption response")
            return None
        return url
    except httpx.HTTPStatusError as exc:
        _failure(animated, f"HTTP {exc.response.status_code}")
    except (httpx.RequestError, ValueError, TypeError) as exc:
        _failure(animated, type(exc).__name__)
    return None
