"""
Nixi Bot Configuration.

All tunables are loaded from environment variables with sensible defaults.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _list_env(name: str, default: str = "") -> list[str]:
    return [
        item.strip()
        for item in os.environ.get(name, default).split(",")
        if item.strip()
    ]


def _int_list_env(name: str, default: str = "") -> list[int]:
    values: list[int] = []
    for item in _list_env(name, default):
        try:
            values.append(int(item))
        except ValueError:
            continue
    return values


# Discord
DISCORD_TOKEN: str = os.environ["DISCORD_TOKEN"]
OWNER_ID: int = int(os.environ.get("OWNER_ID", "0"))

OWNER_ALIASES: list[str] = _list_env(
    "OWNER_ALIASES",
    "ronix,roni,ronyx,ronnie,icosa,icosahedron,icosahedronix,icosahedronixon",
)
# Backward-compatible name used by older code.
OWNER_NAMES: list[str] = OWNER_ALIASES
OWNER_AWAY_AFTER_SECONDS: int = int(os.environ.get("OWNER_AWAY_AFTER_SECONDS", "300"))
ENABLE_NATURAL_OWNER_ACTIONS: bool = _bool_env("ENABLE_NATURAL_OWNER_ACTIONS", True)

# LLM (Ollama Cloud)
OLLAMA_HOST: str = os.environ.get("OLLAMA_HOST", "https://ollama.com")
OLLAMA_API_KEY: str = os.environ.get("OLLAMA_API_KEY", "")
OLLAMA_MODEL: str = os.environ.get("OLLAMA_MODEL", "gpt-oss:120b")

# Database
DB_PATH: str = os.environ.get("DB_PATH", "nixi.db")

# Memory tuning
CONTEXT_WINDOW_SIZE: int = int(os.environ.get("CONTEXT_WINDOW_SIZE", "100"))
SUMMARY_THRESHOLD: int = int(os.environ.get("SUMMARY_THRESHOLD", "50"))

# Owner-only web/media tools
TAVILY_API_KEY: str = os.environ.get("TAVILY_API_KEY", "")
BRAVE_SEARCH_API_KEY: str = os.environ.get("BRAVE_SEARCH_API_KEY", "")
TENOR_API_KEY: str = os.environ.get("TENOR_API_KEY", "")
GIPHY_API_KEY: str = os.environ.get("GIPHY_API_KEY", "")
MEDIA_SAFE_MODE: bool = _bool_env("MEDIA_SAFE_MODE", True)
AUTOMATIC_MEDIA_ENABLED: bool = _bool_env("AUTOMATIC_MEDIA_ENABLED", True)
AUTOMATIC_MEDIA_COOLDOWN_SECONDS: int = max(0, int(os.environ.get("AUTOMATIC_MEDIA_COOLDOWN_SECONDS", "45")))
AUTOMATIC_MEDIA_CHANCE: float = min(1.0, max(0.0, float(os.environ.get("AUTOMATIC_MEDIA_CHANCE", "0.95"))))

# Morning news
MORNING_NEWS_ENABLED: bool = _bool_env("MORNING_NEWS_ENABLED", False)
MORNING_NEWS_CHANNEL_ID: int = int(os.environ.get("MORNING_NEWS_CHANNEL_ID", "0") or "0")
MORNING_NEWS_TIME: str = os.environ.get("MORNING_NEWS_TIME", "08:00")
MORNING_NEWS_TIMEZONE: str = os.environ.get("MORNING_NEWS_TIMEZONE", "Asia/Kolkata")
MORNING_NEWS_TOPICS: list[str] = _list_env(
    "MORNING_NEWS_TOPICS", "world,india,technology,cybersecurity"
)

# Dead chat nudges
DEAD_CHAT_ENABLED: bool = _bool_env("DEAD_CHAT_ENABLED", False)
DEAD_CHAT_IDLE_SECONDS: int = int(os.environ.get("DEAD_CHAT_IDLE_SECONDS", "300"))
DEAD_CHAT_COOLDOWN_SECONDS: int = int(os.environ.get("DEAD_CHAT_COOLDOWN_SECONDS", "3600"))
DEAD_CHAT_CHANNEL_IDS: list[int] = _int_list_env("DEAD_CHAT_CHANNEL_IDS")

# Sticker spam
STICKER_SPAM_ENABLED: bool = _bool_env("STICKER_SPAM_ENABLED", False)
STICKER_SPAM_CHANNEL_ID: int = int(os.environ.get("STICKER_SPAM_CHANNEL_ID", "0") or "0")
STICKER_SPAM_INTERVAL_SECONDS: int = int(os.environ.get("STICKER_SPAM_INTERVAL_SECONDS", "60"))

# Independent meme drops, using the spam channel unless another is specified.
MEME_DROP_ENABLED: bool = _bool_env("MEME_DROP_ENABLED", True)
MEME_DROP_CHANNEL_ID: int = int(os.environ.get("MEME_DROP_CHANNEL_ID") or STICKER_SPAM_CHANNEL_ID)
MEME_DROP_INTERVAL_SECONDS: int = max(60, int(os.environ.get("MEME_DROP_INTERVAL_SECONDS", "300")))

# Persona
PERSONA_PATH: Path = Path(__file__).parent / "persona.md"


def load_persona() -> str:
    """Read the full persona system prompt from persona.md."""
    return PERSONA_PATH.read_text(encoding="utf-8")


# Bot-watching rules
BOT_RULES_PATH: Path = Path(__file__).parent / "bot_rules.json"
