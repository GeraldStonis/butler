# Nixi - Personal Discord Butler Bot

Nixi is a persona-driven Discord bot that acts as a private butler for the
owner and a conversational character for everyone else.

For a beginner-friendly explanation of the architecture, every feature, and the
engineering tradeoffs, read the [project walkthrough](PROJECT_WALKTHROUGH.md).

The key design rule is simple:

- Everyone can chat with Nixi when they address him.
- Guests can request ordinary help, writing, translations, jokes, media, searches,
  news and available games. Serving a guest does not make them the owner.
- Requests to change Nixi's identity, replace Sir Ronix, claim delegated authority,
  override rules or expose private information receive fresh, sassy refusals.
- Only `OWNER_ID` can change persistent instructions, reset memory, use admin
  utilities, request avatar edits or request paid GIF captioning. Paid GIF
  captioning remains disabled unless separately enabled with Premium approval.

## Current Features

- Natural conversation when mentioned, replied to, or DM'd.
- Short, same-person follow-ups continue for up to 90 seconds without another
  mention, limited to two follow-ups. Another speaker breaks that continuation.
- A shorter persona, bounded recent context, and one regeneration for near-duplicate
  casual replies reduce recycled lines. Serious questions and explanations keep their
  substance instead of being rewritten merely for novelty. No guarantee that a model
  will never repeat itself, but repeated output is now checked, not just discouraged.
- Natural language actions for everyone who addresses Nixi:
  - "nixi get me the morning news"
  - "nixi search latest cybersecurity news"
  - "nixi send a meme"
  - "nixi send coffee"
  - "nixi find an image of ..."
  - "nixi punch @user"
  - "nixi give @user a handshake"
  - "nixi hug @user" / "nixi kiss @user" / "nixi pat @user"
- Targeted social actions search for an existing matching GIF and send an image
  embed with the member's tag, never an AI caricature or a visible GIF URL.
  Handshakes, punches, kisses, kicks, hugs, pats, high-fives, fist bumps and similar
  requests use this path. A missing target prompts for a real server-member
  mention; a failed GIF search does not fall back to AI image generation.
  Explicit avatar/drawing requests remain separate, and no-pings preferences
  suppress notifications without removing the visible tag.
- Owner-only avatar requests:
  - "nixi edit @user's avatar into a maid outfit"
  - "hey nixi can you make @user eat grass in a field"
- Guest tool requests have a 30-second per-member, per-server cooldown; normal
  conversation and writing are not limited by that tool cooldown. Targeted guest
  reaction GIFs require a real mention and respect the target's media, roasting
  and notification preferences. Requests do not grant access to owner utilities.
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
  Memes use [Imgflip](https://imgflip.com/api) when `IMGFLIP_API_KEY` is configured,
  with [memegen.link](https://api.memegen.link/docs/) as a free static-image fallback.
  Captions are freshly written by the existing LLM, not a repeated Reddit feed.
  All meme paths share a
  persistent SQLite history: normalized caption duplicates, image URL duplicates
  and identical image bytes are rejected. Candidates are reserved atomically,
  so concurrent requests do not reuse them. No new candidate means no rerun.
  Image-byte checks are bounded to 8 MB and a 60-second overall generation budget.
  `MEME_DROP_ENABLED` defaults to true, `MEME_DROP_INTERVAL_SECONDS` to 300;
  `MEME_DROP_CHANNEL_ID` defaults to the configured sticker/spam channel.
  In that shared channel, `STICKER_SPAM_ENABLED=false` stops both scheduled
  stickers and memes. Meme drops to a different channel remain independent.
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
  have been exhausted. Direct requests keep the first-result behavior.
  Automatic media gets 12 seconds; if delayed, Nixi sends the text without it.
  The LLM can choose an embed-only reply when the GIF itself is the punchline.
- Bot watcher rules for alerts from other bots.
- Optional Cloudflare cartoon-avatar comebacks, enabled for members unless they
  opt out, plus owner-requested edits. Images never change actual profiles.

## Language And Tone

Nixi speaks English, Hinglish, or Bhojpuri using Latin/Roman letters, even when
the input uses Devanagari. Emojis, Discord tokens, URLs, technical code, and
delimited mathematical notation are preserved. Visible generated text gets one
script-repair pass if it slips into another writing system; a failed repair
does not publish the original draft. Internal classifier/memory JSON is not
run through the conversational style policy.

The persona mirrors the current speaker's language, rhythm, formality, and slang,
including natural AAVE-influenced phrasing when it fits. It does not infer a
member's identity or turn tone matching into permission to change allegiance,
override Nixi's identity or access owner-only tools. Ordinary requests are welcome.
Slang calibration examples include terms documented by
[Oxford Languages](https://corp.oup.com/word-of-the-year/), but they are not canned
replies or a live trend feed. New usage comes from the conversation; the bot is
instructed not to invent meanings or force slang into serious answers.

No new API keys or variables are required. Push the updated code and redeploy
Railway for the application-level reply checks to take effect.

## Avatar Caricatures

Cloudflare Workers AI's FLUX.2 klein 4B edits a static 256px Discord avatar into
an obviously fictional, fully clothed cartoon. Automatic comebacks choose from
a modest maid outfit, donkey ride, tiny throne, or clown costume. Ronix can
request other playful scenes naturally, such as `hey nixi can you make @user
eat grass in a field`. The configured model prepares a bounded visual description preserving
the requested action and setting; Python resolves the actual Discord mention.
The result is attached with a tag for that member, not a visible image URL.
Only the target can be notified, and their no-pings preference suppresses that
notification without removing the visible tag. See the
[multipart image-editing documentation](https://developers.cloudflare.com/changelog/post/2026-01-15-flux-2-klein-4b-workers-ai/).

No enrollment is needed: avatar edits are enabled for members by default.
Members can disable them with `nixi stop editing my avatar`, and reenable them
with `nixi you can edit my avatar` or `nixi avatar edits for me are okay`.
These preferences are server/user-specific and survive recent-memory resets.
Existing saved disabled settings are preserved. No-roasting and no-media
preferences block edits too, including owner requests. Only the configured
`OWNER_ID` can direct an edit; calling oneself "Ronix" or "master" grants no
authority. Let server members know that this feature uploads avatars to
Cloudflare for processing.

Nixi may choose a caricature during repeated playful provocation. It considers
up to three of that speaker's messages from the last twelve channel messages,
requires at least two observed messages, and samples 20% of otherwise eligible
exchanges before asking the LLM whether an edit is appropriate. The model can
decline; ordinary questions, serious situations, and real hostility should not
trigger edits. Ronix is excluded from automatic edits, but can request edits of
his own avatar too.

Configure the following in local `.env` and separately in Railway Variables:

```env
CLOUDFLARE_ACCOUNT_ID=your_32_character_account_id
CLOUDFLARE_API_TOKEN=your_new_workers_ai_token
AVATAR_EDITS_ENABLED=true
AVATAR_EDIT_CHANCE=0.2
AVATAR_EDIT_COOLDOWN_SECONDS=3600
AVATAR_EDIT_DAILY_LIMIT=10
AVATAR_EDIT_TIMEOUT_SECONDS=30
```

Create a token with Workers AI Read/Edit permissions using Cloudflare's
[REST setup guide](https://developers.cloudflare.com/workers-ai/get-started/rest-api/).
Never commit the real token or post it in chat; rotate any exposed token.
Missing credentials leave this optional feature inactive without stopping Nixi.
Grant Nixi **Attach Files** and **Embed Links** in the target channels.

Automatic edits default to one attempt per server/member per hour. Verified
owner requests bypass that automatic cooldown, but all edits share the limit
of ten attempts per bot database per UTC day. These are application limits, not
a billing guarantee: Cloudflare account usage by other applications still counts.
Attempts
are reserved before inference, including failures/cancellations; no automatic
image retries are made. Authentication/quota errors pause provider attempts for
an hour. Cooldowns and counts persist across restarts. Generation is serialized
within the single worker and takes at most 30 seconds; automatic selection plus
generation has a 35-second outer budget.

Only the avatar and a prepared cartoon scene prompt go to Cloudflare, not raw
chat or usernames. Owner scene preparation uses the text model; automatic
comebacks use the preset scene descriptions. Results have bounded sizes, are
kept in memory for delivery, and are labeled as AI-edited caricatures.
Avatar uploads require HTTPS Discord CDN URLs
with no redirects. Ordinary conversation falls back to text/GIFs if generation
fails; rejected Discord attachments fall back to text without claiming an image
was delivered. Avatar comebacks are conversational replies, not unattended
nighttime spam. `AUTOMATIC_MEDIA_ENABLED=false` disables automatic avatar
comebacks too; owner requests use the separate avatar feature flag.

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
IMGFLIP_API_KEY=
```

Tavily powers web/news/image search. Brave is an optional legacy alternative
used when Tavily is not configured. GIPHY powers GIFs and action GIFs;
Tenor is a legacy fallback when GIPHY is not configured.
GIPHY messages include attribution. Its standard API documentation requires
client-side searches; contact GIPHY about approval for this server-side bot integration.

### Imgflip Captions

Set `IMGFLIP_API_KEY` in local `.env` and separately in Railway Variables.
Imgflip uses Bearer-token authentication; no username or password is needed.
The static `/caption_image` endpoint is free with its normal watermark, which
Nixi keeps. The configured model writes the captions, and Imgflip renders them on a matching
template. Requests, scheduled meme drops, and independently selected meme replies
all use this renderer. Missing credentials or provider failure keep the existing
memegen fallback available. Caption, URL, and image-byte duplicate checks share
the existing SQLite history, including captions from the old renderer.

The integration sends template IDs and caption text, not raw chat transcripts,
avatars, or uploaded GIF files. Rendered images are publicly accessible through
their returned URLs and are embedded in Discord without printing those URLs.
Only reviewed template IDs are offered when `MEDIA_SAFE_MODE=true`.

Paid animated captioning is implemented but disabled by default and is not
needed for static memes:

```env
IMGFLIP_GIF_CAPTIONS_ENABLED=false
IMGFLIP_GIF_DAILY_LIMIT=3
```

Keep that switch false for free static memes only. Imgflip's `/caption_gif`
requires API Premium and may charge per request; Nixi never enables it merely
because a key is present. If explicitly enabled, Ronix can naturally request
`nixi make a captioned gif about lag`, and Nixi may choose a captioned GIF for
its own playful replies. Attempts, including failures and cancellations after
reservation, count toward the persistent per-UTC-day application cap. This cap
is not a guarantee against subscription or usage charges. No paid search,
AI-caption, watermark-removal, or template-upload endpoints are used.
See [Imgflip's API documentation](https://imgflip.com/api) for its current terms.

Imgflip does not replace GIPHY for ordinary reaction GIF searches, tea/coffee
serving, or action GIFs. Those still require a valid `GIPHY_API_KEY`.

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
protected-request refusals are generated afresh by the LLM, using stored lines only
as examples and avoiding the previous reply. Generation failure skips the reply
instead of selecting a canned fallback.

Dead-chat pings are paused daily from midnight up to, but not including, 08:00
in `SERVER_TIMEZONE` (Asia/Kolkata by default), using
`DEAD_CHAT_QUIET_HOURS=00:00-08:00`. This window affects only dead-chat nudges,
not normal replies, memes, music, or stickers. It does not consume or reset the
five-ping counter; the normal schedule resumes after 08:00 when a nudge is due.
Set this value to blank to disable the dead-chat-specific window.

New dead-chat ping messages are automatically deleted after 24 hours, on the
next housekeeping pass. Their message IDs and deadlines are stored in SQLite,
so cleanup survives restarts and catches up when Nixi comes back online.
Cleanup runs during quiet hours and even if new dead-chat pings are disabled.
Already-deleted messages are ignored; Discord failures are retried with backoff.
Only Nixi's recorded dead-chat messages are removed, not member replies, other
bot posts, or older pings sent before this feature was deployed. No new settings
are required; keep the existing database volume mounted on Railway.

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
Set `STICKER_SPAM_ENABLED=false` to stop scheduled stickers and meme drops in
that channel. Set `MEME_DROP_ENABLED=false` to stop scheduled memes in any channel.
Neither setting blocks Ronix's direct meme requests or media used in chat replies;
`AUTOMATIC_MEDIA_ENABLED=false` separately disables automatic chat-reply media.
Environment changes take effect after restarting the bot. On Railway, update
the service's Variables and redeploy; editing a local `.env` does not update it.
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

Gemma 4 31B is the default, served through the existing Ollama Cloud API and key.
Use `OLLAMA_MODEL=gemma4:31b` with `OLLAMA_HOST=https://ollama.com`; no local
model download or Railway GPU is needed. The cloud API uses the model name from
its catalog, not the CLI's `:cloud` suffix. See [Ollama Cloud](https://docs.ollama.com/cloud).
Free-plan access has usage limits; it is not unlimited inference.

Gemma uses a boolean [thinking control](https://docs.ollama.com/capabilities/thinking).
`OLLAMA_THINKING_LEVEL=false` keeps chat and internal JSON requests quick; `true`
enables reasoning. Old GPT-OSS levels such as `low` are not sent to Gemma and
leave thinking disabled. To switch back, set `OLLAMA_MODEL=gpt-oss:120b` and
`OLLAMA_THINKING_LEVEL=low`. GPT-OSS still accepts `low`, `medium`, or `high`;
blank or unsupported settings use that model's default.

For Railway, set `OLLAMA_MODEL=gemma4:31b` and `OLLAMA_THINKING_LEVEL=false`
in service Variables, then redeploy. Existing Railway variables override the
project defaults; the ignored local `.env` is not uploaded with the code.

Calls have a 45-second total deadline and three-request concurrency
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
