import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import config
from cogs.chat import ChatCog
from services import actions, avatar_edits, database as db, intent, media, memory


class ActionGifTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.enterContext(patch.object(config, 'DB_PATH', ':memory:'))
        self.enterContext(patch.object(config, 'OWNER_ID', 99))
        self.enterContext(patch.object(config, 'ENABLE_NATURAL_OWNER_ACTIONS', True))
        self.enterContext(patch.object(intent.llm, 'generate_response', AsyncMock(side_effect=AssertionError('Unexpected model request'))))
        self.edit = self.enterContext(patch.object(avatar_edits, 'generate', AsyncMock()))
        self.prepare = self.enterContext(patch.object(avatar_edits, 'prepare_owner_scene', AsyncMock()))
        self.delivered = self.enterContext(patch.object(memory, 'log_delivery', AsyncMock()))
        self.target = SimpleNamespace(id=20, bot=False, name='guest', display_name='Guest')
        self.owner = SimpleNamespace(id=99, bot=False, name='ronix', display_name='Ronix')
        self.bot_user = SimpleNamespace(id=999, bot=True)
        members = {member.id: member for member in (self.target, self.owner, self.bot_user)}
        self.guild = SimpleNamespace(id=1, get_member=members.get, members=list(members.values()), stickers=[], emojis=[])
        self.cog = ChatCog(SimpleNamespace(user=self.bot_user))
        self.cog._detect_mode = AsyncMock(return_value='DIRECT_ADDRESS')
        self.cog._maybe_send_stfu_sticker = AsyncMock(return_value=False)
        self.cog._avatar_reaction = AsyncMock()
        self.cog._automatic_media.choose = AsyncMock()
        self.result = media.MediaResult('A matching reaction', 'https://example.test/reaction.gif')

    async def asyncTearDown(self):
        await memory.shutdown()
        await db.close()

    def message(self, text, *, author=None, mentions=None):
        sent = SimpleNamespace(author=self.bot_user)
        return SimpleNamespace(content=text, author=author or self.owner, guild=self.guild,
                               mentions=[self.target] if mentions is None else mentions, reference=None,
                               channel=SimpleNamespace(id=10, typing=MagicMock(), send=AsyncMock(return_value=sent)),
                               reply=AsyncMock(return_value=sent))

    def test_social_actions_are_deterministic_gif_intents(self):
        for text, action in [
            ('nixi give <@20> a handshake', 'handshake'),
            ("nixi shake <@20>'s hand", 'handshake'),
            ('nixi shake hands with <@20>', 'handshake'),
            ('nixi punch <@20>', 'punch'),
            ('nixi give <@20> a kiss', 'kiss'),
            ('nixi kick <@20>', 'kick'),
            ('nixi hug <@20>', 'hug'),
            ('nixi pat <@20> on the head', 'pat'),
            ('nixi give <@20> a headpat', 'pat'),
            ('nixi high-five <@20>', 'high_five'),
            ('nixi give <@20> a fist bump', 'fist_bump'),
            ('nixi can you cuddle <@20>', 'cuddle'),
            ('nixi make <@20> hug someone', 'hug'),
            ('nixi make <@20> give a handshake', 'handshake'),
            ('nixi send a cartoon punching gif to <@20>', 'punch'),
        ]:
            with self.subTest(text=text):
                request = intent.heuristic_intent(text, [self.target])
                self.assertEqual(request.name, 'action_gif')
                self.assertEqual(request.action, action)
                self.assertEqual(request.target_user_id, self.target.id)

    def test_explicit_avatar_edits_keep_the_requested_scene(self):
        for text in ['nixi edit <@20>\'s avatar to hug a teddy bear',
                     'nixi draw <@20> giving a handshake',
                     'nixi make an image of <@20> getting a headpat',
                     'nixi make <@20> eat grass in a field']:
            with self.subTest(text=text):
                self.assertEqual(intent.heuristic_intent(text, [self.target]).name, 'avatar_edit')

    def test_questions_and_negations_are_not_performed_by_fast_path(self):
        for text in ['nixi what does a handshake mean to <@20>?',
                     'nixi why did <@20> get a hug?',
                     'nixi can you explain a handshake to <@20>?',
                     "nixi don't punch <@20>",
                     'nixi never make <@20> hug anyone']:
            with self.subTest(text=text):
                self.assertIsNone(intent.heuristic_intent(text, [self.target]))

    async def test_each_requested_action_delivers_only_gif_and_target_tag(self):
        for action in ['handshake', 'punch', 'kiss', 'kick', 'hug', 'pat']:
            message = self.message(f'nixi give <@20> a {action}')
            with self.subTest(action=action), patch.object(media, 'fetch_gif', AsyncMock(return_value=self.result)) as fetch:
                await self.cog._handle_message(message)
                fetch.assert_awaited_once_with(media.SAFE_ACTION_QUERIES[action])
                message.reply.assert_not_awaited()
                message.channel.send.assert_awaited_once()
                delivery = message.channel.send.await_args
                self.assertEqual(delivery.args, ('<@20>',))
                self.assertEqual(delivery.kwargs['embed'].image.url, self.result.url)
                allowed = delivery.kwargs['allowed_mentions']
                self.assertEqual([user.id for user in allowed.users], [20])
                self.assertFalse(allowed.everyone)
                self.assertFalse(allowed.roles)
                self.assertFalse(allowed.replied_user)
        self.edit.assert_not_awaited()
        self.prepare.assert_not_awaited()
        self.cog._avatar_reaction.assert_not_awaited()
        self.cog._automatic_media.choose.assert_not_awaited()

    async def test_provider_failure_never_falls_back_to_caricature(self):
        for result in [None, RuntimeError('Provider unavailable')]:
            message = self.message('nixi give <@20> a kiss')
            provider = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=None)
            with patch.object(media, 'fetch_gif', provider):
                if isinstance(result, Exception):
                    with self.assertLogs('nixi.actions', level='ERROR'):
                        await self.cog._handle_message(message)
                else:
                    await self.cog._handle_message(message)
            message.channel.send.assert_not_awaited()
            message.reply.assert_awaited_once()
        self.edit.assert_not_awaited()
        self.prepare.assert_not_awaited()
        self.cog._avatar_reaction.assert_not_awaited()

    async def test_guest_handshake_request_uses_the_same_gif_path(self):
        guest = SimpleNamespace(id=21, bot=False, name='guest2', display_name='Guest 2')
        message = self.message('nixi give <@20> a handshake', author=guest)
        with patch.object(media, 'fetch_gif', AsyncMock(return_value=self.result)) as fetch:
            await self.cog._handle_message(message)
        fetch.assert_awaited_once_with(media.SAFE_ACTION_QUERIES['handshake'])
        self.assertEqual(message.channel.send.await_args.args, ('<@20>',))
        self.edit.assert_not_awaited()
        self.cog._avatar_reaction.assert_not_awaited()

    async def test_missing_member_asks_for_a_mention_without_fetch_or_edit(self):
        message = self.message('nixi give someone a hug', mentions=[])
        with patch.object(media, 'fetch_gif', AsyncMock()) as fetch:
            await self.cog._handle_message(message)
        fetch.assert_not_awaited()
        self.assertIn('Mention a member', message.reply.await_args.args[0])
        self.edit.assert_not_awaited()
        self.cog._avatar_reaction.assert_not_awaited()

    async def test_owner_request_respects_target_notification_preference(self):
        await db.set_member_preferences(1, 20, {'pings': False})
        message = self.message('nixi hug <@20>')
        with patch.object(media, 'fetch_gif', AsyncMock(return_value=self.result)):
            await self.cog._handle_message(message)
        self.assertEqual(message.channel.send.await_args.args, ('<@20>',))
        self.assertFalse(message.channel.send.await_args.kwargs['allowed_mentions'].users)

    async def test_member_can_opt_out_during_provider_request(self):
        async def fetch(_query):
            await db.set_member_preferences(1, 20, {'media': False})
            return self.result

        message = self.message('nixi hug <@20>')
        with patch.object(media, 'fetch_gif', AsyncMock(side_effect=fetch)):
            await self.cog._handle_message(message)
        message.channel.send.assert_not_awaited()
        self.edit.assert_not_awaited()

    async def test_model_can_classify_additional_social_actions_without_inventing_target(self):
        text = 'nixi do a celebratory dab for <@20>'
        output = {'intent': 'action_gif', 'action': 'celebratory dab', 'target_user_id': 666}
        with patch.object(intent.llm, 'generate_response', AsyncMock(return_value=json.dumps(output))) as model:
            request = await intent.classify_owner_intent(text, [self.bot_user, self.target])
        self.assertEqual(request.name, 'action_gif')
        self.assertEqual(request.action, 'celebratory dab')
        self.assertEqual(request.target_user_id, 20)
        self.assertIn('NOT an AI caricature', model.await_args.args[0][0]['content'])
        with patch.object(media, 'fetch_gif', AsyncMock(return_value=self.result)) as fetch:
            self.assertTrue(await actions.execute_owner_intent(self.message(text), request))
        fetch.assert_awaited_once_with('funny celebratory dab gif')

    async def test_model_generated_mentions_or_urls_are_not_sent_to_gif_provider(self):
        for action in ['<@666>', 'https://example.test', '@everyone', 'a' * 61]:
            with self.subTest(action=action), patch.object(intent.llm, 'generate_response', AsyncMock(return_value=json.dumps({'intent': 'action_gif', 'action': action}))):
                request = await intent.classify_owner_intent('nixi do something goofy for <@20>', [self.target])
                self.assertEqual(request.name, 'action_gif')
                self.assertEqual(request.action, '')
                with patch.object(media, 'fetch_gif', AsyncMock()) as fetch:
                    self.assertTrue(await actions.execute_owner_intent(self.message('nixi do something goofy for <@20>'), request))
                    fetch.assert_not_awaited()
        self.edit.assert_not_awaited()

    def test_gif_reactions_can_target_ronix_without_changing_avatar_target_rules(self):
        request = intent.heuristic_intent('nixi give <@99> a hug', [self.bot_user, self.owner])
        self.assertEqual(request.name, 'action_gif')
        self.assertEqual(request.target_user_id, 99)
        self.assertIsNone(intent._first_mention_id([self.owner]))
