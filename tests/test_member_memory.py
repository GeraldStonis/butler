import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
from services import database as db, memory, intent
from cogs.commands import CommandsCog


class MemberMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(config, "DB_PATH", str(Path(self.directory.name) / "test.db"))
        self.path_patch.start()
        await db.close()
        await db.get_db()

    async def asyncTearDown(self):
        await db.close()
        self.path_patch.stop()
        self.directory.cleanup()

    async def test_reset_preserves_profiles_instructions_and_other_channel(self):
        await db.save_message(10, 1, 20, "Sam", "old conversation")
        await db.save_summary(10, 1, "old summary", 1)
        await db.save_message(11, 1, 20, "Sam", "other channel")
        await db.save_member_fact(1, 20, "drink_preference", "coffee", "I prefer coffee")
        await db.save_instruction("Keep replies brief")
        await db.clear_channel_memory(10)
        self.assertEqual(await db.get_recent_messages(10), [])
        self.assertIsNone(await db.get_latest_summary(10))
        self.assertEqual(len(await db.get_recent_messages(11)), 1)
        self.assertEqual((await db.get_member_facts(1, 20))[0]["content"], "coffee")
        self.assertEqual(await db.get_active_instructions(), ["Keep replies brief"])

    async def test_identity_and_server_isolation_and_corrections(self):
        await db.update_member_profile(1, 20, "Sam", "sam")
        await db.update_member_profile(1, 21, "Sam", "other_sam")
        await db.save_member_fact(1, 20, "drink_preference", "tea", "I prefer tea")
        await db.update_member_profile(1, 20, "Renamed", "sam")
        await db.save_member_fact(1, 20, "drink_preference", "coffee", "Actually I prefer coffee")
        self.assertEqual(await db.get_member_facts(1, 21), [])
        self.assertEqual(await db.get_member_facts(2, 20), [])
        facts = await db.get_member_facts(1, 20)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["content"], "coffee")

    async def test_extraction_requires_evidence_and_excludes_bot_reply(self):
        extracted = '[{"fact_key":"drink_preference","content":"coffee","evidence":"I prefer coffee"},{"fact_key":"interests","content":"chess","evidence":"I love chess"}]'
        with patch.object(memory.llm, "generate_response", AsyncMock(return_value=extracted)) as model:
            await memory.extract_and_store_memories(10, "I prefer coffee", "Sam", "I love chess", guild_id=1, user_id=20)
            self.assertEqual(model.call_args.args[0][-1]["content"], "I prefer coffee")
        facts = await db.get_member_facts(1, 20)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["fact_key"], "drink_preference")

    async def test_context_loads_member_notes_without_keyword_match(self):
        await db.save_member_fact(1, 20, "drink_preference", "coffee", "I prefer coffee")
        context = await memory.build_context(10, "hello", "Renamed", "DIRECT_ADDRESS", guild_id=1, current_author_id=20)
        self.assertTrue(any("drink_preference: coffee" in entry["content"] for entry in context))
        other = await memory.build_context(10, "hello", "Renamed", "DIRECT_ADDRESS", guild_id=2, current_author_id=20)
        self.assertFalse(any("drink_preference: coffee" in entry["content"] for entry in other))

    async def test_owner_only_reset(self):
        await db.save_message(10, 1, 20, "Sam", "keep this")
        cog = CommandsCog(SimpleNamespace())
        interaction = SimpleNamespace(user=SimpleNamespace(id=config.OWNER_ID + 1), channel_id=10,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await cog.reset_memory.callback(cog, interaction)
        interaction.response.send_message.assert_awaited_once()
        self.assertEqual(len(await db.get_recent_messages(10)), 1)
        interaction.user.id = config.OWNER_ID
        await cog.reset_memory.callback(cog, interaction)
        self.assertEqual(await db.get_recent_messages(10), [])

    def test_natural_requests_and_explanatory_question(self):
        self.assertIsNone(intent.heuristic_intent("nixi reset your recent memory"))
        self.assertIsNone(intent.heuristic_intent("how do I reset recent memory?"))
        target = SimpleNamespace(id=config.OWNER_ID + 1, bot=False)
        self.assertIsNone(intent.heuristic_intent("nixi remember <@123> likes chess", [target]))
        self.assertIsNone(intent.heuristic_intent("nixi what do you remember about <@123>", [target]))
        self.assertTrue(memory.has_member_fact_candidate("I prefer coffee"))
        self.assertFalse(memory.has_member_fact_candidate("Sam likes coffee"))


if __name__ == "__main__":
    unittest.main()
