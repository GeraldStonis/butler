"""
Nixi Bot Configuration
All tunables loaded from environment variables with sensible defaults.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Discord ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN: str = os.environ["DISCORD_TOKEN"]
OWNER_ID: int = int(os.environ.get("OWNER_ID", "0"))

# Aliases the bot recognises as referring to the boss (case-insensitive match)
OWNER_NAMES: list[str] = [
    n.strip()
    for n in os.environ.get(
        "OWNER_NAMES", "Icosahedronixon,Icosahedronix,Icosa,Pokemon"
    ).split(",")
    if n.strip()
]

# ── LLM (Ollama Cloud) ──────────────────────────────────────────────────────
OLLAMA_HOST: str = os.environ.get("OLLAMA_HOST", "https://ollama.com")
OLLAMA_API_KEY: str = os.environ.get("OLLAMA_API_KEY", "")
OLLAMA_MODEL: str = os.environ.get("OLLAMA_MODEL", "gpt-oss:120b")

# ── Database ─────────────────────────────────────────────────────────────────
DB_PATH: str = os.environ.get("DB_PATH", "nixi.db")

# ── Memory tuning ───────────────────────────────────────────────────────────
CONTEXT_WINDOW_SIZE: int = int(os.environ.get("CONTEXT_WINDOW_SIZE", "100"))
SUMMARY_THRESHOLD: int = int(os.environ.get("SUMMARY_THRESHOLD", "50"))

# ── Persona ──────────────────────────────────────────────────────────────────
PERSONA_PATH: Path = Path(__file__).parent / "persona.md"

def load_persona() -> str:
    """Read the full persona system prompt from persona.md."""
    return PERSONA_PATH.read_text(encoding="utf-8")

# ── Bot-watching rules ───────────────────────────────────────────────────────
BOT_RULES_PATH: Path = Path(__file__).parent / "bot_rules.json"
