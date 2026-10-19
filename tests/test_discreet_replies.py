import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
from services import actions, avatar_edits, conversation, database as db, intent, llm, media, search
from services.response_text import FALLBACK_REACTION


class DiscreetReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_replies_are_fresh_and_owner_context_is_trusted(self):
        with patch.object(llm, 'generate_visible_response', AsyncMock(side_effect=[
            'A little warmth for the room.', 'Good company deserves a little ceremony.',
        ])) as model:
            first = await conversation.discreet_reply('nixi serve tea', is_owner=True)
            second = await conversation.discreet_reply('nixi serve tea', is_owner=True)
        self.assertNotEqual(first, second)
        self.assertEqual(model.await_count, 2)
        instructions = model.await_args.args[0][1]['content']
        self.assertIn('verified Sir Ronix', instructions)
        self.assertIn('No tool has delivered media or verified new facts', instructions)
        self.assertIn('Do not blame, scold, mock or embarrass', instructions)

    async def test_diagnostic_or_fake_delivery_drafts_are_not_published(self):
        for draft in ['', 'GIPHY returned HTTP 401.', 'Set your API key in Railway.',
                      'I cannot find a GIF. Try again.', 'Here is your meme.',
                      "I've sent the GIF.", 'Traceback: RuntimeError', 'Forbidden 403',
                      'Mention a member to get a GIF.', 'Please tag someone first.',
                      'Coming right up, Sir Ronix!', "I'll make sure the brew is perfect.",
                      "I'm preparing the tea now.", 'Consider it done.',
                      'a' * 301, '\u092d\u093e\u0908']:
            with self.subTest(draft=draft), patch.object(llm, 'generate_visible_response', AsyncMock(return_value=draft)):
                self.assertEqual(await conversation.discreet_reply('nixi hug someone'), FALLBACK_REACTION)

    async def test_model_outage_has_a_nontechnical_last_resort(self):
        for error in [RuntimeError('private-token HTTP 500'), TimeoutError()]:
            with self.subTest(error=type(error).__name__), patch.object(llm, 'generate_visible_response', AsyncMock(side_effect=error)):
                self.assertEqual(await conversation.discreet_reply('nixi serve tea'), FALLBACK_REACTION)

    async def test_reply_cannot_include_unwanted_links_or_mentions(self):
        with patch.object(llm, 'generate_visible_response', AsyncMock(return_value='A little warmth. <@20> @everyone https://example.test')):
            reply = await conversation.discreet_reply('nixi serve tea')
        self.assertNotIn('http', reply)
        self.assertNotIn('@', reply)

    async def test_llm_default_failure_is_not_a_public_error_announcement(self):
        client = SimpleNamespace(chat=AsyncMock(side_effect=RuntimeError('secret-provider-body')))
        with patch.object(llm, '_get_client', return_value=client), self.assertLogs('nixi.llm', level='WARNING') as logs:
            reply = await llm.generate_response([{'role': 'user', 'content': 'hello'}])
        self.assertEqual(reply, FALLBACK_REACTION)
        self.assertNotIn('secret-provider-body', '\n'.join(logs.output))


class DiscreetActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.enterContext(patch.object(config, 'DB_PATH', ':memory:'))
        self.enterContext(patch.object(config, 'OWNER_ID', 99))
        self.generated = self.enterContext(patch.object(conversation, 'discreet_reply', AsyncMock(return_value='A little warmth for the room.')))
        self.target = SimpleNamespace(id=20, bot=False)
        self.guild = SimpleNamespace(id=1, get_member=lambda member_id: self.target if member_id == 20 else None)

    async def asyncTearDown(self):
        await db.close()

    def message(self, *, guild=True, mentions=None):
        return SimpleNamespace(content='nixi please', author=SimpleNamespace(id=99),
                               guild=self.guild if guild else None, mentions=mentions or [],
                               reply=AsyncMock(), channel=SimpleNamespace(id=10, send=AsyncMock()))

    async def test_empty_media_results_get_generated_replies_not_error_strings(self):
        for name, provider in [('gif_search', 'fetch_gif'), ('image_search', 'fetch_image'),
                               ('cat', 'fetch_cat'), ('meme', 'fetch_meme'),
                               ('tea', 'fetch_tea_or_coffee'), ('coffee', 'fetch_tea_or_coffee'),
                               ('action_gif', 'fetch_action_gif')]:
            with self.subTest(name=name), patch.object(media, provider, AsyncMock(return_value=None)):
                message = self.message()
                self.assertTrue(await actions.execute_owner_intent(message, intent.OwnerIntent(name, action='hug')))
                self.assertEqual(message.reply.await_args.args[0], self.generated.return_value)
                self.assertNotIn('embed', message.reply.await_args.kwargs)
                self.assertFalse(message.reply.await_args.kwargs['allowed_mentions'].users)

    async def test_provider_exception_is_logged_without_exposing_its_body(self):
        message = self.message()
        with patch.object(media, 'fetch_gif', AsyncMock(side_effect=RuntimeError('private-key HTTP 401'))), self.assertLogs('nixi.actions', level='ERROR') as logs:
            await actions.execute_owner_intent(message, intent.OwnerIntent('gif_search', query='handshake'))
        self.assertEqual(message.reply.await_args.args[0], self.generated.return_value)
        self.generated.assert_awaited_once_with(message.content, is_owner=True)
        self.assertIn('gif_search', '\n'.join(logs.output))
        self.assertNotIn('private-key', '\n'.join(logs.output))

    async def test_no_mention_and_dm_requests_still_deliver_a_gif_without_tags(self):
        result = media.MediaResult('Handshake', 'https://example.test/handshake.gif')
        for guild in [True, False]:
            with self.subTest(guild=guild), patch.object(media, 'fetch_action_gif', AsyncMock(return_value=result)) as fetch:
                message = self.message(guild=guild)
                await actions.execute_owner_intent(message, intent.OwnerIntent('action_gif', action='handshake'))
                fetch.assert_awaited_once_with('handshake')
                self.assertEqual(message.channel.send.await_args.args, (None,))
                self.assertEqual(message.channel.send.await_args.kwargs['embed'].image.url, result.url)
                self.assertFalse(message.channel.send.await_args.kwargs['allowed_mentions'].users)
                message.reply.assert_not_awaited()

    async def test_unresolved_target_does_not_invent_a_ping_or_request_another_mention(self):
        result = media.MediaResult('Hug', 'https://example.test/hug.gif')
        message = self.message()
        with patch.object(media, 'fetch_action_gif', AsyncMock(return_value=result)):
            await actions.execute_owner_intent(message, intent.OwnerIntent('action_gif', action='hug', target_user_id=666))
        self.assertEqual(message.channel.send.await_args.args, (None,))
        self.assertFalse(message.channel.send.await_args.kwargs['allowed_mentions'].users)
        message.reply.assert_not_awaited()

    async def test_target_preferences_remain_private_and_are_not_bypassed(self):
        await db.set_member_preferences(1, 20, {'media': False})
        message = self.message(mentions=[self.target])
        with patch.object(media, 'fetch_action_gif', AsyncMock()) as fetch:
            await actions.execute_owner_intent(message, intent.OwnerIntent('action_gif', action='hug', target_user_id=20))
        fetch.assert_not_awaited()
        message.channel.send.assert_not_awaited()
        self.assertEqual(message.reply.await_args.args[0], self.generated.return_value)

    async def test_disabled_paid_gif_generation_never_discloses_setup_or_calls_provider(self):
        with patch.object(config, 'IMGFLIP_GIF_CAPTIONS_ENABLED', False), patch.object(media, 'fetch_captioned_gif', AsyncMock()) as fetch:
            message = self.message()
            await actions.execute_owner_intent(message, intent.OwnerIntent('captioned_gif'))
        fetch.assert_not_awaited()
        self.assertEqual(message.reply.await_args.args[0], self.generated.return_value)

    async def test_avatar_configuration_is_not_announced_in_public(self):
        with patch.object(avatar_edits, 'ready', return_value=False), patch.object(avatar_edits, 'generate', AsyncMock()) as generate:
            message = self.message(mentions=[self.target])
            await actions.execute_owner_intent(message, intent.OwnerIntent('avatar_edit', action='maid', target_user_id=20))
        generate.assert_not_awaited()
        self.assertEqual(message.reply.await_args.args[0], self.generated.return_value)

    async def test_empty_searches_do_not_publish_setup_messages_or_invent_facts(self):
        for name, provider in [('web_search', 'web_search'), ('news', 'news_search')]:
            with self.subTest(name=name), patch.object(search, provider, AsyncMock(return_value=[])), patch.object(search.llm, 'generate_visible_response', AsyncMock()) as summarize:
                message = self.message()
                await actions.execute_owner_intent(message, intent.OwnerIntent(name, query='today weather'))
                self.assertEqual(message.reply.await_args.args[0], self.generated.return_value)
                summarize.assert_not_awaited()

    async def test_missing_action_uses_a_natural_reply_without_fetching(self):
        with patch.object(media, 'fetch_action_gif', AsyncMock()) as fetch:
            message = self.message()
            await actions.execute_owner_intent(message, intent.OwnerIntent('action_gif'))
        fetch.assert_not_awaited()
        self.assertEqual(message.reply.await_args.args[0], self.generated.return_value)
