"""Regression suite: full pagination, durable channel history, strict delivery.

No secrets, Discord connections, Gelbooru API calls or media downloads.
Run: python -m unittest -v test_antirepeat
"""
import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch

import nextcord
import booru
import bot_gelbooru as bot
import selection as sel
import content_filter as cf


def post(i, score=None):
    return {'id': i, 'md5': f'image-{i}', '_site': 'Gelbooru',
            'tags': '1girl nude', 'rating': 'explicit',
            'score': 10000 - i if score is None else score}


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class Interaction:
    channel_id = 100
    guild_id = 1

    def __init__(self, channel=100):
        self.channel_id = channel
        self.followup = Followup()


async def payload(p):
    return {'content': sel.post_uid(p)}, ''


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'shown.sqlite3')
        self.mem = sel.ShownMemory(self.path)

    def tearDown(self):
        self.mem.close()
        self.tmp.cleanup()

    def test_more_than_500_images_and_channels(self):
        for i in range(700):
            self.mem.remember('one-channel', f'image-{i}')
            self.mem.remember(f'channel-{i}', 'older')
        self.assertEqual(len(self.mem.recent('one-channel')), 700)
        self.assertIn('image-0', self.mem.recent('one-channel'))
        self.assertIn('older', self.mem.recent('channel-0'))

    def test_committed_without_autosave(self):
        self.mem.remember('channel', 'a')
        other = sel.ShownMemory(self.path)
        try:
            self.assertIn('a', other.recent('channel'))
        finally:
            other.close()

    def test_pending_survives_restart(self):
        self.assertIsNotNone(self.mem.reserve('channel', {'a'}))
        self.mem.close()
        restarted = sel.ShownMemory(self.path)
        try:
            self.assertIsNone(restarted.reserve('channel', {'a'}))
        finally:
            restarted.close()

    def test_two_connections_cannot_claim_same_image(self):
        other = sel.ShownMemory(self.path)
        try:
            token = self.mem.reserve('channel', {'a', 'Gelbooru:1'})
            self.assertIsNotNone(token)
            self.assertIsNone(other.reserve('channel', {'a', 'Konachan:10'}))
            self.assertNotIn('Konachan:10', other.recent('channel'))
        finally:
            other.close()

    def test_explicit_release_allows_retry(self):
        token = self.mem.reserve('channel', {'a'})
        self.mem.release(token)
        self.assertIsNotNone(self.mem.reserve('channel', {'a'}))

    def test_channels_are_independent(self):
        self.mem.remember('channel-a', 'a')
        self.assertIsNone(self.mem.reserve('channel-a', {'a'}))
        self.assertIsNotNone(self.mem.reserve('channel-b', {'a'}))

    def test_legacy_import_is_conservative_and_one_time(self):
        legacy = os.path.join(self.tmp.name, 'recent_shown.json')
        with open(legacy, 'w') as f:
            json.dump({'Gelbooru|tag': ['a'], 'Telegram|channel': ['tg-old']}, f)
        self.mem.close()
        self.mem = sel.ShownMemory(self.path, legacy)
        self.mem.load()
        self.assertIn('a', self.mem.recent('any-channel'))
        self.assertIn('tg-old', self.mem.recent('different-channel'))
        with open(legacy, 'w') as f:
            json.dump({'later': ['should-not-import-again']}, f)
        self.mem.close()
        self.mem.load()
        self.assertNotIn('should-not-import-again', self.mem.recent('any-channel'))

    def test_corrupt_legacy_fails_closed(self):
        legacy = os.path.join(self.tmp.name, 'broken.json')
        with open(legacy, 'w') as f:
            f.write('not json')
        bad = sel.ShownMemory(os.path.join(self.tmp.name, 'bad.sqlite3'), legacy)
        with self.assertRaises(json.JSONDecodeError):
            bad.load()
        self.assertIsNone(bad._db)

    def test_corrupt_sqlite_fails_closed(self):
        with open(self.path, 'w') as f:
            f.write('not sqlite')
        with self.assertRaises(sqlite3.DatabaseError):
            self.mem.load()

    def test_no_repeat_when_pool_exhausted(self):
        self.assertEqual(sel.order_candidates([post(1)], {'image-1'}), [])

    def test_site_id_stays_blocked_when_md5_changes(self):
        self.assertEqual(sel.order_candidates([post(1)], {'Gelbooru:1'}), [])

    def test_same_md5_across_sources(self):
        p = post(1)
        other = dict(p, _site='Konachan', id=50)
        self.assertEqual(len(sel.order_candidates([p, other], set())), 1)

    def test_retry_quarantine_expires(self):
        with patch('selection.time.time', return_value=100):
            self.mem.failed('channel', {'a'}, ttl=5)
            self.assertIn('a', self.mem.recent('channel'))
        with patch('selection.time.time', return_value=106):
            self.assertNotIn('a', self.mem.recent('channel'))

    def test_no_scope_without_channel_id(self):
        with self.assertRaises(ValueError):
            sel.channel_scope(object())


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_cache = tempfile.TemporaryDirectory()
        self.env_patch = patch.dict(os.environ, {'DATA_DIR': self.tmp_cache.name})
        self.env_patch.start()
        booru.clear_search_cache()
        self.old_memory = bot.memory
        bot.memory = sel.ShownMemory(':memory:')
        self.source = booru.Gelbooru(None, None)
        self.scope = sel.channel_scope(Interaction())

    async def asyncTearDown(self):
        bot.memory.close()
        bot.memory = self.old_memory
        booru.clear_search_cache()
        self.env_patch.stop()
        self.tmp_cache.cleanup()

    def pages(self, posts, count=True, cap=100):
        calls = []
        async def fetch(source, tags, page):
            calls.append(page)
            return posts[page * cap:(page + 1) * cap], len(posts) if count else None
        return fetch, calls

    async def test_all_3200_posts_searchable(self):
        fetch, calls = self.pages([post(i) for i in range(3200)])
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['1girl'], set(), exhaustive=True)
        self.assertTrue(result.complete)
        self.assertEqual(result.fetched, 3200)
        self.assertEqual(len(result.candidates), 3200)
        self.assertIn(31, calls)
        self.assertEqual(result.candidates[-1]['id'], 3199)

    async def test_whole_pool_ranking_not_just_top_page(self):
        posts = [post(i) for i in range(3200)]
        posts[-1]['tags'] = '1girl nude large_breasts'
        fetch, _ = self.pages(posts)
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['large_breasts'], set(), exhaustive=True)
        self.assertEqual(result.candidates[0]['id'], 3199)

    async def test_650_images_then_exhaustion_no_502_cycle(self):
        fetch, _ = self.pages([post(i) for i in range(650)])
        seen = set()
        with patch('booru.fetch_page', fetch):
            for i in range(700):
                result = await booru.search(self.source, ['1girl'], bot.memory.recent(self.scope), exhaustive=True)
                if i < 650:
                    uid = sel.post_uid(result.candidates[0])
                    self.assertNotIn(uid, seen)
                    seen.add(uid)
                    bot.memory.remember(self.scope, uid)
                else:
                    self.assertEqual(result.candidates, [])
        self.assertEqual(len(seen), 650)

    async def test_unknown_total_until_empty_page(self):
        fetch, calls = self.pages([post(i) for i in range(3201)], count=False)
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['1girl'], set(), exhaustive=True)
        self.assertEqual(len(result.candidates), 3201)
        self.assertIn(33, calls)
        self.assertTrue(result.complete)

    async def test_server_caps_page_size(self):
        fetch, calls = self.pages([post(i) for i in range(301)], cap=50)
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['1girl'], set(), exhaustive=True)
        self.assertEqual(len(result.candidates), 301)
        self.assertIn(6, calls)

    async def test_all_filtered_still_scans_whole_source(self):
        posts = [dict(post(i), tags='ai_generated') for i in range(3200)]
        fetch, _ = self.pages(posts)
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['1girl'], set(), exhaustive=True)
        self.assertEqual(result.fetched, 3200)
        self.assertEqual(result.allowed, 0)
        self.assertEqual(result.candidates, [])

    async def test_later_page_failure_no_replay_and_resume(self):
        posts = [post(i) for i in range(650)]
        calls, fail = [], True
        async def fetch(source, tags, page):
            calls.append(page)
            if fail and page == 2:
                raise booru.SourceError('down')
            return posts[page * 100:(page + 1) * 100], 650
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['1girl'], set(), exhaustive=True)
            self.assertEqual(result.candidates, [])
            self.assertEqual(result.errors, ['down'])
            self.assertFalse(result.complete)
            fail = False
            result = await booru.search(self.source, ['1girl'], set(), exhaustive=True)
        self.assertEqual(len(result.candidates), 650)
        self.assertEqual(calls.count(0), 1)
        self.assertEqual(calls.count(1), 1)

    async def test_timeout_resumes_not_truncates(self):
        posts = [post(i) for i in range(3200)]
        slow = True
        async def fetch(source, tags, page):
            if page > 0 and slow:
                await asyncio.sleep(0.1)
            return posts[page * 100:(page + 1) * 100], 3200
        with patch('booru.fetch_page', fetch), patch('booru.SCAN_TIMEOUT', 0.01):
            result = await booru.search(self.source, ['1girl'], set(), exhaustive=True)
            self.assertEqual(result.errors, ['scan_pending'])
            self.assertEqual(result.fetched, 100)
            self.assertEqual(result.candidates, [])
            slow = False
            with patch('booru.SCAN_TIMEOUT', 5):
                result = await booru.search(self.source, ['1girl'], set(), exhaustive=True)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.candidates), 3200)

    async def test_repeated_api_page_detected(self):
        async def fetch(source, tags, page):
            return [post(i) for i in range(100)], None
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['1girl'], set(), exhaustive=True)
        self.assertEqual(result.errors, ['incomplete'])
        self.assertEqual(result.candidates, [])

    async def test_early_empty_page_not_complete(self):
        async def fetch(source, tags, page):
            return ([post(i) for i in range(100)] if page == 0 else []), 650
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['1girl'], set(), exhaustive=True)
        self.assertEqual(result.errors, ['incomplete'])
        self.assertFalse(result.complete)

    async def test_cache_reused_when_tags_reordered(self):
        fetch, calls = self.pages([post(i) for i in range(3200)])
        with patch('booru.fetch_page', fetch):
            await booru.search(self.source, ['1girl', 'sky'], set(), exhaustive=True)
            first = len(calls)
            await booru.search(self.source, ['sky', '1girl'], set(), exhaustive=True)
        self.assertEqual(len(calls), first)

    async def test_expired_full_scan_refreshes(self):
        posts = [post(i) for i in range(101)]
        fetch, _ = self.pages(posts)
        with patch('booru.fetch_page', fetch):
            await booru.search(self.source, ['1girl'], set(), exhaustive=True)
            state = next(iter(booru._scans.values()))
            state.updated -= booru.CACHE_TTL + 1
            posts.append(post(101))
            result = await booru.search(self.source, ['1girl'], set(), exhaustive=True)
        self.assertEqual(len(result.candidates), 102)

    async def test_malformed_api_response_not_cached_as_empty(self):
        async def text(*args, **kwargs):
            return '<html><body>blocked</body></html>'
        with patch('booru.fetch_text', text):
            result = await booru.search(self.source, ['1girl'], set(), exhaustive=True)
        self.assertEqual(result.errors, ['bad'])
        self.assertFalse(booru._cache)

    async def test_overlapping_requests_stale_candidates(self):
        interaction = Interaction()
        pool = [post(1), post(2)]
        ready, sent = asyncio.Event(), asyncio.Event()
        arrived = 0
        async def request(number):
            nonlocal arrived
            candidates = sel.order_candidates(pool, bot.memory.recent(self.scope))
            arrived += 1
            if arrived == 2:
                ready.set()
            await ready.wait()
            if number == 2:
                await sent.wait()
            await bot.send_first_fitting(interaction, self.scope, candidates, payload)
            if number == 1:
                sent.set()
        await asyncio.gather(request(1), request(2))
        self.assertEqual([m['content'] for m in interaction.followup.sent], ['image-1', 'image-2'])

    async def test_simultaneous_downloads_reserve_before_await(self):
        interaction = Interaction()
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(p):
            if p['id'] == 1:
                started.set()
                await release.wait()
            return await payload(p)
        first = asyncio.create_task(bot.send_first_fitting(interaction, self.scope,
                                                          [post(1), post(2)], delayed))
        await started.wait()
        await bot.send_first_fitting(interaction, self.scope, [post(1), post(2)], payload)
        release.set()
        await first
        self.assertEqual({m['content'] for m in interaction.followup.sent}, {'image-1', 'image-2'})
        self.assertEqual(len(interaction.followup.sent), 2)

    async def test_cross_tag_repeat_blocked_in_same_channel(self):
        interaction = Interaction()
        await bot.send_first_fitting(interaction, self.scope, [post(1)], payload)
        sent, reasons = await bot.send_first_fitting(interaction, self.scope, [post(1)], payload)
        self.assertIsNone(sent)
        self.assertEqual(reasons, {'duplicate'})
        self.assertEqual(len(interaction.followup.sent), 1)

    async def test_actual_bytes_block_different_metadata_and_sources(self):
        interaction = Interaction()
        async def same_bytes(p):
            return {'content': str(p['id']), '_uids': bot.content_uids(b'identical image bytes')}, ''
        await bot.send_first_fitting(interaction, self.scope, [post(1)], same_bytes)
        other = dict(post(2), _site='Konachan')
        sent, _ = await bot.send_first_fitting(interaction, self.scope, [other], same_bytes)
        self.assertIsNone(sent)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertIn('image-2', bot.memory.recent(self.scope))

    async def test_uncertain_delivery_is_not_retried_after_reopen(self):
        interaction = Interaction()
        async def uncertain(**kwargs):
            interaction.followup.sent.append(kwargs)
            raise ConnectionError('ack lost after possible delivery')
        interaction.followup.send = uncertain
        with self.assertRaises(ConnectionError):
            await bot.send_first_fitting(interaction, self.scope, [post(1)], payload)
        sent, reasons = await bot.send_first_fitting(interaction, self.scope, [post(1)], payload)
        self.assertIsNone(sent)
        self.assertEqual(reasons, {'duplicate'})
        self.assertEqual(len(interaction.followup.sent), 1)

    async def test_cancel_during_download_releases_reserve(self):
        started = asyncio.Event()
        async def delayed(p):
            started.set()
            await asyncio.sleep(100)
        task = asyncio.create_task(bot.send_first_fitting(Interaction(), self.scope, [post(1)], delayed))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn('image-1', bot.memory.recent(self.scope))

    async def test_cancel_during_send_preserves_reserve(self):
        interaction = Interaction()
        started = asyncio.Event()
        async def delayed(**kwargs):
            started.set()
            await asyncio.sleep(100)
        interaction.followup.send = delayed
        task = asyncio.create_task(bot.send_first_fitting(interaction, self.scope, [post(1)], payload))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn('image-1', bot.memory.recent(self.scope))

    async def test_download_failures_quarantined_then_next_candidate(self):
        interaction = Interaction()
        async def failed(p):
            return None, 'too_big'
        await bot.send_first_fitting(interaction, self.scope, [post(i) for i in range(9)], failed)
        candidates = sel.order_candidates([post(i) for i in range(9)], bot.memory.recent(self.scope))
        self.assertEqual([p['id'] for p in candidates], [8])
        await bot.send_first_fitting(interaction, self.scope, candidates, payload)
        self.assertEqual(interaction.followup.sent[0]['content'], 'image-8')

    async def test_blocked_payload_file_is_closed(self):
        interaction = Interaction()
        fingerprint = bot.content_uids(b'same')
        async def first(p):
            return {'content': 'a', '_uids': fingerprint}, ''
        await bot.send_first_fitting(interaction, self.scope, [post(1)], first)
        called = []
        class TrackingFile(nextcord.File):
            def close(self):
                called.append(True)
                super().close()
        file = TrackingFile(BytesIO(b'same'), filename='test.jpg')
        async def duplicate(p):
            return {'file': file, '_uids': fingerprint}, ''
        await bot.send_first_fitting(interaction, self.scope, [post(2)], duplicate)
        self.assertTrue(called)

    async def test_concurrent_full_scans_share_index(self):
        posts = [post(i) for i in range(650)]
        calls = []
        async def fetch(source, tags, page):
            calls.append(page)
            await asyncio.sleep(0.001)
            return posts[page * 100:(page + 1) * 100], 650
        with patch('booru.fetch_page', fetch):
            first, second = await asyncio.gather(
                booru.search(self.source, ['1girl'], set(), exhaustive=True),
                booru.search(self.source, ['1girl'], {'image-0'}, exhaustive=True))
        self.assertEqual(len(calls), 10)
        self.assertEqual(len(first.candidates), 650)
        self.assertEqual(len(second.candidates), 649)

    async def test_lazy_disk_index_survives_cache_eviction(self):
        fetch, _ = self.pages([post(i) for i in range(3200)])
        with patch('booru.fetch_page', fetch):
            held = await booru.search(self.source, ['first'], set(), exhaustive=True)
            for i in range(booru.SCAN_CACHE_MAX + 1):
                await booru.search(self.source, [str(i)], set(), exhaustive=True)
        self.assertNotIsInstance(held.candidates, list)
        self.assertEqual(held.candidates[-1]['id'], 3199)
        self.assertLessEqual(len(booru._scans), booru.SCAN_CACHE_MAX)

    async def test_sql_ranking_matches_selection(self):
        import random
        rng = random.Random(123)
        posts = [dict(post(i), tags=rng.choice([
            '1girl nude large_breasts', '1girl nude', '1girl smile',
            '1girl nude ass_focus', 'fully_clothed']), score=rng.randrange(10))
            for i in range(200)]
        seen = {'image-3', 'Gelbooru:12'}
        groups = cf.focus_groups_for(['breasts'])
        nudity = cf.requested_nudity_tags(groups)
        strict, broad = [], []
        for p in posts:
            if cf.post_is_allowed(p, self.source.ratings):
                (strict if cf.has_nudity(p, nudity) else broad).append(p)
        expected = sel.order_candidates(strict, seen, groups, broad)
        fetch, _ = self.pages(posts)
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['breasts'], seen, exhaustive=True)
        self.assertEqual([p['id'] for p in result.candidates], [p['id'] for p in expected])

    async def test_http_5xx_preserves_ambiguous_delivery(self):
        interaction = Interaction()
        class Response:
            status = 502
            reason = 'Bad Gateway'
        async def uncertain(**kwargs):
            raise nextcord.HTTPException(Response(), 'upstream failed')
        interaction.followup.send = uncertain
        with self.assertRaises(nextcord.HTTPException):
            await bot.send_first_fitting(interaction, self.scope, [post(1)], payload)
        self.assertIn('image-1', bot.memory.recent(self.scope))

    async def test_asset_easter_egg_obeys_same_repeat_guard(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        interaction = Interaction()
        interaction.response = SimpleNamespace(defer=AsyncMock())
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'asset.jpg')
            with open(path, 'wb') as stream:
                stream.write(b'test image bytes')
            with patch('bot_gelbooru.KANZAKI_HIDERI_IMAGE', path):
                await bot.answer_verdict(interaction, 'kanzaki')
                await bot.answer_verdict(interaction, 'kanzaki')
        self.assertEqual(sum('file' in m for m in interaction.followup.sent), 1)
        self.assertIn('asset:kanzaki_hideri', bot.memory.recent(self.scope))

    async def test_json_error_and_scalar_not_empty_results(self):
        for body in ['123', '{"error":"denied"}', '{"post":[{"id":1,"tags":[]}]}']:
            booru.clear_search_cache()
            async def text(*args, **kwargs):
                return body
            with patch('booru.fetch_text', text):
                result = await booru.search(self.source, ['1girl'], set(), exhaustive=True)
            self.assertEqual(result.errors, ['bad'])
            self.assertFalse(booru._cache)

    async def test_huge_query_returns_first_page_without_full_scan(self):
        calls = []
        async def fetch(source, tags, page):
            calls.append(page)
            self.assertEqual(page, 0, 'must not pre-index 444972 posts')
            return [post(i) for i in range(100)], 444972
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['1girl'], set())
        self.assertTrue(result.candidates)
        self.assertFalse(result.complete)
        self.assertEqual(result.errors, [])
        self.assertEqual(calls, [0])

    async def test_progressive_650_unique_then_no_replays(self):
        fetch, calls = self.pages([post(i) for i in range(650)])
        seen = set()
        with patch('booru.fetch_page', fetch):
            for i in range(700):
                result = await booru.search(self.source, ['1girl'], seen)
                if i < 650:
                    self.assertTrue(result.candidates)
                    uid = sel.post_uid(result.candidates[0])
                    self.assertNotIn(uid, seen)
                    seen.add(uid)
                else:
                    self.assertFalse(result.candidates)
        self.assertEqual(len(seen), 650)
        self.assertIn(6, calls)

    async def test_partial_budget_resumes_after_restart(self):
        fetch, calls = self.pages([post(i) for i in range(3200)])
        with patch('booru.fetch_page', fetch), patch('booru.PAGES_PER_REQUEST', 1):
            result = await booru.search(self.source, ['1girl'], set())
            result = await booru.search(self.source, ['1girl'], {f'image-{i}' for i in range(100)})
            self.assertEqual(result.fetched, 200)
            result = None
            booru.clear_search_cache()  # new state/connection, disk cursor survives
            result = await booru.search(self.source, ['1girl'], {f'image-{i}' for i in range(200)})
        self.assertEqual(calls, [0, 1, 2])
        self.assertEqual(result.candidates[0]['id'], 200)

    async def test_filtered_pages_continue_beyond_600(self):
        posts = [dict(post(i), tags='ai_generated') for i in range(3200)]
        posts[-1] = post(3199)
        fetch, calls = self.pages(posts)
        with patch('booru.fetch_page', fetch):
            for _ in range(6):
                result = await booru.search(self.source, ['1girl'], set())
                if result.candidates:
                    break
        self.assertTrue(result.candidates)
        self.assertEqual(result.candidates[0]['id'], 3199)
        self.assertIn(31, calls)
        self.assertGreater(result.fetched, 600)

    async def test_unseen_indexed_posts_survive_later_api_error(self):
        async def fetch(source, tags, page):
            if page == 0:
                return [post(1), post(2)], 444972
            raise booru.SourceError('bad', 'temporary invalid response')
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['1girl'], set())
        self.assertEqual(result.errors, [])
        self.assertEqual([p['id'] for p in result.candidates], [1, 2])

    async def test_api_error_never_returns_seen_cached_posts(self):
        async def fetch(source, tags, page):
            if page == 0:
                return [post(1), post(2)], 444972
            raise booru.SourceError('down', 'late page unavailable')
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['1girl'], {'image-1', 'image-2'})
        self.assertFalse(result.candidates)
        self.assertEqual(result.errors, ['down'])

    async def test_empty_gelbooru_page_with_positive_count_valid(self):
        self.assertEqual(booru.parse_posts_page('{"@attributes":{"count":444972}}'), ([], 444972))
        async def text(url, params, **kwargs):
            if params.get('pid') == '0':
                return json.dumps({'@attributes': {'count': 444972}, 'post': [post(1)]})
            return '{"@attributes":{"count":444972}}'
        with patch('booru.fetch_text', text):
            result = await booru.search(self.source, ['1girl'], set())
        self.assertEqual(result.errors, [])
        self.assertEqual(result.candidates[0]['id'], 1)
        self.assertEqual(result.stop_reason, 'api_end_before_count')

    async def test_count_underestimate_does_not_cut_search(self):
        posts = [post(i) for i in range(3200)]
        async def fetch(source, tags, page):
            return posts[page * 100:(page + 1) * 100], 150
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['1girl'], set(), exhaustive=True)
        self.assertEqual(len(result.candidates), 3200)
        self.assertTrue(result.complete)

    async def test_timeout_keeps_genuinely_new_candidates(self):
        async def fetch(source, tags, page):
            if page == 0:
                return [post(1)], 444972
            await asyncio.sleep(1)
        with patch('booru.fetch_page', fetch), patch('booru.SCAN_TIMEOUT', 0.02):
            result = await booru.search(self.source, ['1girl'], set())
        self.assertEqual(result.errors, [])
        self.assertEqual(result.candidates[0]['id'], 1)

    async def test_partial_progress_does_not_expire_after_ten_minutes(self):
        fetch, calls = self.pages([post(i) for i in range(3200)])
        with patch('booru.fetch_page', fetch), patch('booru.PAGES_PER_REQUEST', 1):
            await booru.search(self.source, ['1girl'], set())
            state = next(iter(booru._scans.values()))
            state.updated -= booru.CACHE_TTL + 1
            result = await booru.search(self.source, ['1girl'], {f'image-{i}' for i in range(100)})
        self.assertEqual(calls, [0, 1])
        self.assertEqual(result.candidates[0]['id'], 100)

    async def test_failed_page_cursor_not_skipped_in_default_mode(self):
        fail = True
        async def fetch(source, tags, page):
            if fail and page == 1:
                raise booru.SourceError('down', 'retry this page')
            return [post(page * 100 + i) for i in range(100)], 444972
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['1girl'], {f'image-{i}' for i in range(100)})
            self.assertEqual(result.errors, ['down'])
            self.assertEqual(next(iter(booru._scans.values())).next_page, 1)
            fail = False
            result = await booru.search(self.source, ['1girl'], {f'image-{i}' for i in range(100)})
        self.assertEqual(result.candidates[0]['id'], 100)

    async def test_auth_error_still_explicit_with_partial_index(self):
        async def fetch(source, tags, page):
            if page == 0:
                return [post(1)], 444972
            raise booru.SourceError('auth', 'HTTP 401')
        with patch('booru.fetch_page', fetch):
            result = await booru.search(self.source, ['1girl'], set())
        self.assertFalse(result.candidates)
        self.assertEqual(result.errors, ['auth'])

    async def test_discord_handler_sends_first_art_before_full_index(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        interaction = Interaction()
        interaction.user = SimpleNamespace(id=1)
        interaction.guild = None
        interaction.channel = SimpleNamespace(is_nsfw=lambda: True)
        interaction.response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
        calls = []
        async def fetch(source, tags, page):
            calls.append(page)
            self.assertEqual(page, 0)
            return [post(i) for i in range(100)], 444972
        async def make(source, p, display, max_size):
            return {'content': 'new-art-' + str(p['id'])}, ''
        with patch('booru.fetch_page', fetch), patch('bot_gelbooru.build_booru_payload', make):
            await bot.run_booru_search(interaction, ('1girl',), self.source, bot.CooldownManager(5, 30))
        interaction.response.defer.assert_awaited_once()
        self.assertEqual(calls, [0])
        self.assertEqual(interaction.followup.sent, [{'content': 'new-art-0'}])
        self.assertIn('image-0', bot.memory.recent(self.scope))


if __name__ == '__main__':
    unittest.main()
