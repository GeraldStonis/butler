import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import discord

import config
from services import actions, intent, search


class NewsBriefingTests(unittest.IsolatedAsyncioTestCase):
    def test_generic_morning_requests_keep_default_topic_sections(self):
        for request in ('nixi get me the morning news', 'nixi can you fetch the morning news?',
                        'nixi give us today\'s news please'):
            with self.subTest(request=request):
                parsed = intent.heuristic_intent(request)
                self.assertEqual(parsed.name, 'news')
                self.assertEqual(parsed.query, '')
        self.assertEqual(intent.heuristic_intent('nixi fetch morning news about India').query, 'india')

    def source(self, topic='world', *, title=None, description='A verified development.'):
        return search.SearchItem(title or f'{topic.title()} headline', f'https://example.test/{topic}', description)

    async def test_default_topics_have_ordered_separate_full_width_fields(self):
        topics = ['world', 'india', 'technology', 'cybersecurity']

        async def fetch(topic, *, count):
            self.assertEqual(count, 3)
            return [self.source(topic)]

        async def summarize(topic, results, **kwargs):
            self.assertEqual(results[0].url, f'https://example.test/{topic}')
            self.assertEqual(kwargs, {'mode': 'news', 'include_links': False, 'language_request': ''})
            return f'Latest {topic} development.'

        with patch.object(config, 'MORNING_NEWS_TOPICS', topics), \
                patch.object(search, 'news_search', AsyncMock(side_effect=fetch)) as fetched, \
                patch.object(search, 'summarize_results', AsyncMock(side_effect=summarize)):
            embed = await actions.build_news_briefing()

        self.assertIsInstance(embed, discord.Embed)
        self.assertEqual(embed.title, 'Morning News')
        self.assertEqual([field.name for field in embed.fields], [topic.title() for topic in topics])
        self.assertTrue(all(not field.inline for field in embed.fields))
        self.assertEqual([field.value for field in embed.fields], [f'Latest {topic} development.' for topic in topics])
        self.assertEqual(fetched.await_args_list, [call(topic, count=3) for topic in topics])
        self.assertIn('Recent verified headlines', embed.footer.text)

    async def test_topic_fetches_run_concurrently_and_are_bounded_to_six(self):
        entered = set()
        ready = asyncio.Event()

        async def fetch(topic, *, count):
            entered.add(topic)
            if len(entered) == 6:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=1)
            return [self.source(topic)]

        with patch.object(config, 'MORNING_NEWS_TOPICS', [' ', 'world', ' WORLD ', 'india', 'tech', 'cyber', 'science', 'sport', 'extra']), \
                patch.object(search, 'news_search', AsyncMock(side_effect=fetch)), \
                patch.object(search, 'summarize_results', AsyncMock(return_value='A verified development.')):
            embed = await actions.build_news_briefing()

        self.assertEqual(entered, {'world', 'india', 'tech', 'cyber', 'science', 'sport'})
        self.assertEqual(len(embed.fields), 6)

    async def test_explicit_topic_and_requested_language_and_links_are_forwarded(self):
        item = self.source('space')
        with patch.object(search, 'news_search', AsyncMock(return_value=[item])) as fetched, \
                patch.object(search, 'summarize_results', AsyncMock(return_value='Une nouvelle spatiale.')) as summarized:
            embed = await actions.build_news_briefing(' space ', include_links=True, language_request='Please answer in French with sources.')

        fetched.assert_awaited_once_with('space', count=3)
        summarized.assert_awaited_once_with('space', [item], mode='news', include_links=True,
                                           language_request='Please answer in French with sources.')
        self.assertEqual(embed.title, 'News Briefing')
        self.assertEqual(len(embed.fields), 1)
        self.assertEqual(embed.fields[0].name, 'Space')
        self.assertIn('Une nouvelle spatiale.', embed.fields[0].value)
        self.assertIn(item.url, embed.fields[0].value)

    async def test_empty_model_summary_uses_clean_fetched_headlines_and_descriptions(self):
        item = self.source(title='<b>New &amp; useful</b> @everyone <@20> https://hidden.test',
                           description='<p>Real reporting. <a href="https://hidden.test">Details</a> @here</p>')
        with patch.object(search, 'news_search', AsyncMock(return_value=[item])), \
                patch.object(search, 'summarize_results', AsyncMock(return_value='')):
            embed = await actions.build_news_briefing('world')

        text = embed.fields[0].value
        self.assertIn('New & useful', text)
        self.assertIn('Real reporting.', text)
        self.assertNotIn('@', text)
        self.assertNotIn('<b>', text)
        self.assertNotIn('https://', text)

    async def test_failed_model_does_not_suppress_real_news(self):
        item = self.source()
        with patch.object(search, 'news_search', AsyncMock(return_value=[item])), \
                patch.object(search, 'summarize_results', AsyncMock(side_effect=TimeoutError('private-provider-details'))), \
                self.assertLogs('nixi.actions', level='WARNING') as logs:
            embed = await actions.build_news_briefing('world', include_links=True)

        self.assertIn(item.title, embed.fields[0].value)
        self.assertIn(item.description, embed.fields[0].value)
        self.assertIn(item.url, embed.fields[0].value)
        self.assertNotIn('private-provider-details', '\n'.join(logs.output))

    async def test_failed_or_empty_sections_do_not_hide_successful_topics(self):
        async def fetch(topic, *, count):
            if topic == 'world':
                raise RuntimeError('private-provider-details')
            return [self.source(topic)] if topic == 'india' else []

        with patch.object(config, 'MORNING_NEWS_TOPICS', ['world', 'india', 'technology']), \
                patch.object(search, 'news_search', AsyncMock(side_effect=fetch)), \
                patch.object(search, 'summarize_results', AsyncMock(return_value='India news.')) as summarized, \
                self.assertLogs('nixi.actions', level='WARNING') as logs:
            embed = await actions.build_news_briefing()

        self.assertEqual([field.name for field in embed.fields], ['World', 'India', 'Technology'])
        self.assertEqual(embed.fields[0].value, 'No verified headlines available for this section right now.')
        self.assertEqual(embed.fields[1].value, 'India news.')
        self.assertEqual(embed.fields[2].value, embed.fields[0].value)
        self.assertEqual(summarized.await_count, 1)
        self.assertNotIn('private-provider-details', '\n'.join(logs.output))

    async def test_no_results_never_invokes_model_or_publishes_ungrounded_prose(self):
        with patch.object(search, 'news_search', AsyncMock(return_value=[])), \
                patch.object(search, 'summarize_results', AsyncMock(return_value='Invented news.')) as summarized:
            self.assertIsNone(await actions.build_news_briefing('world'))
        summarized.assert_not_awaited()

    async def test_invalid_source_urls_and_empty_source_text_are_not_usable_news(self):
        results = [search.SearchItem('Bad', 'javascript:alert(1)'),
                   search.SearchItem('Bad', 'https://user:password@example.test'),
                   search.SearchItem('Bad', 'https://example.test/unsafe path'),
                   search.SearchItem('', 'https://example.test/empty', '')]
        with patch.object(search, 'news_search', AsyncMock(return_value=results)), \
                patch.object(search, 'summarize_results', AsyncMock()) as summarized:
            self.assertIsNone(await actions.build_news_briefing('world'))
        summarized.assert_not_awaited()

    async def test_empty_topic_configuration_skips_fetching(self):
        with patch.object(config, 'MORNING_NEWS_TOPICS', ['', '   ']), \
                patch.object(search, 'news_search', AsyncMock()) as fetched:
            self.assertIsNone(await actions.build_news_briefing())
        fetched.assert_not_awaited()

    async def test_summary_mentions_and_unverified_links_are_removed(self):
        item = self.source()
        for include_links in [False, True]:
            with self.subTest(include_links=include_links), \
                    patch.object(search, 'news_search', AsyncMock(return_value=[item])), \
                    patch.object(search, 'summarize_results', AsyncMock(return_value='Real news. @everyone @here <@&20> <@!30> https://invented.test/story')):
                embed = await actions.build_news_briefing('world', include_links=include_links)
            text = embed.fields[0].value
            self.assertIn('Real news.', text)
            self.assertNotIn('@', text)
            self.assertNotIn('invented.test', text)
            self.assertEqual(item.url in text, include_links)

    async def test_large_sections_fit_discord_limits_without_cutting_source_urls(self):
        topics = ['topic' + str(index) + 'x' * 300 for index in range(6)]
        item = search.SearchItem('Headline', 'https://example.test/' + 'x' * 650, 'Details')
        with patch.object(config, 'MORNING_NEWS_TOPICS', topics), \
                patch.object(search, 'news_search', AsyncMock(return_value=[item])), \
                patch.object(search, 'summarize_results', AsyncMock(return_value='A' * 10000)):
            embed = await actions.build_news_briefing(include_links=True)

        self.assertLessEqual(len(embed), 6000)
        for field in embed.fields:
            self.assertLessEqual(len(field.value), 1024)
            self.assertLessEqual(len(field.name), 256)
            self.assertIn(f'<{item.url}>', field.value)

    async def test_manual_news_replies_with_embed_and_remembers_delivery(self):
        message = SimpleNamespace(content='nixi news on space in French with sources',
                                  author=SimpleNamespace(id=config.OWNER_ID), reply=AsyncMock())
        briefing = discord.Embed(title='News Briefing').add_field(name='Space', value='Actual news.')
        with patch.object(actions, 'build_news_briefing', AsyncMock(return_value=briefing)) as build, \
                patch.object(actions, '_remember_result', AsyncMock()) as remembered:
            self.assertTrue(await actions.execute_owner_intent(message, intent.OwnerIntent('news', query='space')))

        build.assert_awaited_once_with('space', include_links=True, language_request=message.content)
        self.assertEqual(message.reply.await_args.args, ())
        self.assertIs(message.reply.await_args.kwargs['embed'], briefing)
        self.assertFalse(message.reply.await_args.kwargs['allowed_mentions'].users)
        remembered.assert_awaited_once_with(message.reply.return_value, 'News briefing delivered: space')
