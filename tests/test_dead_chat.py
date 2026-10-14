import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from services import database as db
from services.engagement import dead_chat_quiet_now, quiet_now
from cogs.housekeeping import HousekeepingCog, dead_chat_delay_seconds, dead_chat_due, generate_dead_chat_line, DEAD_CHAT_EXAMPLES


class DeadChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.directory = tempfile.TemporaryDirectory()
        path = str(Path(self.directory.name) / 'test.db')
        # Exercise the migration from an existing deployment's schema.
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('CREATE TABLE channel_activity (channel_id INTEGER PRIMARY KEY, guild_id INTEGER, last_user_id INTEGER, last_message_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_dead_ping_at TIMESTAMP)')
            connection.commit()
        self.path_patch = patch('config.DB_PATH', path)
        self.interval_patch = patch('config.DEAD_CHAT_IDLE_SECONDS', 60)
        self.path_patch.start()
        self.interval_patch.start()
        self.gap_patch = patch('config.AUTONOMOUS_MIN_GAP_SECONDS', 0)
        self.gap_patch.start()
        self.quiet_patch = patch('cogs.housekeeping.engagement.dead_chat_quiet_now', return_value=False)
        self.quiet_patch.start()
        await db.get_db()

    async def asyncTearDown(self):
        await db.close()
        self.path_patch.stop()
        self.interval_patch.stop()
        self.gap_patch.stop()
        self.quiet_patch.stop()
        self.directory.cleanup()

    async def row(self):
        return (await db.get_channel_idle_candidates([10]))[0]

    async def test_doubling_persistence_and_human_reset(self):
        await db.record_user_activity(1, 10, 20, 'Sam')
        self.assertEqual(dead_chat_delay_seconds(await self.row()), 60)
        for delay in (120, 240, 480):
            await db.mark_dead_chat_ping(10)
            self.assertEqual(dead_chat_delay_seconds(await self.row()), delay)
        await db.close()
        self.assertEqual(dead_chat_delay_seconds(await self.row()), 480)
        await db.record_bot_activity(1, 10)
        self.assertEqual(dead_chat_delay_seconds(await self.row()), 480)
        await db.record_user_activity(1, 10, 21, 'Another member')
        row = await self.row()
        self.assertEqual(dead_chat_delay_seconds(row), 60)
        self.assertEqual(row['dead_ping_count'], 0)
        self.assertIsNone(row['last_dead_ping_at'])

    async def test_bot_activity_moves_timer_without_resetting_sequence(self):
        await db.record_user_activity(1, 10, 20, 'Sam')
        await db.mark_dead_chat_ping(10)
        connection = await db.get_db()
        await connection.execute("UPDATE channel_activity SET last_channel_message_at=datetime('now', '-300 seconds') WHERE channel_id=10")
        await connection.commit()
        self.assertGreater((await self.row())['idle_seconds'], 299)
        await db.record_bot_activity(1, 10)
        row = await self.row()
        self.assertLess(row['idle_seconds'], 2)
        self.assertEqual(dead_chat_delay_seconds(row), 120)

    async def test_scheduler_sends_only_when_next_interval_expires(self):
        await db.record_user_activity(1, 10, 20, 'Sam')
        connection = await db.get_db()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 10
        channel.send = AsyncMock()
        cog = object.__new__(HousekeepingCog)
        cog.bot = SimpleNamespace(get_channel=lambda _: channel)
        cog._pick_random_member = AsyncMock(return_value=SimpleNamespace(id=20, mention='<@20>'))
        async def age(seconds):
            await connection.execute("UPDATE channel_activity SET last_channel_message_at=datetime('now', ?) WHERE channel_id=10", (f'-{seconds} seconds',))
            await connection.commit()
        with patch('config.DEAD_CHAT_ENABLED', True), patch('config.DEAD_CHAT_CHANNEL_IDS', [10]), patch('cogs.housekeeping.generate_dead_chat_line', AsyncMock(side_effect=['ded chat. Your keyboard is on unpaid leave. Give it a topic.', 'ded chat. The silence has tenure. Challenge it.'])):
            await age(61)
            await cog._maybe_nudge_dead_chat()
            self.assertEqual(channel.send.await_count, 1)
            first_line = channel.send.call_args.args[0]
            self.assertIn('<@20>', first_line)
            await age(119)
            await cog._maybe_nudge_dead_chat()
            self.assertEqual(channel.send.await_count, 1)
            await age(121)
            await cog._maybe_nudge_dead_chat()
            self.assertEqual(channel.send.await_count, 2)
            self.assertNotEqual(channel.send.call_args.args[0], first_line)
            self.assertIn('<@20>', channel.send.call_args.args[0])
            self.assertEqual(dead_chat_delay_seconds(await self.row()), 240)

    async def test_five_ping_cap_survives_restart_and_human_resets(self):
        with patch('config.DEAD_CHAT_IDLE_SECONDS', 300):
            await db.record_user_activity(1, 10, 20, 'Sam')
            for delay in [300, 600, 1200, 2400, 4800]:
                self.assertEqual(dead_chat_delay_seconds(await self.row()), delay)
                await db.mark_dead_chat_ping(10)
            connection = await db.get_db()
            await connection.execute("UPDATE channel_activity SET last_channel_message_at=datetime('now', '-99999 seconds') WHERE channel_id=10")
            await connection.commit()
            self.assertFalse(dead_chat_due(await self.row()))
            await db.close()
            self.assertFalse(dead_chat_due(await self.row()))
            await db.record_bot_activity(1, 10)
            self.assertFalse(dead_chat_due(await self.row()))
            await db.record_user_activity(1, 10, 21, 'Another member')
            row = await self.row()
            self.assertEqual(row['dead_ping_count'], 0)
            self.assertEqual(dead_chat_delay_seconds(row), 300)

    async def test_dead_chat_uses_roast_ping_never_an_automatic_game(self):
        await db.record_user_activity(1, 10, 20, 'Sam')
        connection = await db.get_db()
        await connection.execute("UPDATE channel_activity SET last_channel_message_at=datetime('now', '-61 seconds') WHERE channel_id=10")
        await connection.commit()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 10
        channel.send = AsyncMock()
        games = SimpleNamespace(active={}, start_game=AsyncMock(return_value=True))
        cog = object.__new__(HousekeepingCog)
        cog.bot = SimpleNamespace(get_channel=lambda _: channel, get_cog=lambda _: games)
        cog._pick_random_member = AsyncMock(return_value=SimpleNamespace(id=20, mention='<@20>'))
        # Old Railway variables must not re-enable the removed automatic behavior.
        with patch('config.DEAD_CHAT_ENABLED', True), patch('config.DEAD_CHAT_CHANNEL_IDS', [10]), patch('config.AUTOMATIC_GAME_CHANCE', 1, create=True), patch('config.ENGAGEMENT_TOPIC_CHANCE', 1, create=True), patch('cogs.housekeeping.generate_dead_chat_line', AsyncMock(return_value='ded chat. Your keyboard wants a job.')):
            await cog._maybe_nudge_dead_chat()
        games.start_game.assert_not_awaited()
        self.assertEqual(channel.send.await_args.args[0], '<@20> ded chat. Your keyboard wants a job.')
        self.assertEqual((await self.row())['dead_ping_count'], 1)

    async def test_generated_line_retries_examples_and_strips_extra_pings(self):
        example = DEAD_CHAT_EXAMPLES[0].replace('{member}, ', '')
        with patch('cogs.housekeeping.llm.generate_response', AsyncMock(side_effect=[example, 'ded chat. @everyone <@999> Your keyboard wants a comeback. Give it one.'])) as model:
            line = await generate_dead_chat_line(None, 0)
            self.assertNotIn('@everyone', line)
            self.assertNotIn('<@999>', line)
            self.assertIn('keyboard wants a comeback', line)
            self.assertEqual(model.await_count, 2)

    async def test_quiet_hours_skip_work_and_preserve_ping_counter(self):
        await db.record_user_activity(1, 10, 20, 'Sam')
        await db.mark_dead_chat_ping(10)
        before = await self.row()
        cog = object.__new__(HousekeepingCog)
        cog.bot = MagicMock()
        with patch('config.DEAD_CHAT_ENABLED', True), patch('cogs.housekeeping.engagement.dead_chat_quiet_now', return_value=True), patch('cogs.housekeeping.db.get_channel_idle_candidates', new_callable=AsyncMock) as candidates, patch('cogs.housekeeping.generate_dead_chat_line', new_callable=AsyncMock) as generate:
            await cog._maybe_nudge_dead_chat()
        candidates.assert_not_awaited()
        generate.assert_not_awaited()
        cog.bot.get_channel.assert_not_called()
        after = await self.row()
        self.assertEqual(after['dead_ping_count'], before['dead_ping_count'])
        self.assertEqual(after['last_dead_ping_at'], before['last_dead_ping_at'])

    async def test_generation_crossing_midnight_does_not_ping(self):
        await db.record_user_activity(1, 10, 20, 'Sam')
        connection = await db.get_db()
        await connection.execute("UPDATE channel_activity SET last_channel_message_at=datetime('now', '-61 seconds') WHERE channel_id=10")
        await connection.commit()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 10
        channel.send = AsyncMock()
        cog = object.__new__(HousekeepingCog)
        cog.bot = SimpleNamespace(get_channel=lambda _: channel)
        cog._pick_random_member = AsyncMock(return_value=SimpleNamespace(id=20, mention='<@20>'))
        with patch('config.DEAD_CHAT_ENABLED', True), patch('config.DEAD_CHAT_CHANNEL_IDS', [10]), patch('cogs.housekeeping.engagement.dead_chat_quiet_now', side_effect=[False, True]), patch('cogs.housekeeping.generate_dead_chat_line', AsyncMock(return_value='ded chat. Your keyboard wants a comeback.')) as generate:
            await cog._maybe_nudge_dead_chat()
        generate.assert_awaited_once()
        channel.send.assert_not_awaited()
        row = await self.row()
        self.assertEqual(row['dead_ping_count'], 0)
        self.assertIsNone(row['last_dead_ping_at'])


class DeadChatQuietHoursTests(unittest.TestCase):
    def setUp(self):
        self.hours_patch = patch('config.DEAD_CHAT_QUIET_HOURS', '00:00-08:00')
        self.zone_patch = patch('config.SERVER_TIMEZONE', 'Asia/Kolkata')
        self.hours_patch.start()
        self.zone_patch.start()
        self.addCleanup(self.hours_patch.stop)
        self.addCleanup(self.zone_patch.stop)

    def test_midnight_is_inclusive_and_eight_am_is_exclusive(self):
        for hour, minute, expected in [(23, 59, False), (0, 0, True), (7, 59, True), (8, 0, False)]:
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(dead_chat_quiet_now(datetime(2026, 10, 15, hour, minute)), expected)

    def test_utc_clock_is_converted_to_server_timezone(self):
        self.assertTrue(dead_chat_quiet_now(datetime(2026, 10, 14, 18, 30, tzinfo=timezone.utc)))
        self.assertFalse(dead_chat_quiet_now(datetime(2026, 10, 15, 2, 30, tzinfo=timezone.utc)))

    def test_dead_chat_window_does_not_enable_global_quiet_hours(self):
        with patch('config.QUIET_HOURS', ''):
            now = datetime(2026, 10, 15, 3, 0)
            self.assertTrue(dead_chat_quiet_now(now))
            self.assertFalse(quiet_now(now))

    def test_blank_setting_disables_dead_chat_window(self):
        with patch('config.DEAD_CHAT_QUIET_HOURS', ''):
            self.assertFalse(dead_chat_quiet_now(datetime(2026, 10, 15, 3, 0)))
