# Nixi — Personal Discord Butler Bot

Nixi is a persona-driven Discord bot that behaves like a cultured British butler (with deep Indian roots) serving Icosahedronixon. He's not a typical command-only bot — he chats naturally, remembers conversations, watches for noteworthy events, and protects his employer's dignity at all times.

## Features

- **Natural conversation** — responds to @mentions, replies, DMs, and when someone talks about his boss
- **Butler persona** — full Wodehouse-inspired character with Hindi/Bhojpuri code-switching
- **Slash commands** — `/ask`, `/info-yourself`, `/info-boss`, `/instruct` (owner-only)
- **Memory system** — rolling window of recent messages + summarised older context + persistent long-term facts
- **Bot watching** — monitors other bots (Pokémon, economy) for noteworthy events and sends butler-style DM alerts
- **Emoji filtering** — only uses custom emojis uploaded by the bot owner
- **Railway-ready** — deploys as a background worker with no public port needed

---

## Quick Start (Local)

### 1. Prerequisites

- Python 3.11+
- A Discord bot token ([Developer Portal](https://discord.com/developers/applications))
- An [Ollama Cloud](https://ollama.com) API key (or a self-hosted Ollama instance)

### 2. Clone & Install

```bash
cd butler
pip install -r requirements.txt
```

### 3. Configure

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

**Required variables:**

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Your bot token from the Discord Developer Portal |
| `OLLAMA_API_KEY` | API key from [ollama.com](https://ollama.com) |
| `OWNER_ID` | Your Discord user ID (numeric). Right-click your profile → Copy User ID |

**Optional variables** (defaults shown):

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_HOST` | `https://ollama.com` | Ollama API host |
| `OLLAMA_MODEL` | `gpt-oss:120b` | Model name |
| `OWNER_NAMES` | `Icosahedronixon,Icosahedronix,Icosa,Pokemon` | Comma-separated boss aliases |
| `DB_PATH` | `nixi.db` | SQLite database path |
| `CONTEXT_WINDOW_SIZE` | `100` | Recent messages to include in LLM context |
| `SUMMARY_THRESHOLD` | `50` | Message count before triggering summarisation |

### 4. Discord Developer Portal Setup

Go to your [application settings](https://discord.com/developers/applications):

1. **Bot → Privileged Gateway Intents**: Enable:
   - ✅ Message Content Intent
   - ✅ Server Members Intent
2. **OAuth2 → URL Generator**: Select scopes `bot` + `applications.commands`, then permissions:
   - Send Messages
   - Read Message History
   - Use Slash Commands
   - View Audit Log
   - Read Messages/View Channels
   - Send Messages in Threads
3. Use the generated URL to invite the bot to your server.

### 5. Run

```bash
python bot.py
```

### 6. Sync Slash Commands

On first run (or after adding new commands), send `!sync` in any channel where the bot can see your message. This registers the slash commands with Discord. Only the bot owner can do this.

---

## Bot-Watching Rules

Edit `bot_rules.json` to configure which bots to monitor and what to alert about:

```json
{
    "watched_bots": [
        {
            "bot_id": 716390085896962058,
            "name": "Pokétwo",
            "rules": [
                {
                    "type": "embed_title_contains",
                    "pattern": "wild .+ appeared",
                    "rarity_keywords": ["legendary", "shiny", "mythical"],
                    "alert": "A rare creature, Young Master! {details}"
                }
            ]
        }
    ],
    "alert_via_dm": true,
    "alert_channel_id": null
}
```

**Rule types:**
- `content_contains` — matches against `message.content`
- `embed_title_contains` — matches against embed title + description
- `embed_field_contains` — matches against embed field names + values

**Options:**
- `rarity_keywords` — only alert if ANY of these keywords appear (prevents spam)
- `cooldown_seconds` — minimum seconds between alerts for this rule
- `alert` — message template (`{details}` is replaced with message content)

Set `alert_via_dm: true` to DM the owner, or set `alert_channel_id` to a channel ID for alerts.

---

## Project Structure

```
butler/
├── persona.md              # System prompt (butler personality & rules)
├── bot.py                  # Entry point
├── config.py               # Environment config loader
├── cogs/
│   ├── chat.py             # on_message triggers & LLM responses
│   ├── commands.py         # Slash commands (/ask, /info-*, /instruct)
│   └── watcher.py          # Bot-message monitoring & alerts
├── services/
│   ├── database.py         # SQLite schema & async CRUD
│   ├── llm.py              # Ollama Cloud SDK wrapper
│   ├── memory.py           # Two-tier memory (rolling + long-term)
│   └── emoji_filter.py     # Emoji ownership cache & response filter
├── bot_rules.json          # Bot-watching rules config
├── requirements.txt
├── Procfile                # Railway worker process
├── railway.toml            # Railway build config
├── .env.example            # Environment variable template
└── README.md
```

---

## Memory Architecture

Nixi uses a two-tier memory system:

### Tier 1: Rolling Window (Short-Term)
- Stores the last ~150 raw messages per channel in SQLite.
- When building LLM context, the most recent 100 messages are included.
- When the buffer exceeds 150 messages, the oldest 50 are summarised by the LLM and the raw rows are deleted.
- Summaries are merged with previous summaries to maintain continuity.

### Tier 2: Long-Term Memory
- After each conversation turn, the LLM is asked to extract noteworthy facts.
- Extracted facts (preferences, traits, decisions) are stored persistently.
- Relevant memories are keyword-searched and included in the LLM context.
- Owner instructions (`/instruct`) are stored separately and always included.

---

## Deploy to Railway

### 1. Push to GitHub

Make sure your repo contains all project files. **Do not commit `.env`** — add it to `.gitignore`.

```bash
echo ".env" >> .gitignore
echo "nixi.db" >> .gitignore
echo "__pycache__/" >> .gitignore
git init
git add .
git commit -m "Initial commit — Nixi butler bot"
git remote add origin https://github.com/your-username/nixi-butler.git
git push -u origin main
```

### 2. Create Railway Project

1. Go to [railway.app](https://railway.app/) → **New Project** → **Deploy from GitHub**
2. Select your repository.
3. Railway will auto-detect the `Procfile` and treat this as a **worker** service (no port needed).

### 3. Set Environment Variables

In your Railway service dashboard → **Variables** tab, add:

```
DISCORD_TOKEN=your-bot-token
OLLAMA_API_KEY=your-api-key
OWNER_ID=your-discord-user-id
OLLAMA_MODEL=gpt-oss:120b
OWNER_NAMES=Icosahedronixon,Icosahedronix,Icosa,Pokemon
```

### 4. Deploy

Railway will automatically build and deploy. Monitor logs in the **Deployments** tab.

### 5. Persistent Storage

Railway's filesystem is ephemeral by default. For the SQLite database to persist across deploys:

1. Add a **Volume** to your Railway service.
2. Set the mount path to `/data`.
3. Update the `DB_PATH` env var to `/data/nixi.db`.

---

## Trigger Behaviour

Nixi does **NOT** respond to every message. He responds only when:

| Trigger | Mode | Behaviour |
|---|---|---|
| Bot @-mentioned | `DIRECT_ADDRESS` | Full conversation, sass & charm |
| Reply to bot's message | `REPLY_CHAIN` | Continues existing conversation |
| DM to bot | `DIRECT_ADDRESS` | Full conversation in DMs |
| Boss name/alias in message | `BOSS_MENTIONED` | Steps in as butler, speaks on behalf |
| `/ask` command | `ASK_COMMAND` | Factual answer, still in character |
| Other messages | — | Logged for context only, no response |

---

## Slash Commands

| Command | Access | Description |
|---|---|---|
| `/ask <question>` | Everyone | Ask anything — factual, accuracy-first |
| `/info-yourself` | Everyone | Nixi introduces himself |
| `/info-boss` | Everyone | Deliberately vague info about the boss |
| `/instruct <text>` | Owner only | Store a standing instruction |
| `!sync` | Owner only | Sync slash commands with Discord |

---

## License

Private project. Not licensed for redistribution.
