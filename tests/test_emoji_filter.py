import unittest
from types import SimpleNamespace

from services.emoji_filter import filter_response


class EmojiFilterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(members=[], emojis=[
            SimpleNamespace(name="tea", id=1551602238382801048, animated=False),
            SimpleNamespace(name="dance", id=1551602238382801049, animated=True),
        ])

    async def test_existing_tags_remain_intact(self):
        text = "Tea <:tea:1551602238382801048> <a:dance:1551602238382801049>"
        self.assertEqual(await filter_response(text, self.guild), text)

    async def test_short_names_convert_once(self):
        text = "Tea :tea: :dance:"
        expected = "Tea <:tea:1551602238382801048> <a:dance:1551602238382801049>"
        result = await filter_response(text, self.guild)
        self.assertEqual(result, expected)
        self.assertEqual(await filter_response(result, self.guild), expected)

    async def test_unknown_id_removed_without_trailing_digits(self):
        self.assertEqual(await filter_response("Hello <:tea:123456789123456789>", self.guild), "Hello")

    async def test_no_guild_does_not_leave_ids(self):
        self.assertEqual(await filter_response("Hello <:tea:1551602238382801048> :tea:", None), "Hello")
