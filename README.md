# Nixi - Personal Discord Butler Bot

Nixi is a persona-driven Discord bot that acts as a private butler for the
owner and a conversational character for everyone else.

The key design rule is simple:

- Everyone can chat with Nixi when they address him.
- Only `OWNER_ID` can make Nixi perform actions such as search, news, media,
  GIFs, memes, or targetted reaction GIFs.

## Current Features

- Natural conversation when mentioned, replied to, or DM'd.
- Owner-only natural language actions:
  - "nixi get me the morning news"
  - "nixi search latest cybersecurity news"
  - "nixi send a meme"
  - "nixi send coffee"
  - "nixi find an image of ..."
  - "nixi punch @user"
- Owner availability handling:
  - If someone mentions the owner while the owner has been inactive for more
    than `OWNER_AWAY_AFTER_SECONDS`, Nixi replies that the owner is unavailable.
  - Broad aliases like "Pokemon" are no longer used.
- Optional morning news briefing.
- Optional dead-chat nudges after channel inactivity.
- Rolling channel memory, summaries, long-term memories, and owner instructions.
- Server emoji support without forcing an emoji into every reply.
- Bot watcher rules for alerts from other bots.

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in the required values:

```env
DISCORD_TOKEN=your-bot-token-here
OLLAMA_API_KEY=your-ollama-api-key
OWNER_ID=your-discord-user-id
```

Run locally:

```bash
python bot.py
```

Sync slash commands, if you still want the legacy owner utility commands:

```text
!sync
```

## Optional API Keys

Nixi starts without these, but the related features will politely decline until
configured.

```env
BRAVE_SEARCH_API_KEY=
TENOR_API_KEY=
```

Brave powers web/news/image search. Tenor powers GIFs and action GIFs.

## Important Config

```env
OWNER_ALIASES=ronix,roni,ronyx,ronnie,icosa,icosahedron,icosahedronix,icosahedronixon
OWNER_AWAY_AFTER_SECONDS=300
ENABLE_NATURAL_OWNER_ACTIONS=true

MORNING_NEWS_ENABLED=false
MORNING_NEWS_CHANNEL_ID=
MORNING_NEWS_TIME=08:00
MORNING_NEWS_TIMEZONE=Asia/Kolkata
MORNING_NEWS_TOPICS=world,india,technology,cybersecurity

DEAD_CHAT_ENABLED=false
DEAD_CHAT_IDLE_SECONDS=300
DEAD_CHAT_COOLDOWN_SECONDS=3600
DEAD_CHAT_CHANNEL_IDS=
```

`DEAD_CHAT_CHANNEL_IDS` may be left empty to consider channels Nixi has seen
activity in, or set to comma-separated channel IDs.

## Project Structure

```text
bot.py                  Entry point
config.py               Environment config
persona.md              Nixi's system prompt
bot_rules.json          Bot-watching rules
cogs/chat.py            Conversation and owner action router
cogs/commands.py        Legacy owner utility slash commands
cogs/housekeeping.py    Morning news and dead-chat tasks
cogs/watcher.py         Other-bot alert watcher
services/actions.py     Owner action execution
services/intent.py      Natural language intent classifier
services/search.py      Brave web/news/image search
services/media.py       Meme/GIF/image helpers
services/memory.py      Conversation memory
services/database.py    SQLite persistence
services/emoji_filter.py Emoji cleanup and mention resolution
```

## Deployment

Railway can run this as a worker using the included `Procfile` or
`railway.toml`. For persistent memory on Railway, mount a volume and set:

```env
DB_PATH=/data/nixi.db
```
