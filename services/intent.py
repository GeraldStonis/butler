"""Natural language intent detection; execution permissions live in actions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import config
from services import llm, avatar_edits


ACTION_PATTERNS = {
    "handshake": r"hand[ -]?shakes?|shak(?:e|ing)\s+(?:(?:their|his|her|your)\s+)?hands?",
    "high_five": r"high[ -]?fives?",
    "fist_bump": r"fist[ -]?bumps?",
    "pat": r"(?:head[ -]?)?pats?|patting",
    "punch": r"punch(?:es|ing)?",
    "bonk": r"bonk(?:s|ing)?",
    "slap": r"slaps?|slapping",
    "hug": r"hugs?|hugging",
    "kiss": r"kiss(?:es|ing)?",
    "kick": r"kick(?:s|ing)?",
    "boop": r"boop(?:s|ing)?",
    "poke": r"pok(?:e|es|ing)",
    "cuddle": r"cuddl(?:e|es|ing)",
    "tickle": r"tickl(?:e|es|ing)",
    "wave": r"wav(?:e|es|ing)",
    "glare": r"glar(?:e|es|ing)",
    "stare": r"star(?:e|es|ing)",
    "yeet": r"yeet(?:s|ing)?",
    "smack": r"smack(?:s|ing)?",
    "tea": r"tea|chai",
    "coffee": r"coffee|espresso|latte",
}


@dataclass
class OwnerIntent:
    name: str
    query: str = ""
    action: str = ""
    target_user_id: int | None = None


def _strip_bot_name(text: str) -> str:
    return re.sub(r"\b(nixi|nixon|butler)\b[:,]?\s*", "", text, flags=re.IGNORECASE).strip()


def _first_mention_id(mentions: list, *, include_owner: bool = False) -> int | None:
    for user in mentions:
        if (include_owner or getattr(user, "id", None) != config.OWNER_ID) and not getattr(user, "bot", False):
            return user.id
    return None


def _reaction_action(text: str) -> str:
    text = re.sub(r"<@!?\d+>(?:'s)?", " ", text)
    text = " ".join(text.split())
    for action, pattern in ACTION_PATTERNS.items():
        if re.search(rf"\b(?:{pattern})\b", text):
            return action
    return ""


def _song_request(text: str) -> OwnerIntent | None:
    text = re.sub(r"<@!?\d+>", "", text).strip(" ,:!?.\n")
    match = re.match(
        r"^(?:(?:hey|yo|please|pls)[\s,:]+)*(?:(?:can|could|would|will)\s+you\s+)?"
        r"(play|put on|queue|send|share|give|gimme|recommend|suggest|drop)\s+(?:(?:me|us)\s+)?(.+)$",
        text, re.I,
    )
    if not match:
        return None
    verb, query = match.groups()
    musical = re.search(r"\b(?:songs?|music|tracks?|tunes?|playlist)\b", query, re.I)
    if verb.lower() not in {"play", "put on", "queue"} and not musical:
        return None
    if verb.lower() not in {"play", "put on", "queue"} and re.search(
        r"\b(?:news|headlines|information|explanation|books?|articles?)\b", query, re.I,
    ):
        return None
    if re.search(r"\b(?:gifs?|memes?|images?|pictures?|videos?|games?|trivia|quiz|cards|chess)\b", query, re.I):
        return None
    if re.search(r"\b(?:higher[ -](?:or[ -])?lower|high[ -]low)\b", query, re.I):
        return None
    if re.search(r"\b(?:vc|voice(?:\s+(?:channel|chat))?)\b", query, re.I):
        return OwnerIntent("chat")  # Sharing a preview is not voice playback.
    query = re.sub(r"(?:,?\s+(?:please|pls))+$", "", query, flags=re.I).strip(" .!?\"'")
    if re.fullmatch(
        r"(?:(?:a|an|some|any|another|random|one|more)\s+)*(?:random\s+)?"
        r"(?:songs?|music|tracks?|tunes?|something|one)"
        r"(?:\s+(?:from|off)\s+(?:(?:your|the|my|our)\s+)?playlist)?"
        r"(?:\s+for\s+(?:me|us|everyone))?", query, re.I,
    ):
        query = ""
    else:
        query = re.sub(r"^(?:(?:a|some|any)\s+)?(?:songs?|music|tracks?|tunes?|something)\s+", "", query, flags=re.I)
        query = re.sub(r"^(?:called|named|titled|by|from|a|some)\s+", "", query, flags=re.I)
        query = re.sub(r"\s+(?:songs?|music|tracks?|tunes?)$", "", query, flags=re.I)
    return OwnerIntent("song", query=query.strip(" \"'"))


def heuristic_intent(text: str, mentions: list | None = None) -> OwnerIntent | None:
    """Catch common requests without spending an LLM call."""
    mentions = mentions or []
    cleaned = _strip_bot_name(text)
    lower = cleaned.lower()
    song_request = _song_request(cleaned)
    if song_request:
        return song_request
    target_id = _first_mention_id(mentions)
    reaction_target_id = _first_mention_id(mentions, include_owner=True)
    informational = re.match(
        r"^(?:(?:hey|please)\s+)*(?:(?:how|why|what|when|who|does|did|is|are)\b|"
        r"(?:(?:can|could|would|will)\s+you\s+)?(?:explain|define|describe)\b)", lower)
    negated = re.search(r"\b(?:don't|do not|never|stop|avoid)\b", lower)
    explicit_edit = (re.search(r"\b(?:avatar|pfp|profile picture|caricature|draw|paint|render|depict|portray|photoshop)\b", lower)
                     or not re.search(r"\bgifs?\b", lower) and re.search(r"\b(?:edit|image|picture|photo|portrait|illustration)\b", lower))
    action = _reaction_action(lower)
    if (action and action not in {"tea", "coffee"} and not informational and not negated and not explicit_edit
            and (reaction_target_id or re.match(r"^(?:(?:hey|please)\s+)*(?:(?:can|could|would|will)\s+you\s+)?(?:give|send|show|make|go|" + ACTION_PATTERNS[action] + r")\b", lower))):
        return OwnerIntent("action_gif", action=action, target_user_id=reaction_target_id)

    scene = avatar_edits.scene_from_text(lower)
    directed_edit = target_id and re.search(
        rf"\b(?:make|put|dress|turn|depict|portray|draw|paint|render)\s+<@!?{target_id}>", lower)
    requested_picture = (target_id and re.search(r"\b(?:make|create|generate|draw|paint|render)\b", lower)
                         and re.search(r"\b(?:image|picture|photo|portrait|illustration|caricature)\b", lower))
    if (not informational and not negated
            and re.search(r"\b(?:edit|roast|dress|put|make|create|generate|turn|change|depict|portray|draw|paint|render)\b", lower)
            and (re.search(r"\b(?:avatar|pfp|profile picture)\b", lower) or directed_edit or requested_picture or target_id and scene)):
        return OwnerIntent("avatar_edit", query=cleaned, action=scene, target_user_id=target_id)

    if re.search(r"\b(?:start|play|host|give|gimme|launch)\b.*\b(?:trivia|quiz)\b", lower) or lower in {"trivia", "quiz"}:
        return OwnerIntent("trivia")
    if (re.search(r"\b(?:start|play|host|launch)\b.*\b(?:higher[ -](?:or[ -])?lower|high[ -]low)\b", lower)
            or lower.strip(" .!?") in {"higher or lower", "higher-lower", "high low"}):
        return OwnerIntent("cards")
    if re.search(r"\b(?:leaderboard|game scores)\b", lower):
        return OwnerIntent("leaderboard")
    if (not re.search(r"\bgifs?\b", lower)
            and re.search(r"\bcat\b.*\b(?:meme|reaction|caption)\b|\b(?:send|fetch|show|give)\b.*\bcat\b", lower)):
        return OwnerIntent("cat")

    if action in {"tea", "coffee"} and reaction_target_id and not informational and not negated and not explicit_edit:
        return OwnerIntent("action_gif", action=action, target_user_id=reaction_target_id)

    if re.search(r"\b(morning news|news briefing|today'?s news|headlines)\b", lower):
        topic = re.sub(r"\b(fetch|get|give|me|the|morning|news|briefing|headlines|today'?s)\b", "", lower)
        return OwnerIntent("news", query=topic.strip() or ", ".join(config.MORNING_NEWS_TOPICS))

    if re.search(r"\b(?:caption(?:ed)?\s+(?:an?\s+|this\s+)?gifs?|gifs?\s+(?:memes?|with\s+(?:a\s+)?caption))\b", lower):
        return OwnerIntent("captioned_gif", query=cleaned)

    if (
        lower.strip(" .!?\n") in {"meme", "memes"}
        or re.search(r"\b(send|fetch|get|share|give|gimme|show|another|more)\b.*\bmemes?\b", lower)
        or re.search(r"\bmemes?\s+(?:please|pls)\b", lower)
    ):
        return OwnerIntent("meme")

    if re.search(r"\b(tea|chai)\b", lower) and re.search(r"\b(send|fetch|get|share|serve|bring|pour|brew|image|gifs?|cup|please|pls)\b", lower):
        if re.search(r"\bgifs?\b", lower):
            return OwnerIntent("gif_search", query="tea serving")
        return OwnerIntent("tea")

    if re.search(r"\b(coffee|espresso|latte)\b", lower) and re.search(r"\b(send|fetch|get|share|serve|bring|pour|brew|image|gifs?|cup|please|pls)\b", lower):
        if re.search(r"\bgifs?\b", lower):
            return OwnerIntent("gif_search", query="coffee serving")
        return OwnerIntent("coffee")

    if lower.strip() in {"tea", "chai"}:
        return OwnerIntent("tea")

    if lower.strip() in {"coffee", "espresso", "latte"}:
        return OwnerIntent("coffee")

    gif_match = re.search(r"\b(?:gif|reaction gif)\s+(?:of|for)?\s*(.+)", lower)
    if gif_match:
        return OwnerIntent("gif_search", query=gif_match.group(1).strip())

    image_match = re.search(r"\b(?:image|picture|photo|pic)\s+(?:of|for)?\s*(.+)", lower)
    if image_match:
        return OwnerIntent("image_search", query=image_match.group(1).strip())

    search_match = re.search(
        r"\b(?:search|look up|lookup|find info about|find information about|what'?s the latest on|latest on)\b\s*(.+)",
        lower,
    )
    if search_match:
        return OwnerIntent("web_search", query=search_match.group(1).strip())

    return None


async def classify_owner_intent(text: str, mentions: list | None = None) -> OwnerIntent:
    """Classify a message into an action or chat, without granting authority."""
    mentions = mentions or []
    heuristic = heuristic_intent(text, mentions)
    if heuristic:
        return heuristic

    prompt = [
        {
            "role": "system",
            "content": (
                "Classify the user's Discord message for a butler bot. Return ONLY "
                "valid JSON with keys: intent, query, action. Valid intents: chat, "
                "web_search, news, image_search, gif_search, action_gif, captioned_gif, meme, tea, coffee, song, trivia, cards, leaderboard, cat, avatar_edit. "
                "Use song for requests to play/share/recommend music in text chat: the application "
                "selects a real YouTube preview from its local playlist. Set query to only the "
                "requested title, artist, or category, or an empty string for any/random song. "
                "Never invent a video ID or URL. Explicit requests for voice-channel playback "
                "are chat, because the bot shares previews, not voice audio. Questions about "
                "music or how playback works are chat, not song requests. "
                "Use action_gif for social actions toward a member, such as giving a handshake, "
                "punch, kiss, kick, hug, pat, high five, fist bump, wave, or a similar reaction. "
                "Set action to a short plain-English action phrase; do not invent a target ID. "
                "These requests mean search for an existing reaction GIF and tag the member, "
                "NOT an AI caricature, even when phrased as 'make @user hug someone'. "
                "For 'hug someone' or 'give me a handshake GIF' without an actual mention, "
                "still use action_gif: send the GIF without tagging anyone. Mentions are "
                "optional; never ask for one just to send a GIF. Questions discussing actions are chat, "
                "not instructions to perform them. Never perform a negated action request. "
                "Use captioned_gif only when asked to create an animated meme or add new captions "
                "to a GIF template, not to search for an ordinary reaction GIF. "
                "Use avatar_edit when asked to edit a member's avatar/pfp or depict a mentioned member "
                "in an explicitly requested drawing/edit, or a custom visual scene such as "
                "'make @user eat grass in a field'. Ordinary social actions always use action_gif "
                "unless an avatar/image edit is explicitly requested. Preserve their "
                "requested scene in query; it is not limited to preset outfits. Do not invent a target ID. "
                "Use cards for higher-or-lower games, trivia for quizzes, cat for captioned cat reactions. "
                "Use chat if the user is only conversing. Use web_search for factual "
                "lookup requests. Use news for news/headline requests."
            ),
        },
        {"role": "user", "content": text},
    ]
    raw = await llm.generate_response(prompt, temperature=0.1, max_tokens=160)
    try:
        data = llm.parse_json(raw)
        if not isinstance(data, dict):
            return OwnerIntent("chat")
    except (ValueError, TypeError):
        return OwnerIntent("chat")

    name = str(data.get("intent", "chat")).strip()
    if name not in {
        "chat", "web_search", "news", "image_search", "gif_search", "action_gif", "captioned_gif", "meme", "tea", "coffee", "song", "trivia", "cards", "leaderboard", "cat", "avatar_edit"
    }:
        name = "chat"
    action = str(data.get("action", "")).strip()
    if name == "action_gif":
        if not 1 <= len(action) <= 60 or re.search(r"https?://|<[^>]*>|@|[\r\n]", action, re.I):
            action = ""
        else:
            action = _reaction_action(action.lower()) or action.lower()
    return OwnerIntent(
        name=name,
        query=text if name in {"avatar_edit", "captioned_gif"} else str(data.get("query", "")).strip(),
        action=action,
        target_user_id=(_first_mention_id(mentions, include_owner=name == "action_gif")
                        if name in {"avatar_edit", "action_gif"} else None),
    )
