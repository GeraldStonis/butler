import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import config
from cogs.chat import ChatCog
from cogs.housekeeping import HousekeepingCog
from services import conversation, database as db, engagement, intent, llm, memory, social


class CompanionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.path = patch.object(config, 'DB_PATH', ':memory:')
        self.path.start()
        await db.get_db()

    async def asyncTearDown(self):
        await memory.shutdown()
        await db.close()
        self.path.stop()

    async def test_context_attributes_other_bots_and_includes_current_message_once(self):
        await db.save_message(1, 2, 90, 'AnotherBot', 'A wild rare thing appeared', True)
        await db.save_message(1, 2, 99, 'Nixi', 'A real previous reply', True)
        await db.save_message(1, 2, 3, 'Guest', 'nixi hello')
        context = await memory.build_context(1, 'nixi hello', 'Guest', 'DIRECT_ADDRESS', current_author_id=3, nixi_author_id=99)
        data = [json.loads(m['content']) for m in context if m['content'].startswith('{"speaker"')]
        self.assertEqual(json.loads(context[-1]['content'])['text'], 'nixi hello')
        self.assertFalse(any(row['id'] == 3 and row['text'] == 'nixi hello' for row in data))
        assistants = [m for m in context if m['role'] == 'assistant']
        self.assertEqual(len(assistants), 1)
        self.assertIn('A real previous reply', assistants[0]['content'])

    async def test_history_is_bounded_by_character_budget(self):
        for i in range(10):
            await db.save_message(1, 2, 3, 'Guest', f'{i} ' + 'x' * 1400)
        with patch.object(config, 'CONTEXT_CHAR_BUDGET', 2000):
            context = await memory.build_context(1, 'current', 'Guest', 'DIRECT_ADDRESS', current_author_id=3)
        historical = [m for m in context if m['content'].startswith('{"speaker"')]
        self.assertEqual(len(historical), 1)

    async def test_owner_notes_are_not_disclosed_to_guests(self):
        await db.save_member_fact(2, config.OWNER_ID, 'interests', 'Private preference', 'I prefer privacy')
        owner = SimpleNamespace(id=config.OWNER_ID, bot=False, display_name='Ronix', name='ronix')
        guild = SimpleNamespace(members=[owner], get_member=lambda _: owner, emojis=[])
        context = await memory.build_context(1, 'what does Ronix like?', 'Guest', 'DIRECT_ADDRESS', guild_id=2, guild=guild, current_author_id=config.OWNER_ID + 1)
        self.assertFalse(any('Private preference' in m['content'] for m in context))

    async def test_repeat_guard_regenerates_once_and_rejects_recycled_result(self):
        repeated = 'Your confidence has outpaced the available evidence again.'
        await db.save_message(1, 2, 99, 'Nixi', repeated, True)
        context = [{'role': 'user', 'content': 'banter'}]
        with patch.object(llm, 'generate_response', AsyncMock(side_effect=[repeated, 'That take needs a warranty.'])) as model:
            self.assertEqual(await conversation.generate_reply(context, 1, 'I am always right'), 'That take needs a warranty.')
            self.assertEqual(model.await_count, 2)
        with patch.object(llm, 'generate_response', AsyncMock(return_value=repeated)) as model:
            self.assertEqual(await conversation.generate_reply(context, 1, 'I am always right'), '')
            self.assertEqual(model.await_count, 2)

    async def test_useful_explanations_are_not_rewritten_just_for_novelty(self):
        reply = 'Create the environment with python -m venv .venv.'
        await db.save_message(1, 2, 99, 'Nixi', reply, True)
        with patch.object(llm, 'generate_response', AsyncMock(return_value=reply)) as model:
            self.assertEqual(await conversation.generate_reply([{'role': 'user', 'content': 'Q'}], 1, 'how do I create a venv?'), reply)
            model.assert_awaited_once()

    async def test_member_boundaries_are_id_scoped_and_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            await db.close()
            with patch.object(config, 'DB_PATH', str(Path(directory) / 'prefs.db')):
                await db.set_member_preferences(1, 3, {'roasting': False, 'media': False, 'pings': False})
                await db.close()
                self.assertFalse((await db.get_member_preferences(1, 3))['roasting'])
                self.assertTrue((await db.get_member_preferences(2, 3))['roasting'])
                self.assertTrue((await db.get_member_preferences(1, 4))['roasting'])
                await db.close()

    def test_boundary_requests_are_not_guest_errands(self):
        for text, key in [("nixi don't roast me", 'roasting'), ('nixi stop pinging me', 'pings'), ("nixi don't send me gifs", 'media'), ('nixi no gifs please', 'media')]:
            self.assertFalse(social.preference_updates(text)[key])
            self.assertEqual(social.guest_kind_hint(text), 'banter')
        self.assertEqual(social.preference_updates('nixi send me a gif'), {})
        self.assertTrue(social.preference_updates('you can roast me again')['roasting'])

    def test_explanations_and_game_discussions_are_not_errands(self):
        self.assertEqual(social.guest_kind_hint('nixi explain recursion'), 'question')
        self.assertIsNone(intent.heuristic_intent('nixi how does higher or lower work?'))

    async def test_followups_are_same_person_short_window_and_bounded(self):
        bot = SimpleNamespace(user=SimpleNamespace(id=99))
        cog = ChatCog(bot)
        def msg(user_id, text):
            return SimpleNamespace(content=text, author=SimpleNamespace(id=user_id), mentions=[], reference=None,
                                   guild=SimpleNamespace(id=2), channel=SimpleNamespace(id=1))
        cog._followups[1] = (3, time.monotonic() + 60, 2)
        self.assertEqual(await cog._detect_mode(msg(3, 'why though?')), 'REPLY_CHAIN')
        self.assertEqual(await cog._detect_mode(msg(3, 'yeah but what about that?')), 'REPLY_CHAIN')
        self.assertIsNone(await cog._detect_mode(msg(3, 'so really?')))
        cog._followups[1] = (3, time.monotonic() + 60, 2)
        self.assertIsNone(await cog._detect_mode(msg(4, 'why though?')))
        self.assertNotIn(1, cog._followups)

    async def test_direct_address_takes_priority_over_owner_mention(self):
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=99)))
        message = SimpleNamespace(content='nixi what do you think about ronix?', author=SimpleNamespace(id=config.OWNER_ID + 1), mentions=[], reference=None, channel=SimpleNamespace(id=1), guild=SimpleNamespace(id=2))
        with patch.object(db, 'get_user_idle_seconds', AsyncMock(return_value=9999)) as idle:
            self.assertEqual(await cog._detect_mode(message), 'DIRECT_ADDRESS')
            idle.assert_not_awaited()

    async def test_unknown_owner_presence_does_not_trigger_away_claim(self):
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=99)))
        message = SimpleNamespace(content='where is ronix?', author=SimpleNamespace(id=config.OWNER_ID + 1), mentions=[], reference=None, channel=SimpleNamespace(id=1), guild=SimpleNamespace(id=2))
        with patch.object(db, 'get_user_idle_seconds', AsyncMock(return_value=None)):
            self.assertIsNone(await cog._detect_mode(message))

    async def test_away_reply_cooldown_survives_state(self):
        cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=99)))
        message = SimpleNamespace(content='where is ronix?', author=SimpleNamespace(id=config.OWNER_ID + 1), mentions=[], reference=None, channel=SimpleNamespace(id=1), guild=SimpleNamespace(id=2))
        await db.set_state('owner_away_reply:1', str(time.time()))
        with patch.object(db, 'get_user_idle_seconds', AsyncMock(return_value=9999)):
            self.assertIsNone(await cog._detect_mode(message))

    async def test_failing_summary_does_not_destroy_recent_memory(self):
        await db.save_message(1, 2, 3, 'Guest', 'Keep the evidence')
        with patch.object(llm, 'generate_response', AsyncMock(return_value='')):
            await memory._summarise_oldest(1, 2)
        self.assertEqual(len(await db.get_recent_messages(1)), 1)
        self.assertIsNone(await db.get_latest_summary(1))

    async def test_cancelled_summary_cannot_repopulate_reset_channel(self):
        with patch.object(config, 'CONTEXT_WINDOW_SIZE', 0), patch.object(config, 'SUMMARY_THRESHOLD', 0):
            async with memory.channel_lock(1):
                await memory.log_message(1, 2, 3, 'Guest', 'Old topic')
                await memory.cancel_summary(1)
                await db.clear_channel_memory(1)
        self.assertNotIn(1, memory._summary_tasks)
        self.assertEqual(await db.get_recent_messages(1), [])

    async def test_slow_summary_does_not_hold_the_conversation_lock(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def summarize(*args, **kwargs):
            started.set()
            await release.wait()
            return 'A valid concise summary'
        with patch.object(config, 'CONTEXT_WINDOW_SIZE', 0), patch.object(config, 'SUMMARY_THRESHOLD', 1), patch.object(llm, 'generate_response', side_effect=summarize):
            await memory.log_message(1, 2, 3, 'Guest', 'First')
            await memory.log_message(1, 2, 3, 'Guest', 'Second')
            await asyncio.wait_for(started.wait(), 1)
            task = memory._summary_tasks[1]
            await asyncio.wait_for(memory.channel_lock(1).acquire(), 0.1)
            memory.channel_lock(1).release()
            release.set()
            await task

    async def test_stale_summary_version_cannot_restore_cleared_memory(self):
        await db.save_message(1, 2, 3, 'Guest', 'Old topic')
        old_version = memory._summary_versions.get(1, 0)
        memory.invalidate_summary(1)
        with patch.object(llm, 'generate_response', AsyncMock(return_value='A stale summary')):
            await memory._summarise_oldest(1, 2, version=old_version)
        self.assertIsNone(await db.get_latest_summary(1))

    async def test_targets_respect_opt_outs_and_vary_when_possible(self):
        channel = SimpleNamespace(id=1, guild=SimpleNamespace(id=2), members=[SimpleNamespace(id=3, bot=False), SimpleNamespace(id=4, bot=False), SimpleNamespace(id=5, bot=False)])
        await db.record_user_activity(2, 1, 3, 'A')
        await db.record_user_activity(2, 1, 4, 'B')
        await db.record_user_activity(2, 1, 5, 'C')
        await db.set_member_preferences(2, 4, {'pings': False})
        await db.set_state('dead_chat_target:1', '3')
        cog = object.__new__(HousekeepingCog)
        self.assertEqual((await cog._pick_random_member(channel)).id, 5)

    async def test_single_eligible_member_still_gets_old_style_ping(self):
        member = SimpleNamespace(id=config.OWNER_ID + 1, bot=False)
        channel = SimpleNamespace(id=1, guild=SimpleNamespace(id=2), members=[member])
        # Random pings do not require recent participation or a different target.
        await db.set_state('dead_chat_target:1', str(member.id))
        cog = object.__new__(HousekeepingCog)
        self.assertIs(await cog._pick_random_member(channel), member)
        await db.set_member_preferences(2, member.id, {'pings': False})
        self.assertIsNone(await cog._pick_random_member(channel))

    async def test_shared_autonomous_gap_is_persistent(self):
        with patch.object(config, 'QUIET_HOURS', ''), patch.object(config, 'AUTONOMOUS_MIN_GAP_SECONDS', 120):
            self.assertTrue(await engagement.can_post(1))
            await engagement.mark_sent(1)
            self.assertFalse(await engagement.can_post(1))
            self.assertTrue(await engagement.can_post(2))

    def test_quiet_hours_cross_midnight_and_invalid_settings_fail_closed(self):
        with patch.object(config, 'QUIET_HOURS', '23:00-08:00'):
            self.assertTrue(engagement.quiet_now(datetime(2026, 10, 14, 0, 30)))
            self.assertTrue(engagement.quiet_now(datetime(2026, 10, 14, 23, 30)))
            self.assertFalse(engagement.quiet_now(datetime(2026, 10, 14, 14, 0)))
        with patch.object(config, 'QUIET_HOURS', 'broken'), self.assertLogs('nixi.engagement', level='WARNING'):
            self.assertTrue(engagement.quiet_now())

    async def test_database_initialization_is_singleton_under_concurrent_calls(self):
        await db.close()
        connections = await asyncio.gather(*(db.get_db() for _ in range(5)))
        self.assertTrue(all(c is connections[0] for c in connections))

    def test_missing_credentials_are_reported_without_disclosing_values(self):
        with patch.object(config, 'DISCORD_TOKEN', ''), patch.object(config, 'OWNER_ID', 0), patch.object(config, 'OLLAMA_API_KEY', ''), patch.object(config, 'OLLAMA_HOST', 'https://ollama.com'):
            with self.assertRaises(ValueError) as raised:
                config.validate_runtime()
        self.assertIn('DISCORD_TOKEN', str(raised.exception))
        self.assertIn('OWNER_ID', str(raised.exception))
        self.assertIn('OLLAMA_API_KEY', str(raised.exception))

    async def test_setup_hook_registers_extensions_before_gateway_events(self):
        with patch('logging.basicConfig'):
            from bot import NixiBot
        bot = NixiBot(command_prefix='!', intents=discord.Intents.none(), owner_id=1)
        async with bot:
            with patch.object(bot, 'load_extension', AsyncMock()) as load:
                await bot.setup_hook()
                self.assertEqual([call.args[0] for call in load.await_args_list],
                                 ['cogs.activities', 'cogs.chat', 'cogs.commands', 'cogs.watcher', 'cogs.housekeeping'])
        self.assertEqual(len(bot.cogs), 0)


class LlmReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_thinking_is_model_specific_and_empty_response_retries_once(self):
        client = SimpleNamespace(chat=AsyncMock(side_effect=[SimpleNamespace(message=SimpleNamespace(content='')), SimpleNamespace(message=SimpleNamespace(content='Nixi: Fresh answer.'))]))
        with patch.object(llm, '_get_client', return_value=client), patch.object(config, 'OLLAMA_MODEL', 'gpt-oss:120b'), patch.object(config, 'OLLAMA_THINKING_LEVEL', 'low'):
            self.assertEqual(await llm.generate_response([{'role': 'user', 'content': 'hello'}]), 'Fresh answer.')
        self.assertEqual(client.chat.call_args.kwargs['think'], 'low')
        self.assertGreaterEqual(client.chat.call_args.kwargs['options']['num_predict'], 1200)
        client.chat = AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(content='Fine.')))
        with patch.object(llm, '_get_client', return_value=client), patch.object(config, 'OLLAMA_MODEL', 'another-model'):
            await llm.generate_response([{'role': 'user', 'content': 'hello'}])
        self.assertNotIn('think', client.chat.call_args.kwargs)

    async def test_timeout_is_bounded_and_does_not_expose_provider_error(self):
        async def slow(**kwargs):
            await asyncio.sleep(1)
        with patch.object(llm, '_get_client', return_value=SimpleNamespace(chat=slow)), patch.object(config, 'LLM_TIMEOUT_SECONDS', 0.01), self.assertLogs('nixi.llm', level='WARNING'):
            self.assertEqual(await llm.generate_response([{'role': 'user', 'content': 'hello'}], failure_reply=''), '')

    def test_cloud_json_parser_accepts_fences_but_not_invalid_output(self):
        self.assertEqual(llm.parse_json('```json\n{"kind":"gif"}\n```'), {'kind': 'gif'})
        with self.assertRaises(ValueError):
            llm.parse_json('not json')

    async def test_speaker_envelopes_are_not_delivered_to_discord(self):
        payload = json.dumps({'speaker': 'Nixi', 'id': 99, 'text': 'A proper visible answer.'})
        client = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(content=payload))))
        with patch.object(llm, '_get_client', return_value=client):
            self.assertEqual(await llm.generate_response([{'role': 'user', 'content': 'hello'}]), 'A proper visible answer.')
