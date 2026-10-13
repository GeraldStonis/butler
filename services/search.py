"""Web, news, and image search helpers for owner-only actions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote_plus

import httpx

import config
from services import llm
from services.response_text import filter_links

log = logging.getLogger("nixi.search")

BRAVE_BASE = "https://api.search.brave.com/res/v1"


@dataclass
class SearchItem:
    title: str
    url: str
    description: str = ""
    image_url: str = ""


async def _tavily_search(
    query: str, count: int, *, news: bool = False, images: bool = False
) -> dict:
    payload = {
        "query": query,
        "max_results": max(1, min(count, 20)),
        "search_depth": "basic",
        "topic": "news" if news else "general",
        "include_images": images,
        "include_image_descriptions": images,
        "safe_search": config.MEDIA_SAFE_MODE,
    }
    if news:
        payload["time_range"] = "day"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {config.TAVILY_API_KEY}"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()


def _tavily_items(data: dict) -> list[SearchItem]:
    return [
        SearchItem(
            title=item.get("title") or "Untitled",
            url=item["url"],
            description=item.get("content") or "",
        )
        for item in data.get("results", [])
        if item.get("url")
    ]


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
    """Search with Tavily, or Brave when Tavily is not configured."""
    if config.TAVILY_API_KEY:
        return _tavily_items(await _tavily_search(query, count))
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
    """Search the past day's news."""
    if config.TAVILY_API_KEY:
        return _tavily_items(await _tavily_search(query, count, news=True))
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
    """Return query-related images from the configured search provider."""
    if config.TAVILY_API_KEY:
        data = await _tavily_search(query, count, images=True)
        items = []
        for item in data.get("images", []):
            url = item if isinstance(item, str) else item.get("url")
            description = "" if isinstance(item, str) else item.get("description") or ""
            if url:
                items.append(SearchItem(
                    title=description or query, url=url,
                    description=description, image_url=url,
                ))
        return items[:count]
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


async def summarize_results(query: str, results: list[SearchItem], mode: str = "search", *, include_links: bool = False) -> str:
    """Create a short summary; visible sources are opt-in."""
    if not (config.TAVILY_API_KEY or config.BRAVE_SEARCH_API_KEY):
        return (
            "I can search the web once TAVILY_API_KEY is configured. "
            "At present, my opera glasses are decorative."
        )
    if not results:
        return filter_links(f"I searched for '{query}', but found nothing worth presenting.", include_links)

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
                "and useful. Answer the question directly; use bullets only when helpful. "
                + ("Include source links. " if include_links else "Do not include URLs, hyperlinks, or citations. ")
                + "Use "
                "only light butler wit; clarity comes first."
            ),
        },
        {
            "role": "user",
            "content": f"Mode: {mode}\nQuery: {query}\n\nResults:\n{source_block}",
        },
    ]
    reply = await llm.generate_response(prompt, temperature=0.35, max_tokens=500)
    return filter_links(reply, include_links)


def unsplash_source_url(query: str) -> str:
    """Return a no-key image endpoint useful as a fallback Discord embed."""
    return f"https://source.unsplash.com/featured/1200x800/?{quote_plus(query)}"
