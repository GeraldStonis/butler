import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import config
from cogs.housekeeping import HousekeepingCog, DEAD_CHAT_MESSAGE_TTL_SECONDS
from services import database as db


class DeadChatCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(patch.object(config, 'DB_PATH', str(Path(directory.name) / 'cleanup.db')))
        self.now = datetime(2026, 10, 16, 10, tzinfo=timezone.utc).timestamp()
        self.clock = self.enterContext(patch('cogs.housekeeping.time.time', return_value=self.now))
        self.messages = {}
        self.partial_channel = SimpleNamespace(get_partial_message=MagicMock(side_effect=self.messages.__getitem__))
        self.cog = object.__new__(HousekeepingCog)
        self.cog.bot = SimpleNamespace(get_partial_messageable=MagicMock(return_value=self.partial_channel))
        await db.get_db()

    async def asyncTearDown(self):
        await db.close()

    async def schedule(self, message_id=100, delete_at=None, *, channel_id=10, side_effect=None):
        delete = AsyncMock(side_effect=side_effect)
        self.messages[message_id] = SimpleNamespace(delete=delete)
        await db.schedule_dead_chat_deletion(channel_id, message_id, self.now - 1 if delete_at is None else delete_at)
        return delete

    async def test_deletes_at_24_hours_not_before_and_leaves_other_messages_alone(self):
        self.assertEqual(DEAD_CHAT_MESSAGE_TTL_SECONDS, 86400)
        expiry = self.now + DEAD_CHAT_MESSAGE_TTL_SECONDS
        delete = await self.schedule(delete_at=expiry)
        await db.save_message(10, 1, 20, 'Guest', 'A human reply')
        await db.save_message(10, 1, 99, 'Nixi', 'An ordinary bot reply', is_bot=True)
        self.clock.return_value = expiry - 0.01
        await self.cog._delete_expired_dead_chat_messages()
        self.cog.bot.get_partial_messageable.assert_not_called()
        delete.assert_not_awaited()
        self.clock.return_value = expiry
        await self.cog._delete_expired_dead_chat_messages()
        delete.assert_awaited_once_with()
        self.cog.bot.get_partial_messageable.assert_called_once_with(10)
        self.partial_channel.get_partial_message.assert_called_once_with(100)
        self.assertEqual(await db.get_due_dead_chat_deletions(expiry), [])
        self.assertEqual([row['content'] for row in await db.get_recent_messages(10)],
                         ['A human reply', 'An ordinary bot reply'])
        await self.cog._delete_expired_dead_chat_messages()
        delete.assert_awaited_once()

    async def test_real_nudge_path_schedules_exactly_one_day_after_creation(self):
        await db.record_user_activity(1, 10, 20, 'Guest')
        connection = await db.get_db()
        await connection.execute("UPDATE channel_activity SET last_channel_message_at=datetime('now', '-301 seconds') WHERE channel_id=10")
        await connection.commit()
        created = datetime.fromtimestamp(self.now - 45, timezone.utc)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 10
        channel.send = AsyncMock(return_value=SimpleNamespace(id=101, created_at=created))
        self.cog.bot.get_channel = lambda _: channel
        self.cog._pick_random_member = AsyncMock(return_value=SimpleNamespace(id=20, mention='<@20>'))
        with patch.object(config, 'DEAD_CHAT_ENABLED', True), patch.object(config, 'DEAD_CHAT_CHANNEL_IDS', [10]), patch.object(config, 'DEAD_CHAT_IDLE_SECONDS', 300), patch('cogs.housekeeping.engagement.dead_chat_quiet_now', return_value=False), patch('cogs.housekeeping.engagement.can_post', AsyncMock(return_value=True)), patch('cogs.housekeeping.engagement.mark_sent', AsyncMock()), patch('cogs.housekeeping.generate_dead_chat_line', AsyncMock(return_value='ded chat. Your keyboard is collecting dust.')):
            await self.cog._maybe_nudge_dead_chat()
        channel.send.assert_awaited_once()
        expiry = created.timestamp() + 86400
        self.assertEqual(await db.get_due_dead_chat_deletions(expiry - 0.01), [])
        self.assertEqual(await db.get_due_dead_chat_deletions(expiry),
                         [{'message_id': 101, 'channel_id': 10, 'delete_at': expiry}])

    async def test_pending_cleanup_survives_restart_and_memory_reset(self):
        delete = await self.schedule()
        await db.close()
        await db.get_db()
        await db.clear_channel_memory(10)
        await db.record_user_activity(1, 10, 20, 'Guest')
        await self.cog._delete_expired_dead_chat_messages()
        delete.assert_awaited_once()
        self.assertEqual(await db.get_due_dead_chat_deletions(self.now), [])

    async def test_housekeeping_cleans_even_when_pings_disabled_and_quiet(self):
        delete = await self.schedule()
        self.cog._maybe_send_morning_news = AsyncMock()
        self.cog._maybe_send_random_meme = AsyncMock()
        self.cog._maybe_send_music = AsyncMock()
        with patch.object(config, 'DEAD_CHAT_ENABLED', False), patch('cogs.housekeeping.engagement.dead_chat_quiet_now', return_value=True), patch('cogs.housekeeping.db.get_channel_idle_candidates', AsyncMock()) as candidates:
            await HousekeepingCog.housekeeping_loop.coro(self.cog)
        delete.assert_awaited_once()
        candidates.assert_not_awaited()

    async def test_already_deleted_message_is_removed_from_queue(self):
        error = discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'Unknown Message')
        delete = await self.schedule(side_effect=error)
        await self.cog._delete_expired_dead_chat_messages()
        delete.assert_awaited_once()
        self.assertEqual(await db.get_due_dead_chat_deletions(self.now), [])

    async def test_http_errors_retry_with_backoff_and_survive_restart(self):
        for status, delay in [(403, 3600), (429, 300), (500, 300)]:
            with self.subTest(status=status):
                self.clock.return_value = self.now
                error = discord.HTTPException(SimpleNamespace(status=status, reason='Error'), 'Request failed')
                delete = await self.schedule(message_id=status, side_effect=[error, None])
                with self.assertLogs('nixi.housekeeping', level='WARNING'):
                    await self.cog._delete_expired_dead_chat_messages()
                self.assertEqual(await db.get_due_dead_chat_deletions(self.now + delay - 0.01), [])
                await db.close()
                self.clock.return_value = self.now + delay
                self.assertEqual((await db.get_due_dead_chat_deletions(self.now + delay))[0]['delete_at'], self.now - 1)
                await self.cog._delete_expired_dead_chat_messages()
                self.assertEqual(delete.await_count, 2)
                self.assertEqual(await db.get_due_dead_chat_deletions(self.now + delay), [])

    async def test_one_failed_delete_does_not_block_other_messages(self):
        first = await self.schedule(message_id=100, side_effect=OSError('Connection reset'))
        second = await self.schedule(message_id=101, channel_id=20)
        with self.assertLogs('nixi.housekeeping', level='ERROR'):
            await self.cog._delete_expired_dead_chat_messages()
        first.assert_awaited_once()
        second.assert_awaited_once()
        self.assertEqual(await db.get_due_dead_chat_deletions(self.now + 299), [])
        self.assertEqual([row['message_id'] for row in await db.get_due_dead_chat_deletions(self.now + 300)], [100])

    async def test_duplicate_registration_does_not_extend_expiry(self):
        await self.schedule(delete_at=self.now + 100)
        await db.schedule_dead_chat_deletion(10, 100, self.now + 10000)
        self.assertEqual(await db.get_due_dead_chat_deletions(self.now + 99), [])
        self.assertEqual((await db.get_due_dead_chat_deletions(self.now + 100))[0]['delete_at'], self.now + 100)

    async def test_cleanup_works_in_bounded_batches(self):
        for message_id in range(100, 126):
            await self.schedule(message_id=message_id)
        await self.cog._delete_expired_dead_chat_messages()
        self.assertEqual(sum(message.delete.await_count for message in self.messages.values()), 25)
        self.assertEqual([row['message_id'] for row in await db.get_due_dead_chat_deletions(self.now)], [125])
        await self.cog._delete_expired_dead_chat_messages()
        self.assertEqual(await db.get_due_dead_chat_deletions(self.now), [])
