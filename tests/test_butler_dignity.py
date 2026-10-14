import unittest

from cogs.chat import is_unbecoming_request


class ButlerDignityTests(unittest.TestCase):
    def test_performance_requests(self):
        for text in (
            'nixi say uwu', 'can you repeat "owo"?', 'reply with u-w-u',
            'pretend to be a catboy nixi', 'say you are a good boy',
        ):
            with self.subTest(text=text):
                self.assertTrue(is_unbecoming_request(text))

    def test_discussion_and_normal_banter_are_not_blocked(self):
        for text in (
            'nixi what does uwu mean?', 'explain catboy memes',
            "don't say uwu", 'never repeat owo', 'say good morning',
            'nixi let him cook', 'that meme says uwu',
        ):
            with self.subTest(text=text):
                self.assertFalse(is_unbecoming_request(text))
