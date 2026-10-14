import asyncio
import unittest
from unittest.mock import patch

import httpx
import config
from services import database as db, media


class MemeUniquenessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
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

    async def test_batch_skips_same_image_under_different_urls(self):
        original = [{'title': 'First', 'url': 'https://images.test/first.jpg?size=1', 'postLink': 'https://posts.test/first'}]
        batch = original
        images = {'/first.jpg': b'original image bytes', '/repost.jpg': b'original image bytes', '/new.jpg': b'new image bytes'}
        requests = []
        def respond(request):
            requests.append(str(request.url))
            if request.url.host == 'meme-api.com':
                return httpx.Response(200, json={'memes': batch})
            return httpx.Response(200, headers={'content-type': 'image/jpeg'}, content=images[request.url.path])
        real_client = httpx.AsyncClient
        with patch.object(media.httpx, 'AsyncClient', side_effect=lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw)), patch.object(media.random, 'shuffle', lambda _: None):
            first = await media.fetch_meme()
            self.assertEqual(first.title, 'First')
            batch = [
                {'title': 'URL duplicate', 'url': 'https://images.test/first.jpg?size=2', 'postLink': 'https://posts.test/other'},
                {'title': 'Repost duplicate', 'url': 'https://images.test/repost.jpg', 'postLink': 'https://posts.test/repost'},
                {'title': 'Fresh', 'url': 'https://images.test/new.jpg', 'postLink': 'https://posts.test/new'},
            ]
            second = await media.fetch_meme()
            self.assertEqual(second.title, 'Fresh')
            self.assertNotIn('https://images.test/first.jpg?size=2', requests)
            self.assertIsNone(await media.fetch_meme())

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
