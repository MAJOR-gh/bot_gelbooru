"""/tg и скачивание медиа: параллельная загрузка альбома, край истории, таймауты. Без сети."""
import asyncio
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp  # noqa: E402

import delivery  # noqa: E402
import sources  # noqa: E402
import tg_source  # noqa: E402

DELAY = 0.2


def message(i, size=1000, grouped=None, ext=".jpg"):
    return SimpleNamespace(id=i, grouped_id=grouped, photo=None, reply_markup=None, message="",
                           reactions=None, peer_id=SimpleNamespace(channel_id=42),
                           file=SimpleNamespace(size=size, ext=ext, mime_type="image/jpeg"))


class FakeClient:
    def __init__(self, history=()):
        self.history = list(history)
        self.album_reads = 0
        self.downloads = 0

    async def download_media(self, msg, file=bytes):
        self.downloads += 1
        await asyncio.sleep(DELAY)
        return b"x" * msg.file.size

    async def iter_messages(self, peer, limit=None, min_id=None, max_id=None):
        if min_id is not None:
            self.album_reads += 1
        for m in sorted(self.history, key=lambda m: -m.id)[:limit]:
            yield m


def tg_post(msgs, edge=False):
    return {"id": msgs[0].id, "_alias": "chan", "_username": "chan", "_peer_id": None, "_peer": "@chan",
            "_msg": msgs[0], "_msgs": msgs, "_edge": edge, "score": 3}


class TelegramPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_album_files_download_in_parallel_and_keep_order(self):
        msgs = [message(i, grouped=7) for i in (1, 2, 3, 4)]
        client = FakeClient()
        started = time.monotonic()
        payload, reason = await delivery.build_tg_payload(client, tg_post(msgs), 10_000)
        elapsed = time.monotonic() - started
        self.assertEqual(reason, "")
        self.assertEqual([f.filename for f in payload["files"]],
                         [f"tg_chan_{i}.jpg" for i in (1, 2, 3, 4)])
        self.assertLess(elapsed, DELAY * 2.5, "файлы качались по одному")
        self.assertEqual(client.album_reads, 0, "альбом в середине истории не перечитывается")

    async def test_files_over_budget_are_skipped_without_download(self):
        msgs = [message(1, 600, 7), message(2, 600, 7), message(3, 300, 7)]
        client = FakeClient()
        payload, _ = await delivery.build_tg_payload(client, tg_post(msgs), 1000)
        self.assertEqual([f.filename for f in payload["files"]], ["tg_chan_1.jpg", "tg_chan_3.jpg"])
        self.assertEqual(client.downloads, 2)
        self.assertIn("ещё 1 в посте", payload["content"])

    async def test_edge_album_is_reread(self):
        msgs = [message(1, grouped=7)]
        client = FakeClient(history=[message(1, grouped=7), message(2, grouped=7)])
        payload, _ = await delivery.build_tg_payload(client, tg_post(msgs, edge=True), 10_000)
        self.assertEqual(client.album_reads, 1)
        self.assertEqual(len(payload["files"]), 2)

    async def test_scan_marks_only_album_at_history_edge(self):
        history = [message(1, grouped=5), message(2, grouped=5), message(3), message(4, grouped=9),
                   message(5, grouped=9)]
        tg_source._scan_cache.clear()
        posts = await tg_source.fetch_channel_arts(FakeClient(history), "edge-test", "@chan", limit=4)
        edges = {p["id"]: p["_edge"] for p in posts}
        self.assertEqual(edges, {2: True, 3: False, 4: False})
        tg_source._scan_cache.clear()


class MediaDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_is_not_retried(self):
        calls = []

        class Session:
            def get(self, *args, **kwargs):
                calls.append(args)
                raise asyncio.TimeoutError()

        async def session():
            return Session()

        with patch("sources.get_session", session):
            self.assertEqual(await sources.download("http://h/a.jpg", 100), (None, "error"))
        self.assertEqual(len(calls), 1)

    async def test_connection_error_is_retried_once(self):
        calls = []

        class Session:
            def get(self, *args, **kwargs):
                calls.append(args)
                raise aiohttp.ClientConnectionError("reset")

        async def session():
            return Session()

        with patch("sources.get_session", session), patch("sources.asyncio.sleep", return_value=None):
            self.assertEqual(await sources.download("http://h/a.jpg", 100), (None, "error"))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
