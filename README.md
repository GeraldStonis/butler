# Nixi - Personal Discord Butler Bot

Nixi is a persona-driven Discord bot that acts as a private butler for the
owner and a conversational character for everyone else.

The key design rule is simple:

- Everyone can chat with Nixi when they address him.
- Guests can ask questions; explicit performance, media, roleplay, and errand
  commands receive varied short, sassy refusals instead of being carried out.
- Only `OWNER_ID` can make Nixi perform actions such as search, news, media,
  GIFs, memes, or targetted reaction GIFs.

## Current Features

- Natural conversation when mentioned, replied to, or DM'd.
- Short, same-person follow-ups continue for up to 90 seconds without another
  mention, limited to two follow-ups. Another speaker breaks that continuation.
- A shorter persona, bounded recent context, and one regeneration for near-duplicate
  casual replies reduce recycled lines. Serious questions and explanations keep their
  substance instead of being rewritten merely for novelty. No guarantee that a model
  will never repeat itself, but repeated output is now checked, not just discouraged.
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
  Memes now use [memegen.link](https://api.memegen.link/docs/), with fresh LLM-written
  captions instead of the repeated Reddit random feed. All meme paths share a
  persistent SQLite history: normalized caption duplicates, image URL duplicates
  and identical image bytes are rejected. Candidates are reserved atomically,
  so concurrent requests do not reuse them. No new candidate means no rerun.
  Image-byte checks are bounded to 8 MB and a 60-second overall generation budget.
  `MEME_DROP_ENABLED` defaults to true, `MEME_DROP_INTERVAL_SECONDS` to 300;
  `MEME_DROP_CHANNEL_ID` defaults to the configured sticker/spam channel.
  The first drop waits one interval, and the next scheduled time survives restarts.
- Rolling channel memory, summaries, long-term memories, and owner instructions.
- Server emoji support without forcing an emoji into every reply.
- Automatic contextual GIF/meme/captioned-cat reactions accompany Nixi's own replies.
  Defaults: enabled, 95% sampling, 45 seconds between attempts per channel.
  The selector may choose no media; memes are checked for relevance by title.
  The LLM infers its reply's sentiment and comic intent, then generates specific
  GIF search phrases rather than choosing fixed reaction queries. It avoids
  recent queries; automatic searches skip GIF IDs already selected in SQLite,
  including across restarts. Empty/repeated results trigger a fresh search phrase.
  Automatic searches also try up to three ranked result pages when earlier pages
  have been exhausted. Direct owner requests keep the first-result behavior.
  Automatic media gets 12 seconds; if delayed, Nixi sends the text without it.
  The LLM can choose an embed-only reply when the GIF itself is the punchline.
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

Members can set their own comfort boundaries naturally: "nixi don't roast me",
"nixi stop pinging me", or "nixi don't send me gifs". These are personal preferences,
not permission to command errands. They persist by server and Discord user ID.
"You can roast me again" or "gifs for me are okay again" re-enables that preference.
They survive recent-memory resets. Owner/private notes are not loaded into guest
conversations, and other bots' messages are never attributed to Nixi.

Human activity is recorded before waiting for an in-progress reply. Memory
summarization runs as a tracked background task; a failed summary does not delete
the evidence. Resets cancel pending summaries before clearing the channel.

## Setup

Requires Python 3.11 or newer.

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
DEAD_CHAT_IDLE_SECONDS=300
DEAD_CHAT_CHANNEL_IDS=

STICKER_SPAM_ENABLED=false
STICKER_SPAM_CHANNEL_ID=
STICKER_SPAM_INTERVAL_SECONDS=60
```

Dead-chat waits start at `DEAD_CHAT_IDLE_SECONDS`, then double after each
unanswered nudge (5, 10, 20 minutes by default, and so on). Each wait is measured from
the latest channel message. Any human message resets the sequence. Bot messages
do not reset it. State survives restarts; the old fixed cooldown setting is ignored.
At most five unanswered nudges are sent (5, 10, 20, 40, 80 minute waits at the
default setting), then nudging stops until a human speaks. Nudge wording and
guest-command refusals are generated afresh by the LLM, using stored lines only
as examples and avoiding the previous reply. Generation failure skips the reply
instead of selecting a canned fallback.

Dead-chat pings are paused daily from midnight up to, but not including, 08:00
in `SERVER_TIMEZONE` (Asia/Kolkata by default), using
`DEAD_CHAT_QUIET_HOURS=00:00-08:00`. This window affects only dead-chat nudges,
not normal replies, memes, music, or stickers. It does not consume or reset the
five-ping counter; the normal schedule resumes after 08:00 when a nudge is due.
Set this value to blank to disable the dead-chat-specific window.

Dead-chat revival uses the original roast-and-ping behavior: a freshly generated
roast plus one random non-bot member's mention, excluding Ronix and members who
opted out of pings or teasing. Targets vary when possible; a single eligible
member can still be selected. With no eligible member, no nudge is sent.
Games and conversation openers never replace these nudges. Old
`AUTOMATIC_GAME_CHANCE` and `ENGAGEMENT_TOPIC_CHANCE` variables are ignored.
Unsolicited memes, music, and nudges share
a two-minute channel cooldown, so they do not arrive in a burst. Provider failures
back off instead of repeatedly generating a nudge every 15 seconds.

Optional `QUIET_HOURS=23:00-08:00` uses `SERVER_TIMEZONE` (default Asia/Kolkata)
and suppresses routine unsolicited posts, including sticker spam. Owner requests
and normal conversations remain available. A blank value disables quiet hours.

`DEAD_CHAT_CHANNEL_IDS` may be left empty to consider channels Nixi has seen
activity in, or set to comma-separated channel IDs.

Set `STICKER_SPAM_CHANNEL_ID` to your spam channel ID to send one random server
sticker every `STICKER_SPAM_INTERVAL_SECONDS`.
Scheduled stickers and automatic meme/music drops in that channel pause from
midnight until 08:00 in `SERVER_TIMEZONE`, using `SPAM_QUIET_HOURS=00:00-08:00`
by default. Normal replies and Ronix's direct requests still work overnight;
scheduled drops in other channels are unchanged. Set `SPAM_QUIET_HOURS` to blank
to disable this additional window. Skipped overnight posts are not queued.

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

## Games And Rare Music

Ask naturally as Ronix: "nixi start trivia", "nixi play higher or lower",
"nixi show the leaderboard", or "nixi send a cat meme". No new slash commands.
Everyone can answer game buttons, once per round. Scores persist in SQLite.
Trivia is supplied by [Open Trivia DB](https://opentdb.com/api_config.php), with
decoded text, batch caching, rate limiting and a persistent no-repeat question ledger.
Higher-or-lower uses [Deck of Cards](https://deckofcardsapi.com/), five 60-second
rounds, ace high, and no points for ties. A game ends early if nobody participates.
Only one game runs per channel; inactive buttons close even if users keep clicking.
Games start only on Ronix's explicit natural-language request, never automatically
as a replacement for dead-chat roast pings.
Captioned reactions use [Cataas](https://cataas.com/). Meme and cat captions are
invented independently: no server conversation is sent to these image APIs.
The new public APIs need no extra keys; caption writing uses the existing LLM.

Music uses your `songs_data.json` playlist (`songs.json` is accepted as a fallback).
Video IDs are validated and duplicate playlist entries are collapsed. Once a channel
has had no human activity for a randomly chosen 10-15 minutes, Nixi gets exactly
one 10% chance to post a song. It does not reroll every timer tick. Human messages
and game participation reset the quiet spell; bot posts do not. The decision and
recent songs survive restarts. Active games suppress music drops.
The announcement is generated afresh, followed by a YouTube URL for Discord's
native playable preview. This is an explicit exception to the usual no-links policy;
playback/embedding still depends on Discord settings and the video's availability.
No voice-channel playback is involved.

```env
ACTIVITIES_ENABLED=true
MUSIC_DROP_ENABLED=true
MUSIC_CHANNEL_IDS=your-chat-channel-id
SONGS_PATH=songs_data.json
```

If `MUSIC_CHANNEL_IDS` is blank, it uses `DEAD_CHAT_CHANNEL_IDS`. If both are blank,
automatic music stays inactive. Ensure Nixi has Send Messages and Embed Links
permissions. Commit the new files, including the playlist, and redeploy Railway.
Keep a single bot worker; game buttons are intentionally not resumed after a restart.

## Deployment

Railway can run this as a worker using the included `Procfile` or
`railway.toml`. For persistent memory on Railway, mount a volume and set:

```env
DB_PATH=/data/nixi.db
```

New settings have defaults; do not replace your existing Railway variables or
check `.env` into Git. The new preference table is created automatically in the
existing database. Upgrade dependencies during redeployment (`ollama>=0.6.2`).

The included [Nixpacks install override](https://nixpacks.com/docs/configuration/file)
creates the build virtualenv and runs `scripts/install_dependencies.py` with that
virtualenv's Python. It upgrades pip, disables pip's download cache, uses public
PyPI over HTTPS, and gives each install stage up to three attempts with backoff.
Connection and incomplete-download retries are also enabled. Failed installs or
dependency conflicts still fail the build; the bot is never launched during install.

If a build fails with `JSONDecodeError: Unterminated string` inside pip's
`parse_links`, pip received malformed package-index JSON, not a bot configuration
or Python syntax error. Push `nixpacks.toml` and `scripts/install_dependencies.py`
with the rest of the project and rebuild. If Railway has a custom install/build
command or `NIXPACKS_INSTALL_CMD` overriding the project settings, remove that
override so the install phase uses the script. This hardening helps transient
index/cache failures; persistent PyPI or builder-network failures can still require
another build or provider investigation.

Docker's `SecretsUsedInArgOrEnv` and `UndefinedVar` entries in the supplied build
log are separate warnings, not the fatal pip error. Do not publish build images
containing secrets or commit `.env`; keep credentials in your Railway variables.

## Model Tuning And Checks

GPT-OSS 120B remains the default. `OLLAMA_THINKING_LEVEL=low` uses its named
[thinking control](https://docs.ollama.com/capabilities/thinking); blank uses the
model default. Calls have a 45-second total deadline and three-request concurrency
limit. Empty final answers get at most one larger-budget retry. Reasoning traces
and accidental speaker envelopes are not sent to Discord. Ollama Cloud currently
does not support [schema-enforced output](https://docs.ollama.com/capabilities/structured-outputs),
so JSON is parsed and checked locally rather than enabling an unsupported format.

```bash
python -m unittest discover -s tests -q
python persona_probe.py --live
```

The probe opts into real LLM requests using synthetic dialogue and a temporary
database. It does not read server history, modify your normal database, or post
anything to Discord. Review its answers after future persona/model changes.
