"""Shared pacing for unsolicited posts; owner-requested actions are exempt."""

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config
from services import database as db

log = logging.getLogger("nixi.engagement")


def quiet_now(now: datetime | None = None, *, hours: str | None = None) -> bool:
    hours = config.QUIET_HOURS if hours is None else hours
    if not hours:
        return False
    try:
        start, end = hours.split("-", 1)
        def minute(value):
            hour, minute = map(int, value.strip().split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError("Invalid time")
            return hour * 60 + minute
        start, end = minute(start), minute(end)
        zone = ZoneInfo(config.SERVER_TIMEZONE)
        now = now or datetime.now(zone)
        if now.tzinfo is not None:
            now = now.astimezone(zone)
        current = now.hour * 60 + now.minute
        return start <= current < end if start < end else current >= start or current < end if start > end else False
    except (ValueError, ZoneInfoNotFoundError):
        log.warning("Invalid quiet-hours configuration; suppressing unsolicited posts")
        return True


def dead_chat_quiet_now(now: datetime | None = None) -> bool:
    return quiet_now(now, hours=config.DEAD_CHAT_QUIET_HOURS)


def spam_quiet_now(channel_id: int, now: datetime | None = None) -> bool:
    return bool(config.STICKER_SPAM_CHANNEL_ID and channel_id == config.STICKER_SPAM_CHANNEL_ID
                and quiet_now(now, hours=config.SPAM_QUIET_HOURS))


async def can_post(channel_id: int) -> bool:
    if quiet_now() or spam_quiet_now(channel_id):
        return False
    stored = await db.get_state(f"autonomous_last:{channel_id}")
    try:
        last = float(stored or 0)
    except ValueError:
        last = 0
    return time.time() - last >= config.AUTONOMOUS_MIN_GAP_SECONDS


async def mark_sent(channel_id: int) -> None:
    await db.set_state(f"autonomous_last:{channel_id}", str(time.time()))
