import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import config
from services import database as db, media, social


class GifVarietyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.directory = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(config, 'DB_PATH', str(Path(self.directory.name) / 'gifs.db'))
        self.path_patch.start()
        await db.get_db()

    async def asyncTearDown(self):
        await db.close()
        self.path_patch.stop()
        self.directory.cleanup()

    async def test_ranked_search_skips_seen_ids_and_survives_restart(self):
        def respond(request):
            self.assertEqual(request.url.params['limit'], '8')
            return httpx.Response(200, json={'data': [
                {'id': 'one', 'images': {'original': {'url': 'https://images.test/one.gif'}}},
                {'id': 'two', 'images': {'original': {'url': 'https://images.test/two.gif'}}},
            ]})
        real_client = httpx.AsyncClient
        with patch.object(config, 'GIPHY_API_KEY', 'test-key'), patch.object(media.httpx, 'AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw)):
            first = await media.fetch_gif('a generated phrase', avoid_repeats=True)
            await db.close()
            second = await media.fetch_gif('another generated phrase', avoid_repeats=True)
            self.assertNotEqual(first.url, second.url)
            self.assertIsNone(await media.fetch_gif('third generated phrase', avoid_repeats=True))

    async def test_empty_search_regenerates_queries_using_sentiment_and_reply(self):
        service = social.AutomaticMedia()
        result = media.MediaResult('Fresh', 'https://images.test/fresh.gif')
        decisions = [
            json.dumps({'kind': 'gif', 'sentiment': 'mock disbelief', 'adds_punchline': True,
                        'visual_punchline': 'The detective searches an empty folder for evidence of perfection.',
                        'gif_queries': ['detective skeptical blink']}),
            json.dumps({'kind': 'gif', 'sentiment': 'mock disbelief', 'adds_punchline': True,
                        'visual_punchline': 'A silent cat judge refuses to dignify the boast with an answer.',
                        'gif_queries': ['detective skeptical blink', 'cat judges your life choices'], 'delivery': 'media_only'}),
        ]
        with patch.object(config, 'AUTOMATIC_MEDIA_ENABLED', True), patch.object(config, 'AUTOMATIC_MEDIA_CHANCE', 1.0), patch('services.social.random.random', return_value=0), patch.object(social.llm, 'generate_response', AsyncMock(side_effect=decisions)) as model, patch.object(media, 'fetch_available_gif', AsyncMock(side_effect=[None, result])) as fetch:
            selected = await service.choose(10, 'I never make mistakes', 'Your last take disagrees.')
            self.assertIs(selected, result)
            self.assertTrue(result.standalone)
            self.assertEqual([call.args[0] for call in fetch.call_args_list], ['detective skeptical blink', 'cat judges your life choices'])
            second_context = json.loads(model.call_args.args[0][-1]['content'])
            self.assertEqual(second_context['nixi_reply'], 'Your last take disagrees.')
            self.assertIn('detective skeptical blink', second_context['recent_queries_to_avoid'])

    async def test_search_continues_after_exhausted_first_page(self):
        await db.claim_reaction_gif('giphy:seen')
        offsets = []
        def respond(request):
            offset = int(request.url.params['offset'])
            offsets.append(offset)
            key = 'seen' if offset == 0 else 'fresh'
            return httpx.Response(200, json={'data': [{'id': key, 'images': {'original': {'url': f'https://images.test/{key}.gif'}}}]})
        real_client = httpx.AsyncClient
        with patch.object(config, 'GIPHY_API_KEY', 'test-key'), patch.object(media.httpx, 'AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw)):
            result = await media.fetch_gif('smug reaction', avoid_repeats=True)
        self.assertEqual(offsets, [0, 8])
        self.assertEqual(result.url, 'https://images.test/fresh.gif')

    async def test_owner_request_still_uses_first_result_without_pagination(self):
        offsets = []
        def respond(request):
            self.assertEqual(request.url.params['limit'], '1')
            offsets.append(int(request.url.params['offset']))
            return httpx.Response(200, json={'data': [{'id': 'one', 'images': {'original': {'url': 'https://images.test/one.gif'}}}]})
        real_client = httpx.AsyncClient
        with patch.object(config, 'GIPHY_API_KEY', 'test-key'), patch.object(media.httpx, 'AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw)):
            await media.fetch_gif('tea serving')
        self.assertEqual(offsets, [0])
