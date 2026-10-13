"""Two-tier memory system for Nixi."""

from __future__ import annotations

import json
import logging
import re

import discord

import config
from services import database as db
from services import emoji_filter, llm

log = logging.getLogger("nixi.memory")


async def build_context(
    channel_id: int,
    current_message: str,
    current_author: str,
    mode: str,
    guild_id: int | None = None,
    guild: discord.Guild | None = None,
) -> list[dict[str, str]]:
    """Assemble the full LLM context for a response."""
    messages: list[dict[str, str]] = []

    messages.append({"role": "system", "content": config.load_persona()})

    instructions = await db.get_active_instructions()
    if instructions:
        instr_block = "\n".join(f"- {i}" for i in instructions)
        messages.append({
            "role": "system",
            "content": (
                "The following are standing instructions from the Young Master. "
                "Obey them at all times:\n" + instr_block
            ),
        })

    keywords = _extract_keywords(current_message)
    memories = await db.search_memories(keywords, limit=8)
    if memories:
        mem_lines = []
        for m in memories:
            subj = f" (about {m['subject']})" if m.get("subject") else ""
            mem_lines.append(f"- [{m['category']}]{subj}: {m['content']}")
        messages.append({
            "role": "system",
            "content": (
                "Relevant things you remember from past interactions:\n"
                + "\n".join(mem_lines)
            ),
        })

    summary = await db.get_latest_summary(channel_id)
    if summary:
        messages.append({
            "role": "system",
            "content": f"Summary of earlier conversation in this channel:\n{summary}",
        })

    recent = await db.get_recent_messages(channel_id)
    for msg in recent:
        role = "assistant" if msg["is_bot"] else "user"
        messages.append({
            "role": role,
            "content": f"[{msg['author']}]: {msg['content']}",
        })

    mode_descriptions = {
        "DIRECT_ADDRESS": (
            "MODE: DIRECT_ADDRESS - Someone is talking directly TO you (Nixi). "
            "Be helpful, socially aware, and lightly witty when appropriate."
        ),
        "OWNER_UNAVAILABLE": (
            "MODE: OWNER_UNAVAILABLE - Someone mentioned your employer while he "
            "appears to be away. Reply briefly and politely that he is unavailable "
            "and that you will tell him. Do not answer private questions about him."
        ),
        "REPLY_CHAIN": (
            "MODE: REPLY_CHAIN - You are continuing an existing conversation. "
            "Stay consistent with the tone you opened with."
        ),
        "ASK_COMMAND": (
            "MODE: DIRECT_ADDRESS - The user has asked a factual question via /ask. "
            "Prioritize giving a clear, correct, and helpful answer."
        ),
    }

    messages.append({
        "role": "system",
        "content": mode_descriptions.get(mode, f"MODE: {mode}"),
    })

    emoji_context = emoji_filter.get_emoji_context(guild)
    if emoji_context:
        messages.append({"role": "system", "content": emoji_context})

    messages.append({
        "role": "system",
        "content": (
            "FORMATTING RULE: If the user is speaking Hindi, Hinglish, or any "
            "Indian language, reply in ROMAN SCRIPT (Latin letters). Example: "
            "'arre bhai mast hoon', not Devanagari. This is Discord chat; type "
            "like real people type online.\n\n"
            "When addressing or referring to a specific user other than the Young "
            "Master, prefix their name with an @ symbol, e.g. '@chingu'.\n\n"
            "Do not prepend your messages with '[Nixi]:' or 'Nixi:'. Output your "
            "response directly.\n\n"
            "Use custom server emojis only when they fit naturally. Never force an "
            "emoji into a serious reply."
        ),
    })

    messages.append({
        "role": "user",
        "content": f"[{current_author}]: {current_message}",
    })

    return messages


async def log_message(
    channel_id: int,
    guild_id: int | None,
    author_id: int,
    author_name: str,
    content: str,
    is_bot: bool = False,
) -> None:
    """Log a message and trigger summarisation if the buffer is too large."""
    await db.save_message(
        channel_id=channel_id,
        guild_id=guild_id,
        author_id=author_id,
        author_name=author_name,
        content=content,
        is_bot=is_bot,
    )

    count = await db.count_messages(channel_id)
    threshold = config.CONTEXT_WINDOW_SIZE + config.SUMMARY_THRESHOLD
    if count > threshold:
        await _summarise_oldest(channel_id, guild_id)


async def _summarise_oldest(channel_id: int, guild_id: int | None) -> None:
    """Summarise the oldest batch of messages and remove them."""
    oldest = await db.get_oldest_messages(channel_id, config.SUMMARY_THRESHOLD)
    if not oldest:
        return

    transcript_lines = []
    for msg in oldest:
        prefix = "[BOT] " if msg["is_bot"] else ""
        transcript_lines.append(f"{prefix}{msg['author_name']}: {msg['content']}")
    transcript = "\n".join(transcript_lines)

    summary_text = await llm.generate_response(
        messages=[
            {
                "role": "system",
                "content": (
                    "Summarise the following Discord conversation concisely. "
                    "Capture key topics, decisions, jokes, and important facts. "
                    "Keep it under 200 words. Write in third person."
                ),
            },
            {"role": "user", "content": transcript},
        ],
        temperature=0.3,
        max_tokens=300,
    )

    existing = await db.get_latest_summary(channel_id)
    if existing:
        summary_text = await llm.generate_response(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Merge these two conversation summaries into one coherent "
                        "summary. Keep it under 300 words. Preserve all key facts."
                    ),
                },
                {
                    "role": "user",
                    "content": f"OLDER SUMMARY:\n{existing}\n\nNEWER SUMMARY:\n{summary_text}",
                },
            ],
            temperature=0.2,
            max_tokens=400,
        )

    await db.save_summary(
        channel_id=channel_id,
        guild_id=guild_id,
        summary=summary_text,
        messages_covered=len(oldest),
    )

    ids = [msg["id"] for msg in oldest]
    await db.delete_messages_by_ids(ids)
    log.info("Summarised %d messages for channel %d", len(oldest), channel_id)


async def extract_and_store_memories(
    channel_id: int,
    user_message: str,
    user_name: str,
    bot_reply: str,
) -> None:
    """Extract noteworthy long-term facts after a conversation turn."""
    snippet = f"{user_name}: {user_message}\nNixi: {bot_reply}"
    raw = await llm.extract_memories(snippet)
    if not raw:
        return

    try:
        facts = json.loads(raw)
        if not isinstance(facts, list):
            return
        for fact in facts:
            if isinstance(fact, dict) and "content" in fact:
                await db.save_memory(
                    category=fact.get("category", "fact"),
                    content=fact["content"],
                    subject=fact.get("subject"),
                    source="conversation",
                )
        log.info("Extracted %d memories from conversation", len(facts))
    except (json.JSONDecodeError, TypeError):
        log.debug("Memory extraction returned unparseable output: %s", raw)


def _extract_keywords(text: str) -> list[str]:
    """Pull meaningful keywords from a message for memory search."""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"https?://\S+", "", text)
    words = re.findall(r"[a-zA-Z\u0900-\u097F]{3,}", text.lower())
    stopwords = {
        "the", "and", "for", "are", "but", "not", "you", "all",
        "can", "had", "her", "was", "one", "our", "out", "has",
        "have", "been", "from", "this", "that", "they", "will",
        "with", "what", "when", "where", "who", "how", "why",
        "does", "did", "just", "like", "your", "about", "would",
        "there", "their", "which", "could", "other", "into",
        "some", "than", "them", "then", "these", "only",
    }
    return [w for w in words if w not in stopwords][:10]
