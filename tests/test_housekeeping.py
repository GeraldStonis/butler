import unittest
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from cogs.housekeeping import HousekeepingCog


class HousekeepingTests(unittest.IsolatedAsyncioTestCase):
    def news_context(self, *, now=None, stored=None):
        stack = self.enterContext(ExitStack())
        for key, value in {
            'MORNING_NEWS_ENABLED': True, 'MORNING_NEWS_CHANNEL_ID': 123,
            'MORNING_NEWS_TIME': '08:00', 'MORNING_NEWS_TIMEZONE': 'Asia/Kolkata',
        }.items():
            stack.enter_context(patch(f'cogs.housekeeping.config.{key}', value))
        self.clock = stack.enter_context(patch('cogs.housekeeping.datetime'))
        self.clock.now.return_value = now or datetime(2026, 10, 14, 8, 5)
        self.wall_clock = stack.enter_context(patch('cogs.housekeeping.time.time', return_value=1000.0))
        self.state = dict(stored or {})
        self.get_state = stack.enter_context(patch('cogs.housekeeping.db.get_state', AsyncMock(side_effect=self.state.get)))

        async def store(key, value):
            self.state[key] = value

        self.save_state = stack.enter_context(patch('cogs.housekeeping.db.set_state', AsyncMock(side_effect=store)))
        self.briefing = discord.Embed(title='Morning news', description='Today’s headlines')
        self.build_news = stack.enter_context(patch('cogs.housekeeping.actions.build_news_briefing', AsyncMock(return_value=self.briefing)))
        self.remember = stack.enter_context(patch('cogs.housekeeping.memory.log_delivery', AsyncMock()))
        self.permissions = SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id = 123
        self.channel.guild = SimpleNamespace(me=SimpleNamespace(id=999))
        self.channel.permissions_for.return_value = self.permissions
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=456))
        self.cog = object.__new__(HousekeepingCog)
        self.cog.bot = SimpleNamespace(get_channel=MagicMock(return_value=self.channel),
                                       fetch_channel=AsyncMock(return_value=self.channel))
        return self.cog

    async def test_news_catches_up_and_deduplicates(self):
        cog = self.news_context(now=datetime(2026, 10, 14, 11, 30))
        await cog._maybe_send_morning_news()
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.await_args.args, ())
        self.assertIs(self.channel.send.await_args.kwargs['embed'], self.briefing)
        mentions = self.channel.send.await_args.kwargs['allowed_mentions']
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)
        self.assertFalse(mentions.users)
        self.assertEqual(self.state['morning_news_sent:123'], '2026-10-14')
        self.remember.assert_awaited_once_with(self.channel.send.return_value)
        await cog._maybe_send_morning_news()
        self.channel.send.assert_awaited_once()

    async def test_news_respects_local_schedule_and_uses_configured_timezone(self):
        cog = self.news_context(now=datetime(2026, 10, 14, 7, 59))
        await cog._maybe_send_morning_news()
        self.build_news.assert_not_awaited()
        self.channel.send.assert_not_awaited()
        self.assertEqual(str(self.clock.now.call_args.args[0]), 'Asia/Kolkata')
        self.clock.now.return_value = datetime(2026, 10, 14, 8, 0)
        await cog._maybe_send_morning_news()
        self.channel.send.assert_awaited_once()

    async def test_news_daily_deduplication_survives_restart_and_resets_next_day(self):
        cog = self.news_context(stored={'morning_news_sent:123': '2026-10-14'})
        await cog._maybe_send_morning_news()
        self.build_news.assert_not_awaited()
        self.clock.now.return_value = datetime(2026, 10, 15, 8, 5)
        await cog._maybe_send_morning_news()
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.state['morning_news_sent:123'], '2026-10-15')

    async def test_news_fetches_channel_when_gateway_cache_misses(self):
        cog = self.news_context()
        cog.bot.get_channel.return_value = None
        await cog._maybe_send_morning_news()
        cog.bot.fetch_channel.assert_awaited_once_with(123)
        self.channel.send.assert_awaited_once()

    async def test_missing_news_channel_is_logged_and_retried_later(self):
        cog = self.news_context()
        cog.bot.get_channel.return_value = None
        cog.bot.fetch_channel.return_value = None
        with self.assertLogs('nixi.housekeeping', level='WARNING'):
            await cog._maybe_send_morning_news()
        self.build_news.assert_not_awaited()
        self.assertNotIn('morning_news_sent:123', self.state)
        self.assertGreaterEqual(float(self.state['morning_news_retry:123']), 1300)

    async def test_news_needs_view_send_and_embed_permissions(self):
        cog = self.news_context()
        for permission in ('view_channel', 'send_messages', 'embed_links'):
            with self.subTest(permission=permission):
                self.state.clear()
                setattr(self.permissions, permission, False)
                with self.assertLogs('nixi.housekeeping', level='WARNING'):
                    await cog._maybe_send_morning_news()
                self.assertNotIn('morning_news_sent:123', self.state)
                self.assertGreaterEqual(float(self.state['morning_news_retry:123']), 1300)
                setattr(self.permissions, permission, True)
        self.build_news.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_failed_news_generation_backs_off_across_restart_then_recovers(self):
        cog = self.news_context()
        self.build_news.side_effect = RuntimeError('news provider offline')
        with self.assertLogs('nixi.housekeeping', level='WARNING'):
            await cog._maybe_send_morning_news()
        self.assertNotIn('morning_news_sent:123', self.state)
        self.assertGreaterEqual(float(self.state['morning_news_retry:123']), 1300)
        fresh = object.__new__(HousekeepingCog)
        fresh.bot = cog.bot
        self.wall_clock.return_value = 1299
        await fresh._maybe_send_morning_news()
        self.build_news.assert_awaited_once()
        self.channel.send.assert_not_awaited()
        self.wall_clock.return_value = 1301
        self.build_news.side_effect = None
        await fresh._maybe_send_morning_news()
        self.assertEqual(self.build_news.await_count, 2)
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.state['morning_news_sent:123'], '2026-10-14')

    async def test_news_send_permission_failure_is_not_marked_delivered(self):
        cog = self.news_context()
        self.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), 'Missing permissions')
        with self.assertLogs('nixi.housekeeping', level='WARNING'):
            await cog._maybe_send_morning_news()
        self.assertNotIn('morning_news_sent:123', self.state)
        self.assertGreaterEqual(float(self.state['morning_news_retry:123']), 1300)
        self.remember.assert_not_awaited()

    async def test_memory_failure_after_delivery_cannot_cause_duplicate_news(self):
        cog = self.news_context()

        async def fail_memory(*args, **kwargs):
            self.assertEqual(self.state.get('morning_news_sent:123'), '2026-10-14')
            raise RuntimeError('memory offline')

        self.remember.side_effect = fail_memory
        with self.assertLogs('nixi.housekeeping', level='WARNING'):
            await cog._maybe_send_morning_news()
        await cog._maybe_send_morning_news()
        self.channel.send.assert_awaited_once()

    async def test_sent_marker_write_failure_retries_persistence_without_sending_again(self):
        cog = self.news_context()
        store = self.save_state.side_effect
        failed = False

        async def fail_once(key, value):
            nonlocal failed
            if key == 'morning_news_sent:123' and not failed:
                failed = True
                raise RuntimeError('database temporarily unavailable')
            await store(key, value)

        self.save_state.side_effect = fail_once
        with self.assertLogs('nixi.housekeeping', level='WARNING'):
            await cog._maybe_send_morning_news()
        self.assertNotIn('morning_news_sent:123', self.state)
        self.wall_clock.return_value = 1600
        await cog._maybe_send_morning_news()
        self.channel.send.assert_awaited_once()
        self.build_news.assert_awaited_once()
        self.assertEqual(self.state['morning_news_sent:123'], '2026-10-14')

    async def test_news_failure_does_not_stop_dead_chat(self):
        cog = object.__new__(HousekeepingCog)
        cog._delete_expired_dead_chat_messages = AsyncMock()
        cog._maybe_send_morning_news = AsyncMock(side_effect=RuntimeError('offline'))
        cog._maybe_nudge_dead_chat = AsyncMock()
        cog._maybe_send_random_meme = AsyncMock()
        cog._maybe_send_music = AsyncMock()
        with self.assertLogs('nixi.housekeeping', level='ERROR'):
            await HousekeepingCog.housekeeping_loop.coro(cog)
        cog._maybe_nudge_dead_chat.assert_awaited_once()

    async def test_empty_news_is_not_sent_as_a_public_status_message(self):
        cog = self.news_context()
        self.build_news.return_value = None
        with self.assertLogs('nixi.housekeeping', level='WARNING'):
            await cog._maybe_send_morning_news()
        self.channel.send.assert_not_awaited()
        self.assertNotIn('morning_news_sent:123', self.state)
        self.assertGreaterEqual(float(self.state['morning_news_retry:123']), 1300)

    async def test_cleanup_failure_does_not_stop_scheduled_posts(self):
        cog = object.__new__(HousekeepingCog)
        cog._delete_expired_dead_chat_messages = AsyncMock(side_effect=RuntimeError('database offline'))
        cog._maybe_send_morning_news = AsyncMock()
        cog._maybe_nudge_dead_chat = AsyncMock()
        cog._maybe_send_random_meme = AsyncMock()
        cog._maybe_send_music = AsyncMock()
        with self.assertLogs('nixi.housekeeping', level='ERROR'):
            await HousekeepingCog.housekeeping_loop.coro(cog)
        cog._maybe_send_morning_news.assert_awaited_once()
        cog._maybe_nudge_dead_chat.assert_awaited_once()
        cog._maybe_send_random_meme.assert_awaited_once()
        cog._maybe_send_music.assert_awaited_once()
