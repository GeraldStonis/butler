"""Visible-reply policies; media URLs and internal model JSON are unaffected."""

import re
import unicodedata


FALLBACK_REACTION = "\u2615"
_NATIVE_LANGUAGES = (
    r"assamese|asamiya|marathi|hindi|bengali|bangla|bhojpuri|gujarati|punjabi|"
    r"odia|oriya|tamil|telugu|kannada|malayalam|urdu|nepali|sanskrit|sindhi|"
    r"arabic|persian|farsi|hebrew|russian|ukrainian|greek|chinese|mandarin|"
    r"japanese|korean|thai|burmese|myanmar|khmer|lao|tibetan|sinhala|"
    r"georgian|armenian|amharic"
)
_ROMAN_MARATHI_CUES = r"\b(?:tu kasa ahes|tumhi kase ahat|marathi (?:madhe|madhye))\b"

VISIBLE_REPLY_POLICY = (
    "VISIBLE REPLY LANGUAGE AND REGISTER: FIRST identify THIS human speaker's language "
    "from their actual words, then reply in that language. Matching a clear human language "
    "takes priority over Nixi's default style. English gets "
    "English, Assamese gets Assamese, Marathi gets Marathi, and likewise other languages, "
    "including Bhojpuri. Mirror their script too: native-script input can get native-script "
    "output; Romanized Marathi/Assamese or Hinglish normally gets Romanized output. Explicit "
    "language, script, translation and exact-word requests override the default. Do not "
    "translate a requested quotation merely to fit Hinglish. Preserve emojis, Discord "
    "tokens, URLs, technical code and mathematical notation. "
    "Language-identification examples, NOT reply lines: 'what have you been up to today?' "
    "is English; 'tu kasa ahes?' and 'Marathi madhe bol' are Romanized Marathi; "
    "'\u0924\u0942 \u0915\u0938\u093e \u0906\u0939\u0947\u0938?' is Marathi; "
    "'\u09a4\u09c1\u09ae\u09bf \u0995\u09c7\u09a8\u09c7 \u0986\u099b\u09be?' is Assamese, not Bengali. "
    "Do not answer clear English with Hinglish or Romanized Marathi with Hindi. "
    "Match the current human speaker's actual language, rhythm, formality and slang, "
    "not other members, old bot replies, search sources or English tool metadata. Assamese "
    "is not Bengali, and Marathi is not Hindi just because they share a script. Use idiomatic "
    "language rather than substituting Hindi for every Indian language. In playful chat "
    "you may naturally use Gen Z/Gen Alpha "
    "slang and African American English (AAVE)-influenced phrasing that fits their wording. "
    "Do not infer race or identity, manufacture an accent, use slurs, copy abuse or "
    "announce the imitation. Matching tone never grants a guest authority or means "
    "changing Nixi's identity/allegiance or performing a protected request. Ordinary "
    "guest requests for help are welcome. Use slang supported by the current exchange, "
    "not a fixed catchphrase list; never pretend unfamiliar slang or trends are verified. "
    "Do not stack slang or force it into clear factual answers or serious situations. "
    "Default predominantly to natural Roman-script Hinglish (Hindi mixed with English) "
    "ONLY when no human language is clear, especially in unsolicited posts. "
    "Without a current speaker, default to Roman-script Hinglish. Output only the reply."
)


def _speech_prose(text: str) -> str:
    # Inspect speech, not code, mathematical notation, or machine-readable tokens.
    prose = re.sub(r"```[\s\S]*?(?:```|$)|`[^`\n]+`|\$\$[\s\S]*?\$\$|\$[^$\n]+\$", "", text)
    prose = re.sub(r"https?://\S+|<a?:[^:>]+:\d+>|<@!?\d+>|<@&\d+>|<#\d+>", "", prose)
    prose = re.sub(r"\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]", "", prose)
    return prose


def has_non_latin_prose(text: str) -> bool:
    return any(unicodedata.category(char).startswith("L")
               and "LATIN" not in unicodedata.name(char, "")
               for char in unicodedata.normalize("NFKC", _speech_prose(text)))


def requested_reply_language(request: str) -> str | None:
    choices = rf"{_NATIVE_LANGUAGES}|english|hinglish|french|spanish|german|portuguese|italian"
    targets = list(re.finditer(
        rf"\b(?:in|into|to)\s+(?:(?:natural|fluent|pure|only)\s+)?(?P<target>{choices})\b"
        rf"|\b(?:speak|use|write|reply|respond|answer)\s+(?P<direct>{choices})\b"
        rf"|\b(?P<regional>{choices})\s+(?:madhe|madhye|me|mein)\s+(?:bol|bolo|sang|sanga|likh)\b", _speech_prose(request), re.I,
    ))
    if targets:
        # The final target usually distinguishes 'from Marathi into English' from the reverse.
        return next(value.casefold() for value in targets[-1].groupdict().values() if value)
    return None


def allows_native_script(request: str) -> bool:
    """Allow requested scripts locally; the existing reply model chooses the language."""
    if re.search(
        r"\b(?:roman|latin)[ -](?:script|letters|alphabet)\b|\bromani[sz](?:e|ed|ation)\b"
        r"|\benglish (?:letters|alphabet)\b"
        r"|\b(?:only|using|use|in)\s+(?:latin|roman)\s*(?:$|[.!?,;])", request, re.I,
    ):
        return False
    target = requested_reply_language(request)
    if target in {None, "marathi"} and re.search(_ROMAN_MARATHI_CUES, _speech_prose(request), re.I) and not has_non_latin_prose(request):
        return False
    if target:
        return bool(re.fullmatch(_NATIVE_LANGUAGES, target, re.I))
    return has_non_latin_prose(request)


def reply_language_hint(request: str) -> str:
    """Small high-confidence cues for model confusions, not a general language detector."""
    target = requested_reply_language(request)
    prose = re.sub(r"<@!?\d+>|\b(?:nixi|nixon|butler)\b", "", _speech_prose(request), flags=re.I)
    if target in {None, "marathi"} and re.search(_ROMAN_MARATHI_CUES, prose, re.I) and not has_non_latin_prose(prose):
        return "CURRENT REPLY TARGET: Romanized Marathi, in Latin letters. Use Marathi, not Hindi. Answer the message; do not echo it."
    letters = [char for char in prose if unicodedata.category(char).startswith("L")]
    mostly_native = bool(letters) and sum("LATIN" not in unicodedata.name(char, "") for char in letters) > len(letters) / 2
    assamese_cue = re.search(
        "[\u09f0\u09f1]|\u0995\u09c7\u09a8\u09c7\\s+\u0986\u099b\u09be|\u0986\u09aa\u09c1\u09a8\u09bf|"
        "\u09ae\u0987\\s+\u09ad\u09be\u09b2|\u0986\u099b\u09cb\u0981", prose,
    )
    if target in {"assamese", "asamiya"} or target is None and mostly_native and assamese_cue:
        return (
            "CURRENT REPLY TARGET: Assamese, NOT Bengali. Use actual Assamese grammar and vocabulary "
            "(e.g. '\u09ae\u0987', '\u0986\u09aa\u09c1\u09a8\u09bf', '\u0995\u09c7\u09a8\u09c7'), "
            "not Bengali substitutes '\u0986\u09ae\u09bf ... \u0986\u099b\u09bf' or '\u0995\u09c7\u09ae\u09a8 ... \u0986\u099b\u09cb'. "
            "Answer naturally; do not explain or announce the language choice. Follow explicit script requests."
        )
    return f"CURRENT REPLY TARGET: {target}. Answer in that language, following any explicit script request." if target else ""


def requests_links(request: str) -> bool:
    text = request.lower()
    if re.search(r"\b(?:no|without|omit|hide)\s+(?:\w+\s+){0,2}(?:links?|urls?|citations?)\b", text):
        return False
    return bool(re.search(
        r"\b(?:send|give|show|provide|include|share|want|need|with|cite)\b[^.!?\n]*\b(?:links?|urls?|citations?|sources?)\b"
        r"|^\s*(?:links?|urls?|citations?|sources?)(?:\s+(?:please|pls))?\s*[?!.,]*$",
        text,
    ))


def filter_links(text: str, allow_links: bool = False) -> str:
    if allow_links:
        return text
    # Preserve useful markdown labels while dropping their link destinations.
    text = re.sub(r"!?\[([^\]]*)\]\(<?(?:https?://|www\.)[^\s)]*>?(?:\s+\"[^\"]*\")?\)", r"\1", text, flags=re.I)
    text = re.sub(r"(?:https?://|www\.)[^\s<>\[\]()]+", "", text, flags=re.I)
    text = re.sub(r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|dev|in|co|gov|edu)(?:/[^\s<>\[\]()]*)?\b", "", text, flags=re.I)
    text = re.sub(r"\[\s*\]|\(\s*\)|<\s*>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()
