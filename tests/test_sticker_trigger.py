import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cogs.chat import ChatCog


class StickerTriggerTests(unittest.IsolatedAsyncioTestCase):
    def message(self, text, stickers, bot=False):
        return SimpleNamespace(content=text, author=SimpleNamespace(bot=bot),
            guild=SimpleNamespace(id=1, stickers=stickers, fetch_stickers=AsyncMock(return_value=[])),
            channel=SimpleNamespace(id=10, send=AsyncMock()))

    async def test_static_animated_and_typed_emoji_send_one_sticker(self):
        cog = ChatCog(SimpleNamespace())
        sticker = SimpleNamespace(name='Kidhar_Chali', available=True)
        for text in [':stfu:', '<:stfu:123>', '<a:STFU:123> <:stfu:123>']:
            message = self.message(text, [sticker])
            with patch('cogs.chat.db.record_bot_activity', AsyncMock()):
                self.assertTrue(await cog._maybe_send_stfu_sticker(message))
            self.assertEqual(message.channel.send.call_args.kwargs['stickers'], [sticker])
            message.channel.send.assert_awaited_once()

    async def test_bots_and_unrelated_text_do_not_trigger(self):
        cog = ChatCog(SimpleNamespace())
        for text, is_bot in [(':stfu:', True), ('stfu', False), (':stfu_extra:', False)]:
            message = self.message(text, [], bot=is_bot)
            self.assertFalse(await cog._maybe_send_stfu_sticker(message))
            message.channel.send.assert_not_awaited()

    async def test_cache_miss_fetches_sticker_and_missing_sticker_is_quiet(self):
        cog = ChatCog(SimpleNamespace())
        sticker = SimpleNamespace(name='kidhar chali', available=True)
        message = self.message(':stfu:', [])
        message.guild.fetch_stickers.return_value = [sticker]
        with patch('cogs.chat.db.record_bot_activity', AsyncMock()):
            self.assertTrue(await cog._maybe_send_stfu_sticker(message))
        message.channel.send.assert_awaited_once()
        missing = self.message(':stfu:', [SimpleNamespace(name='kidhar chali', available=False)])
        with self.assertLogs('nixi.chat', level='WARNING'):
            self.assertTrue(await cog._maybe_send_stfu_sticker(missing))
        missing.channel.send.assert_not_awaited()
