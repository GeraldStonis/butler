# Selected Evaluation Fixes

This follow-up implements only the requested areas from the evaluation summary:
2 (routing), 4 (comfort boundaries), 5 (tool-result memory), and 6 (reply quality,
requested sources and playlist moods). The original audit remains historical.

## Routing

- Clear negations, quoted orders and explanatory questions return chat before
  any keyword shortcut or semantic tool classifier can execute an errand.
- "How do I brew coffee?" gets an explanation, not a coffee GIF.
- A clear replacement such as "don't send a meme, serve coffee instead" routes
  only the replacement. Mentions from the discarded clause are excluded.
- Polite positive requests, action GIFs without targets, music requests and
  song titles such as "Don't Stop Believin'" remain supported.
- This is a conservative fast-path parser, not a complete natural-language grammar.
  Ambiguous compound requests may remain conversation rather than execute tools.

## Comfort Boundaries

- Direct English and Romanized Hinglish boundaries update the actual sender's
  settings. Examples: "stop tagging me", "mujhe ping mat karna", "mujhe roast
  mat kar", and "mere se flirt mat kar".
- Definitions, quotations and statements about other people do not change the
  speaker's settings. Re-enabling requires an explicit self-directed statement.
- A disabled flirting preference now supplies an exclusive nonromantic policy,
  not positive flirt-back instructions followed by a contradictory exception.
- A cheap output check catches a limited set of recognizable romantic/coy cues
  and requests one small rewrite. If that rewrite still violates the check,
  Nixi uses the existing neutral reaction rather than delivering that draft.
  This is not a complete semantic classifier or a guarantee against all innuendo.

## Confirmed Tool Memory

- Tea, coffee, GIF search, image search, cat images, web search and news now
  record successful Discord sends through the shared delivery logger.
- History contains the actual delivered text, media marker and action/query/title
  context. Media results and web-search records also retain returned source URLs.
- Recording happens after Discord accepts the message, never while delivery is
  merely planned. Failed sends are not recorded as successful tool results.
- A failed memory write after a successful send is logged privately and does
  not trigger a duplicate public reply. It still means that result was not saved;
  no durable retry queue is claimed.
- Tea/coffee GIFs can stand alone, without a repetitive canned service caption.

## Reply Quality

- Application code, not the model, renders requested source URLs from retrieved
  results. Model-supplied links and opaque citation markers are removed first.
- Sources are HTTP(S) destinations with basic structural validation. Links stay
  opt-in, duplicate destinations are suppressed, and output fits Discord's
  2,000-character limit. Long link sets are bounded rather than sending oversized
  messages. Structural validation does not prove that a source is trustworthy.
- Mood/language selection uses explicit playlist metadata. The starter sidecar
  annotates 23 existing tracks without rewriting the 928-track catalog or sending
  it to another provider. "No.1 Party Anthem" is not treated as party music just
  because of its title. Explicit titles and artist searches still work.
- Unclassified tracks remain available for random/title/artist requests. Unsupported
  tag combinations return no match instead of pretending to understand the audio.
- Prompts emphasize specific banter, care without entertainment pitches, shorter
  simple explanations and requested line counts. Detectable care/format defects
  get at most one compact rewrite, using the current speaker's language sample.
- No per-reply judging model was added. Ordinary valid drafts keep the existing
  reply path; a detected defect can add one rewrite and therefore some latency.
  GPT-OSS 120B, low reasoning, history budgets and automatic GIF frequency remain
  unchanged. Wording and multilingual fluency still need human review.

## Verification

- 427 automated tests passed, including 21 new evaluation regression tests.
- The offline audit's eight failing routes now pass, all five comfort examples
  pass, and all seven previously missing tool-delivery records are observed.
- Three final live samples each covered opted-out flirting, distress, two-line
  writing and simple English explanations. All three writing samples had two
  nonempty lines; all three distress samples avoided unsolicited entertainment.
  No explicit romantic ownership/crush/marriage claim appeared in those final
  opted-out samples, although one metaphor remained overly familiar and several
  replies still used stock butler references. Do not read these samples as proof
  of perfect personality or boundary compliance. Explanation length also varied.
- A separate live supplied-source check returned the real supplied URL, not
  a citation placeholder. Synthetic sources make this a rendering/grounding
  check, not a verification of real-world news accuracy.
- Python compilation and whitespace validation pass. No real Discord messages
  were sent. Live generation used synthetic members and the existing Ollama account.

Artifacts: [unit results](evaluation/2026-10-21/selected-fixes/unit.json),
[offline results](evaluation/2026-10-21/selected-fixes/offline.json), and
[final focused model samples](evaluation/2026-10-21/selected-fixes/verified/model.json).
The `live` and `live-final` subdirectories retain intermediate prompt experiments;
they are not the final acceptance samples.

## Deployment And Scope

Redeploy the changed source, persona and `song_tags.json` with the existing
Railway variables and volume. No environment changes or schema migration are needed.

Private-memory provenance/isolation (finding 1) and GIPHY authentication (finding 3)
were deliberately not changed. The offline private-fact probe still reports the
known privacy problem. Provider-dependent GIF delivery remains subject to the
previously identified credential issue; these changes do not repair it.
