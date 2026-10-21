import tempfile
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import config
import discord
from cogs.housekeeping import HousekeepingCog, dead_chat_delay_seconds, generate_dead_chat_line
from services import database as db, engagement


class PreferredDeadChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.stack = ExitStack()
        for name, value in [('DB_PATH', ':memory:'), ('OWNER_ID', 99),
                            ('DEAD_CHAT_ENABLED', True), ('DEAD_CHAT_CHANNEL_IDS', [10]),
                            ('DEAD_CHAT_IDLE_SECONDS', 60), ('DEAD_CHAT_PREFERRED_USER_ID', 20),
                            ('DEAD_CHAT_PREFERRED_CHANCE', 0.8), ('AUTONOMOUS_MIN_GAP_SECONDS', 120),
                            ('QUIET_HOURS', ''), ('STICKER_SPAM_CHANNEL_ID', 0)]:
            self.stack.enter_context(patch.object(config, name, value))
        self.stack.enter_context(patch('cogs.housekeeping.engagement.dead_chat_quiet_now', return_value=False))
        self.roll = self.stack.enter_context(patch('cogs.housekeeping.random.random', return_value=0))
        self.preferred = SimpleNamespace(id=20, bot=False, mention='<@20>')
        self.others = [SimpleNamespace(id=i, bot=False, mention=f'<@{i}>') for i in (21, 22)]
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id = 10
        self.channel.guild = SimpleNamespace(id=1)
        self.channel.members = [self.preferred, *self.others, SimpleNamespace(id=99, bot=False), SimpleNamespace(id=50, bot=True)]
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=100, created_at=datetime.now(timezone.utc)))
        self.cog = object.__new__(HousekeepingCog)
        self.cog.bot = SimpleNamespace(get_channel=lambda _: self.channel)

    async def asyncTearDown(self):
        await db.close()
        self.stack.close()

    async def ready(self):
        await db.record_user_activity(1, 10, 21, 'Guest')
        connection = await db.get_db()
        await connection.execute("UPDATE channel_activity SET last_channel_message_at=datetime('now', '-61 seconds') WHERE channel_id=10")
        await connection.commit()

    async def test_preferred_member_can_be_selected_again(self):
        await self.cog._record_dead_chat_target(10, 20)
        self.assertIs(await self.cog._pick_random_member(self.channel), self.preferred)

    async def test_two_in_a_row_then_rotation_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(config, 'DB_PATH', str(Path(directory) / 'targets.db')):
                for _ in range(2):
                    selected = await self.cog._pick_random_member(self.channel)
                    self.assertIs(selected, self.preferred)
                    await self.cog._record_dead_chat_target(10, selected.id)
                await db.close()
                selected = await self.cog._pick_random_member(self.channel)
                self.assertIn(selected, self.others)
                await self.cog._record_dead_chat_target(10, selected.id)
                self.assertIs(await self.cog._pick_random_member(self.channel), self.preferred)
                await db.close()

    async def test_failed_priority_roll_rotates_other_members(self):
        self.roll.return_value = 0.99
        await self.cog._record_dead_chat_target(10, 21)
        self.assertEqual((await self.cog._pick_random_member(self.channel)).id, 22)

    async def test_zero_probability_keeps_normal_random_rotation(self):
        with patch.object(config, 'DEAD_CHAT_PREFERRED_CHANCE', 0), patch('cogs.housekeeping.random.choice', return_value=self.preferred) as choose:
            self.assertIs(await self.cog._pick_random_member(self.channel), self.preferred)
        self.roll.assert_not_called()
        self.assertEqual({m.id for m in choose.call_args.args[0]}, {20, 21, 22})

    async def test_preferred_member_must_be_present_and_not_owner_or_bot(self):
        for user_id in [999, 99, 50]:
            with self.subTest(user_id=user_id), patch.object(config, 'DEAD_CHAT_PREFERRED_USER_ID', user_id):
                selected = await self.cog._pick_random_member(self.channel)
                self.assertIn(selected.id, {20, 21, 22})
        self.roll.assert_not_called()

    async def test_preferred_member_can_stop_pings_or_roasting(self):
        for key in ['pings', 'roasting']:
            preferences = {'pings': True, 'roasting': True}
            preferences[key] = False
            await db.set_member_preferences(1, 20, preferences)
            self.assertIn(await self.cog._pick_random_member(self.channel), self.others)

    async def test_single_eligible_preferred_member_is_still_usable(self):
        self.channel.members = [self.preferred]
        for _ in range(3):
            await self.cog._record_dead_chat_target(10, 20)
        self.assertIs(await self.cog._pick_random_member(self.channel), self.preferred)
        await db.set_member_preferences(1, 20, {'pings': False})
        self.assertIsNone(await self.cog._pick_random_member(self.channel))

    async def test_preferred_delivery_gets_savage_prompt_and_exactly_one_ping(self):
        await self.ready()
        with patch('cogs.housekeeping.generate_dead_chat_line', AsyncMock(return_value='ded chat. Even your drafts are lurking. Defend the take.')) as generate:
            await self.cog._maybe_nudge_dead_chat()
        generate.assert_awaited_once_with(None, 0, savage=True)
        self.assertTrue(self.channel.send.await_args.args[0].startswith('<@20> '))
        mentions = self.channel.send.await_args.kwargs['allowed_mentions']
        self.assertEqual([user.id for user in mentions.users], [20])
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)
        self.assertEqual(await db.get_state('dead_chat_target_streak:10'), '1')
        row = (await db.get_channel_idle_candidates([10]))[0]
        self.assertEqual(dead_chat_delay_seconds(row), 120)

    async def test_other_members_keep_the_normal_roast_prompt(self):
        self.roll.return_value = 0.99
        await self.ready()
        with patch('cogs.housekeeping.generate_dead_chat_line', AsyncMock(return_value='ded chat. Give your keyboard something to do.')) as generate:
            await self.cog._maybe_nudge_dead_chat()
        self.assertFalse(generate.await_args.kwargs['savage'])

    async def test_opt_out_during_generation_prevents_delivery(self):
        await self.ready()

        async def generate(*args, **kwargs):
            await db.set_member_preferences(1, 20, {'pings': False})
            return 'ded chat. A fresh roast.'

        with patch('cogs.housekeeping.generate_dead_chat_line', side_effect=generate):
            await self.cog._maybe_nudge_dead_chat()
        self.channel.send.assert_not_awaited()
        self.assertEqual((await db.get_channel_idle_candidates([10]))[0]['dead_ping_count'], 0)
        self.assertIsNone(await db.get_state('dead_chat_target_streak:10'))

    async def test_shared_two_minute_cooldown_does_not_override_one_minute_dead_chat(self):
        await self.ready()
        await db.set_state('autonomous_last:10', str(time.time() - 70))
        self.assertFalse(await engagement.can_post(10))
        with patch('cogs.housekeeping.generate_dead_chat_line', AsyncMock(return_value='ded chat. Your keyboard needs a shift.')):
            await self.cog._maybe_nudge_dead_chat()
        self.channel.send.assert_awaited_once()
        self.assertFalse(await engagement.can_post(10))


class SavagePromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_savage_style_is_generated_and_does_not_pass_target_identity_to_model(self):
        with patch('cogs.housekeeping.llm.generate_visible_response', AsyncMock(return_value='ded chat. That comeback is still in its loading screen. Drop a take.')) as model:
            text = await generate_dead_chat_line(None, 0, savage=True)
        prompt = ' '.join(m['content'] for m in model.await_args.args[0])
        self.assertIn('ASKED for savage playful roasting', prompt)
        self.assertIn('No apologies', prompt)
        self.assertIn('protected traits', prompt)
        self.assertIn('never copy them', prompt)
        self.assertNotIn('1423982225938190346', prompt)
        self.assertIn('loading screen', text)

    async def test_normal_style_does_not_claim_the_member_requested_savage_roasts(self):
        with patch('cogs.housekeeping.llm.generate_visible_response', AsyncMock(return_value='ded chat. Lurking overtime again? Give us a topic.')) as model:
            await generate_dead_chat_line(None, 0)
        self.assertNotIn('ASKED for savage', ' '.join(m['content'] for m in model.await_args.args[0]))
