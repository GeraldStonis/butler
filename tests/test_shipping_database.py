import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite
import config
from services import database as db


class ShippingDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "shipping.db")
        self.enterContext(patch.object(config, "DB_PATH", self.path))

    async def asyncTearDown(self):
        await db.close()

    async def sample(self, message_id=100, *, guild_id=1, channel_id=10,
                     author_id=20, target_id=21, content=None, now=1000):
        return await db.record_ship_sample(
            guild_id, channel_id, message_id, author_id, target_id,
            f"Exchange {message_id}" if content is None else content, now,
        )

    async def test_pair_keys_are_canonical_but_sample_direction_is_preserved(self):
        self.assertTrue(await self.sample(author_id=21, target_id=20))
        pair = await db.get_ship_pair(1, 21, 20)
        self.assertEqual((pair["low_id"], pair["high_id"]), (20, 21))
        self.assertEqual(pair["interaction_count"], 1)
        self.assertEqual(await db.get_ship_samples(1, 21, 20, 0), [{
            "message_id": 100, "channel_id": 10, "author_id": 21,
            "target_id": 20, "content": "Exchange 100", "created_at": 1000,
        }])
        await db.save_ship_pair(1, 21, 20, 2, -1, "playful", 1001)
        pair = await db.get_ship_pair(1, 20, 21)
        self.assertEqual((pair["low_affinity"], pair["high_affinity"]), (-1, 2))

    async def test_message_ids_and_repeated_directed_content_are_deduplicated(self):
        self.assertTrue(await self.sample(content="Glad you made it"))
        self.assertFalse(await self.sample(content="An edited message"))
        self.assertFalse(await self.sample(101, content="Glad you made it"))
        self.assertTrue(await self.sample(102, author_id=21, target_id=20,
                                          content="Glad you made it"))
        pair = await db.get_ship_pair(1, 20, 21)
        self.assertEqual(pair["interaction_count"], 2)
        self.assertEqual([row["message_id"] for row in await db.get_ship_samples(1, 20, 21, 0)],
                         [100, 102])
        self.assertFalse(await self.sample(100, target_id=22, content="A new target"))
        self.assertIsNone(await db.get_ship_pair(1, 20, 22))

    async def test_concurrent_duplicate_recordings_count_only_once(self):
        await db.get_db()
        inserted = await asyncio.gather(*(self.sample() for _ in range(8)))
        self.assertEqual(sum(inserted), 1)
        self.assertEqual((await db.get_ship_pair(1, 20, 21))["interaction_count"], 1)

    async def test_latest_twelve_samples_follow_insertion_order(self):
        for index in range(15):
            await self.sample(index, now=2000 - index)
        samples = await db.get_ship_samples(1, 20, 21, 0)
        self.assertEqual([row["message_id"] for row in samples], list(range(3, 15)))
        self.assertEqual((await db.get_ship_pair(1, 20, 21))["interaction_count"], 15)
        self.assertEqual([row["message_id"] for row in await db.get_ship_samples(1, 20, 21, 1990)],
                         list(range(3, 11)))

    async def test_thirty_day_retention_only_purges_the_current_pair(self):
        await self.sample(content="An old conversation", now=1000)
        await self.sample(200, target_id=22, now=1000)
        self.assertTrue(await self.sample(101, content="An old conversation",
                                          now=1000 + 30 * 86400 + 1))
        self.assertEqual([row["message_id"] for row in await db.get_ship_samples(1, 20, 21, 0)], [101])
        self.assertEqual([row["message_id"] for row in await db.get_ship_samples(1, 20, 22, 0)], [200])
        self.assertEqual((await db.get_ship_pair(1, 20, 21))["interaction_count"], 2)

    async def test_guild_and_channel_message_ids_are_isolated(self):
        await self.sample(content="First guild")
        self.assertTrue(await self.sample(guild_id=2, content="Second guild"))
        self.assertTrue(await self.sample(channel_id=11, content="Another channel"))
        self.assertEqual((await db.get_ship_pair(1, 20, 21))["interaction_count"], 2)
        self.assertEqual((await db.get_ship_pair(2, 20, 21))["interaction_count"], 1)
        self.assertEqual([row["content"] for row in await db.get_ship_samples(2, 20, 21, 0)],
                         ["Second guild"])

    async def test_review_claim_is_atomic_and_requires_new_samples_after_cooldown(self):
        for message_id in range(3):
            await self.sample(message_id)
        self.assertFalse(await db.claim_ship_review(1, 20, 21, 1000))
        await self.sample(3)
        claims = await asyncio.gather(*(db.claim_ship_review(1, 21, 20, 1000) for _ in range(8)))
        self.assertEqual(sum(claims), 1)
        pair = await db.get_ship_pair(1, 20, 21)
        self.assertEqual((pair["reviewed_count"], pair["next_review_at"]), (4, 1900))
        for message_id in range(4, 8):
            await self.sample(message_id, now=1001)
        self.assertFalse(await db.claim_ship_review(1, 20, 21, 1899))
        self.assertTrue(await db.claim_ship_review(1, 20, 21, 1900))
        self.assertFalse(await db.claim_ship_review(1, 20, 21, 2800))
        self.assertEqual((await db.get_ship_pair(1, 20, 21))["reviewed_count"], 8)

    async def test_lists_only_recent_reviewed_pairs_in_the_requested_guild(self):
        await self.sample()
        self.assertEqual(await db.list_ship_pairs(1, 0), [])
        await db.save_ship_pair(1, 20, 21, 1, 2, "warmth", 1000)
        await db.save_ship_pair(1, 20, 22, 2, 1, "support", 900)
        await db.save_ship_pair(2, 20, 21, 2, 2, "affection", 1000)
        pairs = await db.list_ship_pairs(1, 950)
        self.assertEqual([(row["low_id"], row["high_id"], row["tone"]) for row in pairs],
                         [(20, 21, "warmth")])
        self.assertEqual(pairs[0]["interaction_count"], 1)

    async def test_samples_pairs_review_cooldowns_and_preferences_survive_reopen(self):
        for message_id in range(4):
            await self.sample(message_id)
        await db.claim_ship_review(1, 20, 21, 1000)
        await db.save_ship_pair(1, 20, 21, 2, 1, "playful", 1001)
        await db.set_member_preferences(1, 20, {"shipping": False})
        await db.close()
        pair = await db.get_ship_pair(1, 20, 21)
        self.assertEqual((pair["low_affinity"], pair["high_affinity"], pair["tone"]),
                         (2, 1, "playful"))
        self.assertEqual((pair["interaction_count"], pair["reviewed_count"], pair["next_review_at"]),
                         (4, 4, 1900))
        self.assertEqual(len(await db.get_ship_samples(1, 20, 21, 0)), 4)
        self.assertFalse((await db.get_member_preferences(1, 20))["shipping"])
        self.assertTrue((await db.get_member_preferences(2, 20))["shipping"])

    async def test_shipping_preference_migrates_without_changing_existing_choices(self):
        async with aiosqlite.connect(self.path) as connection:
            await connection.execute("""CREATE TABLE member_preferences (
                guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                roasting INTEGER NOT NULL DEFAULT 1, media INTEGER NOT NULL DEFAULT 1,
                pings INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (guild_id, user_id)
            )""")
            await connection.execute("INSERT INTO member_preferences VALUES (1, 20, 0, 1, 0)")
            await connection.commit()
        preferences = await db.get_member_preferences(1, 20)
        self.assertTrue(preferences["shipping"])
        self.assertFalse(preferences["roasting"])
        self.assertFalse(preferences["pings"])
        await db.set_member_preferences(1, 20, {"shipping": False, "pings": True})
        preferences = await db.get_member_preferences(1, 20)
        self.assertFalse(preferences["shipping"])
        self.assertTrue(preferences["pings"])

    async def test_invalid_affinities_tones_and_self_pairs_are_rejected(self):
        self.assertFalse(await self.sample(target_id=20))
        for affinity in (True, -3, 3, 0.5, "1"):
            with self.subTest(affinity=affinity), self.assertRaises(ValueError):
                await db.save_ship_pair(1, 20, 21, affinity, 1, "warmth", 1000)
        with self.assertRaises(ValueError):
            await db.save_ship_pair(1, 20, 21, 1, 1, "invented", 1000)
        with self.assertRaises(ValueError):
            await db.save_ship_pair(1, 20, 20, 1, 1, "warmth", 1000)
        self.assertEqual(await db.list_ship_pairs(1, 0), [])


if __name__ == "__main__":
    unittest.main()
