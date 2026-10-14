import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from services import database as db
from cogs.housekeeping import HousekeepingCog, dead_chat_delay_seconds


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
        await db.get_db()

    async def asyncTearDown(self):
        await db.close()
        self.path_patch.stop()
        self.interval_patch.stop()
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
        cog._pick_random_member = lambda _: SimpleNamespace(mention='<@20>')
        async def age(seconds):
            await connection.execute("UPDATE channel_activity SET last_channel_message_at=datetime('now', ?) WHERE channel_id=10", (f'-{seconds} seconds',))
            await connection.commit()
        with patch('config.DEAD_CHAT_ENABLED', True), patch('config.DEAD_CHAT_CHANNEL_IDS', [10]):
            await age(61)
            await cog._maybe_nudge_dead_chat()
            self.assertEqual(channel.send.await_count, 1)
            await age(119)
            await cog._maybe_nudge_dead_chat()
            self.assertEqual(channel.send.await_count, 1)
            await age(121)
            await cog._maybe_nudge_dead_chat()
            self.assertEqual(channel.send.await_count, 2)
            self.assertEqual(dead_chat_delay_seconds(await self.row()), 240)
