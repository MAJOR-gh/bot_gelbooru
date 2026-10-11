"""Глубокий поиск: вся выдача за потолком pid, доказанная ранняя остановка, приоритет фокуса.

Без сети: сайт эмулируется по тем же правилам, что Gelbooru (sort:score, score:<N,
score:N + id:<N, потолок pid*limit).
Запуск: python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import content_filter as cf  # noqa: E402
import search  # noqa: E402
import sources  # noqa: E402


def post(i, score, tags="1girl nude"):
    return {"id": i, "md5": f"m{i}", "_site": "Fake", "tags": tags, "rating": "explicit", "score": score}


class FakeSite:
    """Эмуляция booru: сортировка по score, курсор score/id и потолок глубины."""

    def __init__(self, posts, page_size=100, max_offset=None, deep_error=False):
        self.posts, self.page_size = posts, page_size
        self.max_offset, self.deep_error = max_offset, deep_error
        self.calls = []

    async def fetch(self, source, tags, page, cursor=None):
        self.calls.append((page, cursor))
        if cursor and cursor.bucket is not None:
            rows = [p for p in self.posts if p["score"] == cursor.bucket
                    and (cursor.before_id is None or p["id"] < cursor.before_id)]
            rows.sort(key=lambda p: -p["id"])
        else:
            rows = [p for p in self.posts if not cursor or cursor.below is None or p["score"] < cursor.below]
            rows.sort(key=lambda p: (-p["score"], -p["id"]))
        start = page * self.page_size
        if self.max_offset is not None and start + self.page_size > self.max_offset:
            if self.deep_error:
                raise sources.SourceError("deep", "Too deep! Pull it back some.")
            raise AssertionError(f"бот запросил pid={page} за потолком сайта")
        return rows[start:start + self.page_size], len(self.posts)


class Capped(sources.DapiSource):
    name = "Fake"
    URL = "https://fake.example/index.php"
    max_offset = 300

    def normalize(self, p):
        return p


class DeepSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        self.env.start()
        search.clear_search_cache()

    async def asyncTearDown(self):
        search.clear_search_cache()
        self.env.stop()
        self.tmp.cleanup()

    async def test_whole_result_set_beyond_pagination_cap(self):
        # 1000 постов, много одинаковых score — потолок сайта 300 постов.
        posts = [post(i, score=i // 7) for i in range(1, 1001)]
        site = FakeSite(posts, max_offset=300)
        with patch("search.fetch_page", site.fetch):
            result = await search.search(Capped(), ["1girl"], set(), exhaustive=True)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.candidates), 1000)
        self.assertEqual(result.candidates[0]["score"], max(p["score"] for p in posts))
        self.assertTrue(any(c and c.bucket is not None for _, c in site.calls))
        self.assertTrue(any(c and c.below is not None for _, c in site.calls))

    async def test_too_deep_answer_switches_to_cursor(self):
        posts = [post(i, score=i % 50) for i in range(1, 801)]
        site = FakeSite(posts, max_offset=300, deep_error=True)
        uncapped = type("Uncapped", (Capped,), {"max_offset": None})()
        with patch("search.fetch_page", site.fetch):
            result = await search.search(uncapped, ["1girl"], set(), exhaustive=True)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.candidates), 800)

    async def test_progressive_calls_reach_past_cap_without_repeats(self):
        posts = [post(i, score=10_000 - i) for i in range(1, 801)]
        site = FakeSite(posts, max_offset=300)
        seen = set()
        with patch("search.fetch_page", site.fetch):
            for _ in range(800):
                result = await search.search(Capped(), ["1girl"], seen)
                self.assertTrue(result.candidates)
                uid = result.candidates[0]["md5"]
                self.assertNotIn(uid, seen)
                seen.add(uid)
            result = await search.search(Capped(), ["1girl"], seen)
        self.assertFalse(result.candidates)
        self.assertEqual(len(seen), 800)

    async def test_focus_priority_reads_next_page_instead_of_settling(self):
        # Ищем breasts. На 1-й странице только посты с фокусом на ДРУГОЙ части тела,
        # на 2-й — с фокусом на груди. Раньше бот довольствовался 1-й страницей.
        page1 = [post(i, 1000 - i, "1girl nude large_breasts ass_focus") for i in range(100)]
        page2 = [post(100 + i, 800 - i, "1girl nude large_breasts") for i in range(100)]
        site = FakeSite(page1 + page2)
        with patch("search.fetch_page", site.fetch):
            result = await search.search(Capped(), ["breasts"], set())
        top = result.candidates[0]
        self.assertEqual(cf.focus_tier(top, {"breasts"}), cf.TOP_TIER)
        self.assertEqual(top["id"], 100)

    async def test_stops_early_when_best_is_proven(self):
        posts = [post(i, 10_000 - i) for i in range(1000)]
        site = FakeSite(posts)
        with patch("search.fetch_page", site.fetch):
            result = await search.search(Capped(), ["1girl"], set())
        self.assertEqual(len(site.calls), 1)
        self.assertEqual(result.candidates[0]["id"], 0)


class QueryTests(unittest.TestCase):
    def test_cursor_tags(self):
        g = sources.Gelbooru("K", "U")
        _, p = g.request(["1girl"], 0, sources.Cursor(below=12))
        self.assertTrue(p["tags"].startswith("1girl score:<12 sort:score -rating:general"))
        _, p = g.request(["1girl"], 0, sources.Cursor(bucket=12, before_id=500))
        self.assertTrue(p["tags"].startswith("1girl score:12 id:<500 sort:id:desc -rating:general"))
        self.assertEqual(g.max_offset, 20_000)

    def test_too_deep_text_is_distinct_error(self):
        with self.assertRaises(sources.SourceError) as ctx:
            sources._validate(None, "Too deep! Pull it back some. Holy fuck.")
        self.assertEqual(ctx.exception.kind, "deep")


if __name__ == "__main__":
    unittest.main()
