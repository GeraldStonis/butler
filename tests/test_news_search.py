"""News provider recovery and bounded public publisher feed coverage."""

import asyncio
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch

import httpx

from services import search


_HTTP_CLIENT = httpx.AsyncClient
_NOW = datetime(2026, 10, 30, 8, 0, tzinfo=timezone.utc)


def feed(*items):
    root = ET.Element("rss")
    channel = ET.SubElement(root, "channel")
    for fields in items:
        node = ET.SubElement(channel, "item")
        values = {"title": "Fresh headline", "link": "https://www.bbc.com/news/article",
                  "description": "A short summary.", "pubDate": format_datetime(_NOW - timedelta(hours=1))}
        values.update(fields)
        for key, value in values.items():
            if value is not None:
                ET.SubElement(node, key).text = value
    return ET.tostring(root)


def mock_http(handler):
    return patch("services.search.httpx.AsyncClient", side_effect=lambda **kwargs:
                 _HTTP_CLIENT(transport=httpx.MockTransport(handler), **kwargs))


class NewsSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch("services.search.config.TAVILY_API_KEY", ""))
        self.enterContext(patch("services.search.config.BRAVE_SEARCH_API_KEY", ""))
        self.clock = self.enterContext(patch("services.search.datetime"))
        self.clock.now.return_value = _NOW
        search._rss_cache.clear()
        search._rss_fetches.clear()

    async def asyncTearDown(self):
        pending = list(search._rss_fetches.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        search._rss_cache.clear()
        search._rss_fetches.clear()

    async def test_tavily_success_caps_count_and_skips_fallbacks(self):
        data = {"results": [{"title": str(i), "url": f"https://example.com/{i}"} for i in range(30)]}
        with patch("services.search.config.TAVILY_API_KEY", "test-key"), \
                patch("services.search._tavily_search", AsyncMock(return_value=data)) as tavily, \
                patch("services.search._brave_get", AsyncMock()) as brave, \
                patch("services.search._rss_news_search", AsyncMock()) as rss:
            items = await search.news_search("a specific query", count=100)
        self.assertEqual(len(items), 20)
        tavily.assert_awaited_once_with("a specific query", 20, news=True)
        brave.assert_not_awaited()
        rss.assert_not_awaited()

    async def test_tavily_quota_failure_uses_brave_and_logs_only_status(self):
        request = httpx.Request("POST", "https://example.invalid/?api_key=do-not-log")
        error = httpx.HTTPStatusError("secret-provider-message", request=request,
                                      response=httpx.Response(432, request=request))
        with patch("services.search.config.TAVILY_API_KEY", "test-key"), \
                patch("services.search.config.BRAVE_SEARCH_API_KEY", "test-key"), \
                patch("services.search._tavily_search", AsyncMock(side_effect=error)), \
                patch("services.search._brave_get", AsyncMock(return_value={"results": [
                    {"title": "Brave headline", "url": "https://example.com/news"}]})) as brave, \
                patch("services.search._rss_news_search", AsyncMock()) as rss, \
                self.assertLogs("nixi.search", level="WARNING") as captured:
            items = await search.news_search("specific query", count=2)
        self.assertEqual(items[0].title, "Brave headline")
        brave.assert_awaited_once_with("/news/search", {"q": "specific query", "count": 2, "freshness": "pd"})
        rss.assert_not_awaited()
        logs = " ".join(captured.output)
        self.assertIn("provider=tavily", logs)
        self.assertIn("HTTP 432", logs)
        self.assertNotIn("do-not-log", logs)
        self.assertNotIn("secret-provider-message", logs)
        self.assertNotIn("https://", logs)

    async def test_empty_tavily_then_failed_brave_uses_rss(self):
        with patch("services.search.config.TAVILY_API_KEY", "test-key"), \
                patch("services.search.config.BRAVE_SEARCH_API_KEY", "test-key"), \
                patch("services.search._tavily_search", AsyncMock(return_value={"results": []})), \
                patch("services.search._brave_get", AsyncMock(side_effect=httpx.ConnectError("private-url"))), \
                mock_http(lambda request: httpx.Response(200, content=feed({}))), \
                self.assertLogs("nixi.search", level="WARNING"):
            items = await search.news_search("world")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].description.startswith("BBC News:"))

    async def test_no_search_credentials_uses_publisher_feed(self):
        requests = []
        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(200, content=feed({}))
        with mock_http(handler), patch("services.search._tavily_search", AsyncMock()) as tavily, \
                patch("services.search._brave_get", AsyncMock()) as brave:
            items = await search.news_search("  InDiA  ", count=1)
        self.assertEqual(len(items), 1)
        self.assertEqual(requests, ["https://feeds.bbci.co.uk/news/world/asia/india/rss.xml"])
        tavily.assert_not_awaited()
        brave.assert_not_awaited()

    async def test_specific_failed_queries_never_receive_general_news(self):
        with mock_http(lambda request: self.fail("Unrelated topic must not fetch a feed")):
            for query in ("cats", "world cup", "technology layoffs", "world, specific event", ""):
                with self.subTest(query=query):
                    self.assertEqual(await search.news_search(query), [])

    async def test_feed_rejects_stale_future_undated_invalid_urls_and_duplicates(self):
        entries = [
            {"link": "https://example.com/fresh", "pubDate": format_datetime(
                (_NOW - timedelta(hours=1)).astimezone(timezone(timedelta(hours=5, minutes=30))))},
            {"link": "https://example.com/stale", "pubDate": format_datetime(_NOW - timedelta(hours=49))},
            {"link": "https://example.com/future", "pubDate": format_datetime(_NOW + timedelta(hours=1))},
            {"link": "https://example.com/undated", "pubDate": None},
            {"link": "https://example.com/naive", "pubDate": "Fri, 30 Oct 2026 07:00:00"},
            {"link": "https://example.com/bad-date", "pubDate": "yesterday"},
            {"link": "javascript:alert(1)"}, {"link": "https://user:password@example.com/news"},
            {"link": "https://example.com/white space"}, {"link": "https://[invalid"},
            {"link": "https://example.com/fresh"}, {"title": "", "link": "https://example.com/no-title"},
        ]
        with mock_http(lambda request: httpx.Response(200, content=feed(*entries))):
            items = await search.news_search("world")
        self.assertEqual([item.url for item in items], ["https://example.com/fresh"])

    async def test_feed_strips_html_and_preserves_plain_text(self):
        xml = feed({"title": "A <b>bold</b> &amp; plain headline",
                    "description": "<p>One &amp; two.</p><script>ignore me</script><style>hidden</style><p>Three.</p>"})
        with mock_http(lambda request: httpx.Response(200, content=xml)):
            items = await search.news_search("cybersecurity")
        self.assertEqual(items[0].title, "A bold & plain headline")
        self.assertEqual(items[0].description, "The Hacker News: One & two. Three.")

    async def test_shared_technology_feed_is_cached_for_ten_minutes(self):
        requests = []
        async def handler(request):
            requests.append(request.url)
            await asyncio.sleep(0)
            return httpx.Response(200, content=feed({}))
        with mock_http(handler), patch("services.search.time") as clock:
            clock.monotonic.return_value = 100
            technology, internet = await asyncio.gather(search.news_search("technology"), search.news_search("internet"))
            self.assertEqual(len(technology), 1)
            self.assertEqual(len(internet), 1)
            self.assertEqual(len(requests), 1)
            clock.monotonic.return_value = 699
            await search.news_search("technology")
            self.assertEqual(len(requests), 1)
            clock.monotonic.return_value = 701
            await search.news_search("internet")
            self.assertEqual(len(requests), 2)

    async def test_cancelling_one_caller_does_not_cancel_shared_feed(self):
        started = asyncio.Event()
        release = asyncio.Event()
        requests = []
        async def handler(request):
            requests.append(request.url)
            started.set()
            await release.wait()
            return httpx.Response(200, content=feed({}))
        with mock_http(handler):
            first = asyncio.create_task(search.news_search("technology"))
            second = asyncio.create_task(search.news_search("internet"))
            await started.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            items = await second
        self.assertEqual(len(items), 1)
        self.assertEqual(len(requests), 1)

    async def test_combined_briefing_uses_each_feed_and_deduplicates_aliases(self):
        requests = []
        def handler(request):
            requests.append(str(request.url))
            section = "india" if "/india/" in request.url.path else "technology" if "/technology/" in request.url.path else "world"
            return httpx.Response(200, content=feed(
                {"title": section, "link": f"https://example.com/{section}/first"},
                {"title": section, "link": f"https://example.com/{section}/second"}))
        with mock_http(handler):
            items = await search.news_search("world, india, technology, internet", count=3)
        self.assertEqual([item.title for item in items], ["world", "india", "technology"])
        self.assertEqual(len(requests), 3)

    async def test_oversized_feed_returns_no_results(self):
        with mock_http(lambda request: httpx.Response(200, content=b"x" * (search._RSS_MAX_BYTES + 1))), \
                self.assertLogs("nixi.search", level="WARNING") as captured:
            items = await search.news_search("world")
        self.assertEqual(items, [])
        self.assertIn("error=ValueError", " ".join(captured.output))

    async def test_bad_feed_is_cached_briefly_and_does_not_raise(self):
        requests = []
        def handler(request):
            requests.append(request.url)
            return httpx.Response(200, content=b"<not valid xml")
        with mock_http(handler), self.assertLogs("nixi.search", level="WARNING"):
            self.assertEqual(await search.news_search("technology"), [])
            self.assertEqual(await search.news_search("internet"), [])
        self.assertEqual(len(requests), 1)

    async def test_document_entities_are_rejected(self):
        xml = b'<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel></channel></rss>'
        with mock_http(lambda request: httpx.Response(200, content=xml)), \
                self.assertLogs("nixi.search", level="WARNING"):
            self.assertEqual(await search.news_search("world"), [])

    async def test_provider_timeout_still_leaves_time_for_rss(self):
        async def slow_provider(*args, **kwargs):
            await asyncio.sleep(1)
        with patch("services.search.config.TAVILY_API_KEY", "test-key"), \
                patch("services.search._NEWS_PROVIDER_TIMEOUT_SECONDS", 0.01), \
                patch("services.search._tavily_search", side_effect=slow_provider), \
                mock_http(lambda request: httpx.Response(200, content=feed({}))), \
                self.assertLogs("nixi.search", level="WARNING") as captured:
            items = await search.news_search("world")
        self.assertEqual(len(items), 1)
        self.assertIn("TimeoutError", " ".join(captured.output))

    async def test_overall_provider_chain_has_a_hard_deadline(self):
        async def slow_provider(*args, **kwargs):
            await asyncio.sleep(1)
        started = time.perf_counter()
        with patch("services.search.config.TAVILY_API_KEY", "test-key"), \
                patch("services.search._NEWS_TOTAL_TIMEOUT_SECONDS", 0.01), \
                patch("services.search._tavily_search", side_effect=slow_provider), \
                self.assertLogs("nixi.search", level="WARNING"):
            self.assertEqual(await search.news_search("world"), [])
        self.assertLess(time.perf_counter() - started, 0.5)

    async def test_network_failure_is_logged_without_url_or_exception_message(self):
        def handler(request):
            raise httpx.ConnectError("secret=https://private.example/api?key=do-not-log")
        with mock_http(handler), self.assertLogs("nixi.search", level="WARNING") as captured:
            self.assertEqual(await search.news_search("world"), [])
        logs = " ".join(captured.output)
        self.assertIn("provider=rss", logs)
        self.assertIn("ConnectError", logs)
        self.assertNotIn("private.example", logs)
        self.assertNotIn("do-not-log", logs)

    async def test_zero_or_negative_count_does_not_call_a_provider(self):
        with mock_http(lambda request: self.fail("No results requested")):
            self.assertEqual(await search.news_search("world", count=0), [])
            self.assertEqual(await search.news_search("world", count=-10), [])

    async def test_cached_headlines_are_rechecked_for_freshness(self):
        xml = feed({"pubDate": format_datetime(_NOW - timedelta(hours=47, minutes=59))})
        requests = []
        def handler(request):
            requests.append(request.url)
            return httpx.Response(200, content=xml)
        with mock_http(handler):
            self.assertEqual(len(await search.news_search("world")), 1)
            self.clock.now.return_value = _NOW + timedelta(minutes=2)
            self.assertEqual(await search.news_search("world"), [])
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
