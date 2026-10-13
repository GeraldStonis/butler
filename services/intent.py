"""Natural language intent detection for owner-only operations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import config
from services import llm


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
        if getattr(user, "id", None) != config.OWNER_ID:
            return user.id
    return None


def heuristic_intent(text: str, mentions: list | None = None) -> OwnerIntent | None:
    """Catch common owner requests without spending an LLM call."""
    mentions = mentions or []
    cleaned = _strip_bot_name(text)
    lower = cleaned.lower()
    target_id = _first_mention_id(mentions)

    for action in ACTION_WORDS:
        if target_id and re.search(rf"\b{re.escape(action)}\b", lower):
            return OwnerIntent("action_gif", action=action, target_user_id=target_id)

    if re.search(r"\b(morning news|news briefing|today'?s news|headlines)\b", lower):
        topic = re.sub(r"\b(fetch|get|give|me|the|morning|news|briefing|headlines|today'?s)\b", "", lower)
        return OwnerIntent("news", query=topic.strip() or ", ".join(config.MORNING_NEWS_TOPICS))

    if re.search(r"\b(send|fetch|get|share).*\bmeme\b|\bmeme\b", lower):
        return OwnerIntent("meme")

    if re.search(r"\b(tea|chai)\b", lower) and re.search(r"\b(send|fetch|get|share|image|gif|cup|please|pls)\b", lower):
        return OwnerIntent("tea")

    if re.search(r"\b(coffee|espresso|latte)\b", lower) and re.search(r"\b(send|fetch|get|share|image|gif|cup|please|pls)\b", lower):
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
                "web_search, news, image_search, gif_search, meme, tea, coffee. "
                "Use chat if the user is only conversing. Use web_search for factual "
                "lookup requests. Use news for news/headline requests."
            ),
        },
        {"role": "user", "content": text},
    ]
    raw = await llm.generate_response(prompt, temperature=0.1, max_tokens=160)
    json_text = raw.strip()
    if "{" in json_text and "}" in json_text:
        json_text = json_text[json_text.find("{"):json_text.rfind("}") + 1]
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        return OwnerIntent("chat")

    name = str(data.get("intent", "chat")).strip()
    if name not in {
        "chat", "web_search", "news", "image_search", "gif_search", "meme", "tea", "coffee"
    }:
        name = "chat"
    return OwnerIntent(
        name=name,
        query=str(data.get("query", "")).strip(),
        action=str(data.get("action", "")).strip(),
    )
