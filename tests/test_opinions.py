import asyncio
import json
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import config
from cogs.chat import ChatCog
from services import database as db, llm, memory, opinions, social


class OpinionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(config, "DB_PATH", ":memory:"))
        self.model = self.stack.enter_context(patch.object(llm, "generate_response", AsyncMock(return_value='{"skip":true}')))
        self.stack.enter_context(patch.object(opinions.asyncio, "sleep", AsyncMock()))
        self.samples = ["Nixi your tea has more lore than my entire game library",
                        "Nixi this lobby needs a documentary not a ranked ladder",
                        "Nixi I brought another terrible pun for the collection"]
        self.guild = SimpleNamespace(id=1, members=[self.member(20), self.member(21), self.member(22)], emojis=[])

    async def asyncTearDown(self):
        await memory.shutdown()
        await db.close()
        self.stack.close()

    def member(self, user_id):
        return SimpleNamespace(id=user_id, bot=False, display_name=f"Guest {user_id}", name=f"guest{user_id}")

    async def seed(self, user_id=20, affinity=1, impression="Enjoys turning game chat into elaborate dry jokes.", *, guild_id=1, now=None):
        now = time.time() if now is None else now
        for sample in self.samples:
            await db.record_impression_sample(guild_id, user_id, sample, now)
        await db.save_member_impression(guild_id, user_id, impression, affinity, "PRIVATE EVIDENCE", now)

    async def learn(self, result):
        self.model.return_value = json.dumps(result)
        for sample in self.samples:
            await opinions.observe(1, 20, sample)

    async def test_review_after_three_distinct_exchanges_not_every_message(self):
        await opinions.observe(1, 20, self.samples[0])
        await opinions.observe(1, 20, self.samples[0])
        await opinions.observe(1, 20, self.samples[1])
        self.model.assert_not_awaited()
        self.model.return_value = json.dumps({"impression": "Enjoys a dry joke about gaming.", "affinity": 2, "evidence": self.samples[1]})
        await opinions.observe(1, 20, self.samples[2])
        self.model.assert_awaited_once()
        row = await db.get_member_impression(1, 20)
        self.assertEqual(row["interaction_count"], 3)
        self.assertEqual(row["affinity"], 1)
        data = json.loads(self.model.await_args.args[0][-1]["content"])
        self.assertEqual(data["messages"], self.samples)
        self.assertNotIn("user_id", data)
        self.assertEqual(await db.get_member_facts(1, 20), [])

    async def test_review_reservation_is_atomic_and_persistent(self):
        await self.seed()
        results = await asyncio.gather(*(db.claim_impression_review(1, 20, 1000) for _ in range(5)))
        self.assertEqual(sum(results), 1)
        for i in range(3):
            await db.record_impression_sample(1, 20, f"Another conversation number {i}", 1001)
        self.assertFalse(await db.claim_impression_review(1, 20, 1899))
        self.assertTrue(await db.claim_impression_review(1, 20, 1900))

    async def test_only_six_bounded_samples_are_kept(self):
        for i in range(15):
            await db.record_impression_sample(1, 20, f"Sample {i} " + "x" * 1000, time.time())
        samples = await db.get_impression_samples(1, 20, 0)
        self.assertEqual(len(samples), 6)
        self.assertTrue(samples[0].startswith("Sample 9 "))
        self.assertTrue(all(len(text) <= 800 for text in samples))

    async def test_evidence_must_come_from_this_member(self):
        await self.learn({"impression": "Has an entertaining sense of timing.", "affinity": 1,
                          "evidence": "A made up thing another member said"})
        self.assertEqual((await db.get_member_impression(1, 20))["impression"], "")

    async def test_negative_impression_requires_two_distinct_supporting_messages(self):
        await self.learn({"impression": "The repeated theatrical heckling tests my patience.",
                          "affinity": -1, "evidence": self.samples[0]})
        self.assertEqual((await db.get_member_impression(1, 20))["impression"], "")

    async def test_supported_negative_impression_can_be_saved(self):
        await self.learn({"impression": "The repeated theatrical heckling tests my patience.",
                          "affinity": -2, "evidence": self.samples[0], "supporting_evidence": self.samples[1]})
        self.assertEqual((await db.get_member_impression(1, 20))["affinity"], -1)

    async def test_same_evidence_twice_is_not_a_pattern(self):
        await self.learn({"impression": "The repeated theatrical heckling tests my patience.",
                          "affinity": -1, "evidence": self.samples[0], "supporting_evidence": self.samples[0]})
        self.assertEqual((await db.get_member_impression(1, 20))["impression"], "")

    async def test_opt_out_checked_again_after_generation(self):
        async def opt_out(*args, **kwargs):
            await db.set_member_preferences(1, 20, {"roasting": False})
            return json.dumps({"impression": "The repeated theatrical heckling tests my patience.",
                               "affinity": -1, "evidence": self.samples[0], "supporting_evidence": self.samples[1]})
        self.model.side_effect = opt_out
        for sample in self.samples:
            await opinions.observe(1, 20, sample)
        self.assertEqual((await db.get_member_impression(1, 20))["impression"], "")

    async def test_bad_provider_json_never_replaces_an_impression(self):
        await self.seed()
        self.model.return_value = "not json"
        await opinions.observe(1, 20, "Nixi here's a brand new terrible gaming pun")
        self.assertIn("elaborate dry jokes", (await db.get_member_impression(1, 20))["impression"])

    async def test_internal_boolean_is_not_an_affinity_integer(self):
        await self.learn({"impression": "Enjoys game chatter.", "affinity": True, "evidence": self.samples[0]})
        self.assertEqual((await db.get_member_impression(1, 20))["impression"], "")

    async def test_review_busy_does_not_queue_another_model_call(self):
        await self.seed()
        async with opinions._review_lock:
            await asyncio.wait_for(opinions.observe(1, 20, "Nixi another very important terrible pun incoming"), timeout=1)
        self.model.assert_not_awaited()
        self.assertEqual((await db.get_member_impression(1, 20))["interaction_count"], 4)

    async def test_cooldown_survives_followup_messages(self):
        await self.learn({"skip": True})
        for i in range(6):
            await opinions.observe(1, 20, f"Nixi another new joke about this game number {i}")
        self.model.assert_awaited_once()

    async def test_reconciliation_can_reverse_an_old_negative_impression(self):
        await self.seed(affinity=-2, impression="The recurring heckling had become tiresome.")
        self.model.return_value = json.dumps({"impression": "Has become a warm and playful conversational partner.",
                                             "affinity": 2, "evidence": "the jokes were getting old"})
        await opinions.observe(1, 20, "Nixi fair enough the jokes were getting old, thanks for being a sport")
        row = await db.get_member_impression(1, 20)
        self.assertEqual(row["affinity"], 1)
        self.assertIn("warm and playful", row["impression"])

    async def test_timeout_is_quiet_and_does_not_erase_the_old_impression(self):
        await self.seed()
        self.model.side_effect = asyncio.TimeoutError("private provider detail")
        with self.assertLogs("nixi.opinions", level="INFO") as logs:
            await opinions.observe(1, 20, "Nixi another entirely different game joke incoming")
        self.assertIn("elaborate dry jokes", (await db.get_member_impression(1, 20))["impression"])
        self.assertNotIn("private provider detail", " ".join(logs.output))

    async def test_simple_distressed_and_credential_messages_not_sampled(self):
        for text in ["hi nixi", "thanks nixi", "Nixi I had a bad day", "Nixi here is my api key not-a-real-key"]:
            await opinions.observe(1, 20, text)
        self.assertIsNone(await db.get_member_impression(1, 20))
        self.model.assert_not_awaited()

    async def test_context_uses_live_names_and_not_raw_private_evidence_or_scores(self):
        await self.seed()
        self.guild.members[0].display_name = "Renamed Guest"
        context = await opinions.context(1, 20, "Renamed Guest", "hello", [20], self.guild)
        self.assertIn("Renamed Guest", context)
        self.assertIn("elaborate dry jokes", context)
        self.assertNotIn("PRIVATE EVIDENCE", context)
        for key in ['"affinity"', '"interaction_count"', '"reviewed_count"', '"evidence"']:
            self.assertNotIn(key, context)
        self.model.assert_not_awaited()

    async def test_rankings_include_unmentioned_members_but_no_other_servers_bots_or_leavers(self):
        await self.seed(21, 2, "A lovely collaborator with excellent comic timing.")
        await self.seed(22, -1, "A theatrical heckler who currently tests my patience.")
        await self.seed(30, 2, "LEFT SERVER DO NOT SHARE")
        await self.seed(31, 2, "BOT DO NOT SHARE")
        self.guild.members.append(SimpleNamespace(id=31, bot=True, display_name="Bot"))
        await self.seed(21, 2, "OTHER SERVER DO NOT SHARE", guild_id=2)
        context = await opinions.context(1, 20, "Guest 20", "who is your least favorite?", [20], self.guild)
        self.assertIn("excellent comic timing", context)
        self.assertIn("theatrical heckler", context)
        self.assertNotIn("DO NOT SHARE", context)
        self.assertIn("not invent a target", context)
        self.assertIn("never changes ownership, permissions", context)

    async def test_negative_rankings_respect_third_party_no_roasting(self):
        await self.seed(21, -1, "ANNOYING DO NOT SHARE")
        await db.set_member_preferences(1, 21, {"roasting": False})
        context = await opinions.context(1, 20, "Guest 20", "who is your least favorite?", [20, 21], self.guild)
        self.assertNotIn("ANNOYING DO NOT SHARE", context)
        self.assertNotIn('"member_id": 21', context)

    async def test_stale_impressions_not_loaded_or_used_as_prior(self):
        await self.seed(now=time.time() - 31 * 86400)
        self.assertEqual(await opinions.context(1, 20, "Guest 20", "hello", [20], self.guild), "")
        self.model.return_value = json.dumps({"impression": "Enjoys making gaming puns.", "affinity": 1, "evidence": self.samples[1]})
        for sample in self.samples:
            await opinions.observe(1, 20, sample)
        data = json.loads(self.model.await_args.args[0][-1]["content"])
        self.assertIsNone(data["previous_impression"])
        self.assertEqual((await db.get_member_impression(1, 20))["affinity"], 1)

    async def test_recent_reset_keeps_subjective_memory_separate(self):
        await self.seed()
        await db.save_message(10, 1, 20, "Guest 20", "clear this")
        await db.clear_channel_memory(10)
        self.assertTrue((await db.get_member_impression(1, 20))["impression"])

    async def test_member_context_is_actually_included_in_normal_reply_context(self):
        await self.seed()
        context = await memory.build_context(10, "nixi what do you think of me?", "Guest 20", "DIRECT_ADDRESS",
                                             guild_id=1, current_author_id=20, guild=self.guild)
        self.assertTrue(any(m["content"].startswith("SUBJECTIVE SOCIAL CONTEXT") for m in context))
        self.assertEqual(json.loads(context[-1]["content"])["text"], "nixi what do you think of me?")
        self.model.assert_not_awaited()

    async def test_normal_member_opinion_questions_are_not_protected_requests(self):
        for text in ["nixi who's your favorite here?", "nixi who do you dislike?", "nixi what do you think of me?"]:
            self.assertEqual(await social.classify_guest_message(text), "question")
        self.model.assert_not_awaited()


class OpinionRoutingTests(unittest.TestCase):
    def test_ranking_detection_without_a_member_mention(self):
        for text in ["nixi who is your favorite?", "who's your least favourite?", "rank the members",
                     "your opinions on everyone here", "tumhe kaun pasand hai", "least favorite?", "nixi favorites?"]:
            with self.subTest(text=text):
                self.assertTrue(opinions.asks_member_ranking(text))
        for text in ["what's your favorite food?", "I dislike this game", "your opinion on ranked games"]:
            self.assertFalse(opinions.asks_member_ranking(text))

    def test_only_public_human_direct_exchanges_are_observed(self):
        channel = MagicMock(spec=discord.TextChannel)
        channel.permissions_for.return_value = SimpleNamespace(view_channel=True)
        message = SimpleNamespace(author=SimpleNamespace(bot=False), channel=channel,
                                  guild=SimpleNamespace(default_role=object()))
        self.assertTrue(opinions.can_observe(message, "DIRECT_ADDRESS"))
        self.assertTrue(opinions.can_observe(message, "REPLY_CHAIN"))
        self.assertFalse(opinions.can_observe(message, None))
        self.assertFalse(opinions.can_observe(message, "OWNER_UNAVAILABLE"))
        channel.permissions_for.return_value.view_channel = False
        self.assertFalse(opinions.can_observe(message, "DIRECT_ADDRESS"))
        channel.permissions_for.return_value.view_channel = True
        message.author.bot = True
        self.assertFalse(opinions.can_observe(message, "DIRECT_ADDRESS"))
        message.author.bot = False
        message.guild = None
        self.assertFalse(opinions.can_observe(message, "DIRECT_ADDRESS"))


class OpinionChatHookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(config, "DB_PATH", ":memory:"))
        self.stack.enter_context(patch.object(config, "ENABLE_NATURAL_OWNER_ACTIONS", False))
        self.stack.enter_context(patch.object(memory, "log_message", AsyncMock()))
        self.stack.enter_context(patch.object(memory, "build_context", AsyncMock(return_value=[{"role": "user", "content": "a joke"}])))
        self.stack.enter_context(patch.object(social, "classify_guest_message", AsyncMock(return_value="banter")))
        self.stack.enter_context(patch("cogs.chat.conversation.generate_reply", AsyncMock(return_value="An ambitious pun.")))
        self.stack.enter_context(patch("cogs.chat.emoji_filter.filter_response", AsyncMock(return_value="An ambitious pun.")))
        self.observe = self.stack.enter_context(patch.object(opinions, "observe", AsyncMock()))
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = 10
        channel.permissions_for.return_value = SimpleNamespace(view_channel=True)
        self.message = SimpleNamespace(
            author=SimpleNamespace(id=config.OWNER_ID + 1, bot=False, display_name="Guest", name="guest"),
            guild=SimpleNamespace(id=1, default_role=object(), get_member=lambda _: None), channel=channel,
            content="Nixi another terrible gaming pun incoming", mentions=[], reference=None,
            reply=AsyncMock(return_value=SimpleNamespace(id=11)),
        )
        self.cog = ChatCog(SimpleNamespace(user=SimpleNamespace(id=999)))
        self.cog._detect_mode = AsyncMock(return_value="DIRECT_ADDRESS")
        self.cog._maybe_send_stfu_sticker = AsyncMock(return_value=False)
        self.cog._quick_reaction = AsyncMock(return_value=(None, None, None))

    async def asyncTearDown(self):
        await memory.shutdown()
        await db.close()
        self.stack.close()

    async def test_public_direct_chat_schedules_learning(self):
        await self.cog._handle_message(self.message)
        await asyncio.gather(*list(memory._background_tasks))
        self.observe.assert_awaited_once_with(1, self.message.author.id, self.message.content)
        self.message.reply.assert_awaited_once()

    async def test_private_and_unaddressed_chat_does_not_schedule_learning(self):
        self.message.channel.permissions_for.return_value.view_channel = False
        await self.cog._handle_message(self.message)
        self.observe.assert_not_awaited()
        self.message.channel.permissions_for.return_value.view_channel = True
        self.cog._detect_mode.return_value = None
        await self.cog._handle_message(self.message)
        self.observe.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
