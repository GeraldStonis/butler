import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from cogs.housekeeping import HousekeepingCog


class HousekeepingTests(unittest.IsolatedAsyncioTestCase):
    async def test_news_catches_up_and_deduplicates(self):
        cog = object.__new__(HousekeepingCog)
        channel = MagicMock(spec=discord.TextChannel)
        channel.send = AsyncMock()
        cog.bot = SimpleNamespace(get_channel=lambda _: channel)
        with patch('cogs.housekeeping.config.MORNING_NEWS_ENABLED', True), patch('cogs.housekeeping.config.MORNING_NEWS_CHANNEL_ID', 123), patch('cogs.housekeeping.config.MORNING_NEWS_TIME', '08:00'), patch('cogs.housekeeping.datetime') as clock, patch('cogs.housekeeping.db.get_state', AsyncMock(return_value=None)) as state, patch('cogs.housekeeping.db.set_state', AsyncMock()) as saved, patch('cogs.housekeeping.actions.build_news_briefing', AsyncMock(return_value='News')):
            clock.now.return_value = datetime(2026, 10, 14, 8, 5)
            await cog._maybe_send_morning_news()
            channel.send.assert_awaited_once()
            saved.assert_awaited_once_with('morning_news_sent:123', '2026-10-14')
            state.return_value = '2026-10-14'
            await cog._maybe_send_morning_news()
            channel.send.assert_awaited_once()

    async def test_news_failure_does_not_stop_dead_chat(self):
        cog = object.__new__(HousekeepingCog)
        cog._maybe_send_morning_news = AsyncMock(side_effect=RuntimeError('offline'))
        cog._maybe_nudge_dead_chat = AsyncMock()
        cog._maybe_send_random_meme = AsyncMock()
        cog._maybe_send_music = AsyncMock()
        with self.assertLogs('nixi.housekeeping', level='ERROR'):
            await HousekeepingCog.housekeeping_loop.coro(cog)
        cog._maybe_nudge_dead_chat.assert_awaited_once()
