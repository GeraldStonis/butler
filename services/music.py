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
        tags_path = path.with_name("song_tags.json")
        if tags_path.exists():
            try:
                tags = json.loads(tags_path.read_text(encoding="utf-8"))
                if isinstance(tags, dict):
                    for video_id, metadata in tags.items():
                        if video_id in unique and isinstance(metadata, dict):
                            for field in ("moods", "languages"):
                                values = metadata.get(field)
                                if field not in unique[video_id] and isinstance(values, list):
                                    unique[video_id][field] = [v for v in values if isinstance(v, str)]
            except (OSError, ValueError):
                log.warning("Unable to read playlist tags from %s", tags_path)
        return list(unique.values())
    except (OSError, ValueError):
        log.warning("Unable to read valid playlist from %s", path)
        return []


def _normalized(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", text.replace("'", "").replace("\u2019", "")))


TAG_ALIASES = {
    "sad": "sad", "melancholy": "sad", "heartbreak": "sad", "udaas": "sad",
    "party": "party", "dance": "party", "dancing": "party",
    "happy": "happy", "cheerful": "happy", "uplifting": "happy",
    "chill": "chill", "calm": "chill", "relaxing": "chill", "relaxed": "chill",
    "romantic": "romantic", "romance": "romantic",
    **{language: language for language in ("english", "hindi", "assamese", "marathi", "punjabi", "bhojpuri", "bengali", "tamil", "telugu")},
}


def _matching_songs(songs: list[dict], query: str) -> tuple[list[dict], bool]:
    exact = [s for s in songs if _normalized(str(s.get("title", ""))) == query]
    if exact:
        return exact, True
    terms = set(query.split()) - {"by", "a", "some", "song", "songs", "music", "track", "tracks", "in", "please"}
    tags = {TAG_ALIASES[t] for t in terms if t in TAG_ALIASES}
    words = terms - TAG_ALIASES.keys()
    matches = []
    for song in songs:
        metadata = []
        for field in ("moods", "languages"):
            values = song.get(field, [])
            if isinstance(values, list):
                metadata.extend(v for v in values if isinstance(v, str))
        # Existing explicit categories still work. Titles never imply musical mood.
        metadata.append(str(song.get("category", "")))
        known_tags = {TAG_ALIASES.get(t, t) for value in metadata for t in _normalized(value).split()}
        searchable = set(_normalized(" ".join(str(song.get(field, "")) for field in ("title", "artist", "category"))).split())
        if terms and tags <= known_tags and words <= searchable:
            matches.append(song)
    explicit_track = any(title_terms and title_terms <= terms for s in matches
                         if (title_terms := set(_normalized(str(s.get("title", ""))).split())))
    return matches, explicit_track


async def select_song(channel_id: int, query: str = "") -> dict | None:
    """Pick a real playlist entry, sharing persistent history with idle drops."""
    songs = load_songs()
    query = _normalized(query)
    exact_track = False
    if query:
        songs, exact_track = _matching_songs(songs, query)
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
        if not candidates and exact_track:
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
