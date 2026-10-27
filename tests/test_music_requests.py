import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
from services import actions, database as db, intent, music


SONGS = [
    {'title': 'Every Breath You Take', 'artist': 'The Police', 'youtubeId': 'OMOGaugKpzs'},
    {'title': 'Apocalypse', 'artist': 'Cigarettes After Sex', 'category': 'vibes', 'youtubeId': 'sElE_BfQ67s'},
    {'title': 'Risk', 'artist': 'Gracie Abrams', 'youtubeId': 'TiMuT2BhwO0'},
]


class SongIntentTests(unittest.IsolatedAsyncioTestCase):
    def test_random_requests_do_not_need_a_model_call(self):
        for text in [
            'nixi play a song', 'nixi send some music', 'hey nixi, can you play something?',
            'nixi recommend me a song', 'nixi play another one',
            'nixi give me a song', 'nixi play a song for me',
            'nixi play a random track from your playlist please', '<@999> play a song',
        ]:
            with self.subTest(text=text):
                request = intent.heuristic_intent(text)
                self.assertIsNotNone(request)
                self.assertEqual((request.name, request.query), ('song', ''))

    def test_named_requests_preserve_searchable_metadata(self):
        for text, query in [
            ('nixi play Apocalypse', 'Apocalypse'),
            ('nixi play Apocalypse by Cigarettes After Sex', 'Apocalypse by Cigarettes After Sex'),
            ('nixi play a song by The Police', 'The Police'),
            ('nixi send a vibes song', 'vibes'),
            ('nixi play a song called Risk', 'Risk'),
            ("nixi play Don't Stop Believin'", "Don't Stop Believin"),
            ('nixi send a song called Kiss Me', 'Kiss Me'),
        ]:
            with self.subTest(text=text):
                request = intent.heuristic_intent(text)
                self.assertEqual((request.name, request.query), ('song', query))

    def test_discussion_negation_voice_and_existing_services_are_not_song_delivery(self):
        for text in [
            'nixi how do I play a song?', 'nixi what is your favourite song?',
            "nixi don't play a song", 'nixi can you not play music?',
            'nixi play music in voice chat', 'nixi play a song in vc',
            'nixi play higher or lower', 'nixi start trivia',
            'nixi send a gif of someone playing music', 'nixi send a cat meme',
            "nixi send today's news about music", 'nixi send information about music',
            'nixi recommend a music theory book',
        ]:
            with self.subTest(text=text):
                request = intent.heuristic_intent(text)
                self.assertTrue(request is None or request.name != 'song')

    async def test_semantic_song_requests_are_allowlisted_without_invented_urls(self):
        with patch.object(intent.llm, 'generate_response', AsyncMock(return_value='{"intent":"song","query":"vibes"}')) as model:
            request = await intent.classify_owner_intent('nixi apne playlist se kuch accha suggest karna')
        self.assertEqual((request.name, request.query), ('song', 'vibes'))
        self.assertIn('local playlist', model.await_args.args[0][0]['content'])


class PlaylistRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.path = patch.object(config, 'DB_PATH', ':memory:')
        self.path.start()
        self.songs = patch.object(music, 'load_songs', return_value=SONGS)
        self.songs.start()

    async def asyncTearDown(self):
        await db.close()
        self.songs.stop()
        self.path.stop()

    async def test_title_artist_category_and_combined_search(self):
        for query, video_id in [
            ('APOCALYPSE', 'sElE_BfQ67s'), ('The Police', 'OMOGaugKpzs'),
            ('vibes', 'sElE_BfQ67s'), ('Apocalypse by Cigarettes After Sex', 'sElE_BfQ67s'),
        ]:
            with self.subTest(query=query):
                song = await music.select_song(10, query)
                self.assertEqual(song['youtubeId'], video_id)

    async def test_unknown_song_is_not_replaced_by_random_song(self):
        self.assertIsNone(await music.select_song(10, 'this song is not in the playlist'))
        self.assertIsNone(await db.get_state('music_recent:10'))

    async def test_random_selection_shares_history_with_idle_posts(self):
        first = await music.select_song(10)
        row = {'channel_id': 10, 'last_user_id': 20, 'last_message_at': 'episode1', 'human_idle_seconds': 901}
        with patch.object(music.random, 'random', return_value=0.01):
            second = await music.idle_song(row)
        third = await music.select_song(10)
        self.assertEqual(len({s['youtubeId'] for s in (first, second, third)}), 3)
        self.assertEqual(len(json.loads(await db.get_state('music_recent:10'))), 3)

    async def test_recent_history_survives_a_restart_and_is_channel_scoped(self):
        await db.close()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(config, 'DB_PATH', str(Path(directory) / 'history.db')):
                first = await music.select_song(10)
                await db.close()
                second = await music.select_song(10)
                self.assertNotEqual(first['youtubeId'], second['youtubeId'])
                self.assertIsNone(await db.get_state('music_recent:11'))
                await db.close()

    async def test_explicit_track_can_be_replayed(self):
        first = await music.select_song(10, 'Risk')
        self.assertEqual(await music.select_song(10, 'Risk'), first)

    async def test_broken_history_and_empty_playlist_are_safe(self):
        for value in ['not json', '{}']:
            await db.set_state('music_recent:10', value)
            self.assertIsNotNone(await music.select_song(10))
        with patch.object(music, 'load_songs', return_value=[]):
            self.assertIsNone(await music.select_song(10))

    async def test_manual_request_bypasses_autonomous_settings_and_lottery(self):
        message = SimpleNamespace(author=SimpleNamespace(id=config.OWNER_ID), channel=SimpleNamespace(id=10),
                                  content='nixi play Apocalypse', reply=AsyncMock())
        request = intent.heuristic_intent(message.content)
        with patch.object(config, 'MUSIC_DROP_ENABLED', False), patch.object(config, 'MUSIC_CHANNEL_IDS', []), patch.object(music, 'idle_song', AsyncMock()) as idle, patch.object(music.random, 'random') as roll, patch.object(actions.memory, 'log_delivery', AsyncMock()) as remember:
            self.assertTrue(await actions.execute_owner_intent(message, request))
        idle.assert_not_awaited()
        roll.assert_not_called()
        self.assertEqual(message.reply.await_args.args[0], 'Apocalypse - Cigarettes After Sex\nhttps://www.youtube.com/watch?v=sElE_BfQ67s')
        self.assertFalse(message.reply.await_args.kwargs['mention_author'])
        self.assertFalse(message.reply.await_args.kwargs['allowed_mentions'].users)
        remember.assert_awaited_once_with(message.reply.return_value, description='')
        self.assertIsNone(await db.get_state('music_idle:10'))

    async def test_no_match_explains_playlist_limit_without_generated_aside(self):
        message = SimpleNamespace(author=SimpleNamespace(id=config.OWNER_ID), channel=SimpleNamespace(id=10),
                                  content='nixi play hum tere pyar me', reply=AsyncMock())
        with patch.object(actions, 'reply_discreetly', AsyncMock()) as aside, \
                patch.object(actions.memory, 'log_delivery', AsyncMock()) as remember:
            self.assertTrue(await actions.execute_owner_intent(message, intent.heuristic_intent(message.content)))
        aside.assert_not_awaited()
        self.assertEqual(message.reply.await_args.args[0],
                         "I couldn't match that song in my playlist. What's the exact title or artist?")
        self.assertFalse(message.reply.await_args.kwargs['mention_author'])
        self.assertFalse(message.reply.await_args.kwargs['allowed_mentions'].users)
        remember.assert_awaited_once_with(message.reply.return_value, description='')
        self.assertIsNone(await db.get_state('music_last_delivered:10'))

    async def test_empty_playlist_does_not_ask_for_a_title_on_random_request(self):
        message = SimpleNamespace(author=SimpleNamespace(id=config.OWNER_ID), channel=SimpleNamespace(id=10),
                                  content='nixi play a song', reply=AsyncMock())
        with patch.object(music, 'load_songs', return_value=[]), \
                patch.object(actions, 'reply_discreetly', AsyncMock()) as aside, \
                patch.object(actions.memory, 'log_delivery', AsyncMock()):
            self.assertTrue(await actions.execute_owner_intent(message, intent.OwnerIntent('song')))
        aside.assert_not_awaited()
        self.assertEqual(message.reply.await_args.args[0], "I don't have a song to share from my playlist right now.")

    async def test_database_error_uses_discreet_reply(self):
        message = SimpleNamespace(author=SimpleNamespace(id=config.OWNER_ID), channel=SimpleNamespace(id=10),
                                  content='nixi play a song', reply=AsyncMock())
        with patch.object(actions, 'reply_discreetly', AsyncMock()) as aside:
            with patch.object(music, 'select_song', AsyncMock(side_effect=RuntimeError('private diagnostic'))):
                self.assertTrue(await actions.execute_owner_intent(message, intent.OwnerIntent('song')))
            aside.assert_awaited_once_with(message, 'song', 'exception')
        message.reply.assert_not_awaited()


class PlaylistFileTests(unittest.TestCase):
    def test_fallback_filename_invalid_ids_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'songs_data.json'
            path.with_name('songs.json').write_text(json.dumps(SONGS + [SONGS[0], {'youtubeId': 'https://evil.test'}]), encoding='utf-8')
            with patch.object(config, 'SONGS_PATH', path):
                self.assertEqual(music.load_songs(), SONGS)
