"""Natural language intent detection for owner-only operations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import config
from services import llm, avatar_edits


ACTION_WORDS = {
    "punch", "bonk", "slap", "hug", "glare", "stare", "kick", "boop",
    "yeet", "smack", "tea", "coffee",
}


@dataclass
class OwnerIntent:
    name: str
    query: str = ""
    action: str = ""
    target_user_id: int | None = None


def _strip_bot_name(text: str) -> str:
    return re.sub(r"\b(nixi|nixon|butler)\b[:,]?\s*", "", text, flags=re.IGNORECASE).strip()


def _first_mention_id(mentions: list) -> int | None:
    for user in mentions:
        if getattr(user, "id", None) != config.OWNER_ID and not getattr(user, "bot", False):
            return user.id
    return None


def heuristic_intent(text: str, mentions: list | None = None) -> OwnerIntent | None:
    """Catch common owner requests without spending an LLM call."""
    mentions = mentions or []
    cleaned = _strip_bot_name(text)
    lower = cleaned.lower()
    target_id = _first_mention_id(mentions)

    scene = avatar_edits.scene_from_text(lower)
    directed_edit = target_id and re.search(
        rf"\b(?:make|put|dress|turn|depict|portray|draw|paint|render)\s+<@!?{target_id}>", lower)
    if (not re.match(r"^(?:how|why|what|explain)\b", lower)
            and re.search(r"\b(?:edit|roast|dress|put|make|turn|change|depict|portray|draw|paint|render)\b", lower)
            and (re.search(r"\b(?:avatar|pfp|profile picture)\b", lower) or directed_edit or target_id and scene)):
        return OwnerIntent("avatar_edit", query=cleaned, action=scene, target_user_id=target_id)

    if re.search(r"\b(?:start|play|host|give|gimme|launch)\b.*\b(?:trivia|quiz)\b", lower) or lower in {"trivia", "quiz"}:
        return OwnerIntent("trivia")
    if (re.search(r"\b(?:start|play|host|launch)\b.*\b(?:higher[ -](?:or[ -])?lower|high[ -]low)\b", lower)
            or lower.strip(" .!?") in {"higher or lower", "higher-lower", "high low"}):
        return OwnerIntent("cards")
    if re.search(r"\b(?:leaderboard|game scores)\b", lower):
        return OwnerIntent("leaderboard")
    if re.search(r"\bcat\b.*\b(?:meme|reaction|caption)\b|\b(?:send|fetch|show|give)\b.*\bcat\b", lower):
        return OwnerIntent("cat")

    for action in ACTION_WORDS:
        if target_id and re.search(rf"\b{re.escape(action)}\b", lower):
            return OwnerIntent("action_gif", action=action, target_user_id=target_id)

    if re.search(r"\b(morning news|news briefing|today'?s news|headlines)\b", lower):
        topic = re.sub(r"\b(fetch|get|give|me|the|morning|news|briefing|headlines|today'?s)\b", "", lower)
        return OwnerIntent("news", query=topic.strip() or ", ".join(config.MORNING_NEWS_TOPICS))

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
    """Classify an owner message into an executable action or chat."""
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
                "web_search, news, image_search, gif_search, meme, tea, coffee, trivia, cards, leaderboard, cat, avatar_edit. "
                "Use avatar_edit when asked to edit a member's avatar/pfp or depict a mentioned member "
                "doing something imaginary, such as 'make @user eat grass in a field'. Preserve their "
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
        "chat", "web_search", "news", "image_search", "gif_search", "meme", "tea", "coffee", "trivia", "cards", "leaderboard", "cat", "avatar_edit"
    }:
        name = "chat"
    return OwnerIntent(
        name=name,
        query=text if name == "avatar_edit" else str(data.get("query", "")).strip(),
        action=str(data.get("action", "")).strip(),
        target_user_id=_first_mention_id(mentions) if name == "avatar_edit" else None,
    )
