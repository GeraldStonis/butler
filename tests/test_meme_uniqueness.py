import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import config
from services import database as db, media, generated_memes


class MemeUniquenessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.enterContext(patch.object(config, 'IMGFLIP_API_KEY', ''))
        self.path_patch = patch.object(config, 'DB_PATH', ':memory:')
        self.path_patch.start()
        await db.get_db()

    async def asyncTearDown(self):
        await db.close()
        self.path_patch.stop()

    async def test_atomic_claim_rejects_hash_url_and_post_duplicates(self):
        results = await asyncio.gather(
            db.claim_meme('hash1', 'https://images.test/1.jpg', 'https://posts.test/1'),
            db.claim_meme('hash1', 'https://images.test/2.jpg', 'https://posts.test/2'),
        )
        self.assertEqual(sum(results), 1)
        connection = await db.get_db()
        cursor = await connection.execute('SELECT image_url, post_url FROM meme_history')
        row = await cursor.fetchone()
        self.assertFalse(await db.claim_meme('other', row['image_url'], None))
        self.assertFalse(await db.claim_meme('another', 'https://images.test/3.jpg', row['post_url']))

    async def test_generated_captions_and_rendered_bytes_are_unique(self):
        import json
        renders = []
        def respond(request):
            if request.url.path == '/templates/':
                return httpx.Response(200, json=[{'id': 'buzz', 'name': 'Buzz', 'lines': 2}])
            if request.method == 'POST':
                payload = json.loads(request.content)
                renders.append(payload['text'])
                return httpx.Response(201, json={'url': f'https://api.memegen.link/images/{len(renders)}.png'})
            content = b'same pixels' if len(renders) <= 2 else b'fresh pixels'
            return httpx.Response(200, headers={'content-type': 'image/png'}, content=content)
        responses = [json.dumps({'template_id': 'buzz', 'text': text}) for text in (
            ['Tabs', 'Tabs everywhere'], ['Tabs', 'Tabs everywhere'],
            ['Meetings', 'More meetings'], ['Gaming', 'One more round'],
        )]
        real_client = httpx.AsyncClient
        with patch.object(generated_memes, '_templates', []), patch.object(generated_memes.httpx, 'AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw)), patch.object(generated_memes.llm, 'generate_response', AsyncMock(side_effect=responses)):
            first = await media.fetch_meme()
            second = await media.fetch_meme()
        self.assertEqual(first.title, 'Tabs / Tabs everywhere')
        self.assertEqual(second.title, 'Gaming / One more round')
        self.assertEqual(len(renders), 3)  # repeated caption skipped before rendering

    async def test_legacy_last_drop_and_history_survive_connection_restart(self):
        # File persistence is covered by the existing database tests; reconnect
        # here uses a temporary file to verify this new table specifically.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            await db.close()
            with patch.object(config, 'DB_PATH', str(Path(directory) / 'history.db')):
                await db.set_state('meme_drop_last:10', 'https://images.test/old.jpg')
                self.assertTrue(await db.meme_is_known('https://images.test/old.jpg', None))
                await db.claim_meme('saved', 'https://images.test/saved.jpg', None)
                await db.close()
                self.assertTrue(await db.meme_is_known('https://images.test/saved.jpg', None))
                await db.close()
