"""Дисковый индекс выдачи одного запроса: посты, курсор обхода, ленивые кандидаты.

DATA_DIR/search_cache — одноразовые метаданные API (не путать с shown.sqlite3).
Файл можно удалить при остановленном боте: обход просто начнётся заново.
"""
import contextlib
import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Sequence

import config
from selection import HistoryView

STALE_AFTER = 7 * 24 * 3600   # индексы, не тронутые неделю, удаляются при старте

# Порядок выдачи: сначала «строгие» (с наготой), затем фокус части тела, затем score.
RANK_ORDER = 'p.strict DESC, p.tier DESC, p.score DESC, p.ordinal'


def cache_dir() -> str:
    path = os.path.join(config.data_dir(), 'search_cache')
    os.makedirs(path, exist_ok=True)
    return path


def prune_stale(now: float | None = None) -> int:
    """Удалить заброшенные индексы (старые версии ключа, редкие запросы)."""
    now = now or time.time()
    removed = 0
    for name in os.listdir(cache_dir()):
        path = os.path.join(cache_dir(), name)
        with contextlib.suppress(OSError):
            if now - os.path.getmtime(path) > STALE_AFTER:
                os.unlink(path)
                removed += 1
    return removed


class SearchIndex:
    def __init__(self, cache_key):
        digest = hashlib.sha256(json.dumps(cache_key, sort_keys=True).encode()).hexdigest()
        self.path = os.path.join(cache_dir(), digest + '.sqlite3')
        self.db = sqlite3.connect(self.path, uri=True)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('PRAGMA cache_size=-4096')  # ~4 MiB на открытый индекс
        self.db.executescript(f'''
            CREATE TABLE IF NOT EXISTS posts (
                uid TEXT PRIMARY KEY, site_id TEXT NOT NULL, payload TEXT NOT NULL,
                allowed INTEGER NOT NULL, strict INTEGER NOT NULL,
                tier INTEGER NOT NULL, score INTEGER NOT NULL, ordinal INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ranked ON posts(allowed, {RANK_ORDER.replace('p.', '')});
            CREATE TABLE IF NOT EXISTS progress (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pages (seq INTEGER PRIMARY KEY, signature TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS page_signature ON pages(signature);
            CREATE TEMP TABLE excluded (request TEXT NOT NULL, uid TEXT NOT NULL,
                                        PRIMARY KEY(request, uid));
        ''')
        self.db.commit()
        self.histories: dict[str, str] = {}

    def attach_history(self, uri: str) -> str:
        alias = self.histories.get(uri)
        if alias is None:
            alias = 'history_' + hashlib.sha256(uri.encode()).hexdigest()[:16]
            self.db.execute(f'ATTACH DATABASE ? AS "{alias}"', (uri,))
            self.histories[uri] = alias
        return alias

    # ── посты ──
    def add(self, records) -> tuple[int, int]:
        """Добавить посты (дубли игнорируются) → (новых допустимых, новых всего)."""
        allowed = added = 0
        with self.db:
            for record in records:
                if self.db.execute('INSERT OR IGNORE INTO posts VALUES (?,?,?,?,?,?,?,?)',
                                   record).rowcount:
                    added += 1
                    allowed += record[3]
        return allowed, added

    def allowed_count(self) -> int:
        return self.db.execute('SELECT COUNT(*) FROM posts WHERE allowed=1').fetchone()[0]

    def candidates(self, seen) -> 'Candidates':
        return Candidates(self, seen)

    # ── курсор обхода ──
    def progress(self) -> dict:
        row = self.db.execute("SELECT payload FROM progress WHERE key='state'").fetchone()
        return json.loads(row[0]) if row else {}

    def save_progress(self, state: dict, seq: int | None = None, signature: str | None = None):
        with self.db:
            if seq is not None:
                self.db.execute('INSERT OR REPLACE INTO pages VALUES (?,?)', (seq, signature))
            self.db.execute("INSERT OR REPLACE INTO progress VALUES ('state',?)", (json.dumps(state),))

    def repeated_page(self, signature: str) -> bool:
        return self.db.execute('SELECT 1 FROM pages WHERE signature=? LIMIT 1',
                               (signature,)).fetchone() is not None

    def reset_progress(self):
        # Посты остаются: обновление не должно прятать уже найденные результаты.
        with self.db:
            self.db.execute('DELETE FROM progress')
            self.db.execute('DELETE FROM pages')

    def close(self):
        if getattr(self, 'db', None) is not None:
            self.db.close()
            self.db = None

    def __del__(self):
        with contextlib.suppress(Exception):
            self.close()


class Candidates(Sequence):
    """Ленивый список непоказанных постов в порядке ранжирования.

    У каждого запроса свой снимок исключений (история канала на момент запроса).
    """

    def __init__(self, index: SearchIndex, seen):
        self.index = index  # держит индекс живым на время скачивания
        self.request = uuid.uuid4().hex
        self.rank_filter = ''
        self.rank_params = ()
        self.sql_history = isinstance(seen, HistoryView)
        if self.sql_history:
            alias = index.attach_history(seen.uri)
            self.exclusion = f'''NOT EXISTS (
                SELECT 1 FROM "{alias}".shown h WHERE h.scope IN (?,?)
                    AND h.uid IN (p.uid,p.site_id))
                AND NOT EXISTS (
                SELECT 1 FROM "{alias}".failed f WHERE f.scope=? AND f.retry_at>?
                    AND f.uid IN (p.uid,p.site_id))'''
            self.exclusion_params = (seen.scope, seen.legacy_scope, seen.scope, time.time())
        else:
            with index.db:
                index.db.executemany('INSERT INTO excluded VALUES (?,?)',
                                     [(self.request, uid) for uid in set(seen)])
            self.exclusion = '''NOT EXISTS (
                SELECT 1 FROM excluded e WHERE e.request=? AND e.uid IN (p.uid,p.site_id))'''
            self.exclusion_params = (self.request,)

    def restrict_to_proven(self, tier: int, min_score: int):
        """Только доказанно лучшие: сбой скачивания не откатывает к недоказанному тиру."""
        self.rank_filter = 'AND p.strict=1 AND p.tier>=? AND p.score>=?'
        self.rank_params = (tier, min_score)

    def _params(self, *before):
        return (*before, *self.rank_params, *self.exclusion_params)

    def _query(self, select: str, where: str = '', ordered: bool = True) -> str:
        sql = f'''SELECT {select} FROM posts p WHERE p.allowed=1 {where}
            {self.rank_filter} AND {self.exclusion}'''
        return sql + (f' ORDER BY {RANK_ORDER}' if ordered else '')

    def proven_count(self, tier: int, min_score: int, limit: int) -> int:
        """Сколько непоказанных постов гарантированно лучше ЛЮБОГО ещё не прочитанного.

        Сайт отдаёт выдачу по убыванию score, значит у непрочитанных score ≤ min_score.
        Пост strict=1 с максимальным tier и score ≥ min_score их не уступит.
        """
        sql = self._query('1', 'AND p.strict=1 AND p.tier>=? AND p.score>=?', ordered=False)
        return len(self.index.db.execute(f'{sql} LIMIT ?',
                                         (*self._params(tier, min_score), limit)).fetchall())

    def __bool__(self):
        return self.index.db.execute(self._query('1', ordered=False) + ' LIMIT 1',
                                     self._params()).fetchone() is not None

    def __len__(self):
        return self.index.db.execute(self._query('COUNT(*)', ordered=False),
                                     self._params()).fetchone()[0]

    def __iter__(self):
        cursor = self.index.db.execute(self._query('p.payload'), self._params())
        try:
            for (payload,) in cursor:
                yield json.loads(payload)
        finally:
            cursor.close()

    def __getitem__(self, item):
        if isinstance(item, slice):
            start, stop, step = item.indices(len(self))
            rows = self.index.db.execute(self._query('p.payload') + ' LIMIT ? OFFSET ?',
                                         (*self._params(), max(0, stop - start), start)).fetchall()
            return [json.loads(r[0]) for r in rows][::step]
        if item < 0:
            item += len(self)
        row = None if item < 0 else self.index.db.execute(
            self._query('p.payload') + ' LIMIT 1 OFFSET ?', (*self._params(), item)).fetchone()
        if row is None:
            raise IndexError(item)
        return json.loads(row[0])

    def __eq__(self, other):
        if isinstance(other, list) and not other:
            return not self
        return list(self) == other

    def __del__(self):
        with contextlib.suppress(Exception):
            if not self.sql_history:
                with self.index.db:
                    self.index.db.execute('DELETE FROM excluded WHERE request=?', (self.request,))
