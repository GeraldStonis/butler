"""Playlist selection and a single persistent lottery per quiet spell."""

import json
import logging
import random
import re

import config
from services import database as db

log = logging.getLogger("nixi.music")


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
            if isinstance(song, dict) and re.fullmatch(r"[A-Za-z0-9_-]{11}", str(song.get("youtubeId", ""))):
                unique[song["youtubeId"]] = song
        return list(unique.values())
    except (OSError, ValueError):
        log.warning("Unable to read valid playlist from %s", path)
        return []


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
    songs = load_songs()
    recent_key = f"music_recent:{row['channel_id']}"
    try:
        recent = json.loads(await db.get_state(recent_key) or "[]")
    except ValueError:
        recent = []
    if not isinstance(recent, list):
        recent = []
    candidates = [s for s in songs if s["youtubeId"] not in recent]
    if not candidates:
        candidates = [s for s in songs if not recent or s["youtubeId"] != recent[-1]]
    if not candidates:
        return None
    song = random.choice(candidates)
    await db.set_state(recent_key, json.dumps((recent + [song["youtubeId"]])[-20:]))
    return song
