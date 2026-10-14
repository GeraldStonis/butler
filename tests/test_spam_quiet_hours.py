import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import discord
from cogs.housekeeping import HousekeepingCog
from services import engagement
from services.media import MediaResult


class SpamQuietHoursTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        settings = {
            'SERVER_TIMEZONE': 'Asia/Kolkata',
            'QUIET_HOURS': '',
            'SPAM_QUIET_HOURS': '00:00-08:00',
            'STICKER_SPAM_ENABLED': True,
            'STICKER_SPAM_CHANNEL_ID': 10,
            'MEME_DROP_ENABLED': True,
            'MEME_DROP_CHANNEL_ID': 10,
            'MUSIC_DROP_ENABLED': True,
            'MUSIC_CHANNEL_IDS': [10],
            'AUTONOMOUS_MIN_GAP_SECONDS': 0,
        }
        for name, value in settings.items():
            self.enterContext(patch(f'config.{name}', value))
        self.clock = self.enterContext(patch('services.engagement.datetime'))
        self.set_time(3)
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id = 10
        self.channel.guild = SimpleNamespace(id=1, stickers=[SimpleNamespace(available=True)])
        self.channel.send = AsyncMock()
        self.cog = object.__new__(HousekeepingCog)
        self.cog.bot = SimpleNamespace(get_channel=MagicMock(return_value=self.channel))
        self.states = {'meme_drop_next:10': '0'}

        async def save_state(key, value):
            self.states[key] = value

        self.state = self.enterContext(patch('cogs.housekeeping.db.get_state', AsyncMock(side_effect=self.states.get)))
        self.saved = self.enterContext(patch('cogs.housekeeping.db.set_state', AsyncMock(side_effect=save_state)))
        self.enterContext(patch('cogs.housekeeping.db.record_bot_activity', AsyncMock()))
        self.enterContext(patch('cogs.housekeeping.memory.log_delivery', AsyncMock()))
        self.enterContext(patch('cogs.housekeeping.db.get_channel_idle_candidates', AsyncMock(return_value=[{'channel_id': 10, 'last_message_at': 'episode1'}])))
        self.fetch = self.enterContext(patch('cogs.housekeeping.media.fetch_meme', AsyncMock(return_value=MediaResult('Meme', 'https://example.com/meme.jpg'))))
        self.song = self.enterContext(patch('cogs.housekeeping.music.idle_song', AsyncMock(return_value={'youtubeId': 'OMOGaugKpzs'})))
        self.commentary = self.enterContext(patch('cogs.housekeeping.activities.commentary', AsyncMock(return_value='Taking a music break.')))

    def set_time(self, hour, minute=0):
        self.clock.now.return_value = datetime(2026, 10, 15, hour, minute, tzinfo=ZoneInfo('Asia/Kolkata'))

    def test_quiet_window_boundaries_and_utc_conversion(self):
        for hour, minute, expected in [(23, 59, False), (0, 0, True), (7, 59, True), (8, 0, False)]:
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(engagement.spam_quiet_now(10, datetime(2026, 10, 15, hour, minute)), expected)
        self.assertTrue(engagement.spam_quiet_now(10, datetime(2026, 10, 14, 18, 30, tzinfo=timezone.utc)))
        self.assertFalse(engagement.spam_quiet_now(10, datetime(2026, 10, 15, 2, 30, tzinfo=timezone.utc)))

    async def test_quiet_window_does_not_silence_other_channels(self):
        self.assertFalse(await engagement.can_post(10))
        self.assertTrue(await engagement.can_post(20))
        self.assertFalse(engagement.quiet_now())
        with patch('config.STICKER_SPAM_CHANNEL_ID', 0):
            self.assertTrue(await engagement.can_post(10))

    async def test_blank_window_disables_spam_quiet_hours(self):
        with patch('config.SPAM_QUIET_HOURS', ''):
            self.assertTrue(await engagement.can_post(10))

    async def test_night_skips_stickers_and_meme_music_fetches(self):
        await self.cog._maybe_send_random_sticker()
        await self.cog._maybe_send_random_meme()
        await self.cog._maybe_send_music()
        self.channel.send.assert_not_awaited()
        self.fetch.assert_not_awaited()
        self.song.assert_not_awaited()
        self.commentary.assert_not_awaited()
        self.state.assert_not_awaited()
        self.saved.assert_not_awaited()

    async def test_sticker_resumes_at_eight_without_catching_up(self):
        await self.cog._maybe_send_random_sticker()
        self.set_time(8)
        await self.cog._maybe_send_random_sticker()
        self.channel.send.assert_awaited_once_with(stickers=[self.channel.guild.stickers[0]])

    async def test_meme_resumes_at_eight(self):
        self.set_time(8)
        await self.cog._maybe_send_random_meme()
        self.fetch.assert_awaited_once()
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.await_args.kwargs['embed'].image.url, 'https://example.com/meme.jpg')

    async def test_meme_fetch_crossing_midnight_does_not_send(self):
        self.set_time(23, 59)

        async def fetch():
            self.set_time(0)
            return MediaResult('Meme', 'https://example.com/meme.jpg')

        self.fetch.side_effect = fetch
        await self.cog._maybe_send_random_meme()
        self.fetch.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_music_generation_crossing_midnight_does_not_send(self):
        self.set_time(23, 59)

        async def comment(*args):
            self.set_time(0)
            return 'Taking a music break.'

        self.commentary.side_effect = comment
        await self.cog._maybe_send_music()
        self.commentary.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_global_quiet_hours_still_suppress_stickers(self):
        self.set_time(23, 30)
        with patch('config.QUIET_HOURS', '23:00-08:00'):
            await self.cog._maybe_send_random_sticker()
        self.channel.send.assert_not_awaited()
