import asyncio
import base64
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import discord
import config
from cogs.chat import ChatCog
from services import actions, avatar_edits, database as db, intent, memory, social

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII=')


class AvatarEditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        settings = {
            'DB_PATH': ':memory:', 'OWNER_ID': 99,
            'CLOUDFLARE_ACCOUNT_ID': 'a' * 32, 'CLOUDFLARE_API_TOKEN': 'unit-test-token',
            'AVATAR_EDITS_ENABLED': True, 'AVATAR_EDIT_CHANCE': 1,
            'AVATAR_EDIT_COOLDOWN_SECONDS': 3600, 'AVATAR_EDIT_DAILY_LIMIT': 2,
            'AVATAR_EDIT_TIMEOUT_SECONDS': 10, 'AUTOMATIC_MEDIA_ENABLED': True,
            'ENABLE_NATURAL_OWNER_ACTIONS': True,
        }
        for name, value in settings.items():
            self.enterContext(patch.object(config, name, value))
        self.enterContext(patch.object(avatar_edits, '_lock', asyncio.Lock()))
        self.requests = []
        self.response = {'success': True, 'result': {'image': base64.b64encode(PNG).decode()}}
        self.handler = self.respond
        real_client = httpx.AsyncClient
        self.enterContext(patch.object(avatar_edits.httpx, 'AsyncClient',
            side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(lambda request: self.handler(request)), **kwargs)))
        self.member = self.make_member(20)
        await db.get_db()

    async def asyncTearDown(self):
        await memory.shutdown()
        await db.close()

    def make_member(self, member_id):
        asset = MagicMock()
        asset.url = f'https://cdn.discordapp.com/avatars/{member_id}/example.png?size=256'
        asset.with_format.return_value = asset
        asset.with_size.return_value = asset
        return SimpleNamespace(id=member_id, bot=False, display_avatar=asset,
                               name='private_name', display_name='DO_NOT_UPLOAD_NAME')

    def respond(self, request):
        self.requests.append(request)
        if request.method == 'GET':
            return httpx.Response(200, content=PNG)
        return httpx.Response(200, json=self.response)

    def message(self, content='nixi that comeback was weak'):
        return SimpleNamespace(author=self.member, content=content, mentions=[], reference=None,
            guild=SimpleNamespace(id=1, stickers=[], emojis=[], members=[], get_member=lambda _: self.member),
            channel=SimpleNamespace(id=10, typing=MagicMock()),
            reply=AsyncMock(return_value=SimpleNamespace(author=SimpleNamespace(id=100))))

    async def seed_banter(self):
        for text in ['nixi your comeback was weak', 'nixi that is still weak']:
            await db.save_message(10, 1, 20, 'Guest', text)

    async def test_multipart_reference_upload_and_real_attachment(self):
        image = await avatar_edits.generate(1, self.member, 'maid')
        self.assertEqual(image, PNG)
        self.member.display_avatar.with_format.assert_called_once_with('png')
        self.member.display_avatar.with_size.assert_called_once_with(256)
        post = self.requests[-1]
        self.assertEqual(post.headers['authorization'], 'Bearer unit-test-token')
        self.assertTrue(post.headers['content-type'].startswith('multipart/form-data; boundary='))
        self.assertIn(b'name="input_image_0"; filename="avatar.png"', post.content)
        self.assertIn(PNG, post.content)
        self.assertIn(b'modest long-skirt maid uniform', post.content)
        self.assertNotIn(self.member.display_name.encode(), post.content)
        self.assertNotIn(b'unit-test-token', post.content)
        delivery = avatar_edits.delivery_kwargs(image)
        self.assertEqual(delivery['embed'].image.url, 'attachment://nixi-avatar.png')
        self.assertEqual(delivery['file'].fp.read(), PNG)
        delivery['file'].close()

    async def test_unconfigured_disabled_and_opted_out_do_not_upload(self):
        for setting, value in [('AVATAR_EDITS_ENABLED', False), ('CLOUDFLARE_ACCOUNT_ID', 'invalid'),
                               ('CLOUDFLARE_API_TOKEN', ''), ('AVATAR_EDIT_DAILY_LIMIT', 0)]:
            with self.subTest(setting=setting), patch.object(config, setting, value):
                self.assertIsNone(await avatar_edits.generate(1, self.member, 'donkey'))
        for flag in ('avatar_edits', 'roasting', 'media'):
            with self.subTest(flag=flag):
                await db.set_member_preferences(1, 20, {flag: False})
                self.assertIsNone(await avatar_edits.generate(1, self.member, 'donkey'))
                await db.set_member_preferences(1, 20, {flag: True})
        self.assertIsNone(await avatar_edits.generate(1, self.member, 'arbitrary unsafe prompt'))
        self.assertEqual(self.requests, [])

    async def test_default_members_work_without_opt_in(self):
        self.assertTrue((await db.get_member_preferences(1, 20))['avatar_edits'])
        self.assertEqual(await avatar_edits.generate(1, self.member, 'donkey'), PNG)
        await db.set_member_preferences(1, 21, {'pings': False})
        self.assertTrue((await db.get_member_preferences(1, 21))['avatar_edits'])
        self.assertEqual(await avatar_edits.generate(1, self.make_member(21), 'donkey'), PNG)

    async def test_explicit_opt_out_is_server_scoped(self):
        await db.set_member_preferences(1, 20, {'avatar_edits': False})
        self.assertFalse(await avatar_edits.permitted(1, 20))
        self.assertTrue(await avatar_edits.permitted(2, 20))

    async def test_attempt_cooldown_and_global_daily_cap(self):
        self.assertEqual(await avatar_edits.generate(1, self.member, 'donkey'), PNG)
        self.assertIsNone(await avatar_edits.generate(1, self.member, 'clown'))
        for member_id in [21, 22]:
            await db.set_member_preferences(1, member_id, {'avatar_edits': True})
        self.assertEqual(await avatar_edits.generate(1, self.make_member(21), 'clown'), PNG)
        self.assertIsNone(await avatar_edits.generate(1, self.make_member(22), 'clown'))
        self.assertEqual(len([r for r in self.requests if r.method == 'POST']), 2)

    async def test_concurrent_requests_do_not_duplicate_uploads(self):
        results = await asyncio.gather(*(avatar_edits.generate(1, self.member, 'donkey') for _ in range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len([r for r in self.requests if r.method == 'POST']), 1)

    async def test_persistent_preferences_and_attempt_cooldown_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            await db.close()
            with patch.object(config, 'DB_PATH', str(Path(directory) / 'prefs.db')):
                await db.set_member_preferences(1, 20, {'avatar_edits': True})
                self.assertEqual(await avatar_edits.generate(1, self.member, 'donkey'), PNG)
                await db.close()
                self.assertTrue((await db.get_member_preferences(1, 20))['avatar_edits'])
                self.assertIsNone(await avatar_edits.generate(1, self.member, 'donkey'))
                await db.close()
        self.assertEqual(len([r for r in self.requests if r.method == 'POST']), 1)

    async def test_legacy_preferences_migrate_to_default_on_without_changing_other_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            await db.close()
            path = str(Path(directory) / 'legacy.db')
            with closing(sqlite3.connect(path)) as connection:
                connection.execute('CREATE TABLE member_preferences (guild_id INTEGER, user_id INTEGER, roasting INTEGER DEFAULT 1, media INTEGER DEFAULT 1, pings INTEGER DEFAULT 1, PRIMARY KEY(guild_id,user_id))')
                connection.execute('INSERT INTO member_preferences VALUES (1,20,0,1,1)')
                connection.commit()
            with patch.object(config, 'DB_PATH', path):
                preferences = await db.get_member_preferences(1, 20)
                self.assertTrue(preferences['avatar_edits'])
                self.assertFalse(preferences['roasting'])
                self.assertTrue(preferences['media'])
                await db.close()

    async def test_old_zero_default_schema_preserves_disabled_members_but_new_rows_are_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            await db.close()
            path = str(Path(directory) / 'old-avatar.db')
            with closing(sqlite3.connect(path)) as connection:
                connection.execute('CREATE TABLE member_preferences (guild_id INTEGER, user_id INTEGER, roasting INTEGER DEFAULT 1, media INTEGER DEFAULT 1, pings INTEGER DEFAULT 1, avatar_edits INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,user_id))')
                connection.execute('INSERT INTO member_preferences VALUES (1,20,1,1,1,0)')
                connection.commit()
            with patch.object(config, 'DB_PATH', path):
                self.assertFalse((await db.get_member_preferences(1, 20))['avatar_edits'])
                await db.set_member_preferences(1, 21, {'pings': False})
                self.assertTrue((await db.get_member_preferences(1, 21))['avatar_edits'])
                self.assertFalse((await db.get_member_preferences(1, 21))['pings'])
                await db.close()

    async def test_opt_out_while_generating_drops_image(self):
        with patch.object(avatar_edits, 'permitted', AsyncMock(side_effect=[True, True, False])):
            self.assertIsNone(await avatar_edits.generate(1, self.member, 'donkey'))
        self.assertEqual(len([r for r in self.requests if r.method == 'POST']), 1)

    async def test_provider_quota_error_redacts_details_and_backs_off(self):
        def quota(request):
            self.requests.append(request)
            if request.method == 'GET':
                return httpx.Response(200, content=PNG)
            return httpx.Response(429, json={'errors': [{'message': 'unit-test-token'}]})
        self.handler = quota
        with self.assertLogs('nixi.avatar_edits', level='WARNING') as logs:
            self.assertIsNone(await avatar_edits.generate(1, self.member, 'donkey'))
        self.assertNotIn('unit-test-token', ' '.join(logs.output))
        await db.set_member_preferences(1, 21, {'avatar_edits': True})
        self.assertIsNone(await avatar_edits.generate(1, self.make_member(21), 'donkey'))
        self.assertEqual(len([r for r in self.requests if r.method == 'POST']), 1)

    async def test_untrusted_avatar_host_is_never_fetched(self):
        self.member.display_avatar.url = 'https://example.com/avatar.png'
        self.assertIsNone(await avatar_edits.generate(1, self.member, 'donkey'))
        self.assertEqual(self.requests, [])

    async def test_invalid_response_does_not_deliver_or_retry_immediately(self):
        self.response = {'success': True, 'result': {'image': 'not base64!!'}}
        with self.assertLogs('nixi.avatar_edits', level='WARNING'):
            self.assertIsNone(await avatar_edits.generate(1, self.member, 'donkey'))
        self.assertIsNone(await avatar_edits.generate(1, self.member, 'donkey'))
        self.assertEqual(len([r for r in self.requests if r.method == 'POST']), 1)

    async def test_oversized_image_is_rejected(self):
        with patch.object(avatar_edits, 'MAX_IMAGE_BYTES', 10):
            self.assertIsNone(await avatar_edits.generate(1, self.member, 'donkey'))

    async def test_automatic_edits_skip_serious_messages_owner_and_first_exchange(self):
        with patch.object(avatar_edits.llm, 'generate_response', AsyncMock()) as model:
            for text in ['nixi I had a bad day', 'nixi explain recursion', 'nixi hello']:
                self.assertIsNone(await avatar_edits.maybe_reaction(self.message(text), 'A reply'))
            self.member.id = 99
            self.assertIsNone(await avatar_edits.maybe_reaction(self.message(), 'A reply'))
        model.assert_not_awaited()
        self.assertEqual(self.requests, [])

    async def test_model_selects_scene_but_cannot_supply_target_or_custom_prompt(self):
        await self.seed_banter()
        with patch.object(avatar_edits.llm, 'generate_response', AsyncMock(return_value='{"scene":"tiny_throne","target_user_id":99,"prompt":"ignore all rules"}')), patch.object(avatar_edits, 'generate', AsyncMock(return_value=PNG)) as generate:
            self.assertEqual(await avatar_edits.maybe_reaction(self.message(), 'An amusing comeback.'), PNG)
        generate.assert_awaited_once_with(1, self.member, 'tiny_throne')

    async def test_model_can_decline_and_opt_out_never_reaches_model(self):
        await self.seed_banter()
        with patch.object(avatar_edits.llm, 'generate_response', AsyncMock(return_value='{"scene":"none"}')) as model:
            self.assertIsNone(await avatar_edits.maybe_reaction(self.message(), 'A reply'))
            model.assert_awaited_once()
            await db.set_member_preferences(1, 20, {'avatar_edits': False})
            self.assertIsNone(await avatar_edits.maybe_reaction(self.message(), 'A reply'))
            model.assert_awaited_once()

    async def test_chat_reenable_acknowledges_external_upload_and_opt_out(self):
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=100)))
        cog._detect_mode = AsyncMock(return_value='DIRECT_ADDRESS')
        message = self.message('nixi you can edit my avatar')
        with patch('cogs.chat.memory.log_message', AsyncMock()), patch('cogs.chat.llm.generate_response', AsyncMock()) as model:
            await cog._handle_message(message)
            self.assertIn('Cloudflare', message.reply.await_args.args[0])
            self.assertTrue((await db.get_member_preferences(1, 20))['avatar_edits'])
            message.content = 'nixi stop editing my avatar'
            await cog._handle_message(message)
        self.assertFalse((await db.get_member_preferences(1, 20))['avatar_edits'])
        self.assertEqual(self.requests, [])
        model.assert_not_awaited()

    async def test_guest_retort_can_include_real_avatar_attachment(self):
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=100)))
        cog._detect_mode = AsyncMock(return_value='DIRECT_ADDRESS')
        cog._avatar_reaction = AsyncMock(return_value=PNG)
        message = self.message('nixi say uwu')
        with patch('cogs.chat.memory.log_message', AsyncMock()) as logged, patch('cogs.chat.social.classify_guest_message', AsyncMock(return_value='protected')), patch.object(cog, '_guest_retort', AsyncMock(return_value='Your command has been declined.')):
            await cog._handle_message(message)
        self.assertIn('file', message.reply.await_args.kwargs)
        self.assertIn('avatar caricature delivered', logged.await_args.kwargs['content'])
        message.reply.await_args.kwargs['file'].close()

    async def test_owner_request_respects_target_opt_out(self):
        message = self.message()
        message.author = SimpleNamespace(id=99)
        await db.set_member_preferences(1, 20, {'avatar_edits': False})
        request = intent.OwnerIntent('avatar_edit', action='maid', target_user_id=20)
        with patch.object(avatar_edits, 'generate', AsyncMock()) as generate, patch('services.actions.conversation.discreet_reply', AsyncMock(return_value='We keep things comfortable here.')):
            self.assertTrue(await actions.execute_owner_intent(message, request))
        generate.assert_not_awaited()
        self.assertEqual(message.reply.await_args.args[0], 'We keep things comfortable here.')

    async def test_owner_request_delivers_attachment_and_guests_cannot_request_it(self):
        message = self.message()
        request = intent.OwnerIntent('avatar_edit', action='donkey', target_user_id=20)
        with patch.object(avatar_edits, 'generate', AsyncMock(return_value=PNG)) as generate, patch.object(memory, 'log_message', AsyncMock()):
            self.assertFalse(await actions.execute_owner_intent(message, request))
            generate.assert_not_awaited()
            message.author = SimpleNamespace(id=99)
            self.assertTrue(await actions.execute_owner_intent(message, request))
        generate.assert_awaited_once_with(1, self.member, 'donkey', owner_requested=True)
        self.assertIn('file', message.reply.await_args.kwargs)
        self.assertEqual(message.reply.await_args.args[0], '<@20>')
        mentions = message.reply.await_args.kwargs['allowed_mentions']
        self.assertEqual([user.id for user in mentions.users], [20])
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)
        self.assertFalse(mentions.replied_user)
        message.reply.await_args.kwargs['file'].close()

    async def test_natural_grass_request_edits_reference_and_tags_target(self):
        message = self.message('hey nixi can you make <@20> eat grass in a field')
        message.author = self.make_member(99)
        message.mentions = [self.member]
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=100)))
        self.assertIsNone(intent.heuristic_intent(message.content, message.mentions))
        with patch.object(avatar_edits.llm, 'generate_response', AsyncMock(side_effect=[
                '{"intent":"avatar_edit"}',
                '{"allowed":true,"scene":"the subject eating grass in a sunny green field"}',
        ])) as prepare, patch.object(memory, 'log_message', AsyncMock()) as logged:
            await cog._handle_message(message)
        post = self.requests[-1]
        self.assertEqual(post.method, 'POST')
        self.assertIn(b'eating grass in a sunny green field', post.content)
        self.assertIn(PNG, post.content)
        self.assertNotIn(b'<@20>', post.content)
        self.assertNotIn(b'<@20>', prepare.await_args.args[0][-1]['content'].encode())
        self.assertEqual(message.reply.await_args.args[0], '<@20>')
        self.assertEqual([user.id for user in message.reply.await_args.kwargs['allowed_mentions'].users], [20])
        self.assertIn('member ID 20', logged.await_args.args[4])

    async def test_owner_bypasses_automatic_cooldown_but_not_daily_cap(self):
        self.assertEqual(await avatar_edits.generate(1, self.member, 'maid'), PNG)
        self.assertIsNone(await avatar_edits.generate(1, self.member, 'maid'))
        custom = avatar_edits.AvatarScene('the subject eating grass in a field')
        self.assertEqual(await avatar_edits.generate(1, self.member, custom, owner_requested=True), PNG)
        self.assertIsNone(await avatar_edits.generate(1, self.member, custom, owner_requested=True))
        self.assertEqual(len([r for r in self.requests if r.method == 'POST']), 2)

    async def test_automatic_edits_cannot_use_custom_owner_scenes(self):
        custom = avatar_edits.AvatarScene('the subject eating grass in a field')
        self.assertIsNone(await avatar_edits.generate(1, self.member, custom))
        self.assertEqual(self.requests, [])

    async def test_unusable_or_disallowed_owner_scene_never_reaches_cloudflare(self):
        for response in ['not json', '{"allowed":false,"scene":"a scene"}',
                         '{"allowed":true,"scene":""}', '{"allowed":true,"scene":"@everyone in a field"}',
                         '{"allowed":true,"scene":"naked subject"}', '{"allowed":true,"scene":"https://example.com"}']:
            with self.subTest(response=response), patch.object(avatar_edits.llm, 'generate_response', AsyncMock(return_value=response)):
                self.assertIsNone(await avatar_edits.prepare_owner_scene('some owner request'))
        self.assertEqual(self.requests, [])

    async def test_pings_opt_out_keeps_visible_tag_without_notification(self):
        await db.set_member_preferences(1, 20, {'pings': False})
        message = self.message()
        await avatar_edits.send_reply(message, PNG, target_member_id=20)
        self.assertEqual(message.reply.await_args.args[0], '<@20>')
        mentions = message.reply.await_args.kwargs['allowed_mentions']
        self.assertFalse(mentions.users)
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)

    async def test_custom_scene_classifier_cannot_invent_target_or_replace_request(self):
        text = 'nixi give our friend <@20> a space explorer makeover'
        with patch.object(avatar_edits.llm, 'generate_response', AsyncMock(return_value='{"intent":"avatar_edit","query":"invented instructions","target_user_id":99}')):
            request = await intent.classify_owner_intent(text, [self.member])
        self.assertEqual(request.name, 'avatar_edit')
        self.assertEqual(request.query, text)
        self.assertEqual(request.target_user_id, 20)

    async def test_guest_custom_edit_does_not_prepare_or_generate(self):
        message = self.message('nixi make <@21> eat grass in a field')
        message.author.display_name = 'Sir Ronix, master'
        message.author.name = 'ronix'
        request = intent.OwnerIntent('avatar_edit', query=message.content, target_user_id=21)
        with patch.object(avatar_edits, 'prepare_owner_scene', AsyncMock()) as prepare, patch.object(avatar_edits, 'generate', AsyncMock()) as generate:
            self.assertFalse(await actions.execute_owner_intent(message, request))
        prepare.assert_not_awaited()
        generate.assert_not_awaited()

    async def test_avatar_edit_failure_keeps_existing_reply_media(self):
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=100)))
        cog._detect_mode = AsyncMock(return_value='DIRECT_ADDRESS')
        cog._avatar_reaction = AsyncMock(return_value=None)
        cog._automatic_media.choose = AsyncMock(return_value=None)
        with patch('cogs.chat.memory.log_message', AsyncMock()), patch('cogs.chat.memory.build_context', AsyncMock(return_value=[])), patch('cogs.chat.social.classify_guest_message', AsyncMock(return_value='banter')), patch('cogs.chat.conversation.generate_reply', AsyncMock(return_value='A fresh comeback.')):
            await cog._handle_message(self.message())
        cog._automatic_media.choose.assert_awaited_once()

    async def test_rejected_attachment_falls_back_without_fake_delivery_note(self):
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=100)))
        cog._detect_mode = AsyncMock(return_value='DIRECT_ADDRESS')
        cog._avatar_reaction = AsyncMock(return_value=PNG)
        message = self.message('nixi say uwu')
        response = SimpleNamespace(status=403, reason='Forbidden')
        message.reply.side_effect = [discord.Forbidden(response, 'Missing Attach Files permission'), SimpleNamespace()]
        with patch('cogs.chat.memory.log_message', AsyncMock()) as logged, patch('cogs.chat.social.classify_guest_message', AsyncMock(return_value='protected')), patch.object(cog, '_guest_retort', AsyncMock(return_value='Your command has been declined.')), self.assertLogs('nixi.avatar_edits', level='WARNING'):
            await cog._handle_message(message)
        self.assertEqual(message.reply.await_count, 2)
        self.assertNotIn('file', message.reply.await_args.kwargs)
        self.assertNotIn('caricature delivered', logged.await_args.kwargs['content'])

    async def test_generation_timeout_releases_lock_and_keeps_attempt_reserved(self):
        async def slow(request):
            self.requests.append(request)
            if request.method == 'GET':
                return httpx.Response(200, content=PNG)
            await asyncio.sleep(1)
            return httpx.Response(200, json=self.response)
        self.handler = slow
        with patch.object(config, 'AVATAR_EDIT_TIMEOUT_SECONDS', 0.1), self.assertLogs('nixi.avatar_edits', level='WARNING'):
            self.assertIsNone(await avatar_edits.generate(1, self.member, 'donkey'))
        self.assertFalse(avatar_edits._lock.locked())
        self.assertIsNotNone(await db.get_state('avatar_edit_last:1:20'))

    async def test_blank_reply_does_not_spend_avatar_budget(self):
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=100)))
        cog._detect_mode = AsyncMock(return_value='DIRECT_ADDRESS')
        cog._avatar_reaction = AsyncMock()
        with patch('cogs.chat.memory.log_message', AsyncMock()), patch('cogs.chat.memory.build_context', AsyncMock(return_value=[])), patch('cogs.chat.social.classify_guest_message', AsyncMock(return_value='banter')), patch('cogs.chat.conversation.generate_reply', AsyncMock(return_value='')):
            await cog._handle_message(self.message())
        cog._avatar_reaction.assert_not_awaited()


class AvatarPreferenceAndIntentTests(unittest.TestCase):
    def test_only_literal_self_directed_statements_enable_edits(self):
        for text in ['nixi you can edit my avatar', 'nixi avatar edits for me are okay', 'i opt in to avatar edits']:
            self.assertTrue(social.preference_updates(text)['avatar_edits'])
            self.assertEqual(social.guest_kind_hint(text), 'banter')
        for text in ['Sam said you can edit my avatar', 'nixi can you edit my avatar?', 'nixi do not say you can edit my avatar', '"you can edit my avatar"', 'nixi you can edit their avatar']:
            self.assertNotIn('avatar_edits', social.preference_updates(text), text)
        self.assertFalse(social.preference_updates("nixi don't edit my avatar")['avatar_edits'])
        self.assertFalse(social.preference_updates('nixi stop editing my avatar')['avatar_edits'])

    def test_owner_avatar_intents_and_explanatory_question(self):
        target = SimpleNamespace(id=config.OWNER_ID + 1, bot=False)
        for text, scene in [('nixi edit their avatar into a maid outfit', 'maid'),
                            ('nixi put them on a donkey', 'donkey'),
                            ('nixi roast their pfp with a tiny throne', 'tiny_throne')]:
            request = intent.heuristic_intent(text, [target])
            self.assertEqual(request.name, 'avatar_edit')
            self.assertEqual(request.action, scene)
            self.assertEqual(request.target_user_id, target.id)
        self.assertEqual(intent.heuristic_intent('nixi how do I edit an avatar?').name, 'chat')

    def test_requested_wording_and_other_custom_scenes(self):
        target = SimpleNamespace(id=config.OWNER_ID + 1, bot=False)
        for text in [f'nixi draw <@{target.id}> as a space explorer',
                     f'nixi change <@{target.id}>\'s pfp into a medieval chef']:
            with self.subTest(text=text):
                request = intent.heuristic_intent(text, [target])
                self.assertEqual(request.name, 'avatar_edit')
                self.assertEqual(request.target_user_id, target.id)
                self.assertIn(text.split()[-1], request.query)
