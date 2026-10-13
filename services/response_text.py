"""Visible-link policy for Discord text; media embed URLs are unaffected."""

import re


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
