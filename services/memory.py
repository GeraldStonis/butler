"""
Two-tier memory system for Nixi.

Tier 1 — Rolling Window: raw recent messages per channel, auto-summarised
         when they exceed the threshold.
Tier 2 — Long-Term Memory: extracted facts that persist beyond the window.

The main entry point is `build_context()` which assembles the full
message list ready to send to the LLM.
"""

from __future__ import annotations

import json
import logging
import re

import discord

import config
from services import database as db
from services import llm
from services import emoji_filter

log = logging.getLogger("nixi.memory")


# ── Context Assembly ─────────────────────────────────────────────────────────

async def build_context(
    channel_id: int,
    current_message: str,
    current_author: str,
    mode: str,
    guild_id: int | None = None,
    guild: discord.Guild | None = None,
) -> list[dict[str, str]]:
    """
    Assemble the full LLM context for a response.

    Order:
    1. System prompt (persona)
    2. Owner standing instructions
    3. Relevant long-term memories
    4. Latest channel summary (compressed older context)
    5. Recent raw messages
    6. Current user message with mode tag
    """
    messages: list[dict[str, str]] = []

    # 1. System prompt
    persona = config.load_persona()
    messages.append({"role": "system", "content": persona})

    # 2. Owner instructions
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

    # 3. Long-term memories (keyword search from current message)
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

    # 4. Channel summary
    summary = await db.get_latest_summary(channel_id)
    if summary:
        messages.append({
            "role": "system",
            "content": f"Summary of earlier conversation in this channel:\n{summary}",
        })

    # 5. Recent raw messages
    recent = await db.get_recent_messages(channel_id)
    for msg in recent:
        role = "assistant" if msg["is_bot"] else "user"
        messages.append({
            "role": role,
            "content": f"[{msg['author']}]: {msg['content']}",
        })

    # 6. Current message with mode
    mode_descriptions = {
        "DIRECT_ADDRESS": (
            "MODE: DIRECT_ADDRESS — Someone is talking directly TO you (Nixi). "
            "Full sass-and-charm mode."
        ),
        "BOSS_MENTIONED": (
            "MODE: BOSS_MENTIONED — Someone mentioned your employer by name or "
            "nickname without addressing you. Step in on his behalf as his butler, "
            "speaking about him in the third person."
        ),
        "REPLY_CHAIN": (
            "MODE: REPLY_CHAIN — You are continuing an existing conversation. "
            "Stay consistent with the tone you opened with."
        ),
        "ASK_COMMAND": (
            "MODE: DIRECT_ADDRESS — The user has asked a factual question via /ask. "
            "Prioritize giving a clear, correct, and helpful answer. You may still "
            "speak in character, but accuracy comes first."
        ),
    }
    mode_tag = mode_descriptions.get(mode, f"MODE: {mode}")

    messages.append({
        "role": "system",
        "content": mode_tag,
    })

    # Inject available server emojis so the LLM can use them naturally
    emoji_context = emoji_filter.get_emoji_context(guild)
    if emoji_context:
        messages.append({
            "role": "system",
            "content": emoji_context,
        })

    messages.append({
        "role": "system",
        "content": (
            "FORMATTING RULE (OBEY STRICTLY): If the user is speaking Hindi, "
            "Hinglish, or any Indian language, you MUST reply in ROMAN SCRIPT "
            "(Latin letters) only. Example: 'arre bhai mast hoon' NOT "
            "'अरे भाई मस्त हूं'. NEVER use Devanagari or any non-Latin script "
            "unless quoting a single word for comedic effect. This is Discord "
            "chat — type like real people type online.\n\n"
            "When addressing or referring to a specific user (other than the Young Master), "
            "prefix their name with an @ symbol (e.g. '@chingu').\n\n"
            "DO NOT prepend your messages with '[Nixi]:' or 'Nixi:'. Just output your response directly."
        ),
    })

    messages.append({
        "role": "user",
        "content": f"[{current_author}]: {current_message}",
    })

    return messages


# ── Message Logging & Summarisation ──────────────────────────────────────────

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

    # Check if we need to summarise
    count = await db.count_messages(channel_id)
    threshold = config.CONTEXT_WINDOW_SIZE + config.SUMMARY_THRESHOLD
    if count > threshold:
        await _summarise_oldest(channel_id, guild_id)


async def _summarise_oldest(
    channel_id: int, guild_id: int | None
) -> None:
    """Summarise the oldest batch of messages and remove them."""
    oldest = await db.get_oldest_messages(channel_id, config.SUMMARY_THRESHOLD)
    if not oldest:
        return

    # Build a transcript for the LLM to summarise
    transcript_lines = []
    for msg in oldest:
        prefix = "[BOT] " if msg["is_bot"] else ""
        transcript_lines.append(
            f"{prefix}{msg['author_name']}: {msg['content']}"
        )
    transcript = "\n".join(transcript_lines)

    summary_text = await llm.generate_response(
        messages=[
            {
                "role": "system",
                "content": (
                    "Summarise the following Discord conversation concisely. "
                    "Capture key topics, decisions, jokes, and any important "
                    "facts. Keep it under 200 words. Write in third person."
                ),
            },
            {"role": "user", "content": transcript},
        ],
        temperature=0.3,
        max_tokens=300,
    )

    # Merge with existing summary if there is one
    existing = await db.get_latest_summary(channel_id)
    if existing:
        merged = await llm.generate_response(
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
        summary_text = merged

    await db.save_summary(
        channel_id=channel_id,
        guild_id=guild_id,
        summary=summary_text,
        messages_covered=len(oldest),
    )

    # Delete the summarised raw messages
    ids = [msg["id"] for msg in oldest]
    await db.delete_messages_by_ids(ids)
    log.info(
        "Summarised %d messages for channel %d", len(oldest), channel_id
    )


# ── Memory Extraction ───────────────────────────────────────────────────────

async def extract_and_store_memories(
    channel_id: int,
    user_message: str,
    user_name: str,
    bot_reply: str,
) -> None:
    """
    After a conversation turn, ask the LLM if anything is worth remembering.
    If so, parse and store the extracted facts.
    """
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
        # LLM returned something unparseable; skip silently
        log.debug("Memory extraction returned unparseable output: %s", raw)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_keywords(text: str) -> list[str]:
    """Pull meaningful keywords from a message for memory search."""
    # Strip mentions, URLs, emoji codes
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"https?://\S+", "", text)
    words = re.findall(r"[a-zA-Z\u0900-\u097F]{3,}", text.lower())
    # Remove very common English stopwords
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
