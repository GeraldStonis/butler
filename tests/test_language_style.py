import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services import llm, social
from services.response_text import VISIBLE_REPLY_POLICY, allows_native_script, has_non_latin_prose, reply_language_hint


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


class MultilingualResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_assamese_cues_are_turn_specific_and_do_not_override_an_explicit_language(self):
        request = "nixi \u09a4\u09c1\u09ae\u09bf \u0995\u09c7\u09a8\u09c7 \u0986\u099b\u09be?"
        self.assertIn("Assamese, NOT Bengali", reply_language_hint(request))
        self.assertIn("TARGET: english", reply_language_hint(request + " Reply in English."))
        self.assertEqual(reply_language_hint("Explain this phrase: '" + request + "' in simple terms."), "")

    async def test_roman_marathi_cues_request_marathi_not_hindi_or_native_script(self):
        request = "nixi tu kasa ahes? Marathi madhe bol."
        self.assertIn("Romanized Marathi", reply_language_hint(request))
        self.assertFalse(allows_native_script(request))
        self.assertIn("TARGET: english", reply_language_hint(request + " Reply in English."))

    async def test_script_repair_transforms_the_draft_not_the_users_question(self):
        import json
        draft = "\u092e\u0940 \u092e\u0938\u094d\u0924 \u0906\u0939\u0947."
        context = [{"role": "system", "content": "A long persona unrelated to transliteration."},
                   {"role": "user", "content": "nixi tu kasa ahes?"}]
        with patch.object(llm, "generate_response", AsyncMock(side_effect=[draft, "Mi mast aahe."])) as model:
            self.assertEqual(await llm.generate_visible_response(context), "Mi mast aahe.")
        repair = model.await_args.args[0]
        self.assertEqual(json.loads(repair[-1]["content"]), {"draft_to_transliterate": draft})
        self.assertFalse(any("long persona" in m["content"] for m in repair))

    async def test_default_policy_is_hinglish_but_clear_member_languages_take_priority(self):
        self.assertIn("Default predominantly to natural Roman-script Hinglish", VISIBLE_REPLY_POLICY)
        self.assertIn("Assamese gets Assamese", VISIBLE_REPLY_POLICY)
        self.assertIn("Marathi gets Marathi", VISIBLE_REPLY_POLICY)
        self.assertIn("English gets English", VISIBLE_REPLY_POLICY)
        self.assertIn("Assamese is not Bengali", VISIBLE_REPLY_POLICY)
        self.assertIn("Marathi is not Hindi", VISIBLE_REPLY_POLICY)
        self.assertIn("exact-word requests override the default", VISIBLE_REPLY_POLICY)

    async def test_native_marathi_and_assamese_are_not_repaired_away(self):
        examples = [
            ("nixi \u0924\u0942 \u0915\u0938\u093e \u0906\u0939\u0947\u0938?", "\u092e\u0940 \u092e\u0938\u094d\u0924 \u0906\u0939\u0947!"),
            ("nixi \u09a4\u09c1\u09ae\u09bf \u0995\u09c7\u09a8\u09c7 \u0986\u099b\u09be?", "\u09ae\u0987 \u09ad\u09be\u09b2\u09c7 \u0986\u099b\u09cb\u0981\u0964"),
        ]
        for request, reply in examples:
            with self.subTest(request=request), patch.object(llm, "generate_response", AsyncMock(return_value=reply)) as model:
                result = await llm.generate_visible_response([{"role": "user", "content": request}])
                self.assertEqual(result, reply)
                model.assert_awaited_once()

    async def test_english_requests_for_a_native_language_allow_native_output(self):
        for language, reply in [("Marathi", "\u0928\u092e\u0938\u094d\u0915\u093e\u0930"),
                                ("Assamese", "\u09a8\u09ae\u09b8\u09cd\u0995\u09be\u09f0")]:
            request = f"nixi reply in {language}"
            with patch.object(llm, "generate_response", AsyncMock(return_value=reply)) as model:
                self.assertEqual(await llm.generate_visible_response([{"role": "user", "content": request}]), reply)
                model.assert_awaited_once()

    async def test_romanized_marathi_stays_marathi_during_script_repair(self):
        draft = "\u092e\u0940 \u092e\u0938\u094d\u0924 \u0906\u0939\u0947!"
        with patch.object(llm, "generate_response", AsyncMock(side_effect=[draft, "Mi mast aahe!"])) as model:
            reply = await llm.generate_visible_response([{"role": "user", "content": "nixi tu kasa ahes?"}])
        self.assertEqual(reply, "Mi mast aahe!")
        self.assertEqual(model.await_count, 2)
        self.assertIn("keep the speaker's LANGUAGE", model.await_args.args[0][-2]["content"])
        self.assertIn("Marathi remains Marathi, not Hindi", model.await_args.args[0][-2]["content"])

    async def test_roman_script_request_overrides_native_source_and_target_language(self):
        request = "\u0928\u092e\u0938\u094d\u0915\u093e\u0930. Reply in Marathi using Roman letters."
        self.assertFalse(allows_native_script(request))
        self.assertFalse(allows_native_script("Reply in Assamese using English letters"))
        self.assertFalse(allows_native_script("Reply in Marathi, use Roman."))

    async def test_last_explicit_translation_target_is_respected(self):
        self.assertTrue(allows_native_script("Translate this text in English into Marathi"))
        self.assertFalse(allows_native_script("Translate \u0928\u092e\u0938\u094d\u0915\u093e\u0930 into English"))
        self.assertFalse(allows_native_script("Answer in Hinglish: \u0928\u092e\u0938\u094d\u0915\u093e\u0930"))
        self.assertTrue(allows_native_script("Speak Japanese"))

    async def test_older_members_native_text_does_not_change_the_current_member(self):
        context = [{"role": "user", "content": "\u0928\u092e\u0938\u094d\u0915\u093e\u0930"},
                   {"role": "assistant", "content": "\u0928\u092e\u0938\u094d\u0915\u093e\u0930"},
                   {"role": "user", "content": "bhai ab kya?"}]
        with patch.object(llm, "generate_response", AsyncMock(side_effect=["\u0905\u092c bas", "ab bas"])) as model:
            self.assertEqual(await llm.generate_visible_response(context), "ab bas")
        self.assertEqual(model.await_count, 2)

    async def test_metadata_does_not_override_explicit_empty_language_source(self):
        context = [{"role": "user", "content": '{"previous_nudge":"\u0928\u092e\u0938\u094d\u0915\u093e\u0930"}'}]
        with patch.object(llm, "generate_response", AsyncMock(side_effect=["\u0905\u092c bas", "ab kuch toh bolo"])) as model:
            self.assertEqual(await llm.generate_visible_response(context, language_request=""), "ab kuch toh bolo")
        self.assertEqual(model.await_count, 2)
        first_prompt = model.await_args_list[0].args[0]
        self.assertTrue(any("There is no current human language sample" in m["content"] for m in first_prompt))

    async def test_discreet_asides_allow_the_actual_members_native_script(self):
        from services.conversation import discreet_reply
        reply = "\u092e\u0940 \u0907\u0925\u0947\u091a \u0906\u0939\u0947."
        with patch.object(llm, "generate_response", AsyncMock(return_value=reply)) as model:
            self.assertEqual(await discreet_reply("nixi \u0928\u092e\u0938\u094d\u0915\u093e\u0930"), reply)
        model.assert_awaited_once()

    async def test_search_result_language_cannot_override_the_original_member(self):
        from services.search import SearchItem, summarize_results
        result = SearchItem(title="English result", url="https://example.test", description="English source text.")
        request = "\u092e\u0932\u093e \u092e\u0930\u093e\u0920\u0940\u0924 \u0938\u093e\u0902\u0917"
        reply = "\u0939\u0947 \u0924\u0941\u091d\u0947 \u0909\u0924\u094d\u0924\u0930 \u0906\u0939\u0947."
        with patch.object(llm, "generate_response", AsyncMock(return_value=reply)) as model:
            self.assertEqual(await summarize_results("an English query", [result], language_request=request), reply)
        model.assert_awaited_once()
        prompt = model.await_args.args[0]
        self.assertTrue(any(request in m["content"] and "CURRENT HUMAN" in m["content"] for m in prompt))

    async def test_native_search_snippets_do_not_make_an_english_request_native(self):
        from services.search import SearchItem, summarize_results
        result = SearchItem(title="\u0928\u092e\u0938\u094d\u0915\u093e\u0930", url="https://example.test", description="A source")
        with patch.object(llm, "generate_response", AsyncMock(side_effect=["\u0905\u092c bas", "Here is the answer."])) as model:
            reply = await summarize_results("explain this result in English", [result])
        self.assertEqual(reply, "Here is the answer.")
        self.assertEqual(model.await_count, 2)

    async def test_code_does_not_count_as_a_request_to_switch_speech_scripts(self):
        request = 'Explain `print("\u0928\u092e\u0938\u094d\u0915\u093e\u0930")`'
        self.assertFalse(allows_native_script(request))

    async def test_existing_owner_and_guest_authority_policy_is_unchanged(self):
        from services import database as db, memory
        import config
        await db.close()
        with patch.object(config, "DB_PATH", ":memory:"):
            try:
                context = await memory.build_context(10, "nixi \u0928\u092e\u0938\u094d\u0915\u093e\u0930", "Guest", "DIRECT_ADDRESS",
                                                     guild_id=1, current_author_id=config.OWNER_ID + 1)
                authority = next(m["content"] for m in context if m["content"].startswith("TRUSTED CURRENT SPEAKER CONTEXT"))
                self.assertIn("ordinary member, not your master", authority)
                self.assertTrue(any("Native scripts are allowed" in m["content"] for m in context))
            finally:
                await db.close()
