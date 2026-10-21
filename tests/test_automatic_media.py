import json
import unittest
from unittest.mock import AsyncMock, patch

import config
from services import media, social


class AutomaticMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for name, value in {
            'AUTOMATIC_MEDIA_ENABLED': True,
            'AUTOMATIC_MEDIA_CHANCE': 0.15,
            'AUTOMATIC_MEDIA_COOLDOWN_SECONDS': 300,
        }.items():
            self.enterContext(patch.object(config, name, value))
        self.random = self.enterContext(patch('services.social.random.random', return_value=0))
        self.clock = self.enterContext(patch('services.social.time')).monotonic
        self.clock.return_value = 1000
        self.decision = {
            'kind': 'gif', 'sentiment': 'skeptical amusement',
            'adds_punchline': True,
            'visual_punchline': 'The detective searches an empty folder for proof of the boast.',
            'gif_queries': ['detective empty folder'], 'delivery': 'with_text',
        }
        self.model = self.enterContext(patch.object(social.llm, 'generate_response',
            AsyncMock(return_value=json.dumps(self.decision))))
        self.result = media.MediaResult('No evidence', 'https://images.test/evidence.gif')
        self.gif = self.enterContext(patch.object(media, 'fetch_gif', AsyncMock(return_value=self.result)))
        self.meme = self.enterContext(patch.object(media, 'fetch_meme', AsyncMock()))
        self.cat = self.enterContext(patch.object(media, 'fetch_cat', AsyncMock()))
        self.captioned = self.enterContext(patch.object(media, 'fetch_captioned_gif', AsyncMock()))
        self.service = social.AutomaticMedia()
        self.content = 'I have never made a mistake in my life'
        self.reply = 'The evidence seems to have left the building.'

    async def test_obvious_text_only_turns_skip_model_fetch_and_cooldown(self):
        cases = [
            ('hi nixi', 'Hey!'),
            ('thanks nixi', 'Anytime.'),
            ('okay', 'Done.'),
            ('lol', 'That was good.'),
            ('explain recursion', 'A function calls itself.'),
            ('how do I deploy this?', 'Start with the service settings.'),
            ('I am depressed', 'I am here to listen.'),
            ('I need to talk', 'I am sorry for your grief.'),
            ('say good morning', 'good morning'),
            ('help with this code', '```python\nprint(1)\n```'),
            ('tell me about this', 'A' * 501),
            ('', 'Hello.'),
            ('hi', ''),
            ('\U0001f602', 'Very funny.'),
            ('That was hilarious', '\U0001f602'),
        ]
        for content, reply in cases:
            with self.subTest(content=content, reply=reply[:40]):
                self.assertIsNone(await self.service.choose(10, content, reply))
        self.model.assert_not_awaited()
        self.random.assert_not_called()
        self.gif.assert_not_awaited()
        self.assertEqual(self.service._last_attempt, {})

    async def test_disabled_reactions_never_call_model(self):
        with patch.object(config, 'AUTOMATIC_MEDIA_ENABLED', False):
            self.assertIsNone(await self.service.choose(10, self.content, self.reply))
        self.model.assert_not_awaited()

    async def test_sampling_is_only_permission_to_consider_media(self):
        for value in [0.15, 0.5, 0.99]:
            with self.subTest(value=value):
                self.random.return_value = value
                self.assertIsNone(await self.service.choose(10, self.content, self.reply))
        self.model.assert_not_awaited()
        self.assertEqual(self.service._last_attempt, {})

    async def test_valid_visual_punchline_can_be_delivered(self):
        self.assertIs(await self.service.choose(10, self.content, self.reply), self.result)
        self.gif.assert_awaited_once_with('detective empty folder', avoid_repeats=True)
        self.assertFalse(self.result.standalone)
        instructions = self.model.await_args.args[0][0]['content']
        self.assertIn('Text alone is the default', instructions)
        self.assertIn('MOST exchanges', instructions)
        self.assertIn('not a reason to attach media', instructions)
        self.assertNotIn('Prefer a fitting GIF', instructions)

    async def test_selector_can_decline_media_even_when_sampled(self):
        self.model.return_value = '{"kind":"none","adds_punchline":false}'
        self.assertIsNone(await self.service.choose(10, self.content, self.reply))
        self.model.assert_awaited_once()
        self.gif.assert_not_awaited()

    async def test_all_automatic_media_require_specific_extra_punchline(self):
        invalid = [
            {},
            {'adds_punchline': False, 'visual_punchline': 'It repeats the same feeling.'},
            {'adds_punchline': 'true', 'visual_punchline': 'A detective inspects an empty folder.'},
            {'adds_punchline': 1, 'visual_punchline': 'A detective inspects an empty folder.'},
            {'adds_punchline': True},
            {'adds_punchline': True, 'visual_punchline': None},
            {'adds_punchline': True, 'visual_punchline': []},
            {'adds_punchline': True, 'visual_punchline': '   '},
            {'adds_punchline': True, 'visual_punchline': 'funny'},
            {'adds_punchline': True, 'visual_punchline': 'A' * 241},
        ]
        for kind in ['gif', 'meme', 'cat', 'captioned_gif']:
            for fields in invalid:
                with self.subTest(kind=kind, fields=fields):
                    decision = self.decision | {'kind': kind}
                    decision.pop('adds_punchline')
                    decision.pop('visual_punchline')
                    self.model.return_value = json.dumps(decision | fields)
                    self.assertIsNone(await social.AutomaticMedia().choose(10, self.content, self.reply))
        for fetch in [self.gif, self.meme, self.cat, self.captioned]:
            fetch.assert_not_awaited()

    async def test_unknown_or_malformed_decisions_fail_closed(self):
        for decision in [[], None, {}, self.decision | {'kind': []}, self.decision | {'kind': 'video'}]:
            with self.subTest(decision=decision):
                self.model.return_value = json.dumps(decision)
                self.assertIsNone(await social.AutomaticMedia().choose(10, self.content, self.reply))
        self.gif.assert_not_awaited()

    async def test_cooldown_expires_per_channel_after_five_minutes(self):
        self.assertIs(await self.service.choose(10, self.content, self.reply), self.result)
        self.clock.return_value = 1299.9
        self.assertIsNone(await self.service.choose(10, self.content, self.reply))
        self.assertIs(await self.service.choose(11, self.content, self.reply), self.result)
        self.clock.return_value = 1300
        self.model.return_value = json.dumps(self.decision | {'gif_queries': ['judge empty courtroom']})
        self.assertIs(await self.service.choose(10, self.content, self.reply), self.result)
        self.assertEqual(self.model.await_count, 3)

    async def test_no_media_decision_also_limits_repeated_selector_calls(self):
        self.model.return_value = '{"kind":"none"}'
        await self.service.choose(10, self.content, self.reply)
        self.clock.return_value = 1001
        await self.service.choose(10, self.content, self.reply)
        self.model.assert_awaited_once()

    async def test_text_only_turn_does_not_block_following_visual_joke(self):
        self.assertIsNone(await self.service.choose(10, 'thanks nixi', 'Anytime.'))
        self.assertIs(await self.service.choose(10, self.content, self.reply), self.result)
        self.model.assert_awaited_once()

