"""Natural reply pacing and bounded protection against recycled lines."""

import re
from difflib import SequenceMatcher

from services import database as db, llm


def serious_message(text: str) -> bool:
    return bool(re.search(r"\b(?:suicid\w*|self[ -]harm|panic attack|grief|depressed|depression|abuse|overdose|chest pain|bad day|passed away|stop roasting|stop teasing)\b", text, re.I))


def needs_explanation(text: str) -> bool:
    return bool(re.search(r"\b(?:explain|how (?:do|does|can|to)|why (?:is|does|do)|step[ -]by[ -]step|debug|help me understand|in detail)\b", text, re.I))


def normalized_reply(text: str) -> str:
    text = re.sub(r"<a?:\w+:\d+>", ":emoji:", text)
    text = re.sub(r"<@!?\d+>|\[[^\]]*\]", "", text)
    return " ".join(re.findall(r"\w+", text.casefold()))


def repeats_recent(reply: str, previous: list[str]) -> bool:
    key = normalized_reply(reply)
    if not key:
        return False
    old = [normalized_reply(item) for item in previous]
    if len(key.split()) < 5:
        return old.count(key) >= 2
    return any(key == item or SequenceMatcher(None, key, item).ratio() >= 0.88 for item in old if item)


async def generate_reply(context: list[dict], channel_id: int, content: str, *, nixi_author_id: int | None = None) -> str:
    recent = await db.get_recent_messages(channel_id, limit=20)
    previous = [m['content'] for m in recent if m['is_bot'] and
                (m['author_id'] == nixi_author_id if nixi_author_id is not None else m['author'].casefold() == 'nixi')][-6:]
    serious, detail = serious_message(content), needs_explanation(content)
    brief = bool(re.search(r"\b(?:simply|briefly|quickly|simple|short|tl;?dr)\b", content, re.I))
    pacing = ("Give a grounded, considerate answer. No roast, slang or humorous media." if serious else
              "Give the simplest useful explanation in a few short sentences, without headings or lists." if detail and brief else
              "Give only the explanation needed to actually help; no lecture, extra examples or recap unless requested." if detail else
              "Let the size of the moment decide the size of your answer. Not every reply needs a roast. "
              "Avoid repeating the openings, sentence structure and metaphors in your recent replies.")
    prompt = list(context)
    prompt.insert(-1, {'role': 'system', 'content': pacing})
    reply = await llm.generate_visible_response(prompt, temperature=0.4 if serious or detail else 0.9,
                                        max_tokens=1000 if detail and not brief else 600, failure_reply="")
    if reply and not serious and not detail and repeats_recent(reply, previous):
        prompt.insert(-1, {'role': 'system', 'content': (
            "Your last draft recycled a recent reply. Take a different conversational approach and "
            "wording while answering the actual message. Do not quote or explain this instruction."
        )})
        fresh = await llm.generate_visible_response(prompt, temperature=1.0, max_tokens=600, failure_reply="")
        if fresh and not repeats_recent(fresh, previous):
            return fresh
        return ""
    return reply
