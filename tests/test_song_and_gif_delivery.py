"""Real request -> catalog/provider -> Discord delivery regression coverage."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import config
from cogs.chat import ChatCog
from services import actions, avatar_edits, database as db, intent, media, memory, music, search


SONGS = [
    {'title': 'Happy & Sad', 'artist': 'Kacey Musgraves', 'youtubeId': 'aaaaaaaaaaa'},
    {'title': 'No.1 Party Anthem', 'artist': 'Arctic Monkeys', 'youtubeId': 'bbbbbbbbbbb'},
    {'title': "Don't Start Now", 'artist': 'Dua Lipa', 'youtubeId': 'ccccccccccc'},
    {'title': 'Stand By Me', 'artist': 'Ben E. King', 'youtubeId': 'ddddddddddd'},
    {'title': 'Apocalypse', 'artist': 'Cigarettes After Sex', 'youtubeId': 'eeeeeeeeeee'},
    {'title': 'Jump', 'artist': 'Van Halen', 'youtubeId': 'fffffffffff'},
]


class SongDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.enterContext(patch.object(config, 'DB_PATH', ':memory:'))
        self.enterContext(patch.object(db, '_init_lock', asyncio.Lock()))
        self.enterContext(patch.object(memory, '_channel_locks', {}))

    async def asyncTearDown(self):
        await memory.shutdown()
        await db.close()

    async def test_full_song_requests_match_mood_words_artist_and_title_punctuation(self):
        with patch.object(music, 'load_songs', return_value=SONGS):
            for text, video in [
                ('nixi play me the song Apocalypse', 'eeeeeeeeeee'),
                ('nixi play Happy & Sad by Kacey Musgraves', 'aaaaaaaaaaa'),
                ('nixi play No. 1 Party Anthem by Arctic Monkeys', 'bbbbbbbbbbb'),
                ("nixi play Don't Start Now", 'ccccccccccc'),
                ('nixi play Stand By Me by Ben E King', 'ddddddddddd'),
                ('nixi play Jump', 'fffffffffff'),
            ]:
                with self.subTest(text=text):
                    request = await intent.classify_owner_intent(text)
                    self.assertEqual(request.name, 'song')
                    song = await music.select_song(10, request.query)
                    self.assertIsNotNone(song)
                    self.assertEqual(song['youtubeId'], video)

    async def test_minor_title_typo_and_unambiguous_short_title_are_supported(self):
        with patch.object(music, 'load_songs', return_value=SONGS):
            self.assertEqual((await music.select_song(10, 'Apocalypsee'))['youtubeId'], 'eeeeeeeeeee')
            self.assertEqual((await music.select_song(10, 'Party Anthem by Arctic Monkeys'))['youtubeId'], 'bbbbbbbbbbb')
            self.assertIsNone(await music.select_song(10, 'this song is definitely absent'))
            self.assertIsNone(await music.select_song(10, 'Apocalypse by Wrong Artist'))

    async def test_repeating_a_request_with_one_matching_track_is_not_silenced(self):
        with patch.object(music, 'load_songs', return_value=[SONGS[0]]):
            for _ in range(3):
                self.assertEqual((await music.select_song(10))['youtubeId'], 'aaaaaaaaaaa')
                self.assertEqual((await music.select_song(10, 'Kacey Musgraves'))['youtubeId'], 'aaaaaaaaaaa')

    def test_relative_legacy_filename_finds_actual_catalog_without_cwd_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'songs_data.json').write_text(json.dumps(SONGS), encoding='utf-8')
            with patch.object(config, 'SONGS_PATH', Path('songs.json')), \
                    patch.object(config, 'PERSONA_PATH', path / 'persona.md'):
                self.assertEqual(music.load_songs(), SONGS)

    def test_duplicate_video_ids_preserve_every_original_searchable_label(self):
        rows = [{'title': 'Ajab Si', 'artist': 'KK', 'youtubeId': 'aaaaaaaaaaa'},
                {'title': 'Aankhon Mein Teri', 'artist': 'KK', 'youtubeId': 'aaaaaaaaaaa'},
                {'title': 'Best Part', 'artist': 'Daniel Caesar ft. H.E.R.', 'youtubeId': 'bbbbbbbbbbb'},
                {'title': 'Best Part', 'artist': 'H.E.R. & Daniel Caesar', 'youtubeId': 'bbbbbbbbbbb'}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'songs_data.json'
            path.write_text(json.dumps(rows), encoding='utf-8')
            with patch.object(config, 'SONGS_PATH', path):
                songs = music.load_songs()
        self.assertEqual(len(songs), 2)
        for row in rows:
            query = music._normalized(row['title'] + ' by ' + row['artist'])
            matches, _ = music._matching_songs(songs, query)
            self.assertIn(row['youtubeId'], {s['youtubeId'] for s in matches})

    def test_hinglish_song_requests_do_not_need_the_model(self):
        for text, query in [('nixi koi gaana suna do', ''), ('nixi ek gaana bajao', ''),
                            ('nixi Namo Namo gaana chala do', 'Namo Namo')]:
            request = intent.heuristic_intent(text)
            self.assertEqual((request.name, request.query), ('song', query))

    async def test_actual_catalog_accepts_titles_with_artist_without_mood_confusion(self):
        songs = music.load_songs()
        self.assertGreater(len(songs), 100)
        selected = [s for s in songs if s.get('artist') and any(
            word in s['title'].casefold() for word in ('sad', 'happy', 'party', 'love'))][:12]
        for song in selected + songs[-8:]:
            query = song['title'] + (' by ' + song['artist'] if song.get('artist') else '')
            matches, _ = music._matching_songs(songs, music._normalized(query))
            with self.subTest(query=query):
                self.assertIn(song['youtubeId'], {s['youtubeId'] for s in matches})

    async def test_real_chat_pipeline_sends_actual_playlist_url_not_a_claim(self):
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=999)))
        cog._detect_mode = AsyncMock(return_value='DIRECT_ADDRESS')
        cog._maybe_send_stfu_sticker = AsyncMock(return_value=False)
        message = SimpleNamespace(author=SimpleNamespace(id=config.OWNER_ID, name='Ronix', display_name='Ronix', bot=False),
            guild=None, channel=SimpleNamespace(id=10, typing=MagicMock()), mentions=[], reference=None,
            content='nixi play the song Happy & Sad by Kacey Musgraves', reply=AsyncMock())
        with patch.object(music, 'load_songs', return_value=SONGS), \
                patch.object(config, 'ENABLE_NATURAL_OWNER_ACTIONS', True), \
                patch('cogs.chat.conversation.generate_reply', AsyncMock()) as chat, \
                patch.object(memory, 'log_delivery', AsyncMock()):
            await cog.on_message(message)
        self.assertIn('https://www.youtube.com/watch?v=aaaaaaaaaaa', message.reply.await_args.args[0])
        chat.assert_not_awaited()


class PhysicalRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.enterContext(patch.object(config, 'DB_PATH', ':memory:'))
        self.enterContext(patch.object(db, '_init_lock', asyncio.Lock()))
        self.enterContext(patch.object(memory, '_channel_locks', {}))

    async def asyncTearDown(self):
        await memory.shutdown()
        await db.close()

    async def test_physical_actions_use_gifs_with_or_without_a_target(self):
        target = SimpleNamespace(id=20, bot=False)
        with patch.object(intent.llm, 'generate_response', AsyncMock()) as model:
            for text, action, target_id in [('nixi jump', 'jump', None), ('nixi hit someone', 'hit', None),
                ('nixi kiss <@20>', 'kiss', 20), ('nixi punch <@20>', 'punch', 20),
                ('nixi kick <@20>', 'kick', 20), ('nixi slap <@20>', 'slap', 20),
                ('nixi laugh', 'laugh', None), ('nixi dance for me', 'dance', None),
                ('nixi run', 'run', None), ('nixi clap', 'clap', None), ('nixi bow', 'bow', None)]:
                with self.subTest(text=text):
                    request = await intent.classify_owner_intent(text, [target] if '<@20>' in text else [])
                    self.assertEqual((request.name, request.action, request.target_user_id), ('action_gif', action, target_id))
            model.assert_not_awaited()

    async def test_negated_actions_and_explanations_never_fetch_media(self):
        with patch.object(intent.llm, 'generate_response', AsyncMock()) as model:
            for text in ['nixi do not jump', 'nixi never slap someone', 'nixi do not laugh',
                         'nixi why do people jump?', 'nixi explain how to kick a ball']:
                self.assertEqual((await intent.classify_owner_intent(text)).name, 'chat')
            model.assert_not_awaited()

    def test_explicit_gif_scenes_keep_the_subject_instead_of_becoming_generic_actions(self):
        for text, query in [('nixi give me a jumping gif', 'jumping'),
                            ('nixi send a gif of a dancing cat', 'a dancing cat')]:
            request = intent.heuristic_intent(text)
            self.assertEqual((request.name, request.query), ('gif_search', query))

    async def test_unlisted_physical_actions_are_semantically_routed_not_assumed_to_be_avatar_edits(self):
        target = SimpleNamespace(id=20, bot=False)
        text = 'nixi make <@20> do a cartwheel'
        self.assertIsNone(intent.heuristic_intent(text, [target]))
        with patch.object(intent.llm, 'generate_response', AsyncMock(return_value='{"intent":"action_gif","action":"cartwheel"}')):
            request = await intent.classify_owner_intent(text, [target])
        self.assertEqual((request.name, request.action, request.target_user_id), ('action_gif', 'cartwheel', 20))

    async def test_custom_avatar_scenes_still_resolve_semantically_without_changing_the_requested_scene(self):
        target = SimpleNamespace(id=20, bot=False)
        for text in ['nixi make <@20> eat grass in a field', 'nixi put <@20> on the moon holding a teacup']:
            with self.subTest(text=text), patch.object(intent.llm, 'generate_response', AsyncMock(return_value='{"intent":"avatar_edit","query":"ignored"}')):
                request = await intent.classify_owner_intent(text, [target])
                self.assertEqual(request.name, 'avatar_edit')
                self.assertIn(text.split()[-1], request.query)
                self.assertEqual(request.target_user_id, 20)

    async def test_owner_and_guest_explicit_jump_requests_work_when_automatic_media_is_disabled(self):
        result = media.MediaResult('Jumping', 'https://images.test/jump.gif')
        for user_id in [config.OWNER_ID, config.OWNER_ID + 1]:
            with self.subTest(user_id=user_id), patch.object(config, 'AUTOMATIC_MEDIA_ENABLED', False), \
                    patch.object(config, 'ENABLE_NATURAL_OWNER_ACTIONS', True), \
                    patch.object(media, 'fetch_available_gif', AsyncMock(return_value=result)) as fetch, \
                    patch.object(avatar_edits, 'generate', AsyncMock()) as avatar, \
                    patch('cogs.chat.conversation.generate_reply', AsyncMock()) as chat, \
                    patch.object(memory, 'log_delivery', AsyncMock()):
                cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=999)))
                cog._detect_mode = AsyncMock(return_value='DIRECT_ADDRESS')
                cog._maybe_send_stfu_sticker = AsyncMock(return_value=False)
                message = SimpleNamespace(author=SimpleNamespace(id=user_id, display_name='Requester', name='requester', bot=False),
                    guild=None, mentions=[], reference=None, content='nixi jump', reply=AsyncMock(),
                    channel=SimpleNamespace(id=10, typing=MagicMock(), send=AsyncMock()))
                await cog.on_message(message)
                fetch.assert_awaited_once_with(media.SAFE_ACTION_QUERIES['jump'])
                self.assertEqual(message.channel.send.await_args.kwargs['embed'].image.url, result.url)
                self.assertFalse(message.channel.send.await_args.kwargs['allowed_mentions'].users)
                avatar.assert_not_awaited()
                chat.assert_not_awaited()


class GifProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.enterContext(patch.object(config, 'DB_PATH', ':memory:'))
        self.enterContext(patch.object(db, '_init_lock', asyncio.Lock()))
        self.enterContext(patch.object(media, '_gif_selection_lock', asyncio.Lock()))
        self.enterContext(patch.object(config, 'GIPHY_API_KEY', 'invalid-test-key'))

    async def asyncTearDown(self):
        await db.close()

    def client(self, handler):
        real = httpx.AsyncClient
        return patch.object(media.httpx, 'AsyncClient', side_effect=lambda **kwargs: real(
            transport=httpx.MockTransport(handler), **kwargs))

    async def test_unauthorized_giphy_falls_back_to_free_matching_action_and_rotates(self):
        def respond(request):
            if request.url.host == 'api.giphy.com':
                return httpx.Response(401)
            self.assertEqual(request.url.path, '/api/v2/punch')
            self.assertIn('NixiButler', request.headers['User-Agent'])
            return httpx.Response(200, json={'results': [
                {'url': 'https://nekos.best/api/v2/punch/one.gif'},
                {'url': 'https://nekos.best/api/v2/punch/two.gif'},
            ]})
        with self.client(respond), patch.object(search, 'image_search', AsyncMock()) as images:
            first = await media.fetch_action_gif('punch')
            second = await media.fetch_action_gif('punch')
        self.assertNotEqual(first.url, second.url)
        self.assertIn('/punch/', first.url)
        images.assert_not_awaited()

    async def test_jumping_and_serving_use_real_gif_search_not_unrelated_reactions(self):
        results = [search.SearchItem('still', 'https://images.test/still.jpg', image_url='https://images.test/still.jpg'),
                   search.SearchItem('animated', 'https://images.test/correct.gif', image_url='https://images.test/correct.gif')]
        with patch.object(media, 'fetch_gif', AsyncMock(side_effect=RuntimeError('HTTP 401'))), \
                patch.object(search, 'image_search', AsyncMock(return_value=results)) as search_images:
            for action in ['jump', 'tea', 'coffee']:
                result = await media.fetch_action_gif(action)
                self.assertTrue(result.url.endswith('.gif'))
                self.assertIn(media.SAFE_ACTION_QUERIES[action], search_images.await_args.args[0])

    async def test_explicit_small_result_pool_rotates_instead_of_stopping_after_exhaustion(self):
        def respond(request):
            return httpx.Response(200, json={'data': [
                {'id': name, 'images': {'original': {'url': f'https://images.test/{name}.gif'}}}
                for name in ['one', 'two']]})
        with self.client(respond):
            urls = [(await media.fetch_gif('laugh', avoid_repeats=True, allow_reuse=True)).url for _ in range(5)]
        self.assertTrue(all(a != b for a, b in zip(urls, urls[1:])))

    async def test_automatic_media_can_still_decline_reused_gifs(self):
        await db.claim_reaction_gif('giphy:one')
        def respond(request):
            return httpx.Response(200, json={'data': [
                {'id': 'one', 'images': {'original': {'url': 'https://images.test/one.gif'}}}]})
        with self.client(respond):
            self.assertIsNone(await media.fetch_gif('a generic reaction', avoid_repeats=True, allow_reuse=False))

    async def test_all_providers_unavailable_returns_no_fake_success(self):
        with patch.object(media, 'fetch_gif', AsyncMock(side_effect=RuntimeError('HTTP 401'))), \
                patch.object(search, 'image_search', AsyncMock(side_effect=httpx.ConnectError('offline'))):
            self.assertIsNone(await media.fetch_available_gif('cartwheel'))
