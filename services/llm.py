"""
LLM service — wraps the Ollama Cloud API via the official ollama SDK.

Uses AsyncClient for non-blocking calls within the Discord event loop.
Connects to https://ollama.com with Bearer token auth.
"""

from __future__ import annotations

import logging
import re
from ollama import AsyncClient

import config

log = logging.getLogger("nixi.llm")

_client: AsyncClient | None = None


def _get_client() -> AsyncClient:
    """Lazy-initialise the Ollama async client."""
    global _client
    if _client is None:
        _client = AsyncClient(
            host=config.OLLAMA_HOST,
            headers={"Authorization": f"Bearer {config.OLLAMA_API_KEY}"},
        )
    return _client


async def generate_response(
    messages: list[dict[str, str]],
    temperature: float = 0.85,
    max_tokens: int = 600,
) -> str:
    """
    Send a chat request and return the assistant's reply.

    Parameters
    ----------
    messages : list of {"role": ..., "content": ...}
        Full conversation context including the system prompt.
    temperature : float
        Higher = more creative. Use ~0.3 for factual /ask, ~0.85 for chat.
    max_tokens : int
        Cap reply length. Discord messages max out at 2000 chars; 600 tokens
        keeps replies punchy and in-character.
    """
    client = _get_client()
    try:
        response = await client.chat(
            model=config.OLLAMA_MODEL,
            messages=messages,
            options={
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        )
        content = response.message.content
        if content:
            content = content.strip()
            # Strip bot name prefixes if the LLM hallucinates them
            content = re.sub(r"^(?:\[)?Nixi(?:\])?:\s*", "", content, flags=re.IGNORECASE)
            return content
        return ""
    except Exception:
        log.exception("LLM call failed")
        return (
            "I do beg your pardon -- a momentary lapse in my faculties. "
            "Might I suggest trying again in a moment?"
        )


async def extract_memories(conversation_snippet: str) -> str | None:
    """
    Ask the LLM to extract any noteworthy facts from a conversation snippet.

    Returns JSON-like text of facts, or None if nothing worth remembering.
    """
    client = _get_client()
    try:
        response = await client.chat(
            model=config.OLLAMA_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a memory extraction assistant. Given a conversation "
                        "snippet, extract any facts, preferences, personal details, "
                        "or decisions worth remembering long-term. Return ONLY a JSON "
                        "array of objects with keys 'category' (one of: fact, preference, "
                        "user_trait, decision), 'subject' (who/what), and 'content' "
                        "(the fact). If there is nothing worth remembering, return "
                        "exactly the word: nothing"
                    ),
                },
                {"role": "user", "content": conversation_snippet},
            ],
            options={
                "temperature": 0.2,
                "num_predict": 300,
            },
        )
        content = response.message.content
        if not content or content.strip().lower() == "nothing":
            return None
        return content.strip()
    except Exception:
        log.exception("Memory extraction failed")
        return None
