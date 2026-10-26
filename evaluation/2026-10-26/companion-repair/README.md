# Companion Repair Verification

## Changes

- Automatic reactions: 40% sampling of eligible turns, 90-second selected-media
  cooldown, and only 20-second backoff for declined/failed attempts. Serious,
  factual, exact-output and text-only preference exclusions remain.
- Model-selected GIF search phrases now include an allowlisted fallback expression.
  The existing no-key action provider can match this expression without GIPHY or
  image-search availability. Unseen results are preferred; exhausted matching
  pools rotate older results rather than becoming permanently unavailable.
- Slow media enriches the existing reply without delaying or replacing its text.
  GIF selection takes priority over optional avatar edits. Humor guidance favors
  specific observations, grounded callbacks and deadpan timing, not fixed lines.
- Member facts are ID-bound in the final request, with all supported fact fields,
  explicit mention retrieval and ambiguity checks. Pending same-channel self-reports
  remain visible during learning, and per-member extraction is ordered. New reply
  recipient IDs survive summaries; old recipients are explicitly unknown.
- Uncached Discord reply references are fetched with a bounded timeout. Foreign
  references are excluded. The new SQLite column migrates automatically.
- Additional English/Hinglish song phrasing routes directly for guests and Ronix.
  Last successfully delivered songs are persisted for channel-scoped replay.
  Song delivery remains a YouTube chat preview, not voice audio.

## Verification

- `unit.json`: 485 tests passing, including 21 new regression tests.
- Compileall and `git diff --check` passed.
- All 2,928 unique catalog title/artist requests routed as songs. The 15 queries
  whose normalized wording changed were also checked against the real matcher;
  none lost their expected video ID. This is not a video/audio availability check.
- `free-gif/services.json`: the no-key expression fallback returned HTTP 200 and
  `image/gif` in a live check. Nothing was posted to Discord.

## External Blockers

Live model probes could not validate humor or factual recall: the configured
Ollama account returned HTTP 429 with a usage-limit response. `live_companion.json`
records the empty results from those blocked probes, not a successful live test.
The evaluator now preflights availability to avoid repeating a blocked run.

The configured GIPHY key returned HTTP 401. Tavily returned HTTP 432, meaning the
plan's usage limit was exceeded according to its
[official error guide](https://help.tavily.com/articles/8645538886-understanding-http-errors).
This prevents specific GIF searches such as tea-serving when no action category
exists. The new category fallback does not invent an exact serving GIF or bypass
account quotas. Recover the existing account allowances and correct GIPHY access
for full functionality. No credentials were changed or printed.

## Deployment

Redeploy the changed code. Existing Railway variables override new defaults:

```dotenv
AUTOMATIC_MEDIA_ENABLED=true
AUTOMATIC_MEDIA_CHANCE=0.4
AUTOMATIC_MEDIA_COOLDOWN_SECONDS=90
```

No new API key, package, volume, database path or manual SQL migration is required.
No production bot was restarted, and the tests did not open the production database.
