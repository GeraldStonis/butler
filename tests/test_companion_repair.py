"""Regression tests for contextual media, member recall, and real song delivery."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import httpx

import config
from cogs.chat import ChatCog
from services import actions, database as db, intent, media, memory, music, social


SONGS = [
    {'title': 'Apocalypse', 'artist': 'Cigarettes After Sex', 'youtubeId': 'aaaaaaaaaaa'},
    {'title': 'Namo Namo', 'artist': 'Amit Trivedi', 'youtubeId': 'bbbbbbbbbbb'},
    {'title': 'Ek Ajnabee Haseena Se', 'artist': 'Kishore Kumar', 'youtubeId': 'ccccccccccc'},
]


class CompanionRepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.enterContext(patch.object(config, 'DB_PATH', ':memory:'))
        self.enterContext(patch.object(config, 'OWNER_ID', 100))
        self.enterContext(patch.object(db, '_init_lock', asyncio.Lock()))
        self.enterContext(patch.object(memory, '_channel_locks', {}))
        self.enterContext(patch.object(media, '_gif_selection_lock', asyncio.Lock()))
        self.enterContext(patch.object(media, '_gif_provider_backoff', ('', 0)))
        self.enterContext(patch.object(music, '_selection_lock', asyncio.Lock()))

    async def asyncTearDown(self):
        await memory.shutdown()
        await db.close()

    async def context(self, text='nixi what do you remember about me?', user_id=200, guild=None):
        return await memory.build_context(10, text, 'Sam', 'DIRECT_ADDRESS', guild_id=1,
            current_author_id=user_id, nixi_author_id=999, guild=guild)

    def message(self, content='nixi play Apocalypse', user_id=100, guild=None):
        return SimpleNamespace(author=SimpleNamespace(id=user_id, display_name='Sam', name='sam', bot=False),
            channel=SimpleNamespace(id=10, typing=MagicMock()), guild=guild, content=content,
            mentions=[], reference=None, reply=AsyncMock(return_value=SimpleNamespace(id=500)))

    def reference(self, author_id=999, text='Here is the plan.'):
        reference = MagicMock(spec=discord.Message)
        reference.id = 123
        reference.author = SimpleNamespace(id=author_id, display_name='Nixi' if author_id == 999 else 'Other')
        reference.channel = SimpleNamespace(id=10)
        reference.guild = SimpleNamespace(id=1)
        reference.content, reference.embeds, reference.attachments = text, [], []
        return reference

    async def test_all_nine_supported_facts_are_bound_to_current_id_at_end_of_context(self):
        keys = ['preferred_name', 'interests', 'favorite_game', 'favorite_food', 'drink_preference',
                'communication_style', 'timezone', 'favorite_song', 'music_preference']
        for key in keys:
            await db.save_member_fact(1, 200, key, 'Sam ' + key, 'self-report')
            await db.save_member_fact(1, 300, key, 'Other ' + key, 'self-report')
        request = json.loads((await self.context())[-1]['content'])
        self.assertEqual(request['speaker_id'], 200)
        self.assertEqual(len(request['member_context']), 1)
        profile = request['member_context'][0]
        self.assertEqual(profile['member_id'], 200)
        self.assertTrue(profile['is_current_speaker'])
        self.assertEqual(len(profile['facts']), 9)
        self.assertFalse(any('Other' in f for f in profile['facts']))

    async def test_explicit_mention_retrieves_facts_with_incomplete_member_cache(self):
        await db.save_member_fact(1, 300, 'favorite_game', 'chess', 'I play chess')
        request = json.loads((await self.context('nixi what does <@300> play?', guild=SimpleNamespace(members=[], emojis=[])))[-1]['content'])
        profile = request['member_context'][0]
        self.assertEqual(profile['member_id'], 300)
        self.assertFalse(profile['is_current_speaker'])

    async def test_ambiguous_display_name_does_not_mix_profiles(self):
        members = [SimpleNamespace(id=i, display_name='Sam', name=f'sam{i}', bot=False) for i in (200, 300)]
        await db.save_member_fact(1, 200, 'drink_preference', 'tea', 'I like tea')
        await db.save_member_fact(1, 300, 'drink_preference', 'coffee', 'I like coffee')
        request = json.loads((await self.context('nixi do you remember Sam?', guild=SimpleNamespace(members=members, emojis=[])))[-1]['content'])
        self.assertEqual([p['member_id'] for p in request['member_context']], [200])

    async def test_pending_self_report_is_available_without_waiting_for_extraction(self):
        release = asyncio.Event()
        async def extract(*args, **kwargs):
            await release.wait()
        with patch.object(memory, 'extract_and_store_memories', AsyncMock(side_effect=extract)):
            memory.queue_member_learning(channel_id=10, user_message='main coffee peeta hoon',
                user_name='Sam', bot_reply='', guild_id=1, user_id=200)
            request = json.loads((await asyncio.wait_for(self.context(), timeout=1))[-1]['content'])
            self.assertEqual(request['member_context'][0]['pending_self_reports'], ['main coffee peeta hoon'])
            other = json.loads((await self.context(user_id=300))[-1]['content'])
            self.assertNotIn('member_context', other)
            different_channel = await memory.build_context(11, 'nixi remember me?', 'Sam', 'DIRECT_ADDRESS',
                guild_id=1, current_author_id=200)
            self.assertNotIn('coffee peeta', str(different_channel))
            release.set()
            await asyncio.gather(*list(memory._background_tasks))
        self.assertEqual(memory._member_learning, {})

    async def test_extractions_for_one_member_cannot_save_older_fact_after_newer_fact(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def model(prompt, **kwargs):
            text = prompt[-1]['content']
            if 'tea' in text:
                started.set()
                await release.wait()
            value = 'tea' if 'tea' in text else 'coffee'
            return json.dumps([{'fact_key': 'drink_preference', 'content': value, 'evidence': text}])
        with patch.object(memory.llm, 'generate_response', AsyncMock(side_effect=model)) as generate:
            first = memory.background(memory.extract_and_store_memories(10, 'I prefer tea', 'Sam', '', 1, 200))
            await started.wait()
            second = memory.background(memory.extract_and_store_memories(11, 'I prefer coffee', 'Sam', '', 1, 200))
            await asyncio.sleep(0)
            self.assertEqual(generate.await_count, 1)
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual((await db.get_member_facts(1, 200))[0]['content'], 'coffee')

    def test_common_self_reports_are_candidates_but_quotes_and_demands_are_not(self):
        for text in ['I usually drink coffee', 'meri favourite game chess hai',
                     'main chai peeta hoon', 'mujhko rock music pasand hai', 'My favorite song is Namo Namo',
                     "I don't like coffee anymore", 'I no longer play chess']:
            with self.subTest(text=text):
                self.assertTrue(memory.has_member_fact_candidate(text))
        for text in ['"I prefer coffee"', 'nixi say I prefer coffee', 'Sam likes tea', 'I love you nixi']:
            with self.subTest(text=text):
                self.assertFalse(memory.has_member_fact_candidate(text))

    async def test_bot_history_records_actual_recipient_not_nearest_human(self):
        await memory.log_message(10, 1, 200, 'Sam', 'I like tea')
        await memory.log_message(10, 1, 300, 'Other', 'I like coffee')
        await memory.log_message(10, 1, 999, 'Nixi', 'A tea person, noted.', True, reply_to_author_id=200)
        context = await self.context()
        own = next(entry['content'] for entry in context if entry['role'] == 'assistant')
        self.assertIn('replying to member ID 200', own)
        self.assertNotIn('replying to member ID 300', own)

    async def test_personal_recall_reserves_older_own_detail_before_busy_channel_fills_budget(self):
        await db.save_message(10, 1, 200, 'Sam', 'My keyboard broke yesterday; I was fixing the USB cable.')
        for i in range(48):
            await db.save_message(10, 1, 300, 'Other', f'Unrelated message {i} ' + 'x' * 3000)
        context = await self.context('nixi do you remember what I was doing earlier?')
        self.assertIn('fixing the USB cable', str(context))

    async def test_uncached_reply_is_fetched_and_used_for_trigger_and_context(self):
        guild = SimpleNamespace(id=1, members=[], emojis=[])
        message = self.message('what do you mean?', user_id=200, guild=guild)
        message.reference = SimpleNamespace(message_id=123, channel_id=10, resolved=None)
        message.channel.fetch_message = AsyncMock(return_value=self.reference())
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=999)))
        cog._maybe_send_stfu_sticker = AsyncMock(return_value=False)
        cog._quick_reaction = AsyncMock(return_value=(None, None, None))
        with patch.object(config, 'ENABLE_NATURAL_OWNER_ACTIONS', False), \
                patch('cogs.chat.conversation.generate_reply', AsyncMock(return_value='The plan is Sunday.')) as reply, \
                patch.object(social, 'classify_guest_message', AsyncMock(return_value='question')), \
                patch('cogs.chat.opinions.can_observe', return_value=False):
            await cog.on_message(message)
        message.channel.fetch_message.assert_awaited_once_with(123)
        prompt = reply.await_args.args[0]
        self.assertIn('Here is the plan.', str(prompt))
        self.assertEqual(json.loads(prompt[-1]['content'])['speaker_id'], 200)
        rows = await db.get_recent_messages(10)
        self.assertEqual(rows[-1]['reply_to_author_id'], 200)

    async def test_foreign_or_deleted_reference_is_not_used(self):
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=999)))
        message = self.message(guild=SimpleNamespace(id=1))
        message.reference = SimpleNamespace(message_id=123, channel_id=20, resolved=None)
        message.channel.fetch_message = AsyncMock()
        self.assertIsNone(await cog._resolve_reference(message))
        message.channel.fetch_message.assert_not_awaited()
        message.reference.channel_id = 10
        message.channel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='gone'), 'gone')
        self.assertIsNone(await cog._resolve_reference(message))

    async def test_legacy_message_table_migrates_without_losing_history(self):
        await db.close()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.db'
            with sqlite3.connect(path) as old:
                old.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, channel_id INTEGER, guild_id INTEGER, '
                            'author_id INTEGER, author_name TEXT, is_bot INTEGER, content TEXT, created_at TIMESTAMP)')
                old.execute("INSERT INTO messages VALUES(1,10,1,200,'Sam',0,'old message',CURRENT_TIMESTAMP)")
            old.close()
            with patch.object(config, 'DB_PATH', str(path)):
                self.assertEqual((await db.get_recent_messages(10))[0]['content'], 'old message')
                await db.save_message(10, 1, 999, 'Nixi', 'new reply', True, reply_to_author_id=200)
                self.assertEqual((await db.get_recent_messages(10))[-1]['reply_to_author_id'], 200)
                await db.close()

    async def test_song_request_variants_route_and_match_without_model(self):
        with patch.object(music, 'load_songs', return_value=SONGS), \
                patch.object(intent.llm, 'generate_response', AsyncMock()) as model:
            for text, video in [
                ('nixi play me Apocalypse bhai', 'aaaaaaaaaaa'),
                ('nixi could you please play Apocalypse for me please', 'aaaaaaaaaaa'),
                ('nixi bajao Namo Namo', 'bbbbbbbbbbb'),
                ('nixi Namo Namo gaana chala do na', 'bbbbbbbbbbb'),
                ('nixi Ek Ajnabee Haseena Se gaana bajao', 'ccccccccccc'),
                ('nixi play Apocalypse from your playlist', 'aaaaaaaaaaa'),
            ]:
                with self.subTest(text=text):
                    self.assertEqual(social.guest_kind_hint(text), 'command')
                    request = await intent.classify_owner_intent(text)
                    self.assertEqual(request.name, 'song')
                    self.assertEqual((await music.select_song(10, request.query))['youtubeId'], video)
            for text in ['nixi koi gaana bajao na', 'nixi ek gaana suna do please']:
                self.assertEqual((await intent.classify_owner_intent(text)).query, '')
        model.assert_not_awaited()

    async def test_replay_uses_delivered_track_and_ignores_failed_selection(self):
        with patch.object(music, 'load_songs', return_value=SONGS):
            self.assertIsNone(await music.select_song(10, 'that again'))
            message = self.message()
            with patch.object(memory, 'log_delivery', AsyncMock()):
                await actions.execute_owner_intent(message, intent.OwnerIntent('song', query='Apocalypse'))
            await music.select_song(10, 'Namo Namo')  # Not actually delivered.
            self.assertEqual((await music.select_song(10, 'that again'))['youtubeId'], 'aaaaaaaaaaa')
            self.assertIsNone(await music.select_song(11, 'that again'))
            self.assertEqual(await db.get_state('music_last_delivered:10'), 'aaaaaaaaaaa')

    async def test_delivered_song_replay_survives_restart(self):
        await db.close()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(config, 'DB_PATH', str(Path(directory) / 'music.db')), \
                    patch.object(music, 'load_songs', return_value=SONGS):
                await music.record_delivery(10, SONGS[1])
                await db.close()
                self.assertEqual((await music.select_song(10, 'that again'))['youtubeId'], 'bbbbbbbbbbb')
                await db.close()

    async def test_hinglish_guest_song_goes_through_actual_cog_to_youtube(self):
        message = self.message('nixi Namo Namo gaana bajao na', user_id=200)
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=999)))
        with patch.object(music, 'load_songs', return_value=SONGS), \
                patch.object(config, 'ENABLE_NATURAL_OWNER_ACTIONS', True), \
                patch.object(memory, 'log_delivery', AsyncMock()), \
                patch('cogs.chat.conversation.generate_reply', AsyncMock()) as reply:
            await cog.on_message(message)
        self.assertIn('https://www.youtube.com/watch?v=bbbbbbbbbbb', message.reply.await_args.args[0])
        reply.assert_not_awaited()

    async def test_provider_auth_failure_backoff_uses_fallback_and_key_change_retries(self):
        primary = self.enterContext(patch.object(media, 'fetch_gif', AsyncMock(side_effect=media.GifProviderError('auth', 900))))
        self.enterContext(patch.object(config, 'GIPHY_API_KEY', 'first-test-key'))
        self.enterContext(patch.object(media.search, 'image_search', AsyncMock(return_value=[])))
        await media.fetch_available_gif('skeptical judge')
        await media.fetch_available_gif('surprised judge')
        self.assertEqual(primary.await_count, 1)
        with patch.object(config, 'GIPHY_API_KEY', 'replacement-test-key'):
            await media.fetch_available_gif('skeptical judge')
        self.assertEqual(primary.await_count, 2)

    async def test_gif_reuse_prefers_least_recent_instead_of_permanent_exhaustion(self):
        candidates = [(f'key{i}', media.MediaResult(str(i), f'https://images.test/{i}.gif')) for i in range(4)]
        selected = [await media._choose_gif(candidates, 'query', allow_reuse=True) for _ in range(8)]
        self.assertEqual([r.title for r in selected], ['0', '1', '2', '3', '0', '1', '2', '3'])

    async def test_fitting_gif_does_not_wait_for_slow_avatar(self):
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=999)))
        result = media.MediaResult('Reaction', 'https://images.test/reaction.gif')
        cog._automatic_media.choose = AsyncMock(return_value=result)
        cog._avatar_reaction = AsyncMock(side_effect=AssertionError('GIF should take priority'))
        self.assertEqual(await cog._prepare_reaction(self.message(), 'A joke.'), (None, result))

    async def test_selected_expression_works_without_search_credentials(self):
        def respond(request):
            self.assertEqual(request.url.path, '/api/v2/smug')
            return httpx.Response(200, json={'results': [{'url': 'https://images.test/smug.gif'}]})
        real_client = httpx.AsyncClient
        with patch.object(media, 'fetch_gif', AsyncMock(return_value=None)), \
                patch.object(media.httpx, 'AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw)), \
                patch.object(media.search, 'image_search', AsyncMock(side_effect=AssertionError('No search needed'))):
            result = await media.fetch_available_gif('villain victory reaction', fallback_action='smug')
        self.assertEqual(result.url, 'https://images.test/smug.gif')

    async def test_model_selected_fallback_expression_is_forwarded_but_unknown_values_are_ignored(self):
        decision = {'kind': 'gif', 'adds_punchline': True,
                    'visual_punchline': 'The smug grin supplies comic timing.',
                    'gif_queries': ['villain satisfied grin'], 'fallback_action': 'smug'}
        with patch.object(config, 'AUTOMATIC_MEDIA_ENABLED', True), \
                patch.object(config, 'AUTOMATIC_MEDIA_CHANCE', 1), \
                patch.object(social.llm, 'generate_response', AsyncMock(return_value=json.dumps(decision))), \
                patch.object(media, 'fetch_available_gif', AsyncMock(return_value=media.MediaResult('Smug', 'https://images.test/smug.gif'))) as fetch:
            await social.AutomaticMedia().choose(10, 'I have never been wrong', 'Naturally. The facts are clearly jealous.')
            fetch.assert_awaited_once_with('villain satisfied grin', allow_reuse=True, fallback_action='smug')
        decision['fallback_action'] = '../../anything'
        with patch.object(config, 'AUTOMATIC_MEDIA_ENABLED', True), \
                patch.object(config, 'AUTOMATIC_MEDIA_CHANCE', 1), \
                patch.object(social.llm, 'generate_response', AsyncMock(return_value=json.dumps(decision))), \
                patch.object(media, 'fetch_available_gif', AsyncMock(return_value=media.MediaResult('GIF', 'https://images.test/a.gif'))) as fetch:
            await social.AutomaticMedia().choose(10, 'I have never been wrong', 'Naturally. The facts are clearly jealous.')
            fetch.assert_awaited_once_with('villain satisfied grin', allow_reuse=True)


class MediaCadenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_declined_attempt_does_not_spend_successful_gif_cooldown(self):
        service = social.AutomaticMedia()
        decision = {'kind': 'gif', 'adds_punchline': True,
                    'visual_punchline': 'A skeptical stare matches this absurd boast.',
                    'gif_queries': ['skeptical judge stare'], 'delivery': 'media_only'}
        with patch.object(config, 'AUTOMATIC_MEDIA_ENABLED', True), \
                patch.object(config, 'AUTOMATIC_MEDIA_CHANCE', 1), \
                patch.object(config, 'AUTOMATIC_MEDIA_COOLDOWN_SECONDS', 90), \
                patch('services.social.random.random', return_value=0), \
                patch('services.social.time.monotonic', return_value=100) as clock, \
                patch.object(social.llm, 'generate_response', AsyncMock(side_effect=['{"kind":"none"}', json.dumps(decision)])) as model, \
                patch.object(media, 'fetch_available_gif', AsyncMock(return_value=media.MediaResult('GIF', 'https://images.test/a.gif'))):
            self.assertIsNone(await service.choose(10, 'I never miss', 'The scoreboard disagrees.'))
            clock.return_value = 121
            result = await service.choose(10, 'I never miss', 'The scoreboard disagrees.')
            self.assertIsNotNone(result)
            self.assertFalse(result.standalone)
            clock.return_value = 200
            self.assertIsNone(await service.choose(10, 'I never miss', 'The scoreboard disagrees.'))
        self.assertEqual(model.await_count, 2)
