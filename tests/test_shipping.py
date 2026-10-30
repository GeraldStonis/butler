import asyncio
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

import config
from services import database as db, llm, memory, shipping


CHANGED_VIEW = "Based on recent developments, I have changed my views."


class ShippingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.directory = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(config, "DB_PATH", str(Path(self.directory.name) / "shipping.db")))
        self.stack.enter_context(patch.object(config, "OWNER_ID", 100))
        self.stack.enter_context(patch.object(config, "OWNER_ALIASES", ["ronix", "roni"]))
        self.stack.enter_context(patch.object(config, "OWNER_NAMES", ["ronix", "roni"]))
        self.model = self.stack.enter_context(patch.object(llm, "generate_response", AsyncMock(return_value='{"skip":true}')))
        self.logged = self.stack.enter_context(patch.object(memory, "log_message", AsyncMock()))
        self.members = [self.member(20, "Ada"), self.member(21, "Ben"),
                        self.member(22, "Cora"), self.member(23, "Dan"),
                        self.member(24, "Eli"), self.member(100, "Sir Ronix"),
                        self.member(999, "Nixi", bot=True)]
        self.guild = self.make_guild(1)
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id = 10
        self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True)
        self.next_message_id = 1000
        await db.get_db()

    async def asyncTearDown(self):
        await db.close()
        self.stack.close()
        self.directory.cleanup()

    @staticmethod
    def member(user_id, display_name, *, bot=False):
        return SimpleNamespace(id=user_id, display_name=display_name,
                               name=display_name.lower().replace(" ", "_"), bot=bot)

    def make_guild(self, guild_id):
        guild = SimpleNamespace(id=guild_id, members=self.members, default_role=object())
        guild.get_member = lambda member_id: next((member for member in guild.members if member.id == member_id), None)
        return guild

    def message(self, content, *, author_id=20, mentions=(), guild=None, channel=None):
        self.next_message_id += 1
        guild = guild or self.guild
        author = guild.get_member(author_id)
        return SimpleNamespace(
            id=self.next_message_id, author=author, content=content, clean_content=content,
            guild=guild, channel=channel or self.channel,
            mentions=[guild.get_member(member_id) for member_id in mentions],
            raw_mentions=list(mentions), reference=None, attachments=[], webhook_id=None,
            _state=SimpleNamespace(self_id=999),
            reply=AsyncMock(return_value=SimpleNamespace(id=self.next_message_id + 10000)),
        )

    async def samples(self, first_id=20, second_id=21, *, guild_id=1, now=None, count=4):
        now = time.time() if now is None else now
        texts = ["Your terrible puns always make my evening brighter.",
                 "I saved a seat for you at our next game.",
                 "I really enjoy spending this time chatting with you.",
                 "You always make these late games so much more fun."]
        rows = []
        for index in range(count):
            self.next_message_id += 1
            author_id, target_id = (first_id, second_id) if index % 2 == 0 else (second_id, first_id)
            text = texts[index % len(texts)]
            if index >= len(texts):
                text += f" Another evening number {index}."
            await db.record_ship_sample(guild_id, 10, self.next_message_id, author_id, target_id, text, now)
            rows.append({"message_id": self.next_message_id, "author_id": author_id, "content": text})
        return rows

    async def seed(self, first_id=20, second_id=21, *, affinity=1, other_affinity=None,
                   guild_id=1, now=None, tone="warmth"):
        now = time.time() if now is None else now
        rows = await self.samples(first_id, second_id, guild_id=guild_id, now=now)
        await db.save_ship_pair(guild_id, first_id, second_id, affinity,
                                affinity if other_affinity is None else other_affinity, tone, now)
        return rows

    @staticmethod
    def verdict(rows, *, low=1, high=1, tone="warmth"):
        return {"low_affinity": low, "high_affinity": high, "tone": tone,
                "evidence": [{"message_id": row["message_id"], "quote": row["content"]} for row in rows]}

    async def ask(self, text, *, reply_to=None, **kwargs):
        message = self.message(text, **kwargs)
        self.assertTrue(await shipping.handle_request(message, reply_to=reply_to))
        message.reply.assert_awaited_once()
        return message.reply.await_args.args[0], message

    def assert_insufficient(self, reply):
        self.assertRegex(reply.casefold(), r"(?:not enough|insufficient|haven.t enough|too little|don.t have enough|no (?:clear|strong|reliable|supported)|need more)")

    async def test_observation_is_public_direct_human_to_human_and_does_not_call_model(self):
        message = self.message("<@21> your terrible puns always brighten my evening", mentions=[21])
        self.assertEqual(await shipping.observe(message), (20, 21))
        rows = await db.get_ship_samples(1, 20, 21, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["author_id"], rows[0]["target_id"]), (20, 21))
        self.model.assert_not_awaited()

    async def test_reply_to_a_human_can_supply_the_single_target(self):
        message = self.message("Your ridiculous jokes are always my favorite part of game night")
        reply_to = SimpleNamespace(author=self.guild.get_member(21))
        self.assertEqual(await shipping.observe(message, reply_to=reply_to), (20, 21))

    async def test_private_and_non_text_channels_are_not_sampled(self):
        message = self.message("<@21> your terrible puns always brighten my evening", mentions=[21])
        self.channel.permissions_for.return_value.view_channel = False
        self.assertIsNone(await shipping.observe(message))
        self.channel.permissions_for.return_value.view_channel = True
        message.channel = SimpleNamespace(id=11)
        self.assertIsNone(await shipping.observe(message))
        message.channel = self.channel
        message.guild = None
        self.assertIsNone(await shipping.observe(message))
        self.assertIsNone(await db.get_ship_pair(1, 20, 21))

    async def test_bots_self_and_ambiguous_targets_are_not_sampled(self):
        cases = [(999, [21]), (20, [999]), (20, [20]), (20, [21, 22])]
        for author_id, targets in cases:
            with self.subTest(author_id=author_id, targets=targets):
                message = self.message("Your terrible puns always brighten my evening", author_id=author_id, mentions=targets)
                self.assertIsNone(await shipping.observe(message))
        message = self.message("<@21> your terrible puns always brighten my evening", mentions=[21])
        self.assertIsNone(await shipping.observe(message, reply_to=SimpleNamespace(author=self.guild.get_member(22))))
        self.assertEqual(await db.list_ship_pairs(1, 0), [])

    async def test_questions_quotes_and_third_party_gossip_are_not_evidence(self):
        for text in ["Nixi who loves <@21>?", "> <@21> I love talking with you every night",
                     '<@21> Ada said "I really love you" yesterday',
                     "<@21> I heard Cora has a massive crush on you"]:
            with self.subTest(text=text):
                self.assertIsNone(await shipping.observe(self.message(text, mentions=[21])))
        self.assertIsNone(await db.get_ship_pair(1, 20, 21))
        self.model.assert_not_awaited()

    async def test_duplicate_messages_do_not_add_evidence(self):
        text = "<@21> your terrible puns always brighten my evening"
        await shipping.observe(self.message(text, mentions=[21]))
        await shipping.observe(self.message(text, mentions=[21]))
        self.assertEqual(len(await db.get_ship_samples(1, 20, 21, 0)), 1)
        self.assertEqual((await db.get_ship_pair(1, 20, 21))["interaction_count"], 1)

    async def test_each_participant_preference_prevents_sampling(self):
        for participant in (20, 21):
            for preference in ("flirting", "introductions", "shipping"):
                with self.subTest(participant=participant, preference=preference):
                    await db.set_member_preferences(1, participant, {preference: False})
                    self.assertIsNone(await shipping.observe(self.message(
                        "<@21> your terrible puns always brighten my evening", mentions=[21])))
                    await db.set_member_preferences(1, participant, {preference: True})
        self.assertIsNone(await db.get_ship_pair(1, 20, 21))

    async def test_review_requires_repeated_evidence_from_both_authors(self):
        await self.samples(count=3)
        await shipping.review_pair(1, 20, 21)
        self.model.assert_not_awaited()
        await db.record_ship_sample(1, 10, 9999, 20, 21, "One more lovely exchange from only the same speaker.", time.time())
        await shipping.review_pair(1, 20, 21)
        self.model.assert_not_awaited()

    async def test_review_saves_grounded_directional_warmth_without_member_identifiers(self):
        rows = await self.samples()
        self.model.return_value = json.dumps(self.verdict(rows, low=2, high=1, tone="playful"))
        await shipping.review_pair(1, 20, 21)
        self.model.assert_awaited_once()
        pair = await db.get_ship_pair(1, 20, 21)
        self.assertGreater(pair["low_affinity"], 0)
        self.assertGreater(pair["high_affinity"], 0)
        self.assertEqual(pair["tone"], "playful")
        payload = json.loads(self.model.await_args.args[0][-1]["content"])
        self.assertEqual({entry["speaker"] for entry in payload["messages"]}, {"low", "high"})
        self.assertEqual(len(payload["messages"]), 4)
        for entry in payload["messages"]:
            self.assertNotIn("author_id", entry)
            self.assertNotIn("target_id", entry)
        self.assertNotIn("Ada", json.dumps(payload))
        self.assertNotIn("Ben", json.dumps(payload))

    async def test_forged_or_misattributed_citations_do_not_replace_a_pair(self):
        for invalid in ("made_up", "wrong_author", "repeated"):
            with self.subTest(invalid=invalid):
                first_id, second_id = (20, 21) if invalid == "made_up" else ((20, 22) if invalid == "wrong_author" else (20, 23))
                rows = await self.seed(first_id, second_id, affinity=1)
                verdict = self.verdict(rows, low=-1, high=-1, tone="boundary")
                if invalid == "made_up":
                    verdict["evidence"][0]["quote"] = "This conversation never actually happened."
                elif invalid == "wrong_author":
                    verdict["evidence"][0]["quote"] = rows[1]["content"]
                else:
                    verdict["evidence"] = [verdict["evidence"][0]] * 4
                self.model.return_value = json.dumps(verdict)
                await shipping.review_pair(1, first_id, second_id)
                pair = await db.get_ship_pair(1, first_id, second_id)
                self.assertEqual((pair["low_affinity"], pair["high_affinity"], pair["tone"]), (1, 1, "warmth"))

    async def test_malformed_provider_output_preserves_the_previous_view(self):
        for index, output in enumerate(["not json", "[]", '{"skip":true}',
                                        '{"low_affinity":true,"high_affinity":1,"tone":"warmth","evidence":[]}']):
            with self.subTest(output=output):
                second_id = 21 + index
                await self.seed(20, second_id, affinity=1)
                self.model.return_value = output
                await shipping.review_pair(1, 20, second_id)
                pair = await db.get_ship_pair(1, 20, second_id)
                self.assertEqual((pair["low_affinity"], pair["high_affinity"]), (1, 1))

    async def test_optout_during_inference_prevents_saving_the_result(self):
        rows = await self.samples()

        async def opt_out(*args, **kwargs):
            await db.set_member_preferences(1, 21, {"shipping": False})
            return json.dumps(self.verdict(rows))

        self.model.side_effect = opt_out
        await shipping.review_pair(1, 20, 21)
        pair = await db.get_ship_pair(1, 20, 21)
        self.assertEqual((pair["low_affinity"], pair["high_affinity"]), (0, 0))

    async def test_pair_review_reservation_is_atomic_and_cooldown_persists(self):
        await self.samples(now=1000)
        reservations = await asyncio.gather(*(db.claim_ship_review(1, 20, 21, 1000) for _ in range(5)))
        self.assertEqual(sum(reservations), 1)
        await self.samples(now=1001)
        # Distinct message IDs alone do not make repeated text new evidence.
        self.assertFalse(await db.claim_ship_review(1, 20, 21, 1900))
        for index in range(4):
            author_id, target_id = (20, 21) if index % 2 == 0 else (21, 20)
            await db.record_ship_sample(1, 10, 20000 + index, author_id, target_id,
                                        f"A genuinely new direct exchange number {index}", 1001)
        await db.close()
        self.assertFalse(await db.claim_ship_review(1, 20, 21, 1899))
        self.assertTrue(await db.claim_ship_review(1, 20, 21, 1900))

    async def test_paraphrases_mentions_name_and_self_share_a_stable_answer(self):
        await self.seed()
        replies = []
        for text, mentions in [("Nixi who loves <@20>?", [20]),
                               ("Who would be perfect for Ada?", []),
                               ("Nixi who do you ship me with?", [])]:
            reply, _ = await self.ask(text, mentions=mentions)
            replies.append(reply)
            self.assertIn("Ada", reply)
            self.assertIn("Ben", reply)
            self.assertNotIn(CHANGED_VIEW, reply)
        self.assertEqual(len(set(replies)), 1)
        self.model.assert_not_awaited()

    async def test_no_signal_does_not_invent_a_lover(self):
        reply, _ = await self.ask("Nixi who would be perfect for Ada?")
        self.assert_insufficient(reply)
        self.assertNotIn("Ben", reply)
        self.assertNotIn("Cora", reply)
        self.model.assert_not_awaited()

    async def test_only_reciprocal_positive_patterns_make_a_match(self):
        await self.seed(20, 21, affinity=2, other_affinity=0)
        await self.seed(20, 22, affinity=1, other_affinity=-1, tone="boundary")
        reply, _ = await self.ask("Who would be perfect for Ada?")
        self.assert_insufficient(reply)
        self.assertNotIn("Ben", reply)
        self.assertNotIn("Cora", reply)

    async def test_ranking_prefers_stronger_reciprocal_evidence(self):
        await self.seed(20, 21, affinity=1)
        await self.seed(20, 22, affinity=2)
        reply, _ = await self.ask("Who would be perfect for Ada?")
        self.assertIn("Cora", reply)
        if "Ben" in reply:
            self.assertLess(reply.index("Cora"), reply.index("Ben"))
        self.assertNotRegex(reply.casefold(), r"low_affinity|high_affinity|interaction_count|score[:=]")
        self.assertNotIn("Your terrible puns", reply)
        self.assertRegex(reply.casefold(), r"playful|guess|chemistry|vibes?|banter")

    async def test_pairs_from_other_guilds_stale_pairs_bots_and_leavers_are_excluded(self):
        await self.seed(20, 21, affinity=2, guild_id=2)
        await self.seed(20, 22, affinity=2, now=time.time() - 31 * 86400)
        await self.seed(20, 999, affinity=2)
        await self.seed(20, 777, affinity=2)
        reply, _ = await self.ask("Who would be perfect for Ada?")
        self.assert_insufficient(reply)
        for excluded in ("Ben", "Cora", "Nixi", "777"):
            self.assertNotIn(excluded, reply)

    async def test_participant_optouts_remove_existing_matches(self):
        await self.seed()
        for participant in (20, 21):
            for preference in ("flirting", "introductions", "shipping"):
                with self.subTest(participant=participant, preference=preference):
                    await db.set_member_preferences(1, participant, {preference: False})
                    reply, _ = await self.ask("Who would be perfect for Ada?")
                    self.assertNotIn("Ben", reply)
                    await db.set_member_preferences(1, participant, {preference: True})

    async def test_owner_alias_uses_the_verified_member(self):
        await self.seed(21, 100, affinity=1)
        reply, _ = await self.ask("Nixi who would be perfect for roni?")
        self.assertIn("Sir Ronix", reply)
        self.assertIn("Ben", reply)

    async def test_ambiguous_and_unknown_names_are_clarified_without_guessing(self):
        self.members.append(self.member(25, "Ada"))
        await self.seed()
        reply, _ = await self.ask("Who would be perfect for Ada?")
        self.assertRegex(reply.casefold(), r"mention|which|multiple|more than one")
        self.assertNotIn("Ben", reply)
        reply, _ = await self.ask("Who would be perfect for SomebodyMissing?")
        self.assertNotIn("Ben", reply)
        self.assertRegex(reply.casefold(), r"mention|find|recognize|recognise|member|which|who")

    async def test_new_evidence_announces_changed_view_once_across_paraphrases_and_restart(self):
        await self.seed(20, 21, affinity=1)
        original, _ = await self.ask("Who would be perfect for Ada?")
        self.assertIn("Ben", original)
        await db.save_ship_pair(1, 20, 21, 0, 0, "neutral", time.time())
        await self.seed(20, 22, affinity=2)
        await db.close()
        revised, _ = await self.ask("Nixi who loves <@20>?", mentions=[20])
        self.assertIn(CHANGED_VIEW, revised)
        self.assertIn("Cora", revised)
        self.assertNotIn("Ben", revised)
        await db.close()
        repeated, _ = await self.ask("Who do you ship Ada with?")
        self.assertIn("Cora", repeated)
        self.assertNotIn(CHANGED_VIEW, repeated)

    async def test_failed_delivery_keeps_changed_view_notice_pending(self):
        await self.seed(20, 21, affinity=1)
        await self.ask("Who would be perfect for Ada?")
        await db.save_ship_pair(1, 20, 21, 0, 0, "neutral", time.time())
        await self.seed(20, 22, affinity=2)
        message = self.message("Nixi who loves <@20>?", mentions=[20])
        message.reply.side_effect = RuntimeError("test delivery failed")
        try:
            await shipping.handle_request(message)
        except RuntimeError:
            pass
        self.assertIn(CHANGED_VIEW, message.reply.await_args.args[0])
        revised, _ = await self.ask("Who would be perfect for Ada?")
        self.assertIn(CHANGED_VIEW, revised)
        repeated, _ = await self.ask("Who would be perfect for Ada?")
        self.assertNotIn(CHANGED_VIEW, repeated)

    async def test_score_change_without_changed_match_does_not_announce_a_new_view(self):
        await self.seed(20, 21, affinity=1)
        await self.ask("Who would be perfect for Ada?")
        await db.save_ship_pair(1, 20, 21, 2, 2, "affection", time.time())
        reply, _ = await self.ask("Who loves Ada?")
        self.assertIn("Ben", reply)
        self.assertNotIn(CHANGED_VIEW, reply)

    async def test_observed_rejection_immediately_revises_a_previously_delivered_ship(self):
        await self.seed()
        await self.ask("Who would be perfect for Ada?")
        message = self.message(
            "<@21> I am not interested in you that way, please stop flirting with me.", mentions=[21])
        self.assertEqual(await shipping.observe(message), (20, 21))
        revised, _ = await self.ask("Who loves Ada?")
        self.assertIn(CHANGED_VIEW, revised)
        self.assert_insufficient(revised)
        self.assertNotIn("Ben", revised)
        self.model.assert_not_awaited()

    async def test_reversed_pair_questions_use_the_same_saved_view(self):
        await self.seed()
        first, _ = await self.ask("Nixi do Ada and Ben have chemistry?")
        reversed_reply, _ = await self.ask("Would Ben and Ada make a good couple?")
        self.assertEqual(first, reversed_reply)
        await db.save_ship_pair(1, 20, 21, 0, 0, "neutral", time.time())
        revised, _ = await self.ask("Do Ben and Ada belong together?")
        self.assertIn(CHANGED_VIEW, revised)
        repeated, _ = await self.ask("Do Ada and Ben have chemistry?")
        self.assertNotIn(CHANGED_VIEW, repeated)

    async def test_ship_me_with_a_mention_resolves_the_speaker_and_target_pair(self):
        await self.seed(20, 21, affinity=1)
        await self.seed(21, 23, affinity=2)
        pair_reply, _ = await self.ask("Do Ada and Ben have chemistry?")
        reply, _ = await self.ask("Nixi ship me with <@21>", mentions=[21])
        self.assertEqual(reply, pair_reply)
        self.assertNotIn("Dan", reply)

    async def test_who_do_you_ship_returns_the_same_server_view(self):
        await self.seed(20, 21, affinity=1)
        await self.seed(22, 23, affinity=2)
        global_reply, _ = await self.ask("Who loves who?")
        reply, _ = await self.ask("Nixi who do you ship?")
        self.assertEqual(reply, global_reply)
        for name in ("Ada", "Ben", "Cora", "Dan"):
            self.assertIn(name, reply)

    async def test_model_in_flight_cannot_overwrite_a_new_explicit_rejection(self):
        rows = await self.seed()

        async def rejection_during_inference(*args, **kwargs):
            message = self.message(
                "<@21> I am not interested in you that way, please stop flirting with me.", mentions=[21])
            await shipping.observe(message)
            return json.dumps(self.verdict(rows, low=2, high=2, tone="affection"))

        self.model.side_effect = rejection_during_inference
        await shipping.review_pair(1, 20, 21)
        self.model.assert_awaited_once()
        pair = await db.get_ship_pair(1, 20, 21)
        self.assertEqual(pair["tone"], "boundary")
        self.assertLessEqual(pair["low_affinity"], 0)
        self.assertLessEqual(pair["high_affinity"], 0)

    async def test_previously_public_evidence_is_rechecked_before_answering(self):
        self.guild.get_channel = lambda channel_id: self.channel if channel_id == 10 else None
        await self.seed()
        initial, _ = await self.ask("Who would be perfect for Ada?")
        self.assertIn("Ben", initial)
        self.channel.permissions_for.return_value.view_channel = False
        revised, _ = await self.ask("Who would be perfect for Ada?")
        self.assert_insufficient(revised)
        self.assertNotIn("Ben", revised)
        self.assertIn(CHANGED_VIEW, revised)
        self.channel.permissions_for.return_value.view_channel = True
        self.guild.get_channel = lambda channel_id: None
        deleted, _ = await self.ask("Who would be perfect for Ada?")
        self.assert_insufficient(deleted)
        self.assertNotIn("Ben", deleted)

    async def test_distressed_rejection_invalidates_the_ship_without_retaining_distress(self):
        await self.seed()
        message = self.message("<@21> I am having a panic attack, stop flirting with me.", mentions=[21])
        await shipping.observe(message)
        pair = await db.get_ship_pair(1, 20, 21)
        self.assertEqual(pair["tone"], "boundary")
        self.assertLessEqual(pair["low_affinity"], 0)
        self.assertLessEqual(pair["high_affinity"], 0)
        samples = await db.get_ship_samples(1, 20, 21, 0)
        self.assertNotIn("panic attack", json.dumps(samples).casefold())
        self.model.assert_not_awaited()

    async def test_members_named_who_or_me_do_not_capture_general_or_self_queries(self):
        self.members.extend([self.member(25, "Who"), self.member(26, "Me")])
        await self.seed(20, 21, affinity=1)
        await self.seed(22, 23, affinity=2)
        await self.seed(26, 23, affinity=2)
        general, _ = await self.ask("Who loves who?")
        for name in ("Ada", "Ben", "Cora", "Dan"):
            self.assertIn(name, general)
        own, _ = await self.ask("Who would be perfect for me?")
        self.assertIn("Ada", own)
        self.assertIn("Ben", own)
        self.assertNotIn("Dan", own)

    async def test_mixed_mention_and_name_resolve_the_same_pair(self):
        await self.seed(20, 22, affinity=1)
        await self.seed(20, 21, affinity=2)
        expected, _ = await self.ask("Do Ada and Cora have chemistry?")
        mixed, _ = await self.ask("Nixi ship <@20> with Cora", mentions=[20])
        self.assertEqual(mixed, expected)
        self.assertNotIn("Ben", mixed)

    async def test_optout_while_loading_delivered_state_is_respected_before_sending(self):
        await self.seed()
        original_get_state = db.get_state

        async def opt_out_during_state_read(key):
            value = await original_get_state(key)
            await db.set_member_preferences(1, 21, {"shipping": False})
            return value

        with patch.object(db, "get_state", AsyncMock(side_effect=opt_out_during_state_read)):
            reply, _ = await self.ask("Who would be perfect for Ada?")
        self.assertNotIn("Ben", reply)
        self.assert_insufficient(reply)

    async def test_rejection_while_loading_delivered_state_is_respected_before_sending(self):
        await self.seed()
        original_get_state = db.get_state

        async def rejection_during_state_read(key):
            value = await original_get_state(key)
            await shipping.observe(self.message(
                "<@21> I am not interested in you that way, please stop flirting with me.", mentions=[21]))
            return value

        with patch.object(db, "get_state", AsyncMock(side_effect=rejection_during_state_read)):
            reply, _ = await self.ask("Who would be perfect for Ada?")
        self.assertNotIn("Ben", reply)
        self.assert_insufficient(reply)

    async def test_reply_anchor_resolves_this_user_him_and_her_consistently(self):
        await self.seed(22, 23, affinity=1)
        await self.seed(20, 21, affinity=2)
        anchor = SimpleNamespace(author=self.guild.get_member(22))
        replies = []
        for referent in ("this user", "him", "her"):
            reply, _ = await self.ask(f"Nixi who would be perfect for {referent}?", reply_to=anchor)
            self.assertIn("Cora", reply)
            self.assertIn("Dan", reply)
            self.assertNotIn("Ben", reply)
            replies.append(reply)
        self.assertEqual(len(set(replies)), 1)

    async def test_guild_view_and_member_view_have_separate_delivered_state(self):
        await self.seed(20, 21, affinity=1)
        await self.ask("Who would be perfect for Ada?")
        await db.save_ship_pair(1, 20, 21, 0, 0, "neutral", time.time())
        await self.seed(20, 22, affinity=2)
        guild_reply, _ = await self.ask("Nixi who loves who?")
        self.assertIn("Cora", guild_reply)
        self.assertNotIn(CHANGED_VIEW, guild_reply)
        member_reply, _ = await self.ask("Who would be perfect for Ada?")
        self.assertIn(CHANGED_VIEW, member_reply)

    async def test_answers_use_live_display_names_and_disable_all_mentions(self):
        await self.seed()
        self.guild.get_member(21).display_name = "Renamed Ben @everyone"
        reply, message = await self.ask("Who would be perfect for Ada?")
        self.assertIn("Renamed Ben", reply)
        self.assertNotIn("<@", reply)
        self.assertNotIn("@everyone", reply)
        allowed = message.reply.await_args.kwargs.get("allowed_mentions")
        self.assertIsInstance(allowed, discord.AllowedMentions)
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.users)
        self.assertFalse(allowed.roles)
        self.assertFalse(message.reply.await_args.kwargs.get("mention_author", True))
        self.logged.assert_awaited_once()


class ShippingRoutingTests(unittest.TestCase):
    def test_common_shipping_questions_have_one_route(self):
        for text in ["nixi who loves who?", "who likes whom?", "who would be perfect for Ada?",
                     "Who do you ship me with?", "nixi who is Ada's soulmate?", "Who has a crush on Ben?",
                     "who is the best match for <@20>?", "who should Ada date?", "nixi ship Ada",
                     "nixi ship <@20>", "nixi who do you ship?"]:
            with self.subTest(text=text):
                self.assertTrue(shipping.asks_shipping(text))

    def test_ordinary_love_and_shipping_talk_is_not_a_matchmaking_request(self):
        for text in ["I love this song", "who loves pizza?", "when will my order ship?",
                     "what is your favorite spaceship?", "I like Ben's idea", "what is love?",
                     "Nixi who do you like?", "Who do you love?", "Who would you like?", "Who is your crush?"]:
            with self.subTest(text=text):
                self.assertFalse(shipping.asks_shipping(text))


if __name__ == "__main__":
    unittest.main()
