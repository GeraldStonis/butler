"""Web, news, and image search helpers for owner-only actions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote_plus

import httpx

import config
from services import llm

log = logging.getLogger("nixi.search")

BRAVE_BASE = "https://api.search.brave.com/res/v1"


@dataclass
class SearchItem:
    title: str
    url: str
    description: str = ""
    image_url: str = ""


async def _brave_get(path: str, params: dict[str, str | int]) -> dict | None:
    if not config.BRAVE_SEARCH_API_KEY:
        return None
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": config.BRAVE_SEARCH_API_KEY,
    }
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.get(f"{BRAVE_BASE}{path}", params=params, headers=headers)
        response.raise_for_status()
        return response.json()


async def web_search(query: str, count: int = 5) -> list[SearchItem]:
    """Run a Brave web search and return normalized results."""
    data = await _brave_get("/web/search", {"q": query, "count": count})
    if not data:
        return []
    results = data.get("web", {}).get("results", [])
    return [
        SearchItem(
            title=item.get("title", "Untitled"),
            url=item.get("url", ""),
            description=item.get("description", ""),
        )
        for item in results
        if item.get("url")
    ]


async def news_search(query: str, count: int = 8) -> list[SearchItem]:
    """Run a Brave news search and return normalized results."""
    data = await _brave_get("/news/search", {"q": query, "count": count, "freshness": "pd"})
    if not data:
        return []
    results = data.get("results", [])
    return [
        SearchItem(
            title=item.get("title", "Untitled"),
            url=item.get("url", ""),
            description=item.get("description", ""),
        )
        for item in results
        if item.get("url")
    ]


async def image_search(query: str, count: int = 6) -> list[SearchItem]:
    """Run a Brave image search and return normalized results."""
    data = await _brave_get("/images/search", {"q": query, "count": count, "safesearch": "strict"})
    if not data:
        return []
    results = data.get("results", [])
    items: list[SearchItem] = []
    for item in results:
        image = item.get("properties", {}).get("url") or item.get("thumbnail", {}).get("src")
        page = item.get("url") or image
        if image:
            items.append(
                SearchItem(
                    title=item.get("title", query),
                    url=page,
                    description=item.get("source", ""),
                    image_url=image,
                )
            )
    return items


async def summarize_results(query: str, results: list[SearchItem], mode: str = "search") -> str:
    """Create a short Nixi-style summary with source links."""
    if not config.BRAVE_SEARCH_API_KEY:
        return (
            "I can search the web once BRAVE_SEARCH_API_KEY is configured. "
            "At present, my opera glasses are decorative."
        )
    if not results:
        return f"I searched for '{query}', but found nothing worth presenting."

    source_block = "\n".join(
        f"{idx + 1}. {item.title}\nURL: {item.url}\nSummary: {item.description}"
        for idx, item in enumerate(results[:6])
    )
    prompt = [
        {"role": "system", "content": config.load_persona()},
        {
            "role": "system",
            "content": (
                "Summarize these search results for Discord. Be concise, factual, "
                "and useful. Include 3-6 bullets and keep the source URLs. Use "
                "only light butler wit; clarity comes first."
            ),
        },
        {
            "role": "user",
            "content": f"Mode: {mode}\nQuery: {query}\n\nResults:\n{source_block}",
        },
    ]
    return await llm.generate_response(prompt, temperature=0.35, max_tokens=500)


def unsplash_source_url(query: str) -> str:
    """Return a no-key image endpoint useful as a fallback Discord embed."""
    return f"https://source.unsplash.com/featured/1200x800/?{quote_plus(query)}"
