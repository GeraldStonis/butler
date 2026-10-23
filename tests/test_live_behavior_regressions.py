"""Check actual wrong-recipient drafts and bot-noise starvation, not just prompts."""

import asyncio
import runpy
import os
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

import config
from cogs.chat import ChatCog
from cogs.housekeeping import HousekeepingCog, dead_chat_due
from services import database as db, emoji_filter, memory, reply_target


class ReplyBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.enterContext(patch.object(config, 'DB_PATH', ':memory:'))
        self.enterContext(patch.object(config, 'OWNER_ID', 100))
        self.enterContext(patch.object(db, '_init_lock', asyncio.Lock()))
        self.owner = SimpleNamespace(id=100, display_name='RoNiX', name='ronix', bot=False)
        self.potato = SimpleNamespace(id=200, display_name='Potato', name='potato', bot=False)
        self.alex = SimpleNamespace(id=300, display_name='Alex', name='alex', bot=False)
        self.members = [self.owner, self.potato, self.alex]
        self.context = [{'role': 'user', 'content': 'nixi do you remember me?'}]

    async def asyncTearDown(self):
        await memory.shutdown()
        await db.close()

    async def repair(self, text, *, requester=None, request='nixi do you remember me?'):
        return await reply_target.ensure_reply(text, requester or self.owner,
                                               self.members, request, self.context)

    async def test_screenshot_wrong_name_is_reanswered_not_blindly_renamed(self):
        wrong = 'Yes, I remember you, Potato! You play Minecraft.'
        with patch.object(reply_target.llm, 'generate_visible_response', AsyncMock(return_value='Of course, Sir Ronix.')) as model:
            self.assertEqual(await self.repair(wrong), 'Of course, Sir Ronix.')
        model.assert_awaited_once()
        policy = model.await_args.args[0][-2]['content']
        self.assertIn('Discord ID 100', policy)
        self.assertIn('do not attach the other member', policy)
        self.assertNotIn('Minecraft', 'Of course, Sir Ronix.')

    async def test_wrong_actual_mention_and_inline_unpunctuated_name_are_detected(self):
        for wrong in ['Hey <@200>!', 'Hello @Potato!', 'Thanks, Potato!', 'Welcome back Potato!', 'And yes, I remember you Potato!',
                      'Potato, I remember you.', 'Hello Potato!\nHow are you?']:
            with self.subTest(wrong=wrong), patch.object(reply_target.llm, 'generate_visible_response',
                    AsyncMock(return_value='Of course, Sir Ronix.')) as model:
                self.assertEqual(await self.repair(wrong), 'Of course, Sir Ronix.')
                model.assert_awaited_once()

    async def test_guest_after_owner_is_not_addressed_as_ronix(self):
        with patch.object(reply_target.llm, 'generate_visible_response', AsyncMock(return_value='Welcome back, Potato.')):
            self.assertEqual(await self.repair('Sir Ronix, I remember you.', requester=self.potato), 'Welcome back, Potato.')

    async def test_failed_or_still_misdirected_rewrite_never_posts_the_stale_answer(self):
        for rewrite in ['', 'Of course, Potato! You love Minecraft.']:
            with self.subTest(rewrite=rewrite), patch.object(reply_target.llm, 'generate_visible_response', AsyncMock(return_value=rewrite)) as model:
                text = await self.repair('Hey Potato!')
                self.assertEqual(text, reply_target.FALLBACK_REACTION)
                self.assertNotIn('Minecraft', text)
                model.assert_awaited_once()

    async def test_normal_third_person_references_quotes_and_lists_are_not_retargeted(self):
        for text in ['Potato plays Minecraft; you prefer Valorant.', 'Ronix is my only owner.',
                     'Your favorite people are Alex, Potato.', 'You asked about Potato.',
                     'The example says "Hello Potato!"', 'Use `hello you Potato!` as a test.',
                     'Of course, Sir Ronix.', 'Hello <@100>!']:
            with self.subTest(text=text), patch.object(reply_target.llm, 'generate_visible_response', AsyncMock()) as model:
                self.assertEqual(await self.repair(text), text)
                model.assert_not_awaited()

    async def test_requested_performances_keep_their_intended_addressee(self):
        for request in ['nixi say "Hello Potato!"', 'write a birthday greeting for Potato',
                        'translate Hello Potato to Hinglish', 'greet Potato for me']:
            with self.subTest(request=request), patch.object(reply_target.llm, 'generate_visible_response', AsyncMock()) as model:
                self.assertEqual(await self.repair('Hello Potato!', request=request), 'Hello Potato!')
                model.assert_not_awaited()

    async def test_duplicate_display_name_resolves_to_current_actual_sender(self):
        impostor = SimpleNamespace(id=400, display_name='RoNiX', name='impostor', bot=False)
        guild = SimpleNamespace(members=[impostor, self.owner], emojis=[])
        self.assertEqual(await emoji_filter.filter_response('Hello @RoNiX!', guild, current_author_id=100), 'Hello <@100>!')
        self.assertEqual(await emoji_filter.filter_response('Hello @RoNiX!', guild, current_author_id=400), 'Hello <@400>!')

    async def test_longer_member_name_is_not_swallowed_by_current_short_name(self):
        short = SimpleNamespace(id=500, display_name='Sam', name='sam', bot=False)
        long = SimpleNamespace(id=600, display_name='Sam Smith', name='sam_smith', bot=False)
        guild = SimpleNamespace(members=[short, long], emojis=[])
        self.assertEqual(await emoji_filter.filter_response('I spoke to @Sam Smith.', guild, current_author_id=500),
                         'I spoke to <@600>.')
        self.assertEqual(await emoji_filter.filter_response('Already <@600>.', guild, current_author_id=500),
                         'Already <@600>.')

    async def test_ambiguous_bystander_alias_is_not_guessed(self):
        duplicate = SimpleNamespace(id=400, display_name='Potato', name='also_potato', bot=False)
        guild = SimpleNamespace(members=[self.potato, duplicate], emojis=[])
        self.assertEqual(await emoji_filter.filter_response('Ask @Potato.', guild, current_author_id=100),
                         'Ask @Potato.')

    async def test_recent_sender_metadata_catches_wrong_name_with_partial_member_cache(self):
        context = [{'role': 'user', 'content': '{"speaker":"Potato","id":200,"text":"old message"}'},
                   {'role': 'user', 'content': 'nixi do you remember me?'}]
        with patch.object(reply_target.llm, 'generate_visible_response', AsyncMock(return_value='Of course, Sir Ronix.')) as model:
            reply = await reply_target.ensure_reply('Yes, I remember you Potato!', self.owner, [],
                                                     'nixi do you remember me?', context)
        self.assertEqual(reply, 'Of course, Sir Ronix.')
        model.assert_awaited_once()

    async def test_actual_chat_handler_sends_and_saves_only_the_corrected_reply(self):
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=999)))
        cog._detect_mode = AsyncMock(return_value='DIRECT_ADDRESS')
        cog._maybe_send_stfu_sticker = AsyncMock(return_value=False)
        cog._quick_reaction = AsyncMock(return_value=(None, None, None))
        guild = SimpleNamespace(id=1, members=self.members, emojis=[])
        message = SimpleNamespace(author=self.owner, guild=guild, content='nixi do you remember me?',
            channel=SimpleNamespace(id=10, typing=MagicMock()), mentions=[], reference=None,
            reply=AsyncMock(return_value=SimpleNamespace(id=11)))
        with patch.object(config, 'ENABLE_NATURAL_OWNER_ACTIONS', False), \
                patch('cogs.chat.conversation.generate_reply', AsyncMock(return_value='And yes, I remember you, Potato!')), \
                patch.object(reply_target.llm, 'generate_visible_response', AsyncMock(return_value='Of course, Sir Ronix.')):
            await cog.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], 'Of course, Sir Ronix.')
        self.assertFalse(message.reply.await_args.kwargs['allowed_mentions'].users)
        rows = await db.get_recent_messages(10)
        self.assertEqual(rows[-1]['content'], 'Of course, Sir Ronix.')
        self.assertNotIn('Potato', rows[-1]['content'])


class DeadChatStarvationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.enterContext(patch.object(config, 'DB_PATH', ':memory:'))
        self.enterContext(patch.object(db, '_init_lock', asyncio.Lock()))
        self.enterContext(patch.object(memory, '_channel_locks', {}))
        for name, value in [('OWNER_ID', 100), ('DEAD_CHAT_ENABLED', True),
                            ('DEAD_CHAT_CHANNEL_IDS', [10]), ('DEAD_CHAT_IDLE_SECONDS', 60),
                            ('DEAD_CHAT_PREFERRED_USER_ID', 1423982225938190346),
                            ('DEAD_CHAT_PREFERRED_CHANCE', 1.0), ('QUIET_HOURS', ''),
                            ('SPAM_QUIET_HOURS', ''), ('AUTONOMOUS_MIN_GAP_SECONDS', 120)]:
            self.enterContext(patch.object(config, name, value))
        self.enterContext(patch('cogs.housekeeping.engagement.dead_chat_quiet_now', return_value=False))
        self.target = SimpleNamespace(id=1423982225938190346, mention='<@1423982225938190346>', bot=False)
        self.other = SimpleNamespace(id=200, mention='<@200>', bot=False)
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id = 10
        self.channel.guild = SimpleNamespace(id=1)
        self.channel.members = [self.target, self.other]
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=111, created_at=datetime.now(timezone.utc)))
        self.cog = object.__new__(HousekeepingCog)
        self.cog.bot = SimpleNamespace(get_channel=lambda _: self.channel)
        self.generate = self.enterContext(patch('cogs.housekeeping.generate_dead_chat_line',
            AsyncMock(return_value='ded chat. Even your keyboard is waiting for a contribution.')))
        await db.record_user_activity(1, 10, 200, 'Other')

    async def asyncTearDown(self):
        await db.close()

    async def row(self):
        return (await db.get_channel_idle_candidates([10]))[0]

    async def age_human(self, seconds):
        connection = await db.get_db()
        await connection.execute("UPDATE channel_activity SET last_message_at=datetime('now', ?) WHERE channel_id=10", (f'-{seconds} seconds',))
        await connection.commit()

    async def test_frequent_bot_posts_do_not_postpone_first_one_minute_nudge(self):
        await self.age_human(61)
        for _ in range(3):
            await db.record_bot_activity(1, 10)
        self.assertLess((await self.row())['idle_seconds'], 2)
        await db.set_state('autonomous_last:10', str(time.time()))
        await self.cog._maybe_nudge_dead_chat()
        self.channel.send.assert_awaited_once()
        self.assertTrue(self.channel.send.await_args.args[0].startswith(self.target.mention))
        self.generate.assert_awaited_once_with(None, 0, savage=True)

    async def test_followup_waits_two_minutes_from_nudge_not_latest_other_bot_post(self):
        await self.age_human(600)
        await db.mark_dead_chat_ping(10)
        self.assertFalse(dead_chat_due(await self.row()))
        connection = await db.get_db()
        await connection.execute("UPDATE channel_activity SET last_dead_ping_at=datetime('now', '-121 seconds') WHERE channel_id=10")
        await connection.commit()
        await db.record_bot_activity(1, 10)
        await self.cog._maybe_nudge_dead_chat()
        self.channel.send.assert_awaited_once()
        self.assertEqual((await self.row())['dead_ping_count'], 2)

    async def test_human_message_still_resets_timer_and_campaign(self):
        await self.age_human(600)
        await db.mark_dead_chat_ping(10)
        await db.record_user_activity(1, 10, 200, 'Other')
        await self.cog._maybe_nudge_dead_chat()
        self.channel.send.assert_not_awaited()
        self.assertEqual((await self.row())['dead_ping_count'], 0)

    async def test_five_ping_cap_and_member_opt_out_are_preserved(self):
        await self.age_human(10000)
        for _ in range(5):
            await db.mark_dead_chat_ping(10)
        await self.cog._maybe_nudge_dead_chat()
        self.channel.send.assert_not_awaited()
        await db.set_member_preferences(1, self.target.id, {'pings': False})
        self.assertIs(await self.cog._pick_random_member(self.channel), self.other)

    async def test_default_priority_is_majority_of_five_with_rotation_not_random_luck(self):
        picks = []
        for _ in range(5):
            member = await self.cog._pick_random_member(self.channel)
            picks.append(member.id)
            await self.cog._record_dead_chat_target(10, member.id)
        self.assertEqual(picks, [self.target.id, self.target.id, self.other.id, self.target.id, self.target.id])

    async def test_human_activity_during_generation_cancels_stale_ping(self):
        await self.age_human(61)
        async def interrupted(*args, **kwargs):
            await db.record_user_activity(1, 10, 200, 'Other')
            return 'ded chat. A fresh roast.'
        self.generate.side_effect = interrupted
        await self.cog._maybe_nudge_dead_chat()
        self.channel.send.assert_not_awaited()
        self.assertEqual(await db.get_state('engagement_retry:10'), '0')


class DeploymentPriorityDefaultsTests(unittest.TestCase):
    def test_missing_or_blank_variables_keep_requested_priority_and_explicit_zero_disables_it(self):
        names = ['DEAD_CHAT_PREFERRED_USER_ID', 'DEAD_CHAT_PREFERRED_CHANCE', 'DEAD_CHAT_IDLE_SECONDS']
        original = {name: os.environ.get(name) for name in names}
        try:
            for value in [None, '']:
                for name in names[:2]:
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
                # config calls load_dotenv, so isolate the environment under test.
                with patch('dotenv.load_dotenv'):
                    values = runpy.run_path(config.__file__)
                self.assertEqual(values['DEAD_CHAT_PREFERRED_USER_ID'], 1423982225938190346)
                self.assertEqual(values['DEAD_CHAT_PREFERRED_CHANCE'], 1.0)
            os.environ['DEAD_CHAT_PREFERRED_USER_ID'] = '0'
            os.environ['DEAD_CHAT_PREFERRED_CHANCE'] = '0'
            with patch('dotenv.load_dotenv'):
                values = runpy.run_path(config.__file__)
            self.assertEqual(values['DEAD_CHAT_PREFERRED_USER_ID'], 0)
            self.assertEqual(values['DEAD_CHAT_PREFERRED_CHANCE'], 0)
        finally:
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
