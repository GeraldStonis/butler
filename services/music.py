"""Shared playlist selection and a single persistent lottery per quiet spell."""

import asyncio
import json
import logging
import random
import re
import unicodedata
from difflib import SequenceMatcher

import config
from services import database as db

log = logging.getLogger("nixi.music")
_selection_lock = asyncio.Lock()


def load_songs() -> list[dict]:
    path = config.SONGS_PATH
    if not path.is_absolute():
        path = config.PERSONA_PATH.parent / path
    if not path.exists() and path.name in {"songs_data.json", "songs.json"}:
        alternate = path.with_name("songs.json" if path.name == "songs_data.json" else "songs_data.json")
        if alternate.exists():
            path = alternate
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
                previous = unique.get(video_id)
                record = {**song, "youtubeId": video_id}
                if previous:
                    aliases = [*previous.get('aliases', []), {
                        'title': previous.get('title', ''), 'artist': previous.get('artist', ''),
                    }]
                    canonical = (_normalized(str(record.get('title', ''))), _normalized(str(record.get('artist', ''))))
                    distinct = {(_normalized(str(alias.get('title', ''))), _normalized(str(alias.get('artist', '')))): alias
                                for alias in aliases}
                    distinct.pop(canonical, None)
                    if distinct:
                        record['aliases'] = list(distinct.values())
                unique[video_id] = record
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
    variants = []
    for song in songs:
        variants.append(song)
        for alias in song.get('aliases', []):
            if isinstance(alias, dict) and isinstance(alias.get('title'), str):
                variants.append({**song, 'title': alias['title'], 'artist': alias.get('artist', '')})
    songs = variants
    exact = [s for s in songs if _normalized(str(s.get("title", ""))) == query]
    if exact:
        return exact, True
    # Match names before treating words such as "happy", "sad" or "party" as moods.
    query = re.sub(r"^(?:play\s+)?(?:(?:me|us)\s+)?(?:(?:the|a)\s+)?(?:song|track|tune)\s+(?:(?:called|named|titled)\s+)?", "", query)
    named, separator, artist = query.rpartition(' by ')
    if not separator:
        named = query
    eligible = [s for s in songs if not separator or set(artist.split()) <= set(_normalized(str(s.get('artist', ''))).split())]
    exact = [s for s in eligible if _normalized(str(s.get('title', ''))) == named]
    if exact:
        return exact, True
    terms = set(query.split()) - {"by", "a", "some", "song", "songs", "music", "track", "tracks", "in", "please"}
    named_matches = []
    for song in eligible:
        title_terms = set(_normalized(str(song.get('title', ''))).split())
        all_terms = title_terms | set(_normalized(str(song.get('artist', ''))).split())
        title_words = title_terms - {'the', 'a', 'an'}
        if title_words and title_words <= terms and terms <= all_terms | {'the', 'a', 'an'}:
            named_matches.append(song)
    if named_matches:
        return named_matches, True
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
    if matches:
        return matches, explicit_track
    # Modest title typos/shortened titles are allowed; an unknown request is never
    # replaced by a random track. Ambiguous fuzzy matches remain no-match.
    if len(named) <= 160 and (not (set(named.split()) & TAG_ALIASES.keys()) or separator):
        titles = {}
        for song in eligible:
            title = _normalized(str(song.get('title', '')))
            if title:
                titles.setdefault(title, []).append(song)
        partial = [title for title in titles if len(named) >= 5 and f' {named} ' in f' {title} ']
        if len(partial) == 1:
            return titles[partial[0]], True
        ranked = sorted(((SequenceMatcher(None, named, title).ratio(), title) for title in titles), reverse=True)
        if len(named) >= 5 and ranked and ranked[0][0] >= 0.88 and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.08):
            return titles[ranked[0][1]], True
    return [], False


async def select_song(channel_id: int, query: str = "") -> dict | None:
    """Pick a real playlist entry, sharing persistent history with idle drops."""
    songs = load_songs()
    query = _normalized(query)
    if query:
        songs, _ = _matching_songs(songs, query)
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
        # An explicit request must not disappear just because its pool was played.
        if not candidates:
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
