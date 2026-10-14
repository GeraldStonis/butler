import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from cogs.housekeeping import HousekeepingCog
from services.media import MediaResult


class MemeDropTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_drop_waits_and_due_drop_uses_embed(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 10
        channel.guild = SimpleNamespace(id=1)
        channel.send = AsyncMock()
        cog = object.__new__(HousekeepingCog)
        cog.bot = SimpleNamespace(get_channel=lambda _: channel)
        states = {}
        async def get(key):
            return states.get(key)
        async def set_value(key, value):
            states[key] = value
        result = MediaResult('A meme', 'https://example.com/meme.jpg')
        with patch('config.MEME_DROP_ENABLED', True), patch('config.MEME_DROP_CHANNEL_ID', 10), patch('config.MEME_DROP_INTERVAL_SECONDS', 300), patch('cogs.housekeeping.time.time', return_value=1000) as clock, patch('cogs.housekeeping.random.uniform', return_value=1.0), patch('cogs.housekeeping.db.get_state', AsyncMock(side_effect=get)), patch('cogs.housekeeping.db.set_state', AsyncMock(side_effect=set_value)), patch('cogs.housekeeping.db.record_bot_activity', AsyncMock()), patch('cogs.housekeeping.media.fetch_meme', AsyncMock(return_value=result)) as fetch:
            await cog._maybe_send_random_meme()
            self.assertEqual(states['meme_drop_next:10'], '1300.0')
            fetch.assert_not_awaited()
            clock.return_value = 1299
            await cog._maybe_send_random_meme()
            channel.send.assert_not_awaited()
            clock.return_value = 1300
            await cog._maybe_send_random_meme()
            self.assertEqual(channel.send.call_args.kwargs['embed'].image.url, result.url)
            self.assertEqual(channel.send.call_args.args, ())
            self.assertEqual(states['meme_drop_next:10'], '1600.0')
            # A restart must not reset the saved schedule or repeat a meme.
            cog = object.__new__(HousekeepingCog)
            cog.bot = SimpleNamespace(get_channel=lambda _: channel)
            clock.return_value = 1500
            await cog._maybe_send_random_meme()
            channel.send.assert_awaited_once()
            clock.return_value = 1600
            await cog._maybe_send_random_meme()
            channel.send.assert_awaited_once()

    async def test_disabled_drops_do_not_fetch(self):
        cog = object.__new__(HousekeepingCog)
        with patch('config.MEME_DROP_ENABLED', False), patch('cogs.housekeeping.media.fetch_meme', AsyncMock()) as fetch:
            await cog._maybe_send_random_meme()
            fetch.assert_not_awaited()
