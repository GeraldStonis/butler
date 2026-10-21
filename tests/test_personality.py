import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
from services import conversation, database as db, llm, media, memory, opinions, social


class PersonalityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.enterContext(patch.object(config, 'DB_PATH', ':memory:'))
        self.model = self.enterContext(patch.object(llm, 'generate_response',
            AsyncMock(return_value='A fresh reply suited to the moment.')))
        self.guild = SimpleNamespace(id=1, emojis=[], members=[
            SimpleNamespace(id=20, bot=False, display_name='Sam', name='sam'),
            SimpleNamespace(id=21, bot=False, display_name='Alex', name='alex'),
        ])

    async def asyncTearDown(self):
        await memory.shutdown()
        await db.close()

    async def seed(self, user_id=20, affinity=1, impression='Enjoys elaborate dry jokes.'):
        await db.record_impression_sample(1, user_id, 'A synthetic public banter exchange.', time.time())
        await db.save_member_impression(1, user_id, impression, affinity, 'PRIVATE EVIDENCE', time.time())

    async def social_rows(self, *, current_user=20, text='nixi what do you think?'):
        context = await opinions.context(1, current_user, 'Current Guest', text, [20, 21], self.guild)
        rows = json.loads(context.split('\n', 2)[1]) if context else []
        return {row['member_id']: row for row in rows}, context

    async def test_fond_rapport_adds_warmth_not_a_requirement_to_flirt(self):
        await self.seed(affinity=2)
        rows, context = await self.social_rows()
        delivery = rows[20]['suggested_delivery']
        self.assertIn('warm familiarity', delivery)
        self.assertIn('genuine amusement', delivery)
        self.assertIn('bashful', delivery)
        self.assertIn('barbs are optional', delivery)
        self.assertNotIn('PRIVATE EVIDENCE', context)
        self.model.assert_not_awaited()

    async def test_friction_can_be_sassy_without_mandatory_hostility(self):
        await self.seed(affinity=-1)
        rows, context = await self.social_rows()
        delivery = rows[20]['suggested_delivery']
        self.assertIn('theatrical exasperation', delivery)
        self.assertIn('mutual banter', delivery)
        self.assertIn('No automatic hostility', delivery)
        self.assertIn('current moment and comfort boundaries override old chemistry', context)

    async def test_neutral_rapport_is_open_to_warmth_not_a_fixed_rude_label(self):
        await self.seed(affinity=0)
        rows, _ = await self.social_rows()
        self.assertIn('open to warmth', rows[20]['suggested_delivery'])
        self.assertIn('this exchange', rows[20]['suggested_delivery'])

    async def test_no_roasting_boundary_overrides_friendly_banter_suggestion(self):
        await self.seed(affinity=2)
        await db.set_member_preferences(1, 20, {'roasting': False})
        rows, _ = await self.social_rows()
        delivery = rows[20]['suggested_delivery']
        self.assertIn('no teasing or sarcastic digs', delivery)
        self.assertNotIn('Mutual barbs', delivery)

    async def test_distinct_members_keep_their_own_chemistry(self):
        await self.seed(20, 2, 'Enjoys clever wordplay and kind exchanges.')
        await self.seed(21, -1, 'Enjoys theatrical heckling during game chat.')
        rows, context = await self.social_rows()
        self.assertIn('warm familiarity', rows[20]['suggested_delivery'])
        self.assertIn('theatrical exasperation', rows[21]['suggested_delivery'])
        self.assertIn("CURRENT speaker's own impression", context)
        self.assertIn("not a mentioned member's reputation", context)
        self.model.assert_not_awaited()

    async def test_affection_does_not_override_flirting_opt_out(self):
        await self.seed(affinity=2)
        await db.set_member_preferences(1, 20, {'flirting': False})
        context = await memory.build_context(10, 'nixi you are sweet', 'Sam', 'DIRECT_ADDRESS',
            guild_id=1, guild=self.guild, current_author_id=20)
        policy = ' '.join(item['content'] for item in context if item['role'] == 'system')
        self.assertIn('warm familiarity', policy)
        self.assertIn('This member has disabled flirting', policy)
        self.model.assert_not_awaited()

    async def test_unknown_member_is_not_assigned_an_unearned_impression(self):
        rows, _ = await self.social_rows()
        self.assertEqual(rows, {})
        context = await memory.build_context(10, 'nixi hey there', 'Sam', 'DIRECT_ADDRESS',
            guild_id=1, guild=self.guild, current_author_id=20)
        self.assertFalse(any('suggested_delivery' in item['content'] for item in context))
        self.model.assert_not_awaited()

    async def test_banter_has_range_and_initiative_in_the_existing_reply_call(self):
        content = 'nixi I spent another hour making the same avatar'
        context = [{'role': 'user', 'content': content}]
        original = list(context)
        reply = await conversation.generate_reply(context, 10, content, current_author_id=20)
        self.assertEqual(reply, self.model.return_value)
        self.model.assert_awaited_once()
        self.assertEqual(context, original)
        prompt = ' '.join(item['content'] for item in self.model.await_args.args[0] if item['role'] == 'system')
        self.assertIn('bashful sweetness', prompt)
        self.assertIn('do not wait for a roast command', prompt)
        self.assertIn('Warmth needs no flirtation or baby talk', prompt)
        self.assertIn('not a randomly assigned mood', prompt)
        self.assertIn('Do not make every reply a roast', prompt)
        self.assertIn('Recognize comic boasts as banter', prompt)
        self.assertIn('not flowery flattery', prompt)
        self.assertEqual(self.model.await_args.kwargs['temperature'], 0.9)
        messages = self.model.await_args.args[0]
        self.assertEqual(messages[-1]['content'], content)

    async def test_care_takes_priority_even_with_negative_rapport(self):
        await self.seed(affinity=-1)
        content = "nixi I'm not okay today"
        context = await memory.build_context(10, content, 'Sam', 'DIRECT_ADDRESS',
            guild_id=1, guild=self.guild, current_author_id=20)
        await conversation.generate_reply(context, 10, content, current_author_id=20)
        prompt = ' '.join(item['content'] for item in self.model.await_args.args[0] if item['role'] == 'system')
        self.assertIn('theatrical exasperation', prompt)
        self.assertIn('Current need outranks old banter', prompt)
        self.assertIn('Give a grounded, considerate answer', prompt)
        self.assertIn('No roast, jokes, memes, GIFs', prompt)
        self.assertIn('usually 2-4 short sentences', prompt)
        self.assertIn('not repeat their entire message', prompt)
        self.assertIn('detailed practical steps only when asked', prompt)
        self.assertEqual(self.model.await_args.kwargs['temperature'], 0.4)
        self.model.assert_awaited_once()

    async def test_semantic_care_instruction_is_not_limited_to_english_keywords(self):
        content = 'nixi aaj kuch bhi sahi nahi lag raha'
        await conversation.generate_reply([{'role': 'user', 'content': content}], 10, content, current_author_id=20)
        prompt = ' '.join(item['content'] for item in self.model.await_args.args[0] if item['role'] == 'system')
        self.assertIn('even if no English distress keyword appears', prompt)
        self.assertIn('drop sarcasm, roasts and flirting', prompt)
        self.model.assert_awaited_once()

    async def test_personality_does_not_add_automatic_gifs_to_care_turns(self):
        with patch.object(config, 'AUTOMATIC_MEDIA_ENABLED', True), \
                patch.object(config, 'AUTOMATIC_MEDIA_CHANCE', 1.0), \
                patch.object(llm, 'generate_response', AsyncMock()) as selector, \
                patch.object(media, 'fetch_gif', AsyncMock()) as fetch:
            self.assertIsNone(await social.AutomaticMedia().choose(10,
                'nixi mood off hai', 'Bata, kya hua?'))
        selector.assert_not_awaited()
        fetch.assert_not_awaited()


class CareCueTests(unittest.TestCase):
    def test_clear_distress_and_discomfort_are_not_playful_banter(self):
        for text in [
            "I'm not okay", 'I am actually not alright', 'I\u2019m really struggling',
            "I can't cope today", 'I had a rough day', 'please be serious for once',
            'that actually hurt my feelings', "this isn't funny anymore",
            'my dad just died', 'our friend has just died', 'mood off hai',
            'bahut bura lag raha hai', 'bohot bura lag raha h',
            'mazaak nahi kar raha', 'mazak nahi kar rahi',
        ]:
            with self.subTest(text=text):
                self.assertTrue(conversation.serious_message(text))

    def test_normal_banter_is_not_promoted_to_distress_by_loose_slang(self):
        for text in [
            'I died laughing', 'ded chat', 'my rank is cooked lol', 'roast me nixi',
            'I hate you lol', 'this beat is sick', 'my dad jokes are legendary',
            'I am not upset',
        ]:
            with self.subTest(text=text):
                self.assertFalse(conversation.serious_message(text))
