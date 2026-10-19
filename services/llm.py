"""
LLM service — wraps the Ollama Cloud API via the official ollama SDK.

Uses AsyncClient for non-blocking calls within the Discord event loop.
Connects to https://ollama.com with Bearer token auth.
"""

from __future__ import annotations

import logging
import re
import asyncio
import json
from ollama import AsyncClient

import config
from services.response_text import FALLBACK_REACTION, VISIBLE_REPLY_POLICY, has_non_latin_prose

log = logging.getLogger("nixi.llm")

_client: AsyncClient | None = None
_requests = asyncio.Semaphore(config.LLM_MAX_CONCURRENT)


def parse_json(text: str):
    """Accept fenced model JSON without relying on cloud schema enforcement."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    return json.loads(text)


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
    failure_reply: str | None = None,
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
        async with asyncio.timeout(config.LLM_TIMEOUT_SECONDS):
            async with _requests:
                kwargs = {}
                model = config.OLLAMA_MODEL.lower()
                thinking = config.OLLAMA_THINKING_LEVEL.lower()
                if "gpt-oss" in model and thinking in {"low", "medium", "high"}:
                    kwargs["think"] = thinking
                elif "gemma4" in model:
                    # Gemma uses a boolean, not GPT-OSS's named reasoning levels.
                    kwargs["think"] = thinking in {"true", "1", "yes", "on"}
                for attempt in range(2):
                    response = await client.chat(
                        model=config.OLLAMA_MODEL, messages=messages,
                        options={"temperature": temperature,
                                 "num_predict": max(max_tokens, 1200) if attempt else max_tokens},
                        **kwargs,
                    )
                    content = response.message.content or ""
                    if content.strip():
                        # Never deliver reasoning traces or speaker labels.
                        content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
                        if not content:
                            continue
                        if content.startswith("{"):
                            try:
                                envelope = parse_json(content)
                                if isinstance(envelope, dict) and envelope.get("speaker", "").casefold() == "nixi" and isinstance(envelope.get("text"), str):
                                    content = envelope["text"].strip()
                            except (ValueError, AttributeError):
                                pass
                        return re.sub(r"^(?:\[)?Nixi(?:\])?:\s*", "", content, flags=re.I)
        return failure_reply if failure_reply is not None else FALLBACK_REACTION
    except Exception as exc:
        log.warning("LLM call failed (%s)", type(exc).__name__)
        if failure_reply is not None:
            return failure_reply
        return FALLBACK_REACTION


async def generate_visible_response(
    messages: list[dict[str, str]],
    temperature: float = 0.85,
    max_tokens: int = 600,
    failure_reply: str | None = None,
) -> str:
    """Apply speech rules to public text, with at most one script-repair pass."""
    prompt = list(messages)
    position = len(prompt) - 1 if prompt and prompt[-1]["role"] == "user" else len(prompt)
    prompt.insert(position, {"role": "system", "content": VISIBLE_REPLY_POLICY})
    reply = await generate_response(prompt, temperature=temperature, max_tokens=max_tokens,
                                    failure_reply=failure_reply)
    if not has_non_latin_prose(reply):
        return reply
    log.info("Rewriting a visible reply into Latin-script speech")
    repair = [*prompt,
              {"role": "assistant", "content": reply},
              {"role": "system", "content": (
                  "Rewrite the previous draft once: its conversational prose used a forbidden "
                  "script. Keep the same meaning, tone and level of detail, but write English, "
                  "Roman-script Hinglish, or Roman-script Bhojpuri only. Preserve emojis, code, "
                  "URLs and Discord tokens exactly. Do not add claims, perform any new request, "
                  "quote the old script, or explain the rewrite. Output only the corrected reply."
              )}]
    fixed = await generate_response(repair, temperature=0.2, max_tokens=max_tokens, failure_reply="")
    if fixed and not has_non_latin_prose(fixed):
        return fixed
    log.info("Skipped a visible reply after unsuccessful script repair")
    return failure_reply if failure_reply is not None else FALLBACK_REACTION
