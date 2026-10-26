"""Live synthetic probes. No Discord connection or production database access."""

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from scripts.evaluate_system import reset, save, safe_error
from services import conversation, database as db, llm, media, memory, social


async def main():
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('evaluation/2026-10-26/companion-repair')
    await reset()
    records = []
    people = [SimpleNamespace(id=200, display_name='Sam', name='sam', bot=False),
              SimpleNamespace(id=300, display_name='Alex', name='alex', bot=False)]
    guild = SimpleNamespace(id=1, members=people, emojis=[])
    try:
        try:
            options = {'think': 'low'} if 'gpt-oss' in config.OLLAMA_MODEL.lower() else {}
            await asyncio.wait_for(llm._get_client().chat(model=config.OLLAMA_MODEL,
                messages=[{'role': 'user', 'content': 'Reply only OK.'}],
                options={'num_predict': 512}, **options), timeout=config.LLM_TIMEOUT_SECONDS)
        except Exception as exc:
            save(directory, 'live_companion', {'status': 'blocked', 'provider': 'ollama',
                'error': safe_error(exc), 'scope': 'Preflight only. No Discord connection or production database access.'})
            print('Live companion probes blocked:', safe_error(exc), flush=True)
            return
        statement = 'main coffee peeta hoon, aur mujhe chess pasand hai'
        await memory.extract_and_store_memories(10, statement, 'Sam', '', 1, 200)
        await db.save_member_fact(1, 300, 'drink_preference', 'tea', 'I prefer tea')
        await db.save_member_fact(1, 300, 'favorite_game', 'Valorant', 'I play Valorant')
        await db.save_message(10, 1, 300, 'Alex', 'I prefer tea and play Valorant')
        await db.save_message(10, 1, 999, 'Nixi', 'Tea and Valorant, noted.', True, reply_to_author_id=300)
        for case_id, text, expected, excluded in [
            ('drink_binding_after_other_member', 'nixi which drink do I prefer?', 'coffee', 'tea'),
            ('game_binding_after_other_member', 'nixi which game do I like?', 'chess', 'valorant'),
            ('current_correction', 'nixi I prefer tea now, not coffee. Which drink should you pick for me?', 'tea', ''),
            ('comic_avatar_indecision', 'nixi I spent three hours choosing an avatar and picked the old one', '', ''),
            ('comic_gaming_boast', 'nixi meri aim itni achhi hai ki enemies mujhe dekh ke surrender kar dete hain', '', ''),
        ]:
            try:
                context = await memory.build_context(10, text, 'Sam', 'DIRECT_ADDRESS',
                    guild_id=1, guild=guild, current_author_id=200, nixi_author_id=999)
                reply = await conversation.generate_reply(context, 10, text, current_author_id=200, nixi_author_id=999)
                records.append({'id': case_id, 'input': text, 'reply': reply,
                    'checks': {'nonempty': bool(reply.strip()),
                               **({'expected_detail': expected in reply.casefold()} if expected else {}),
                               **({'no_other_member_detail': excluded not in reply.casefold()} if excluded else {})}})
            except Exception as exc:
                records.append({'id': case_id, 'error': safe_error(exc)})
            print(case_id, 'complete', flush=True)
        selector = social.AutomaticMedia()
        with patch.object(config, 'AUTOMATIC_MEDIA_ENABLED', True), \
                patch.object(config, 'AUTOMATIC_MEDIA_CHANCE', 1), \
                patch.object(config, 'AUTOMATIC_MEDIA_COOLDOWN_SECONDS', 0):
            for index, (content, reply) in enumerate([
                ('I have never been wrong in my life', 'Even your browser history asked for a second opinion.'),
                ('I am carrying this whole team', 'The scoreboard has filed a missing-person report.'),
            ]):
                try:
                    result = await asyncio.wait_for(selector.choose(20 + index, content, reply), timeout=30)
                    records.append({'id': f'live_automatic_gif_{index + 1}', 'selected': bool(result),
                        'kind': 'gif' if result and media._gif_url(result.url) else 'other_or_none',
                        'url': result.url if result else None, 'keeps_text': not result or not result.standalone})
                except Exception as exc:
                    records.append({'id': f'live_automatic_gif_{index + 1}', 'error': safe_error(exc)})
                print(f'live_automatic_gif_{index + 1} complete', flush=True)
        save(directory, 'live_companion', {
            'scope': 'Fictional members only. Existing Ollama model and GIF sources. No Discord sends. '
                     'Automatic-media sampling forced to 100% for relevance/provider probes, not a production frequency measurement. '
                     'Substring checks are narrow signals; humor needs human review.',
            'learned_facts': await db.get_member_facts(1, 200), 'cases': records})
    finally:
        await memory.shutdown()
        await db.close()


if __name__ == '__main__':
    asyncio.run(main())
