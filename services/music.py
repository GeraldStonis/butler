"""Shared playlist selection and a single persistent lottery per quiet spell."""

import asyncio
import json
import logging
import random
import re
import unicodedata

import config
from services import database as db

log = logging.getLogger("nixi.music")
_selection_lock = asyncio.Lock()


def load_songs() -> list[dict]:
    path = config.SONGS_PATH
    if not path.exists() and path.name == "songs_data.json":
        path = path.with_name("songs.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, list):
            raise ValueError("Playlist must be an array")
        unique = {}
        for song in data:
            if not isinstance(song, dict):
                continue
            video_id = str(song.get("youtubeId", ""))
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                unique[video_id] = {**song, "youtubeId": video_id}
        return list(unique.values())
    except (OSError, ValueError):
        log.warning("Unable to read valid playlist from %s", path)
        return []


def _normalized(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", text.replace("'", "").replace("\u2019", "")))


async def select_song(channel_id: int, query: str = "") -> dict | None:
    """Pick a real playlist entry, sharing persistent history with idle drops."""
    songs = load_songs()
    query = _normalized(query)
    if query:
        exact = [s for s in songs if _normalized(str(s.get("title", ""))) == query]
        terms = set(query.split()) - {"by"}
        songs = exact or [s for s in songs if terms and terms <= set(_normalized(
            " ".join(str(s.get(field, "")) for field in ("title", "artist", "category"))).split())]
    if not songs:
        return None
    async with _selection_lock:
        recent_key = f"music_recent:{channel_id}"
        try:
            recent = json.loads(await db.get_state(recent_key) or "[]")
        except ValueError:
            recent = []
        if not isinstance(recent, list):
            recent = []
        candidates = [s for s in songs if s["youtubeId"] not in recent]
        if not candidates:
            candidates = [s for s in songs if not recent or s["youtubeId"] != recent[-1]]
        # Honor a specifically requested track even if it was just played.
        if not candidates and query:
            candidates = songs
        if not candidates:
            return None
        song = random.choice(candidates)
        await db.set_state(recent_key, json.dumps((recent + [song["youtubeId"]])[-20:]))
        return song


async def idle_song(row: dict) -> dict | None:
    if row.get("last_user_id") is None:
        return None
    state_key = f"music_idle:{row['channel_id']}"
    try:
        state = json.loads(await db.get_state(state_key) or "{}")
    except ValueError:
        state = {}
    if not isinstance(state, dict):
        state = {}
    episode = str(row["last_message_at"])
    if state.get("episode") != episode:
        state = {"episode": episode, "after": random.uniform(600, 900), "checked": False}
        await db.set_state(state_key, json.dumps(state))
    if state.get("checked") or float(row.get("human_idle_seconds") or 0) < state["after"]:
        return None
    # Persist the attempt before rolling: one 10% chance, never one per timer tick.
    state["checked"] = True
    await db.set_state(state_key, json.dumps(state))
    if random.random() >= 0.1:
        return None
    return await select_song(row["channel_id"])
