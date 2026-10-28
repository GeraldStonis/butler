"""Shared media must not become an implicit request to Nixi."""

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

import config
from cogs.chat import ChatCog
from services import avatar_edits, conversation, database as db, intent, llm, media, memory, social


class MessageTriggerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await db.close()
        self.enterContext(patch.object(config, 'DB_PATH', ':memory:'))
        self.enterContext(patch.object(db, '_init_lock', asyncio.Lock()))
        self.enterContext(patch.object(config, 'OWNER_ID', 99))
        self.enterContext(patch.object(config, 'OWNER_ALIASES', ['ronix']))
        self.enterContext(patch.object(config, 'ENABLE_NATURAL_OWNER_ACTIONS', True))
        self.owner = SimpleNamespace(id=99, name='ronix', display_name='Ronix', bot=False)
        self.guest = SimpleNamespace(id=20, name='guest', display_name='Guest', bot=False)
        self.bot_user = SimpleNamespace(id=999, name='nixi', display_name='Nixi', bot=True)
        self.guild = SimpleNamespace(id=1, members=[self.owner, self.guest, self.bot_user])
        self.channel = SimpleNamespace(id=10, send=AsyncMock(), typing=MagicMock())
        self.cog = ChatCog(SimpleNamespace(user=self.bot_user))

    async def asyncTearDown(self):
        await memory.shutdown()
        await db.close()

    def message(self, content, *, author=None, mentions=None, attachments=None):
        return SimpleNamespace(
            content=content, author=author or self.owner, guild=self.guild,
            channel=self.channel, mentions=mentions or [], attachments=attachments or [],
            reference=None, reply=AsyncMock(),
        )

    def start_followup(self, author):
        session = (author.id, time.monotonic() + 60, 2)
        self.cog._followups[self.channel.id] = session
        return session

    async def test_owner_tenor_giphy_and_cdn_gif_shares_stay_silent(self):
        for url in [
            'https://tenor.com/view/happy-dance-gif-12345',
            'https://media.giphy.com/media/abc123/giphy.gif',
            'https://cdn.discordapp.com/attachments/10/11/dancing.gif?ex=123',
        ]:
            with self.subTest(url=url):
                self.assertIsNone(await self.cog._detect_mode(self.message(url)))

    async def test_ordinary_caption_with_shared_gif_stays_silent(self):
        message = self.message('look at this https://tenor.com/view/happy-dance-gif-12345')
        self.assertIsNone(await self.cog._detect_mode(message))

    async def test_bot_names_inside_url_slugs_do_not_address_nixi(self):
        for author, url in [
            (self.owner, 'https://tenor.com/view/nixi-dancing-gif-12345'),
            (self.owner, 'https://media.giphy.com/nixon/giphy.gif'),
            (self.guest, 'https://tenor.com/view/nixi-dancing-gif-12345'),
            (self.guest, 'https://media.giphy.com/nixon/giphy.gif'),
        ]:
            with self.subTest(author=author.id, url=url):
                self.assertIsNone(await self.cog._detect_mode(self.message(url, author=author)))

    async def test_wrapped_gif_urls_and_markdown_labels_stay_silent(self):
        for content in [
            'https://cdn.discordapp.com/attachments/10/11/dance(nixi).gif',
            '[funny gif](https://media.giphy.com/media/abc123/giphy.gif)',
            '![nixi gif](https://media.giphy.com/media/abc123/giphy.gif)',
            '<https://tenor.com/view/nixi-dancing-gif-12345>',
        ]:
            with self.subTest(content=content):
                self.assertIsNone(await self.cog._detect_mode(self.message(content)))

    async def test_attachment_only_gif_does_not_consume_active_followup(self):
        session = self.start_followup(self.owner)
        attachment = SimpleNamespace(filename='dancing.gif', content_type='image/gif',
                                     url='https://cdn.discordapp.com/attachments/10/11/dancing.gif')
        self.assertIsNone(await self.cog._detect_mode(self.message('', attachments=[attachment])))
        self.assertEqual(self.cog._followups[self.channel.id], session)

    async def test_url_only_share_does_not_consume_active_followup(self):
        session = self.start_followup(self.guest)
        # A bare i.giphy.com URL previously matched the conversational "i" prefix.
        message = self.message('i.giphy.com/media/abc123/giphy.gif', author=self.guest)
        self.assertIsNone(await self.cog._detect_mode(message))
        self.assertEqual(self.cog._followups[self.channel.id], session)

    async def test_real_owner_media_request_without_name_still_triggers(self):
        message = self.message('send me a dancing gif')
        self.assertEqual(await self.cog._detect_mode(message), 'DIRECT_ADDRESS')

    async def test_owner_search_and_play_url_requests_without_name_still_trigger(self):
        for content in [
            'search https://example.com/news',
            'look up https://example.com/news',
            'play https://www.youtube.com/watch?v=abc123',
        ]:
            with self.subTest(content=content):
                message = self.message(content)
                self.assertEqual(await self.cog._detect_mode(message), 'DIRECT_ADDRESS')
                self.assertEqual(message.content, content)

    async def test_explicit_name_with_shared_url_still_triggers(self):
        message = self.message('nixi look at this https://tenor.com/view/happy-dance-gif-12345',
                               author=self.guest)
        self.assertEqual(await self.cog._detect_mode(message), 'DIRECT_ADDRESS')

    async def test_actual_bot_mention_with_shared_url_still_triggers(self):
        message = self.message('<@999> https://media.giphy.com/media/abc123/giphy.gif',
                               author=self.guest, mentions=[self.bot_user])
        self.assertEqual(await self.cog._detect_mode(message), 'DIRECT_ADDRESS')

    async def test_shared_gif_in_reply_to_actual_bot_message_still_triggers(self):
        message = self.message('https://media.giphy.com/media/abc123/giphy.gif', author=self.guest)
        previous = MagicMock(spec=discord.Message)
        previous.author = self.bot_user
        previous.channel = self.channel
        previous.guild = self.guild
        message.reference = SimpleNamespace(message_id=111, resolved=previous)
        self.assertEqual(await self.cog._detect_mode(message), 'REPLY_CHAIN')

    async def test_dm_media_share_still_triggers(self):
        message = self.message('https://media.giphy.com/media/abc123/giphy.gif')
        message.guild = None
        message.channel = MagicMock(spec=discord.DMChannel)
        message.channel.id = 11
        self.assertEqual(await self.cog._detect_mode(message), 'DIRECT_ADDRESS')

    async def test_owner_gif_handler_records_activity_without_reply_or_generation(self):
        log_message = self.enterContext(patch.object(memory, 'log_message', AsyncMock()))
        guards = []
        for module, name in [
            (intent, 'classify_owner_intent'), (social, 'classify_guest_message'),
            (conversation, 'generate_reply'), (llm, 'generate_response'),
            (llm, 'generate_visible_response'), (media, 'fetch_available_gif'),
            (avatar_edits, 'maybe_reaction'), (self.cog._automatic_media, 'choose'),
        ]:
            guard = AsyncMock(side_effect=AssertionError(f'Unexpected {name} for a shared GIF'))
            guards.append(self.enterContext(patch.object(module, name, guard)))
        content = 'https://tenor.com/view/happy-dance-gif-12345'
        message = self.message(content)

        await self.cog._handle_message(message)

        message.reply.assert_not_awaited()
        self.channel.send.assert_not_awaited()
        for guard in guards:
            guard.assert_not_awaited()
        log_message.assert_awaited_once_with(
            channel_id=10, guild_id=1, author_id=99, author_name='Ronix', content=content,
            is_bot=False, reply_to_author_id=None,
        )
        activity = (await db.get_channel_idle_candidates([10]))[0]
        self.assertEqual(activity['last_user_id'], self.owner.id)
        self.assertEqual(activity['dead_ping_count'], 0)
        self.assertIsNotNone(await db.get_user_idle_seconds(self.guild.id, self.owner.id))
        self.assertEqual(message.content, content)
        self.assertEqual(self.cog._followups, {})
