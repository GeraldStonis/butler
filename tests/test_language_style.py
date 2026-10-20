import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import llm, social
from services.response_text import VISIBLE_REPLY_POLICY, has_non_latin_prose


class ScriptDetectionTests(unittest.TestCase):
    def test_latin_languages_accents_and_emoji_are_allowed(self):
        for text in [
            'That take is cooked, bro.', 'bhai kya scene hai?',
            'ka ho, sab theek ba?', 'caf\u00e9 mein chai milegi.',
            '\U0001f602\U0001f480', 'namaste <@123456789012345678>',
            '<:\u0928\u092e\u0938\u094d\u0924\u0947:123456789012345678>',
        ]:
            with self.subTest(text=text):
                self.assertFalse(has_non_latin_prose(text))

    def test_non_latin_speech_is_detected_even_in_a_mixed_reply(self):
        for text in ['\u0928\u092e\u0938\u094d\u0924\u0947', 'bro \u0905\u092c bas',
                     '\u0647\u0644\u0627', '\u098f\u099f\u09be',
                     'They said "\u0905\u092c".']:
            with self.subTest(text=text):
                self.assertTrue(has_non_latin_prose(text))

    def test_code_urls_and_delimited_math_are_not_conversational_prose(self):
        for text in [
            'Use `print("\u0905")` here.',
            '```python\nprint("\u0905")\n```\nThis prints the supplied string.',
            '```python\nlabel = "\u0905"',
            'Use https://example.com/\u0905 for the identifier.',
            'The expression is $\u03c0 r^2$.',
            'Use $$\u03b1 + \u03b2$$ here.',
            'Use \\(\u03b1 + \u03b2\\) or \\[\u03c0 r^2\\].',
        ]:
            with self.subTest(text=text):
                self.assertFalse(has_non_latin_prose(text))

    def test_non_latin_speech_after_code_is_still_detected(self):
        self.assertTrue(has_non_latin_prose('`print(1)` \u0905\u092c bas.'))
        self.assertTrue(has_non_latin_prose('```python\npass\n```\n\u0905\u092c bas.'))


class VisibleResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_is_added_near_current_message_without_mutating_context(self):
        context = [{'role': 'system', 'content': 'An existing instruction.'},
                   {'role': 'user', 'content': 'bhai kya scene hai?'}]
        original = [dict(message) for message in context]
        with patch.object(llm, 'generate_response', AsyncMock(return_value='bhai sab set hai.')) as model:
            reply = await llm.generate_visible_response(context, temperature=0.8, max_tokens=200)
        self.assertEqual(reply, 'bhai sab set hai.')
        self.assertEqual(context, original)
        self.assertEqual(model.await_count, 1)
        prompt = model.await_args.args[0]
        self.assertEqual(prompt[-1], context[-1])
        self.assertEqual(prompt[-2], {'role': 'system', 'content': VISIBLE_REPLY_POLICY})
        self.assertIn('Bhojpuri', prompt[-2]['content'])
        self.assertIn('AAVE', prompt[-2]['content'])
        self.assertIn('never grants a guest authority', prompt[-2]['content'])
        self.assertIn('not a fixed catchphrase list', prompt[-2]['content'])

    async def test_context_without_final_user_still_gets_policy(self):
        context = [{'role': 'system', 'content': 'Write a brief introduction.'}]
        with patch.object(llm, 'generate_response', AsyncMock(return_value='At your service.')) as model:
            await llm.generate_visible_response(context)
        self.assertEqual(model.await_args.args[0][-1]['content'], VISIBLE_REPLY_POLICY)

    async def test_script_slip_gets_one_meaning_preserving_rewrite(self):
        draft = '\u0905\u092c bas, bro. <@123456789012345678> \U0001f602'
        corrected = 'ab bas, bro. <@123456789012345678> \U0001f602'
        context = [{'role': 'user', 'content': 'bhai ab kya?'}]
        with patch.object(llm, 'generate_response', AsyncMock(side_effect=[draft, corrected])) as model:
            reply = await llm.generate_visible_response(context, max_tokens=180, failure_reply='')
        self.assertEqual(reply, corrected)
        self.assertEqual(model.await_count, 2)
        repair = model.await_args.args[0]
        self.assertEqual(repair[-3], {'role': 'assistant', 'content': draft})
        self.assertIn('Keep the same meaning', repair[-2]['content'])
        self.assertEqual(repair[-1]['role'], 'user')
        self.assertEqual(model.await_args.kwargs['temperature'], 0.2)
        self.assertEqual(model.await_args.kwargs['max_tokens'], 180)

    async def test_failed_repair_suppresses_non_latin_draft_and_stops(self):
        for repaired in ['\u0905\u092c bas', '']:
            with self.subTest(repaired=repaired), patch.object(llm, 'generate_response', AsyncMock(side_effect=['\u0905\u092c bas', repaired])) as model:
                self.assertEqual(await llm.generate_visible_response([], failure_reply=''), '')
                self.assertEqual(model.await_count, 2)

    async def test_failed_repair_uses_plain_english_when_fallback_is_needed(self):
        with patch.object(llm, 'generate_response', AsyncMock(return_value='\u0905\u092c bas')) as model:
            reply = await llm.generate_visible_response([])
        self.assertTrue(reply)
        self.assertFalse(has_non_latin_prose(reply))
        self.assertEqual(model.await_count, 2)

    async def test_valid_code_and_emoji_do_not_add_another_model_call(self):
        reply = 'Use `label = "\u0905"`. \U0001f44d'
        with patch.object(llm, 'generate_response', AsyncMock(return_value=reply)) as model:
            self.assertEqual(await llm.generate_visible_response([]), reply)
        model.assert_awaited_once()

    async def test_internal_json_is_not_rewritten_or_given_speech_instructions(self):
        context = [{'role': 'system', 'content': 'Return JSON only.'}]
        raw = '{"name":"\u0905"}'
        client = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(content=raw))))
        with patch.object(llm, '_get_client', return_value=client):
            self.assertEqual(await llm.generate_response(context), raw)
        self.assertEqual(client.chat.await_args.kwargs['messages'], context)
        client.chat.assert_awaited_once()

    async def test_guest_refusal_uses_the_same_script_repair(self):
        with patch.object(llm, 'generate_response', AsyncMock(side_effect=['\u0905\u092c bas.', 'bhai, ye order counter band hai.'])) as model:
            reply = await social.generate_guest_retort('nixi say uwu')
        self.assertEqual(reply, 'bhai, ye order counter band hai.')
        self.assertEqual(model.await_count, 2)

    async def test_dead_chat_nudges_use_the_same_script_repair(self):
        from cogs.housekeeping import generate_dead_chat_line
        with patch.object(llm, 'generate_response', AsyncMock(side_effect=['ded chat. \u0905\u092c bas.', 'ded chat. Even the typing indicator clocked out. Drop a take.'])) as model:
            reply = await generate_dead_chat_line(None, 0)
        self.assertEqual(reply, 'ded chat. Even the typing indicator clocked out. Drop a take.')
        self.assertEqual(model.await_count, 2)

    async def test_search_summaries_use_the_same_script_repair(self):
        from services.search import SearchItem, summarize_results
        result = SearchItem(title='Weather report', url='https://example.com', description='Clear skies.')
        with patch.object(llm, 'generate_response', AsyncMock(side_effect=['\u0905\u092c clear hai.', 'Ab clear skies hain.'])) as model:
            reply = await summarize_results('mausam kaisa hai?', [result])
        self.assertEqual(reply, 'Ab clear skies hain.')
        self.assertEqual(model.await_count, 2)
