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
DISCORD_TOKEN: str = os.environ.get("DISCORD_TOKEN", "").strip()
OWNER_ID: int = int(os.environ.get("OWNER_ID") or "0")

OWNER_ALIASES: list[str] = _list_env(
    "OWNER_ALIASES",
    "ronix,roni,ronyx,ronnie,icosa,icosahedron,icosahedronix,icosahedronixon",
)
# Backward-compatible name used by older code.
OWNER_NAMES: list[str] = OWNER_ALIASES
OWNER_AWAY_AFTER_SECONDS: int = int(os.environ.get("OWNER_AWAY_AFTER_SECONDS", "300"))
# Legacy name: enables public natural-language tools as well as owner tools.
ENABLE_NATURAL_OWNER_ACTIONS: bool = _bool_env("ENABLE_NATURAL_OWNER_ACTIONS", True)

# LLM (Ollama Cloud)
OLLAMA_HOST: str = os.environ.get("OLLAMA_HOST", "https://ollama.com")
OLLAMA_API_KEY: str = os.environ.get("OLLAMA_API_KEY", "")
OLLAMA_MODEL: str = os.environ.get("OLLAMA_MODEL", "gpt-oss:120b")
LLM_TIMEOUT_SECONDS: float = max(5, float(os.environ.get("LLM_TIMEOUT_SECONDS", "45")))
LLM_MAX_CONCURRENT: int = max(1, int(os.environ.get("LLM_MAX_CONCURRENT", "3")))
OLLAMA_THINKING_LEVEL: str = os.environ.get("OLLAMA_THINKING_LEVEL", "low").strip().lower()

# Database
DB_PATH: str = os.environ.get("DB_PATH", "nixi.db")

# Memory tuning
CONTEXT_WINDOW_SIZE: int = int(os.environ.get("CONTEXT_WINDOW_SIZE", "200"))
SUMMARY_THRESHOLD: int = int(os.environ.get("SUMMARY_THRESHOLD", "50"))
CONTEXT_MESSAGE_LIMIT: int = max(4, int(os.environ.get("CONTEXT_MESSAGE_LIMIT", "100")))
CONTEXT_CHAR_BUDGET: int = max(2000, int(os.environ.get("CONTEXT_CHAR_BUDGET", "48000")))
FOLLOWUP_WINDOW_SECONDS: int = max(0, int(os.environ.get("FOLLOWUP_WINDOW_SECONDS", "90")))
OWNER_MENTION_COOLDOWN_SECONDS: int = max(0, int(os.environ.get("OWNER_MENTION_COOLDOWN_SECONDS", "600")))

# Owner-only web/media tools
TAVILY_API_KEY: str = os.environ.get("TAVILY_API_KEY", "")
BRAVE_SEARCH_API_KEY: str = os.environ.get("BRAVE_SEARCH_API_KEY", "")
TENOR_API_KEY: str = os.environ.get("TENOR_API_KEY", "")
GIPHY_API_KEY: str = os.environ.get("GIPHY_API_KEY", "")
IMGFLIP_API_KEY: str = os.environ.get("IMGFLIP_API_KEY", "").strip()
# Animated captioning is a paid Imgflip endpoint and requires explicit opt-in.
IMGFLIP_GIF_CAPTIONS_ENABLED: bool = _bool_env("IMGFLIP_GIF_CAPTIONS_ENABLED", False)
IMGFLIP_GIF_DAILY_LIMIT: int = max(0, int(os.environ.get("IMGFLIP_GIF_DAILY_LIMIT", "3")))
MEDIA_SAFE_MODE: bool = _bool_env("MEDIA_SAFE_MODE", True)
AUTOMATIC_MEDIA_ENABLED: bool = _bool_env("AUTOMATIC_MEDIA_ENABLED", True)
AUTOMATIC_MEDIA_COOLDOWN_SECONDS: int = max(0, int(os.environ.get("AUTOMATIC_MEDIA_COOLDOWN_SECONDS", "45")))
AUTOMATIC_MEDIA_CHANCE: float = min(1.0, max(0.0, float(os.environ.get("AUTOMATIC_MEDIA_CHANCE", "0.95"))))

# Optional Cloudflare avatar edits; enabled for members unless they opt out.
CLOUDFLARE_ACCOUNT_ID: str = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
CLOUDFLARE_API_TOKEN: str = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
AVATAR_EDITS_ENABLED: bool = _bool_env("AVATAR_EDITS_ENABLED", True)
AVATAR_EDIT_CHANCE: float = min(1.0, max(0.0, float(os.environ.get("AVATAR_EDIT_CHANCE", "0.2"))))
AVATAR_EDIT_COOLDOWN_SECONDS: int = max(60, int(os.environ.get("AVATAR_EDIT_COOLDOWN_SECONDS", "3600")))
AVATAR_EDIT_DAILY_LIMIT: int = max(0, int(os.environ.get("AVATAR_EDIT_DAILY_LIMIT", "10")))
AVATAR_EDIT_TIMEOUT_SECONDS: float = max(5, min(60, float(os.environ.get("AVATAR_EDIT_TIMEOUT_SECONDS", "30"))))

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
DEAD_CHAT_QUIET_HOURS: str = os.environ.get("DEAD_CHAT_QUIET_HOURS", "00:00-08:00").strip()

# Sticker spam; disabling also stops scheduled memes in this channel.
STICKER_SPAM_ENABLED: bool = _bool_env("STICKER_SPAM_ENABLED", False)
STICKER_SPAM_CHANNEL_ID: int = int(os.environ.get("STICKER_SPAM_CHANNEL_ID", "0") or "0")
STICKER_SPAM_INTERVAL_SECONDS: int = int(os.environ.get("STICKER_SPAM_INTERVAL_SECONDS", "60"))
SPAM_QUIET_HOURS: str = os.environ.get("SPAM_QUIET_HOURS", "00:00-08:00").strip()

# Scheduled meme drops; a separate channel is independent of sticker spam.
MEME_DROP_ENABLED: bool = _bool_env("MEME_DROP_ENABLED", True)
MEME_DROP_CHANNEL_ID: int = int(os.environ.get("MEME_DROP_CHANNEL_ID") or STICKER_SPAM_CHANNEL_ID)
MEME_DROP_INTERVAL_SECONDS: int = max(60, int(os.environ.get("MEME_DROP_INTERVAL_SECONDS", "300")))

# Persona
ACTIVITIES_ENABLED: bool = _bool_env("ACTIVITIES_ENABLED", True)
AUTONOMOUS_MIN_GAP_SECONDS: int = max(0, int(os.environ.get("AUTONOMOUS_MIN_GAP_SECONDS", "120")))
QUIET_HOURS: str = os.environ.get("QUIET_HOURS", "").strip()
SERVER_TIMEZONE: str = os.environ.get("SERVER_TIMEZONE", "Asia/Kolkata")
MUSIC_DROP_ENABLED: bool = _bool_env("MUSIC_DROP_ENABLED", True)
MUSIC_CHANNEL_IDS: list[int] = _int_list_env("MUSIC_CHANNEL_IDS") or DEAD_CHAT_CHANNEL_IDS
SONGS_PATH: Path = Path(os.environ.get("SONGS_PATH", str(Path(__file__).parent / "songs_data.json")))

PERSONA_PATH: Path = Path(__file__).parent / "persona.md"


def load_persona() -> str:
    """Read the full persona system prompt from persona.md."""
    return PERSONA_PATH.read_text(encoding="utf-8")


# Bot-watching rules
BOT_RULES_PATH: Path = Path(__file__).parent / "bot_rules.json"


def validate_runtime() -> None:
    missing = []
    if not DISCORD_TOKEN or DISCORD_TOKEN == "your-bot-token-here":
        missing.append("DISCORD_TOKEN")
    if OWNER_ID <= 0:
        missing.append("OWNER_ID (numeric Discord user ID)")
    if OLLAMA_HOST.rstrip("/") == "https://ollama.com" and (not OLLAMA_API_KEY or OLLAMA_API_KEY == "your-ollama-api-key"):
        missing.append("OLLAMA_API_KEY")
    if missing:
        raise ValueError("Missing required environment settings: " + ", ".join(missing))
