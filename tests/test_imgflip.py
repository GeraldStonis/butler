import asyncio
import hashlib
import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx
import config
from services import actions, database as db, generated_memes, imgflip, intent, media, memory, social


class ImgflipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        for name, value in {
            'DB_PATH': ':memory:', 'OWNER_ID': 99, 'IMGFLIP_API_KEY': 'test-imgflip-secret',
            'IMGFLIP_GIF_CAPTIONS_ENABLED': False, 'IMGFLIP_GIF_DAILY_LIMIT': 1,
            'MEDIA_SAFE_MODE': True, 'AUTOMATIC_MEDIA_ENABLED': True,
            'AUTOMATIC_MEDIA_CHANCE': 1, 'AUTOMATIC_MEDIA_COOLDOWN_SECONDS': 0,
        }.items():
            self.enterContext(patch.object(config, name, value))
        self.enterContext(patch.object(imgflip, '_catalog', {}))
        self.enterContext(patch.object(imgflip, '_retry_until', {}))
        self.enterContext(patch.object(imgflip, '_gif_lock', asyncio.Lock()))
        self.enterContext(patch.object(generated_memes, '_lock', asyncio.Lock()))
        self.enterContext(patch.object(generated_memes, '_templates', []))
        self.enterContext(patch.object(generated_memes, '_expires', 0))
        self.requests, self.renders = [], []
        self.caption_status, self.caption_success = 200, True
        self.handler = self.respond
        real_client = httpx.AsyncClient
        self.enterContext(patch.object(imgflip.httpx, 'AsyncClient',
            side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(lambda request: self.handler(request)), **kwargs)))
        await db.get_db()

    async def asyncTearDown(self):
        await memory.shutdown()
        await db.close()

    def respond(self, request):
        self.requests.append(request)
        if request.url.path == '/get_memes':
            animated = request.url.params.get('type') == 'gif'
            return httpx.Response(200, json={'success': True, 'data': {'memes': [
                {'id': '222516354' if animated else '181913649',
                 'name': 'Disappearing kid' if animated else 'Drake', 'box_count': 1 if animated else 2},
                {'id': '999999999', 'name': 'Unreviewed template', 'box_count': 2},
            ]}})
        if request.url.path in {'/caption_image', '/caption_gif'}:
            self.renders.append(parse_qs(request.content.decode()))
            extension = 'gif' if request.url.path == '/caption_gif' else 'jpg'
            return httpx.Response(self.caption_status, json={
                'success': self.caption_success,
                'data': {'url': f'https://i.imgflip.com/render{len(self.renders)}.{extension}'},
                'error_message': 'test-imgflip-secret must never be logged',
            })
        if request.url.path == '/templates/':
            return httpx.Response(200, json=[{'id': 'buzz', 'name': 'Buzz', 'lines': 2}])
        if request.url.host == 'api.memegen.link' and request.method == 'POST':
            return httpx.Response(201, json={'url': 'https://api.memegen.link/fallback.png'})
        if request.url.host == 'i.imgflip.com':
            animated = request.url.path.endswith('.gif')
            return httpx.Response(200, headers={'content-type': 'image/gif' if animated else 'image/jpeg'},
                                  content=(b'GIF89a' if animated else b'JPEG') + request.url.path.encode())
        return httpx.Response(200, headers={'content-type': 'image/png'}, content=b'fallback pixels')

    def draft(self, captions=None, *, animated=False):
        return json.dumps({'template_id': '222516354' if animated else '181913649',
                           'text': captions if captions is not None else ['My ping', 'My aim']})

    async def test_static_meme_uses_bearer_form_boxes_and_embed_url(self):
        with patch.object(generated_memes.llm, 'generate_response', AsyncMock(return_value=self.draft())):
            result = await media.fetch_meme()
        self.assertEqual(result.title, 'My ping / My aim')
        self.assertEqual(result.url, 'https://i.imgflip.com/render1.jpg')
        self.assertEqual(actions._media_embed(result).image.url, result.url)
        post = next(request for request in self.requests if request.method == 'POST')
        self.assertEqual(post.headers['authorization'], 'Bearer test-imgflip-secret')
        self.assertEqual(self.renders[0], {'template_id': ['181913649'],
                         'boxes[0][text]': ['My ping'], 'boxes[1][text]': ['My aim']})
        self.assertNotIn(b'test-imgflip-secret', post.content)
        for request in self.requests:
            if request.url.host == 'i.imgflip.com':
                self.assertNotIn('authorization', request.headers)
        self.assertFalse(any(request.url.host == 'api.memegen.link' for request in self.requests))

    async def test_template_catalog_is_cached_and_safe_mode_filters_unknown_ids(self):
        async with httpx.AsyncClient() as client:
            first = await imgflip.templates(client)
            second = await imgflip.templates(client)
        self.assertEqual([item['id'] for item in first], ['181913649'])
        self.assertEqual(first, second)
        self.assertEqual(len(self.requests), 1)

    async def test_caption_duplicates_are_skipped_before_another_render(self):
        with patch.object(generated_memes.llm, 'generate_response', AsyncMock(side_effect=[
            self.draft(), self.draft(), self.draft(['Another tab', 'Another problem']),
        ])):
            first = await media.fetch_meme()
            second = await media.fetch_meme()
        self.assertNotEqual(first.title, second.title)
        self.assertEqual(len(self.renders), 2)

    async def test_legacy_memegen_caption_history_blocks_imgflip_reruns(self):
        title = 'My ping / My aim'
        old_key = 'memegen:' + hashlib.sha256(re.sub(r'\W+', '', title.casefold()).encode()).hexdigest()
        await db.claim_meme('old image bytes', 'https://api.memegen.link/old.png', old_key)
        with patch.object(generated_memes.llm, 'generate_response', AsyncMock(side_effect=[
            self.draft(), self.draft(['New joke', 'Fresh punchline']),
        ])):
            result = await media.fetch_meme()
        self.assertEqual(result.title, 'New joke / Fresh punchline')
        self.assertEqual(len(self.renders), 1)

    async def test_repeated_image_bytes_are_rejected_despite_new_captions_and_urls(self):
        original = self.respond
        def same_pixels(request):
            if request.url.host == 'i.imgflip.com' and len(self.renders) <= 2:
                self.requests.append(request)
                return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=b'same pixels')
            return original(request)
        self.handler = same_pixels
        with patch.object(generated_memes.llm, 'generate_response', AsyncMock(side_effect=[
            self.draft(), self.draft(['Another tab', 'Another problem']), self.draft(['Fresh joke', 'Fresh pixels']),
        ])):
            first = await media.fetch_meme()
            second = await media.fetch_meme()
        self.assertEqual(first.title, 'My ping / My aim')
        self.assertEqual(second.title, 'Fresh joke / Fresh pixels')
        self.assertEqual(len(self.renders), 3)

    async def test_caption_failure_falls_back_without_logging_secrets(self):
        self.caption_status = 401
        fallback = json.dumps({'template_id': 'buzz', 'text': ['Tabs', 'Tabs everywhere']})
        with patch.object(generated_memes.llm, 'generate_response', AsyncMock(side_effect=[self.draft(), fallback])), self.assertLogs('nixi.imgflip', level='WARNING') as logs:
            result = await media.fetch_meme()
        self.assertEqual(result.url, 'https://api.memegen.link/fallback.png')
        self.assertNotIn('test-imgflip-secret', ' '.join(logs.output))
        self.assertIn('HTTP 401', ' '.join(logs.output))

    async def test_api_rejection_in_http_200_is_not_reported_as_an_image(self):
        self.caption_success = False
        with self.assertLogs('nixi.imgflip', level='WARNING') as logs:
            async with httpx.AsyncClient() as client:
                self.assertIsNone(await imgflip.caption(client, '181913649', ['one', 'two']))
        self.assertNotIn('test-imgflip-secret', ' '.join(logs.output))

    async def test_invalid_image_hosts_and_redirects_are_rejected(self):
        def bad_url(request):
            return httpx.Response(200, json={'success': True, 'data': {'url': 'https://example.com/redirect.jpg'}})
        self.handler = bad_url
        with self.assertLogs('nixi.imgflip', level='WARNING'):
            async with httpx.AsyncClient() as client:
                self.assertIsNone(await imgflip.caption(client, '181913649', ['one', 'two']))

    async def test_non_latin_or_mention_captions_never_reach_renderer(self):
        with patch.object(generated_memes.llm, 'generate_response', AsyncMock(return_value=self.draft(['\u0905\u092c', '@everyone']))):
            result = await media.fetch_meme()
        self.assertIsNone(result)
        self.assertEqual(self.renders, [])

    async def test_gif_opt_in_required_even_for_internal_generation(self):
        with patch.object(generated_memes.llm, 'generate_response', AsyncMock()) as model:
            self.assertIsNone(await media.fetch_captioned_gif('lag'))
            self.assertIsNone(await generated_memes.create_meme(animated=True))
        model.assert_not_awaited()
        self.assertEqual(self.requests, [])

    async def test_enabled_captioned_gif_uses_animated_endpoint_and_reserves_budget(self):
        with patch.object(config, 'IMGFLIP_GIF_CAPTIONS_ENABLED', True), patch.object(generated_memes.llm, 'generate_response', AsyncMock(return_value=self.draft(['Me leaving the ranked queue'], animated=True))):
            result = await media.fetch_captioned_gif('ranked queue')
            self.assertIsNone(await media.fetch_captioned_gif('another topic'))
        self.assertEqual(result.url, 'https://i.imgflip.com/render1.gif')
        self.assertTrue(any(request.url.path == '/caption_gif' for request in self.requests))
        self.assertEqual(len(self.renders), 1)
        day = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(await db.get_state('imgflip_gif_daily:' + day), '1')

    async def test_concurrent_paid_calls_cannot_bypass_daily_cap(self):
        with patch.object(config, 'IMGFLIP_GIF_CAPTIONS_ENABLED', True):
            async with httpx.AsyncClient() as client:
                results = await asyncio.gather(*(imgflip.caption(client, '222516354', ['lag'], animated=True) for _ in range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.renders), 1)

    async def test_static_bytes_are_not_delivered_as_an_animated_gif(self):
        original = self.respond
        def wrong_image(request):
            if request.url.host == 'i.imgflip.com':
                self.requests.append(request)
                return httpx.Response(200, headers={'content-type': 'image/png'}, content=b'not GIF bytes')
            return original(request)
        self.handler = wrong_image
        with patch.object(config, 'IMGFLIP_GIF_CAPTIONS_ENABLED', True), patch.object(generated_memes.llm, 'generate_response', AsyncMock(return_value=self.draft(['lag'], animated=True))):
            self.assertIsNone(await media.fetch_captioned_gif('lag'))
        self.assertEqual(len(self.renders), 1)

    async def test_paid_attempt_limit_survives_restart_including_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            await db.close()
            self.caption_status = 503
            with patch.object(config, 'DB_PATH', str(Path(directory) / 'imgflip.db')), patch.object(config, 'IMGFLIP_GIF_CAPTIONS_ENABLED', True):
                with self.assertLogs('nixi.imgflip', level='WARNING'):
                    async with httpx.AsyncClient() as client:
                        self.assertIsNone(await imgflip.caption(client, '222516354', ['lag'], animated=True))
                        await db.close()
                        imgflip._retry_until.clear()
                        self.assertIsNone(await imgflip.caption(client, '222516354', ['another joke'], animated=True))
                await db.close()
        self.assertEqual(len(self.renders), 1)

    async def test_captioned_gif_requests_remain_owner_only(self):
        message = SimpleNamespace(author=SimpleNamespace(id=20), content='nixi make a gif meme about lag', reply=AsyncMock())
        request = intent.heuristic_intent(message.content)
        self.assertEqual(request.name, 'captioned_gif')
        with patch.object(media, 'fetch_captioned_gif', AsyncMock()) as fetch, patch('services.actions.conversation.discreet_reply', AsyncMock(return_value='A little good humor goes a long way.')):
            self.assertFalse(await actions.execute_owner_intent(message, request))
            message.author.id = 99
            self.assertTrue(await actions.execute_owner_intent(message, request))
        fetch.assert_not_awaited()
        self.assertEqual(message.reply.await_args.args[0], 'A little good humor goes a long way.')
        self.assertEqual(social.guest_kind_hint('nixi caption a gif about lag'), 'command')

    async def test_captioned_gif_delivery_is_an_embed_not_a_text_url(self):
        result = media.MediaResult('A fresh caption', 'https://i.imgflip.com/example.gif')
        sent = SimpleNamespace(author=SimpleNamespace(id=100))
        message = SimpleNamespace(author=SimpleNamespace(id=99), content='nixi make a captioned gif',
                                  reply=AsyncMock(return_value=sent), channel=SimpleNamespace(id=10), guild=None)
        with patch.object(config, 'IMGFLIP_GIF_CAPTIONS_ENABLED', True), patch.object(media, 'fetch_captioned_gif', AsyncMock(return_value=result)), patch.object(memory, 'log_message', AsyncMock()) as logged:
            self.assertTrue(await actions.execute_owner_intent(message, intent.OwnerIntent('captioned_gif', query='lag')))
        self.assertEqual(message.reply.await_args.args[0], 'A fresh caption')
        self.assertEqual(message.reply.await_args.kwargs['embed'].image.url, result.url)
        self.assertIn('Captioned GIF delivered', logged.await_args.kwargs['content'])

    async def test_automatic_gif_captioning_requires_explicit_enablement(self):
        decision = json.dumps({'kind': 'captioned_gif', 'sentiment': 'mock disbelief', 'delivery': 'media_only',
                               'adds_punchline': True, 'visual_punchline': 'The evidence vanishes as the boast gets bigger.'})
        result = media.MediaResult('A fresh caption', 'https://i.imgflip.com/example.gif')
        with patch.object(generated_memes.llm, 'generate_response', AsyncMock(return_value=decision)), patch.object(media, 'fetch_captioned_gif', AsyncMock(return_value=result)) as fetch:
            self.assertIsNone(await social.AutomaticMedia().choose(10, 'a joke', 'a comeback'))
            fetch.assert_not_awaited()
            with patch.object(config, 'IMGFLIP_GIF_CAPTIONS_ENABLED', True):
                reaction = await social.AutomaticMedia().choose(10, 'a joke', 'a comeback')
        self.assertFalse(reaction.standalone)
        fetch.assert_awaited_once_with('mock disbelief')


class CaptionedGifIntentTests(unittest.TestCase):
    def test_natural_requests_are_distinct_from_plain_gif_searches(self):
        for text in ['nixi make a captioned gif about wifi', 'nixi caption a gif about lag',
                     'nixi send a gif meme about meetings', 'nixi make a gif with a caption about lurking']:
            self.assertEqual(intent.heuristic_intent(text).name, 'captioned_gif', text)
        self.assertEqual(intent.heuristic_intent('nixi send tea gifs').name, 'gif_search')
