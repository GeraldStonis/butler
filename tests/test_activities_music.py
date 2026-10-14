import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import config
import discord
import httpx
from services import database as db, music, activities, intent
from cogs.activities import ActivitiesCog, GameView
from cogs.housekeeping import HousekeepingCog


class NewActivitiesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.path = patch.object(config, 'DB_PATH', ':memory:')
        self.path.start()
        await db.get_db()

    async def asyncTearDown(self):
        await db.close()
        self.path.stop()

    async def test_idle_lottery_is_once_per_episode_and_resets(self):
        row = {'channel_id': 1, 'last_user_id': 2, 'last_message_at': 'episode1', 'human_idle_seconds': 599}
        with patch.object(music.random, 'uniform', return_value=600), patch.object(music.random, 'random', return_value=0.9) as roll:
            self.assertIsNone(await music.idle_song(row))
            roll.assert_not_called()
            row['human_idle_seconds'] = 901
            self.assertIsNone(await music.idle_song(row))
            self.assertIsNone(await music.idle_song(row))
            self.assertEqual(roll.call_count, 1)
            row['last_message_at'] = 'episode2'
            await music.idle_song(row)
            self.assertEqual(roll.call_count, 2)

    async def test_song_success_not_repeated_and_invalid_ids_filtered(self):
        songs = [{'youtubeId': 'OMOGaugKpzs'}, {'youtubeId': 'sElE_BfQ67s'}]
        row = {'channel_id': 1, 'last_user_id': 2, 'last_message_at': 'episode1', 'human_idle_seconds': 901}
        with patch.object(music, 'load_songs', return_value=songs), patch.object(music.random, 'random', return_value=0.01):
            first = await music.idle_song(row)
            self.assertIsNone(await music.idle_song(row))
            row['last_message_at'] = 'episode2'
            second = await music.idle_song(row)
        self.assertNotEqual(first['youtubeId'], second['youtubeId'])
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'songs.json'
            file.write_text(json.dumps(songs + [{'youtubeId': 'not-a-youtube-id'}]), encoding='utf-8')
            with patch.object(config, 'SONGS_PATH', file):
                self.assertEqual(len(music.load_songs()), 2)

    async def test_bot_posts_do_not_reset_human_idle(self):
        await db.record_user_activity(1, 2, 3, 'Guest')
        connection = await db.get_db()
        await connection.execute("UPDATE channel_activity SET last_message_at=datetime('now','-20 minutes') WHERE channel_id=2")
        await connection.commit()
        await db.record_bot_activity(1, 2)
        row = (await db.get_channel_idle_candidates([2]))[0]
        self.assertGreater(row['human_idle_seconds'], 1190)
        self.assertLess(row['idle_seconds'], 2)

    async def test_trivia_decodes_and_never_repeats(self):
        item = {'question': 'What is &quot;tea&quot;?', 'correct_answer': 'Drink &amp; relax',
                'incorrect_answers': ['A', 'B', 'C'], 'category': 'General'}
        with patch.object(activities, '_questions', [item, item]):
            question = await activities.trivia_question()
            self.assertEqual(question['question'], 'What is "tea"?')
            self.assertIsNone(await activities.trivia_question())

    async def test_guests_cannot_start_games(self):
        cog = ActivitiesCog(SimpleNamespace())
        channel = SimpleNamespace(guild=SimpleNamespace(id=1), id=2)
        with patch.object(config, 'OWNER_ID', 99), patch.object(activities, 'draw_cards', AsyncMock()) as draw:
            self.assertFalse(await cog.start_game(channel, 'cards', requester_id=3))
            draw.assert_not_called()

    async def test_each_user_votes_once_and_correct_score_settles_once(self):
        cog = ActivitiesCog(SimpleNamespace())
        channel = SimpleNamespace(id=2, guild=SimpleNamespace(id=1), send=AsyncMock())
        view = GameView(cog, channel, 'trivia', {'category': 'General', 'question': 'Q', 'answers': ['A','B','C','D'], 'correct': 1})
        view.message = SimpleNamespace(edit=AsyncMock())
        cog.active[2] = view
        interaction = SimpleNamespace(user=SimpleNamespace(id=4, display_name='Guest'), response=SimpleNamespace(send_message=AsyncMock()))
        await view.vote(interaction, 1)
        await view.vote(interaction, 0)
        self.assertEqual(view.votes, {4: 1})
        with patch.object(activities, 'commentary', AsyncMock(return_value='')):
            await view.on_timeout()
            await view.on_timeout()
        self.assertEqual((await db.activity_leaderboard(1))[0]['score'], 1)
        self.assertNotIn(2, cog.active)
        self.assertTrue(all(b.disabled for b in view.children))

    async def test_cards_ties_do_not_award_points_and_rank_ace_high(self):
        self.assertEqual(activities.card_rank({'value': 'ACE'}), 14)
        cog = ActivitiesCog(SimpleNamespace())
        channel = SimpleNamespace(id=2, guild=SimpleNamespace(id=1), send=AsyncMock())
        cards = [{'value': 'KING', 'suit': 'HEARTS', 'image': 'https://example.test/card.png'}] * 6
        view = GameView(cog, channel, 'cards', cards)
        view.round = 4
        view.votes = {4: 0, 5: 1}
        with patch.object(activities, 'commentary', AsyncMock(return_value='')):
            await view.on_timeout()
        self.assertEqual(await db.activity_leaderboard(1), [])

    async def test_owner_natural_requests(self):
        self.assertEqual(intent.heuristic_intent('nixi start trivia').name, 'trivia')
        self.assertEqual(intent.heuristic_intent('nixi play higher or lower').name, 'cards')
        self.assertEqual(intent.heuristic_intent('send a cat meme nixi').name, 'cat')

    async def test_music_sends_native_preview_only_if_human_has_not_returned(self):
        cog = object.__new__(HousekeepingCog)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 2
        channel.guild = SimpleNamespace(id=1)
        channel.send = AsyncMock()
        cog.bot = SimpleNamespace(get_channel=lambda _: channel)
        row = {'channel_id': 2, 'last_message_at': 'episode1'}
        song = {'youtubeId': 'OMOGaugKpzs'}
        with patch.object(config, 'MUSIC_DROP_ENABLED', True), patch.object(config, 'MUSIC_CHANNEL_IDS', [2]), patch('cogs.housekeeping.db.get_channel_idle_candidates', AsyncMock(side_effect=[[row], [row]])), patch('cogs.housekeeping.music.idle_song', AsyncMock(return_value=song)), patch('cogs.housekeeping.activities.commentary', AsyncMock(return_value='Taking a music break.')):
            await cog._maybe_send_music()
        self.assertEqual(channel.send.await_args.args[0], 'Taking a music break.\nhttps://www.youtube.com/watch?v=OMOGaugKpzs')
        channel.send.reset_mock()
        fresh = {**row, 'last_message_at': 'episode2'}
        with patch.object(config, 'MUSIC_DROP_ENABLED', True), patch.object(config, 'MUSIC_CHANNEL_IDS', [2]), patch('cogs.housekeeping.db.get_channel_idle_candidates', AsyncMock(side_effect=[[row], [fresh]])), patch('cogs.housekeeping.music.idle_song', AsyncMock(return_value=song)), patch('cogs.housekeeping.activities.commentary', AsyncMock(return_value='Music break.')):
            await cog._maybe_send_music()
        channel.send.assert_not_awaited()

    async def test_music_lottery_survives_database_restart(self):
        row = {'channel_id': 1, 'last_user_id': 2, 'last_message_at': 'episode1', 'human_idle_seconds': 901}
        with tempfile.TemporaryDirectory() as directory:
            await db.close()
            with patch.object(config, 'DB_PATH', str(Path(directory) / 'history.db')), patch.object(music.random, 'random', return_value=0.9) as roll:
                await music.idle_song(row)
                await db.close()
                await music.idle_song(row)
                self.assertEqual(roll.call_count, 1)
                await db.close()

    async def test_new_card_round_releases_old_view_but_stays_active(self):
        cog = ActivitiesCog(SimpleNamespace())
        channel = SimpleNamespace(id=2, guild=SimpleNamespace(id=1), send=AsyncMock())
        cards = [{'value': str(i + 2), 'suit': 'HEARTS', 'image': 'https://example.test/card.png'} for i in range(6)]
        view = GameView(cog, channel, 'cards', cards)
        view.votes = {4: 0}
        cog.active[2] = view
        with patch.object(activities, 'commentary', AsyncMock(return_value='')):
            await view.on_timeout()
        following = cog.active[2]
        self.assertIsNot(following, view)
        self.assertEqual(following.round, 1)
        following.stop()
        with self.assertRaises(asyncio.CancelledError):
            await following.timer

    async def test_music_is_suppressed_during_active_game(self):
        cog = object.__new__(HousekeepingCog)
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 2
        channel.send = AsyncMock()
        cog.bot = SimpleNamespace(get_channel=lambda _: channel, get_cog=lambda _: SimpleNamespace(active={2: object()}))
        with patch.object(config, 'MUSIC_DROP_ENABLED', True), patch.object(config, 'MUSIC_CHANNEL_IDS', [2]), patch('cogs.housekeeping.db.get_channel_idle_candidates', AsyncMock(return_value=[{'channel_id': 2}])), patch('cogs.housekeeping.music.idle_song', AsyncMock()) as select:
            await cog._maybe_send_music()
        select.assert_not_awaited()
        channel.send.assert_not_awaited()
