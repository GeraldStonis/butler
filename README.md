# Nixi - Personal Discord Butler Bot

Nixi is a persona-driven Discord bot that acts as a private butler for the
owner and a conversational character for everyone else.

The key design rule is simple:

- Everyone can chat with Nixi when they address him.
- Guests can ask questions; explicit performance, media, roleplay, and errand
  commands receive "Shush." instead of being carried out.
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
- Optional random server sticker spam in a configured channel.
- When a human sends `:stfu:` (typed or a static/animated custom emoji), Nixi
  sends the same server's available `kidhar chali` sticker in that channel.
  Spaces, underscores, hyphens, and capitalization in the sticker name are ignored.
- Random meme drops every 4-6 minutes by default, independently of chat replies.
  `MEME_DROP_ENABLED` defaults to true, `MEME_DROP_INTERVAL_SECONDS` to 300;
  `MEME_DROP_CHANNEL_ID` defaults to the configured sticker/spam channel.
  The first drop waits one interval, and the next scheduled time survives restarts.
- Rolling channel memory, summaries, long-term memories, and owner instructions.
- Server emoji support without forcing an emoji into every reply.
- Automatic contextual GIF/meme reactions accompany Nixi's own replies.
  Defaults: enabled, 30% sampling, 180 seconds between attempts per channel.
  The selector may choose no media; memes are checked for relevance by title.
- Bot watcher rules for alerts from other bots.

## Member Memory And Resets

Use the owner-only `/reset-memory` slash command to clear this channel's messages
and summary, keeping member profiles, standing instructions, and activity timers.
Remembering member details is automatic; there are no manual memory actions.

Member profiles are keyed by server ID and Discord user ID. Names update from
Discord, so renames do not lose facts and matching names do not merge people.
During conversations with Nixi and clear self-reports in server chat (for example,
"I prefer coffee"), preferences and interests are learned in the background without
posting a reply just to acknowledge learning. The extractor reads only the member's message,
requires a supporting quote, and replaces older values for the same topic.
It does not scan old server history or import ambiguous legacy conversation facts.
The existing owner-only `/restart` also resets the current channel's recent memory.

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
TAVILY_API_KEY=
BRAVE_SEARCH_API_KEY=
TENOR_API_KEY=
GIPHY_API_KEY=
```

Tavily powers web/news/image search. Brave is an optional legacy alternative
used when Tavily is not configured. GIPHY powers GIFs and action GIFs;
Tenor is a legacy fallback when GIPHY is not configured.
GIPHY messages include attribution. Its standard API documentation requires
client-side searches; contact GIPHY about approval for this server-side bot integration.

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
DEAD_CHAT_IDLE_SECONDS=60
DEAD_CHAT_CHANNEL_IDS=

STICKER_SPAM_ENABLED=false
STICKER_SPAM_CHANNEL_ID=
STICKER_SPAM_INTERVAL_SECONDS=60
```

Dead-chat waits start at `DEAD_CHAT_IDLE_SECONDS`, then double after each
unanswered nudge (60, 120, 240 seconds, and so on). Each wait is measured from
the latest channel message. Any human message resets the sequence. Bot messages
do not reset it. State survives restarts; the old fixed cooldown setting is ignored.

`DEAD_CHAT_CHANNEL_IDS` may be left empty to consider channels Nixi has seen
activity in, or set to comma-separated channel IDs.

Set `STICKER_SPAM_CHANNEL_ID` to your spam channel ID to send one random server
sticker every `STICKER_SPAM_INTERVAL_SECONDS`.

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
services/search.py      Tavily/Brave web/news/image search
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
